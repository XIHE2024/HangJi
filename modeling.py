"""Local, temporally separated arrival-delay research model."""
from pathlib import Path
import sys
BASE = Path(__file__).resolve().parent
if (BASE / '.deps').exists():
    sys.path.insert(0, str(BASE / '.deps'))
import json
import numpy as np
import pandas as pd
import joblib

ROOT = BASE.parent
DATA = BASE / 'data'
SOURCE = ROOT / 'outputs/DT_Dst_ROTI_20260929'
ROUTES = {'SHA': '上海虹桥', 'PVG': '上海浦东', 'CAN': '广州白云', 'SZX': '深圳宝安'}
FEATURES = ['route', 'airline', 'month', 'weekday', 'hour', 'minute', 'duration_min']
SPACE = ['dst_rate_lag', 'roti_lag']


def features(frame, airlines):
    s = pd.to_datetime(frame.scheduled)
    x = pd.DataFrame(index=frame.index)
    x['route'] = frame.destination.map({k: i for i, k in enumerate(ROUTES)})
    x['airline'] = frame.airline.map(airlines).fillna(-1)
    x['month'] = s.dt.month - 1
    x['weekday'] = s.dt.dayofweek
    x['hour'] = s.dt.hour
    x['minute'] = s.dt.minute
    x['duration_min'] = frame.duration_min
    for name in SPACE:
        x[name] = frame[name] if name in frame else np.nan
    return x.astype(float)


def finite_quantile(errors, coverage=.8):
    v = np.sort(np.asarray(errors))
    return float(v[min(len(v)-1, int(np.ceil((len(v)+1)*coverage))-1)])


def score(y, pred):
    e = np.asarray(pred)-np.asarray(y)
    return {'mae': float(np.abs(e).mean()), 'rmse': float(np.sqrt(np.square(e).mean())),
            'bias': float(e.mean()), 'n': int(len(e))}


