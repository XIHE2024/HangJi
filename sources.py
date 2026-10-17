"""Fixed-host data connectors. No credentials are written to disk or logs."""
import json
import math
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

NOAA_URL='https://services.swpc.noaa.gov/products/kyoto-dst.json'
FLIGHTS_URL='https://api.aviationstack.com/v1/flights'
KP_URL='https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json'
KP_FORECAST_URL='https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json'


class SourceError(Exception):
    pass


def fetch_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'FlightResearchDashboard/1.0','Accept':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=18) as response:
            raw=response.read(6_000_001)
        if len(raw)>6_000_000: raise SourceError('数据响应过大，请缩小查询范围。')
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise SourceError(f'数据源返回 HTTP {e.code}。请检查密钥、套餐权限或稍后重试。') from None
    except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError):
        raise SourceError('数据源暂时无法连接或返回格式异常，请稍后重试。') from None


def normalize_dst(payload,now=None):
    if not isinstance(payload,list): raise SourceError('Dst 数据格式异常。')
    if payload and isinstance(payload[0],list):
        payload=[dict(zip(payload[0],row)) for row in payload[1:]]
    rows=[]
    for item in payload:
        try:
            dt=datetime.fromisoformat(str(item['time_tag']).replace('Z','+00:00'))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            value=float(item['dst'])
            if not math.isfinite(value) or abs(value)>=999: continue
            rows.append({'time':dt.astimezone(timezone.utc).isoformat(),'dst':value})
        except (ValueError,KeyError,TypeError): continue
    rows=list({r['time']:r for r in rows}.values())
    rows.sort(key=lambda r:r['time'])
    if not rows: raise SourceError('数据源没有有效 Dst 观测。')
    latest=rows[-1]; rate=None
    if len(rows)>1:
        delta=(datetime.fromisoformat(latest['time'])-datetime.fromisoformat(rows[-2]['time'])).total_seconds()/3600
        if delta==1: rate=abs(latest['dst']-rows[-2]['dst'])
    now=now or datetime.now(timezone.utc)
    age=(now-datetime.fromisoformat(latest['time'])).total_seconds()/3600
    return {'source':'NOAA SWPC / Kyoto Dst','url':NOAA_URL,'fetched_at':now.isoformat(),
            'latest':latest,'abs_rate':rate,'age_hours':age,'stale':age>4 or age<-.1,'series':rows[-72:]}


def get_dst():
    return normalize_dst(fetch_json(NOAA_URL))


def normalize_kp(payload,forecast=False,now=None):
    now=now or datetime.now(timezone.utc)
    if not isinstance(payload,list):raise SourceError('Kp 数据格式异常。')
    if payload and isinstance(payload[0],list):
        payload=[dict(zip(payload[0],row)) for row in payload[1:]]
    rows=[]
    for item in payload:
        try:
            dt=datetime.fromisoformat(str(item['time_tag']).replace('Z','+00:00'))
            if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
            value=float(item.get('kp',item.get('Kp')))
            kind=str(item.get('observed','observed')).lower()
            if not math.isfinite(value) or not 0<=value<=9:continue
            # Do not mix observations/estimates embedded in the forecast product into predictions.
            if forecast and (kind!='predicted' or dt<=now):continue
            if not forecast and dt>now:continue
            rows.append({'time':dt.astimezone(timezone.utc).isoformat(),'kp':value,'kind':kind})
        except (ValueError,TypeError,KeyError):continue
    rows=sorted({r['time']:r for r in rows}.values(),key=lambda r:r['time'])
    if not rows:raise SourceError('Kp 数据源没有有效的未来预报。' if forecast else 'Kp 数据源没有有效观测。')
    latest=rows[-1]
    age=(now-datetime.fromisoformat(latest['time'])).total_seconds()/3600
    return {'source':'NOAA SWPC','url':KP_FORECAST_URL if forecast else KP_URL,'fetched_at':now.isoformat(),
            'latest':latest,'series':rows,'stale':False if forecast else age>6,
            'age_hours':None if forecast else age,'type':'forecast' if forecast else 'observation'}


def get_space_weather():
    """A failed feed does not suppress successful feeds; never reuse old values as fresh."""
    now=datetime.now(timezone.utc);result={'fetched_at':now.isoformat(),'dst':None,'kp':None,'kp_forecast':None,
        'roti':{'value':None,'status':'unavailable','reason':'尚未接入北京站实时 TEC/ROTI 数据源。'},'errors':{}}
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs={name:pool.submit(fetch_json,url) for name,url in [('dst',NOAA_URL),('kp',KP_URL),('kp_forecast',KP_FORECAST_URL)]}
        for name,job in jobs.items():
            try:
                payload=job.result()
                result[name]=normalize_dst(payload,now) if name=='dst' else normalize_kp(payload,name=='kp_forecast',now)
            except SourceError as e:result['errors'][name]=str(e)
    return result


def fetch_route(route,key):
    if not key: raise SourceError('请配置 Aviationstack API 密钥，或使用历史回测。')
    params=urllib.parse.urlencode({'access_key':key,'dep_iata':'PEK','arr_iata':route,'limit':100})
    payload=fetch_json(FLIGHTS_URL+'?'+params)
    if not isinstance(payload,dict) or payload.get('error'):
        raise SourceError('航班服务未授权或套餐不支持本查询，请检查密钥和接口权限。')
    data=payload.get('data')
    if not isinstance(data,list): raise SourceError('航班数据格式异常。')
    output=[];seen=set()
    for f in data:
        dep=f.get('departure') or {};arr=f.get('arrival') or {};flight=f.get('flight') or {}
        if dep.get('iata')!='PEK' or arr.get('iata')!=route: continue
        number=flight.get('iata') or flight.get('number') or '未知'
        identity=(number,dep.get('scheduled'))
        if identity in seen:continue
        seen.add(identity)
        delay=None
        try:
            actual=datetime.fromisoformat(arr['actual'].replace('Z','+00:00'))
            scheduled=datetime.fromisoformat(arr['scheduled'].replace('Z','+00:00'))
            if actual.tzinfo and scheduled.tzinfo: delay=(actual-scheduled).total_seconds()/60
        except (KeyError,TypeError,ValueError,AttributeError): pass
        output.append({'flight_no':number,'destination':route,'status':f.get('flight_status'),
                       'scheduled_departure':dep.get('scheduled'),'scheduled_arrival':arr.get('scheduled'),
                       'estimated_arrival':arr.get('estimated'),'actual_arrival':arr.get('actual'),
                       'actual_DT':delay,'airline':(f.get('airline') or {}).get('iata')})
    total=(payload.get('pagination') or {}).get('total')
    return {'source':'Aviationstack','fetched_at':datetime.now(timezone.utc).isoformat(),
            'route':route,'flights':output,'provider_total':total,'returned':len(output),
            'truncated':isinstance(total,(float,int)) and total>100,
            'notice':'返回当前接口首批最多 100 条记录；时间以服务商返回为准，共享航班可能对应同一次执飞。'}