def train():
    from sklearn.ensemble import HistGradientBoostingRegressor
    DATA.mkdir(exist_ok=True)
    raw = pd.read_pickle(SOURCE / 'flights_peK_raw_cache.pkl')
    d = raw[raw.destination.isin(ROUTES)].copy()
    audit = {'route_records': len(d), 'cancelled': int(d.status.str.contains('取消', na=False).sum())}
    d = d[~d.status.str.contains('取消', na=False)].copy()
    for name, date, time in [('scheduled_arrival', 'scheduled_arrival_date','scheduled_arrival_time'),
                              ('actual_arrival','actual_arrival_date','actual_arrival_time')]:
        d[name] = pd.to_datetime(d[date]+' '+d[time], format='%Y-%m-%d %H:%M',errors='coerce')
    d['arrival_DT'] = (d.actual_arrival-d.scheduled_arrival).dt.total_seconds()/60
    d['duration_min'] = (d.scheduled_arrival-d.scheduled).dt.total_seconds()/60
    d['departure_DT'] = d.delta_min
    ok = d[['scheduled','scheduled_arrival','actual_arrival','arrival_DT']].notna().all(axis=1)
    ok &= (d.arrival_DT >= -120) & d.duration_min.between(30,480)
    audit['invalid_times_removed'] = int((~ok).sum())
    d = d[ok].copy()
    key = ['flight_no','flight_date','origin','destination','scheduled_departure_time']
    if d.duplicated(key).any():
        raise ValueError('Duplicate flight keys; inspect before training.')
    d['airline'] = d.flight_no.str.extract(r'^([A-Z0-9]{2})',expand=False).fillna('??')
    # Completed historical hour at least one full hour before scheduled departure.
    d['feature_hour_utc'] = (d.scheduled-pd.Timedelta(hours=8)).dt.floor('h')-pd.Timedelta(hours=2)
    dst = pd.read_csv(SOURCE/'dst_hourly.csv',parse_dates=['hour_utc'])
    roti = pd.read_csv(SOURCE/'roti_hourly.csv',parse_dates=['hour_utc'])
    space = dst[['hour_utc','abs_dDst_dt_nT_per_h']].merge(roti[['hour_utc','ROTI_TECU_per_min']],on='hour_utc',how='outer',validate='one_to_one')
    space = space.rename(columns={'hour_utc':'feature_hour_utc','abs_dDst_dt_nT_per_h':'dst_rate_lag','ROTI_TECU_per_min':'roti_lag'})
    d = d.merge(space,on='feature_hour_utc',how='left',validate='many_to_one').sort_values('scheduled').reset_index(drop=True)
    tr = d.scheduled < '2018-07-01'
    va = (d.scheduled >= '2018-07-01') & (d.scheduled < '2018-10-01')
    ca = (d.scheduled >= '2018-10-01') & (d.scheduled < '2019-01-01')
    te = d.scheduled >= '2019-01-01'
    airlines = {a: i for i,a in enumerate(sorted(d.loc[tr,'airline'].unique()))}
    X = features(d,airlines)
    medians = d.loc[tr].groupby('destination').arrival_DT.median().to_dict()
    durations = d.loc[tr].groupby('destination').duration_min.median().to_dict()
    models = {}; val_scores={}; predictions={}
    for name, cols in [('baseline',FEATURES),('space',FEATURES+SPACE)]:
        print('Training',name,'rows',int(tr.sum()),flush=True)
        m = HistGradientBoostingRegressor(loss='absolute_error',max_iter=100,max_leaf_nodes=15,
                                          min_samples_leaf=60,l2_regularization=15,
                                          categorical_features=[0,1,2,3],early_stopping=False,random_state=42)
        m.fit(X.loc[tr,cols],d.loc[tr,'arrival_DT'])
        predictions[name] = m.predict(X[cols])
        val_scores[name] = score(d.loc[va,'arrival_DT'],predictions[name][va])
        models[name]=m
    # Pick on validation only. Require a 1% reduction before enabling space inputs.
    selected = 'space' if val_scores['space']['mae'] < .99*val_scores['baseline']['mae'] else 'baseline'
    have_space = d[SPACE].notna().all(axis=1).to_numpy()
    active = np.where(have_space,predictions[selected],predictions['baseline'])
    radii={}; base_radii={}
    for route in ROUTES:
        mask=ca & (d.destination==route)
        radii[route]=finite_quantile(np.abs(d.loc[mask,'arrival_DT']-active[mask]))
        base_radii[route]=finite_quantile(np.abs(d.loc[mask,'arrival_DT']-predictions['baseline'][mask]))
    d['predicted_DT'] = active
    d['baseline_DT'] = predictions['baseline']
    d['space_DT'] = predictions['space']
    d['interval_radius'] = d.destination.map(radii)
    d['lower'] = d.predicted_DT-d.interval_radius
    d['upper'] = d.predicted_DT+d.interval_radius
    route_metrics=[]
    for route in ROUTES:
        mask=te & (d.destination==route)
        v=d[mask]
        route_metrics.append({'code':route,'name':ROUTES[route], **score(v.arrival_DT,v.predicted_DT),
                              'mean_DT':float(v.arrival_DT.mean()),'median_DT':float(v.arrival_DT.median()),
                              'delay15_rate':float(v.arrival_DT.gt(15).mean()),
                              'coverage80':float(((v.arrival_DT>=v.lower)&(v.arrival_DT<=v.upper)).mean()),
                              'radius80':radii[route], 'baseline_mae':score(v.arrival_DT,v.baseline_DT)['mae'],
                              'space_mae':score(v.arrival_DT,v.space_DT)['mae']})
    meta={'target':'到港延误 = 实际到达 − 计划到达（分钟，提前到达保留负值）',
          'trained_at':pd.Timestamp.now(tz='UTC').isoformat(), 'valid_rows':len(d),'audit':audit,
          'splits':{'train':['2017-01-01','2018-06-30',int(tr.sum())],
                    'validation':['2018-07-01','2018-09-30',int(va.sum())],
                    'calibration':['2018-10-01','2018-12-31',int(ca.sum())],
                    'test':['2019-01-01','2019-12-31',int(te.sum())]},
          'selected':selected, 'validation':val_scores,
          'test':score(d.loc[te,'arrival_DT'],active[te]),
          'constant_baseline_test':score(d.loc[te,'arrival_DT'],d.loc[te,'destination'].map(medians)),
          'routes':route_metrics,
          'limitations':['训练数据截至 2018 年，2019 年为独立测试；对当前年份未验证。',
                         '不包含地面天气、流量和前序航班；不预测航班取消。',
                         '北京 TEC 时区按 UTC 假定；ROTI 为单站 5 分钟标准差的小时均值，非论文的小时窗口指标。',
                         '历史空间天气输入滞后 2 个小时标签；未模拟真实发布延迟。',
                         '80% 区间用 2018 年四季度残差校准，覆盖率可能随年份变化。']}
    joblib.dump({'models':models,'airlines':airlines,'selected':selected,'radii':radii,'base_radii':base_radii,'durations':durations},DATA/'model.joblib')
    cols=['flight_no','destination','status','scheduled','scheduled_arrival','actual_arrival','airline',
          'duration_min','arrival_DT','departure_DT','predicted_DT','baseline_DT','space_DT','lower','upper','dst_rate_lag','roti_lag']
    d.loc[te,cols].to_pickle(DATA/'test_flights.pkl')
    (DATA/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(meta,ensure_ascii=False,indent=2),flush=True)


def estimate(bundle, route, scheduled, airline='CA', dst=None, roti=None):
    frame=pd.DataFrame([{'destination':route,'scheduled':scheduled,'airline':airline,
                         'duration_min':bundle['durations'][route],'dst_rate_lag':dst,'roti_lag':roti}])
    x=features(frame,bundle['airlines'])
    base=float(bundle['models']['baseline'].predict(x[FEATURES])[0])
    scenario=None
    if dst is not None and roti is not None:
        scenario=float(bundle['models']['space'].predict(x[FEATURES+SPACE])[0])
    name='space' if scenario is not None and bundle['selected']=='space' else 'baseline'
    pred=scenario if name=='space' else base
    radius=bundle['radii' if name=='space' else 'base_radii'][route]
    return {'predicted_DT':pred,'baseline_DT':base,'scenario_DT':scenario,'model':name,
            'lower':pred-radius,'upper':pred+radius,'nominal_coverage':.8,
            'unknown_airline':airline not in bundle['airlines'],
            'out_of_period':pd.Timestamp(scheduled).year>2019}


if __name__=='__main__':
    train()
