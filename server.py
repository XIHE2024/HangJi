"""航迹 Web service.

Local development stays loopback-only by default. Production deployment is enabled
through explicit environment variables; mutable state is kept separate from model
assets so a persistent volume can be mounted without hiding the trained model.
"""
import os
os.environ.setdefault('OMP_NUM_THREADS', '4')

import argparse
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import ipaddress
import json
import logging
import math
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlparse, parse_qs

import modeling as m
from sources import SourceError, get_space_weather, fetch_airport
from auth_store import (
    AuthStore,
    AuthError,
    REMEMBER_SECONDS,
    SESSION_SECONDS,
    token_from_cookie,
    cookie,
    PROJECT_NAME,
)


KEY = os.environ.get('AVIATIONSTACK_API_KEY', '')
WEATHER = None
FLIGHT_CACHE = {}
LOCK = threading.Lock()
FORECASTS = {}
AIRPORTS = m.AIRPORTS
META = {}
MODEL = None
TEST = None
HISTORICAL_SPACE = None
EVENT_COMPARISON = None
WEATHER_LOCK = threading.Lock()
AUTH = None
PROXY_SECRET = ''
PUBLIC_ORIGIN = ''
ALLOWED_HOSTS = set()
TRUST_PROXY = False
SETTINGS_READONLY = False
REGISTRATION_ENABLED = True
STARTED_AT = time.time()
REQUEST_LOCK = threading.Lock()
REQUEST_HITS = defaultdict(deque)
CAPACITY = threading.BoundedSemaphore(24)

LOG = logging.getLogger('hangji')


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def normalized_origin(value):
    value = (value or '').strip().rstrip('/')
    if not value:
        return ''
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('PUBLIC_ORIGIN 必须是完整的 http(s) 网址，且不能包含路径。')
    return f'{parsed.scheme}://{parsed.netloc}'.lower()


def configure_runtime():
    """Load deployment settings once at process start."""
    global PUBLIC_ORIGIN, ALLOWED_HOSTS, TRUST_PROXY, SETTINGS_READONLY, REGISTRATION_ENABLED, CAPACITY
    candidate = os.environ.get('PUBLIC_ORIGIN', '')
    if not candidate:
        for name in ('RAILWAY_PUBLIC_DOMAIN', 'RENDER_EXTERNAL_HOSTNAME'):
            domain = os.environ.get(name, '').strip()
            if domain:
                candidate = domain if '://' in domain else 'https://' + domain
                break
    PUBLIC_ORIGIN = normalized_origin(candidate)
    ALLOWED_HOSTS = {item.strip().lower() for item in os.environ.get('ALLOWED_HOSTS', '').split(',') if item.strip()}
    if PUBLIC_ORIGIN:
        ALLOWED_HOSTS.add(urlparse(PUBLIC_ORIGIN).netloc.lower())
    TRUST_PROXY = env_flag('TRUST_PROXY', False)
    SETTINGS_READONLY = env_flag('SETTINGS_READONLY', bool(PUBLIC_ORIGIN))
    REGISTRATION_ENABLED = env_flag('REGISTRATION_ENABLED', True)
    try:
        workers = max(4, min(64, int(os.environ.get('MAX_CONCURRENT_REQUESTS', '24'))))
    except ValueError as exc:
        raise ValueError('MAX_CONCURRENT_REQUESTS 必须是整数。') from exc
    CAPACITY = threading.BoundedSemaphore(workers)


def state_root():
    raw = os.environ.get('STATE_DIR', '').strip()
    if not raw:
        return m.DATA
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = m.BASE / path
    return path.resolve()


def state_path(name):
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def write_state_json(name, value):
    path = state_path(name)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(clean(value), ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def weather_view():
    if not WEATHER:
        return None
    result = json.loads(json.dumps(WEATHER))
    now = datetime.now(timezone.utc)
    for name, limit in [('dst', 4), ('kp', 6)]:
        item = result.get(name)
        if item and item.get('latest', {}).get('time'):
            age = (now - datetime.fromisoformat(item['latest']['time'])).total_seconds() / 3600
            item.update(age_hours=age, stale=age > limit or age < -.1)
    forecast = result.get('kp_forecast')
    if forecast:
        forecast['series'] = [row for row in forecast.get('series', []) if datetime.fromisoformat(row['time']) > now]
        age = (now - datetime.fromisoformat(forecast['fetched_at'])).total_seconds() / 3600
        forecast['stale'] = not forecast['series'] or age > 6
    result.pop('_fetched_epoch', None)
    return result


def refresh_weather():
    global WEATHER
    with WEATHER_LOCK:
        if WEATHER and time.time() - WEATHER.get('_fetched_epoch', 0) < 300:
            return weather_view(), True
        WEATHER = get_space_weather()
        WEATHER['_fetched_epoch'] = time.time()
        write_state_json('latest_space_weather.json', WEATHER)
        return weather_view(), False


def auto_parameters(window_start):
    """Only exact, completed historical hours are fed to the validated model."""
    stamp = (m.pd.Timestamp(window_start) - m.pd.Timedelta(hours=8)).floor('h') - m.pd.Timedelta(hours=2)
    result = {'source': '自动匹配', 'feature_hour_utc': stamp.isoformat() + 'Z', 'dst': None, 'roti': None, 'usable': False}
    if HISTORICAL_SPACE is not None and stamp in HISTORICAL_SPACE.index:
        row = HISTORICAL_SPACE.loc[stamp]
        result.update(
            dst=clean(row['abs_dDst_dt_nT_per_h']),
            roti=clean(row['ROTI_TECU_per_min']),
            source='本地历史 Dst 与北京站 TEC',
        )
        result['usable'] = result['dst'] is not None and result['roti'] is not None
        result['reason'] = '已自动匹配预报窗口前可用的历史参数。' if result['usable'] else '对应历史小时存在缺测，使用基础机场模型。'
    else:
        result['reason'] = '该窗口没有可匹配的北京站 ROTI；最新 Dst 与 Kp 仅供观测参考，不能代替未来参数。使用基础机场模型。'
    return result


def attach_window_forecasts(response):
    """Attach one forecast per airport-hour, never one forecast per flight."""
    now = datetime.now(timezone.utc)
    with LOCK:
        for window in response.get('windows', []):
            window['prediction'] = None
            window['prediction_kind'] = 'unavailable'
            window['prediction_issued_at'] = None
            try:
                start = datetime.fromisoformat(window['window_start'].replace('Z', '+00:00'))
                if start.tzinfo is None:
                    continue
                identity = response['airport'] + '|' + start.astimezone(timezone.utc).isoformat() + '|60'
                saved = FORECASTS.get(identity)
                if saved:
                    window.update(prediction=saved['prediction'], prediction_kind='prewindow', prediction_issued_at=saved['issued_at'])
                    continue
                local = start.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
                prediction = m.estimate(MODEL, response['airport'], local)
                kind = 'prewindow' if start > now else 'retrospective'
                window.update(prediction=prediction, prediction_kind=kind, prediction_issued_at=now.isoformat())
                if kind == 'prewindow':
                    FORECASTS[identity] = {'prediction': prediction, 'issued_at': now.isoformat(), 'model_version': META['trained_at']}
            except (ValueError, TypeError, AttributeError):
                continue
        write_state_json('issued_forecasts.json', FORECASTS)
    return response


def clean(obj):
    if isinstance(obj, dict):
        return {str(key): clean(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(value) for value in obj]
    if isinstance(obj, m.pd.Timestamp):
        stamp = obj.tz_localize(m.TIMEZONE) if obj.tzinfo is None else obj
        return stamp.isoformat()
    if isinstance(obj, datetime):
        stamp = obj.replace(tzinfo=timezone(timedelta(hours=8))) if obj.tzinfo is None else obj
        return stamp.isoformat()
    if obj is m.pd.NaT:
        return None
    if hasattr(obj, 'item'):
        return clean(obj.item())
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def airport_arg(value):
    if value not in AIRPORTS:
        raise ValueError('当前模型仅支持北京首都机场 PEK。')
    return value


def date_arg(value, start='2019-01-01', end='2019-12-31'):
    try:
        day = datetime.strptime(value, '%Y-%m-%d').strftime('%Y-%m-%d')
    except (TypeError, ValueError):
        raise ValueError('日期格式必须为 YYYY-MM-DD。') from None
    if not start <= day <= end:
        raise ValueError(f'日期须在 {start} 至 {end} 之间。')
    return day


def backtest(airport, start, end):
    start = date_arg(start)
    end = date_arg(end)
    if start > end:
        raise ValueError('开始日期不能晚于结束日期。')
    days = TEST.window_start.dt.strftime('%Y-%m-%d')
    selected = TEST[days.between(start, end) & TEST.airport.eq(airport)].sort_values('window_start').copy()
    error = selected.predicted_mean_delay - selected.actual_mean_delay
    summary = {
        'airport': airport,
        'window_count': int(len(selected)),
        'flight_count': int(selected.flight_count.sum()),
        'mean_flights_per_window': selected.flight_count.mean(),
        'actual_mean': selected.actual_mean_delay.mean(),
        'predicted_mean': selected.predicted_mean_delay.mean(),
        'mae': error.abs().mean(),
        'rmse': m.np.sqrt(error.pow(2).mean()) if len(selected) else None,
        'bias': error.mean(),
        'coverage80': selected.actual_mean_delay.between(selected.lower, selected.upper).mean(),
        'hours_over_15': selected.actual_mean_delay.gt(15).mean(),
    }
    series = selected[[
        'window_start', 'window_end', 'flight_count', 'actual_mean_delay',
        'predicted_mean_delay', 'simple_baseline_mean_delay', 'lower', 'upper',
    ]].to_dict('records')
    records = selected[[
        'airport', 'window_start', 'window_end', 'flight_count', 'actual_mean_delay',
        'predicted_mean_delay', 'simple_baseline_mean_delay', 'lower', 'upper',
    ]].head(168).to_dict('records')
    return clean({
        'start': start,
        'end': end,
        'summary': summary,
        'series': series,
        'records': records,
        'record_total': len(selected),
        'record_limit': 168,
        'selected_model': META['selected'],
        'target_scope': 'airport_hour',
        'horizon_minutes': 60,
    })


def validate_event_comparison(asset):
    """Validate the frozen aggregate event/quiet comparison before serving it."""
    required = {
        'scope', 'status', 'airport', 'target', 'classification', 'groups',
        'difference', 'calendar_adjusted_event_cluster', 'paper_benchmark',
    }
    missing = required.difference(asset)
    if missing:
        raise ValueError('事件到港对比资产缺少字段：' + ', '.join(sorted(missing)))
    if asset.get('scope') != 'historical_event_quiet_first_leg_arrival_comparison':
        raise ValueError('事件到港对比资产 scope 不匹配。')
    if asset.get('status') != 'historical_exploratory_reanalysis':
        raise ValueError('事件到港对比资产必须标记为历史探索性再分析。')
    if asset.get('airport') != 'PEK':
        raise ValueError('事件到港对比资产机场必须是 PEK。')
    target = asset.get('target', {})
    if target.get('unit') != 'physical_departure_first_leg':
        raise ValueError('事件到港对比资产必须使用物理起飞首段单位。')
    if target.get('metric') != 'first_leg_arrival_delay_minutes':
        raise ValueError('事件到港对比资产必须使用首段到港延误指标。')
    if target.get('threshold_minutes') != 30:
        raise ValueError('事件到港对比资产的比例阈值必须是 30 分钟。')
    classification = asset.get('classification', {})
    if classification.get('event_rows_in_local_period') != 31:
        raise ValueError('事件到港对比资产必须包含研究期 31 条冻结事件。')
    if classification.get('independent_event_clusters') != 28:
        raise ValueError('事件到港对比资产必须包含 28 个合并事件簇。')
    total = int(classification.get('valid_first_leg_arrivals', -1))
    partition = sum(int(classification.get(name, -1)) for name in (
        'quiet_flights', 'event_flights', 'excluded_boundary_or_partial_day_flights'
    ))
    if total < 1 or partition != total:
        raise ValueError('事件到港对比资产的 quiet/event/excluded 样本不守恒。')
    adjusted = asset.get('calendar_adjusted_event_cluster', {})
    if adjusted.get('event_cluster_count') != 28:
        raise ValueError('事件簇调整结果未覆盖全部 28 个事件簇。')
    for endpoint in ('mean_arrival_delay_minutes', 'arrival_delay_over_30_percentage_points'):
        estimate = adjusted.get(endpoint, {})
        ci = estimate.get('ci95', {})
        if ci.get('samples') != 5000 or ci.get('seed') != 20261029:
            raise ValueError('两个事件簇 Bootstrap 终点都必须使用 5,000 次与种子 20261029。')
        try:
            lower = float(ci['lower'])
            point = float(estimate['point'])
            upper = float(ci['upper'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('事件簇 Bootstrap 终点缺少有效的点估计或区间。') from exc
        if not lower <= point <= upper:
            raise ValueError('事件簇 Bootstrap 点估计必须位于 95% 区间内。')
    mean_ci = adjusted['mean_arrival_delay_minutes']['ci95']
    expected_robust = bool(float(mean_ci['lower']) > 0)
    if bool(adjusted.get('robust_direction_supported')) != expected_robust:
        raise ValueError('事件层稳健性标志与均值差 95% 区间不一致。')


def model_comparison_payload(metadata):
    """Keep the predictive A/B evidence separate from the historical event study."""
    validation_a = float(metadata['validation']['baseline']['mae'])
    validation_b = float(metadata['validation']['space']['mae'])
    test_a = float(metadata['baseline_model_test']['mae'])
    test_b = float(metadata['space_model_test']['mae'])
    production_gates = {
        key: bool(value)
        for key, value in metadata.get('production_gates', {}).items()
    }
    selected_product_model = metadata.get('selected', 'baseline')
    space_enabled = bool(
        selected_product_model == 'space'
        and production_gates
        and all(production_gates.values())
    )
    conclusion = (
        '模型 B 已通过历史开发、历史留出、ROTI 时区确认与全新确认段四项生产门槛，当前主预报启用模型 B。'
        if space_enabled else
        '事件期对比不等于预测增益。模型 B 的 2019 配对误差区间仍跨 0，ROTI 时区尚未确认，且没有全新确认时段，因此主预报继续固定使用模型 A。'
    )
    return {
        'metric': 'MAE（分钟，越低越好）',
        'lower_is_better': True,
        'enable_threshold_relative_percent': 1.0,
        'model_a_name': '模型 A · 仅时间规律',
        'model_b_name': '模型 B · 时间规律 + 空间天气',
        'validation': {
            'stage_label': '2018 Q3 历史开发评估',
            'status': 'historical_development',
            'model_a_mae': validation_a,
            'model_b_mae': validation_b,
            'b_relative_improvement_percent': float((validation_a - validation_b) / validation_a * 100),
            'bootstrap': metadata.get('validation_bootstrap'),
        },
        'independent_test': {
            'stage_label': '2019 历史留出审计',
            'status': 'historical_holdout',
            'model_a_mae': test_a,
            'model_b_mae': test_b,
            'b_relative_improvement_percent': float((test_a - test_b) / test_a * 100),
            'bootstrap': metadata.get('test_bootstrap'),
        },
        'rolling_folds': metadata.get('rolling_folds', []),
        'internal_screening_gates': metadata.get('internal_screening_gates', {}),
        'internal_screening_pass': bool(metadata.get('internal_screening_pass', False)),
        'historical_holdout_gates': metadata.get('historical_holdout_gates', {}),
        'production_gates': production_gates,
        'roti_timezone_status': metadata.get('roti_timezone_status'),
        'fresh_confirmation_available': bool(metadata.get('fresh_confirmation_available', False)),
        'space_enabled': space_enabled,
        'selected_product_model': selected_product_model,
        'selected_product_mae': metadata.get('test', {}).get('mae'),
        'selected_reason': metadata.get('selected_reason'),
        'conclusion': conclusion,
    }


def space_weather_comparison(meta=None, comparison=None):
    """Return the frozen event/quiet arrival study plus separate A/B evidence."""
    metadata = META if meta is None else meta
    asset = EVENT_COMPARISON if comparison is None else comparison
    if asset is None:
        asset_path = m.DATA / 'event_arrival_comparison.json'
        if not asset_path.exists():
            raise ValueError('缺少事件期—完整安静期到港对比资产。')
        asset = json.loads(asset_path.read_text(encoding='utf-8'))
    validate_event_comparison(asset)
    response = json.loads(json.dumps(asset, ensure_ascii=False))
    response['model_comparison'] = model_comparison_payload(metadata)
    return clean(response)


def request_rate_ok(ip, method, path):
    if path == '/api/health':
        return True
    now = time.monotonic()
    group = 'auth' if path in {'/api/auth/login', '/api/auth/register'} else ('write' if method == 'POST' else 'read')
    window, limit = ((900, 60) if group == 'auth' else (60, 90) if group == 'write' else (60, 300))
    key = (ip, group)
    with REQUEST_LOCK:
        if len(REQUEST_HITS) > 6000:
            for old_key in list(REQUEST_HITS):
                queue = REQUEST_HITS[old_key]
                if not queue or now - queue[-1] > 900:
                    del REQUEST_HITS[old_key]
        queue = REQUEST_HITS[key]
        while queue and now - queue[0] > window:
            queue.popleft()
        if len(queue) >= limit:
            return False
        queue.append(now)
    return True


def load_historical_space():
    bundled = m.DATA / 'historical_space.csv.gz'
    if bundled.exists():
        frame = m.pd.read_csv(bundled, parse_dates=['hour_utc']).set_index('hour_utc')
        return frame[['abs_dDst_dt_nT_per_h', 'ROTI_TECU_per_min']]
    if m.SOURCE is not None:
        dst_file = m.SOURCE / 'dst_hourly.csv'
        roti_file = m.SOURCE / 'roti_hourly.csv'
        if dst_file.exists() and roti_file.exists():
            dst = m.pd.read_csv(dst_file, parse_dates=['hour_utc']).set_index('hour_utc')
            roti = m.pd.read_csv(roti_file, parse_dates=['hour_utc']).set_index('hour_utc')
            return dst[['abs_dDst_dt_nT_per_h']].join(roti[['ROTI_TECU_per_min']], how='inner')
    LOG.warning('历史空间天气运行数据缺失；自动预测将安全回退到基础模型。')
    return None


def validate_assets(meta, model, test):
    """Fail fast if model, metadata, and test windows do not describe one target."""
    required_meta = {
        'asset_id', 'schema_version', 'prediction_unit', 'interval_unit',
        'primary_metric', 'timezone', 'horizon_minutes', 'airport',
    }
    missing = sorted(required_meta.difference(meta))
    if missing:
        raise ValueError('metadata.json 缺少机场小时资产字段：' + ', '.join(missing))
    if meta['prediction_unit'] != 'airport_hour' or meta['interval_unit'] != 'airport_hour':
        raise ValueError('模型资产不是机场小时口径。')
    if meta['airport'] != 'PEK' or meta['timezone'] != 'Asia/Shanghai' or meta['horizon_minutes'] != 60:
        raise ValueError('模型资产的机场、时区或预报窗口不匹配。')
    if model.get('asset_id') != meta['asset_id'] or model.get('schema_version') != meta['schema_version']:
        raise ValueError('model.joblib 与 metadata.json 版本不一致。')
    if test.empty or 'asset_id' not in test or 'schema_version' not in test:
        raise ValueError('test_windows.pkl 缺少资产标识。')
    if set(test.asset_id.astype(str)) != {str(meta['asset_id'])}:
        raise ValueError('测试窗口与模型资产标识不一致。')
    if set(test.schema_version.astype(int)) != {int(meta['schema_version'])}:
        raise ValueError('测试窗口与模型资产结构版本不一致。')
    if set(test.airport.astype(str)) != {'PEK'}:
        raise ValueError('测试窗口包含不受支持的机场。')


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = 'Hangji/2.0'
    sys_version = ''

    def log_message(self, fmt, *args):
        LOG.info('%s - %s', self.client_address[0], fmt % args)

    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def _trusted_share(self):
        return bool(PROXY_SECRET) and secrets.compare_digest(self.headers.get('X-Share-Token', ''), PROXY_SECRET)

    def identity(self):
        share = self._trusted_share()
        raw_ip = self.client_address[0]
        if share:
            raw_ip = self.headers.get('X-Share-IP', raw_ip)
        elif TRUST_PROXY:
            forwarded = self.headers.get('X-Forwarded-For', '').split(',', 1)[0].strip()
            raw_ip = forwarded or self.headers.get('CF-Connecting-IP', raw_ip)
        try:
            ip = str(ipaddress.ip_address(raw_ip))
        except ValueError:
            ip = self.client_address[0]
        forwarded_https = TRUST_PROXY and self.headers.get('X-Forwarded-Proto', '').split(',', 1)[0].strip().lower() == 'https'
        public_https = bool(PUBLIC_ORIGIN) and urlparse(PUBLIC_ORIGIN).scheme == 'https'
        self.auth_ip = ip
        self.auth_ua = self.headers.get('User-Agent', '')[:300]
        self.secure_cookie = (share and self.headers.get('X-Share-HTTPS') == '1') or forwarded_https or public_https
        self.session_token = token_from_cookie(self.headers.get('Cookie'))
        self.auth_session = AUTH.session(self.session_token)
        self.user = self.auth_session['user'] if self.auth_session else None

    def require_user(self, admin=False):
        if not self.user:
            self.send(401, {'error': '请先登录后使用系统。'})
            return False
        if admin and self.user['role'] != 'admin':
            self.send(403, {'error': '只有管理员可以访问。'})
            return False
        return True

    def send(self, status, data, ctype='application/json; charset=utf-8', extra=None):
        content = json.dumps(clean(data), ensure_ascii=False, allow_nan=False).encode('utf-8') if ctype.startswith('application/json') else data
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=()')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if getattr(self, 'secure_cookie', False):
            self.send_header('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def allowed(self):
        host = self.headers.get('Host', '').strip().lower()
        local = {
            f'127.0.0.1:{self.server.server_port}',
            f'localhost:{self.server.server_port}',
            f'[::1]:{self.server.server_port}',
        }
        if host in local:
            return True
        if '*' in ALLOWED_HOSTS and host:
            return True
        return host in ALLOWED_HOSTS

    def origin_allowed(self, origin):
        if not origin:
            return True
        expected = {
            f'http://127.0.0.1:{self.server.server_port}',
            f'http://localhost:{self.server.server_port}',
        }
        if PUBLIC_ORIGIN:
            expected.add(PUBLIC_ORIGIN)
        return origin.rstrip('/').lower() in expected

    def mime_for(self, file):
        return {
            '.html': 'text/html; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.svg': 'image/svg+xml; charset=utf-8',
        }.get(file.suffix.lower(), 'application/octet-stream')

    def do_GET(self):
        if not CAPACITY.acquire(blocking=False):
            return self.send(503, {'error': '服务当前较忙，请稍后重试。'}, extra={'Retry-After': '5'})
        try:
            self._do_GET()
        finally:
            CAPACITY.release()

    def _do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/health':
            ready = AUTH is not None and MODEL is not None and TEST is not None
            return self.send(200 if ready else 503, {'status': 'ok' if ready else 'starting', 'ready': ready, 'uptime_seconds': int(time.time() - STARTED_AT)})
        if len(self.path) > 2048:
            return self.send(414, {'error': '请求网址过长。'})
        if not self.allowed():
            return self.send(403, {'error': '访问域名未被网站配置允许。'})
        self.identity()
        if not request_rate_ok(self.auth_ip, 'GET', parsed.path):
            return self.send(429, {'error': '请求过于频繁，请稍后再试。'}, extra={'Retry-After': '60'})
        query = parse_qs(parsed.query)
        get = lambda key, default: query.get(key, [default])[0]
        try:
            if parsed.path == '/api/auth/session':
                extra = {}
                if not self.auth_session:
                    token, csrf = AUTH.new_session(None, self.auth_ip, self.auth_ua, self.session_token)
                    extra['Set-Cookie'] = cookie(token, self.secure_cookie, max_age=600)
                    expires_at = time.time() + 600
                else:
                    csrf = self.auth_session['csrf']
                    expires_at = self.auth_session['expires_at']
                return self.send(200, {
                    'authenticated': bool(self.user),
                    'user': self.user,
                    'csrf': csrf,
                    'session_expires_at': expires_at,
                    'project_name': PROJECT_NAME,
                    'registration_enabled': REGISTRATION_ENABLED,
                }, extra=extra)

            public_files = {
                '/login': 'auth.html',
                '/register': 'auth.html',
                '/auth.js': 'auth.js',
                '/auth.css': 'auth.css',
                '/style.css': 'style.css',
                '/favicon.svg': 'favicon.svg',
            }
            if parsed.path in public_files:
                if parsed.path in {'/login', '/register'} and self.user:
                    target = '/admin' if self.user['role'] == 'admin' else '/'
                    return self.send(302, b'', 'text/plain', {'Location': target})
                file = m.BASE / 'static' / public_files[parsed.path]
                if not file.exists():
                    return self.send(404, {'error': '页面资源不存在。'})
                return self.send(200, file.read_bytes(), self.mime_for(file))

            if parsed.path in {'/', '/admin', '/account'} and not self.user:
                return self.send(302, b'', 'text/plain', {'Location': '/login'})
            if not self.require_user(parsed.path in {'/admin', '/admin.js'} or parsed.path.startswith('/api/admin/')):
                return
            if parsed.path == '/api/admin/users':
                return self.send(200, AUTH.admin_users(max(1, min(100000, int(get('page', '1')))), get('search', '')))
            if parsed.path == '/api/admin/events':
                return self.send(200, AUTH.admin_events(max(1, min(100000, int(get('page', '1')))), get('search', '')))
            if parsed.path == '/api/meta':
                now_local = datetime.now(timezone(timedelta(hours=8)))
                next_hour = now_local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                return self.send(200, {
                    **META,
                    'csrf': self.auth_session['csrf'],
                    'user': self.user,
                    'project_name': PROJECT_NAME,
                    'live_configured': bool(KEY),
                    'settings_readonly': SETTINGS_READONLY,
                    'sharing_mode': bool(PUBLIC_ORIGIN),
                    'public_url': PUBLIC_ORIGIN or None,
                    'registration_enabled': REGISTRATION_ENABLED,
                    'today': now_local.strftime('%Y-%m-%d'),
                    'tomorrow': (now_local + timedelta(days=1)).strftime('%Y-%m-%d'),
                    'next_window_start': next_hour.isoformat(),
                })
            if parsed.path == '/api/weather':
                return self.send(200, {'data': weather_view(), 'configured': True})
            if parsed.path == '/api/space-comparison':
                return self.send(200, space_weather_comparison())
            if parsed.path in {'/api/backtest', '/api/export'}:
                airport = airport_arg(get('airport', 'PEK'))
                start = date_arg(get('start', '2019-09-01'))
                end = date_arg(get('end', '2019-09-30'))
                if start > end:
                    raise ValueError('开始日期不能晚于结束日期。')
                if parsed.path == '/api/backtest':
                    return self.send(200, backtest(airport, start, end))
                days = TEST.window_start.dt.strftime('%Y-%m-%d')
                data = TEST[days.between(start, end) & TEST.airport.eq(airport)]
                columns = [
                    'airport', 'window_start', 'window_end', 'flight_count',
                    'actual_mean_delay', 'predicted_mean_delay',
                    'simple_baseline_mean_delay', 'lower', 'upper',
                ]
                csv_text = data[columns].to_csv(index=False)
                return self.send(200, ('\ufeff' + csv_text).encode('utf-8'), 'text/csv; charset=utf-8', {'Content-Disposition': f'attachment; filename="{airport}_hourly_{start}_{end}.csv"'})

            files = {
                '/': 'index.html',
                '/app.js': 'app.js',
                '/admin': 'admin.html',
                '/admin.js': 'admin.js',
                '/account': 'account.html',
                '/account.js': 'account.js',
            }
            if parsed.path in files:
                file = m.BASE / 'static' / files[parsed.path]
                return self.send(200, file.read_bytes(), self.mime_for(file))
            self.send(404, {'error': '页面不存在。'})
        except ValueError as exc:
            self.send(400, {'error': str(exc)})
        except Exception:
            LOG.exception('GET %s failed', parsed.path)
            self.send(500, {'error': '读取失败，请稍后重试。'})

    def do_POST(self):
        if not CAPACITY.acquire(blocking=False):
            return self.send(503, {'error': '服务当前较忙，请稍后重试。'}, extra={'Retry-After': '5'})
        try:
            self._do_POST()
        finally:
            CAPACITY.release()

    def _do_POST(self):
        global KEY, WEATHER
        parsed = urlparse(self.path)
        if len(self.path) > 2048:
            return self.send(414, {'error': '请求网址过长。'})
        if not self.allowed():
            return self.send(403, {'error': '访问域名未被网站配置允许。'})
        self.identity()
        if not request_rate_ok(self.auth_ip, 'POST', parsed.path):
            return self.send(429, {'error': '请求过于频繁，请稍后再试。'}, extra={'Retry-After': '60'})
        if not self.auth_session:
            return self.send(401, {'error': '登录状态已失效，请刷新页面重新登录。'})
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.auth_session['csrf']):
            return self.send(403, {'error': '请求验证失败，请刷新页面。'})
        if not self.origin_allowed(self.headers.get('Origin')) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.send(403, {'error': '不允许跨站请求。'})
        try:
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ValueError('请求长度格式不正确。') from None
            if size < 0 or size > 16384 or self.headers.get('Transfer-Encoding'):
                raise ValueError('请求过大或传输格式不支持。')
            if 'application/json' not in self.headers.get('Content-Type', ''):
                raise ValueError('需要 JSON 请求。')
            obj = json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(obj, dict):
                raise ValueError('请求格式不正确。')
            path = parsed.path
            if path == '/api/auth/register':
                if not REGISTRATION_ENABLED:
                    raise AuthError('当前网站未开放自主注册，请联系管理员创建账号。', 403)
                username = AUTH.register(obj.get('username'), obj.get('password'), self.auth_ip, self.auth_ua)
                return self.send(201, {'message': '注册成功，请使用用户名和密码登录。', 'username': username})
            if path == '/api/auth/login':
                user = AUTH.login(obj.get('username'), obj.get('password'), self.auth_ip, self.auth_ua)
                duration = REMEMBER_SECONDS if obj.get('remember') is True else SESSION_SECONDS
                token, csrf = AUTH.new_session(user['id'], self.auth_ip, self.auth_ua, self.session_token, duration=duration)
                return self.send(200, {
                    'user': user,
                    'csrf': csrf,
                    'redirect': '/admin' if user['role'] == 'admin' else '/',
                }, extra={'Set-Cookie': cookie(token, self.secure_cookie, max_age=duration)})
            if not self.require_user(path.startswith('/api/admin/') or path == '/api/settings'):
                return
            if path == '/api/auth/logout':
                AUTH.logout(self.session_token, self.auth_ip, self.auth_ua)
                return self.send(200, {'message': '已退出登录。'}, extra={'Set-Cookie': cookie('', self.secure_cookie, True)})
            if path == '/api/auth/password':
                AUTH.change_password(self.user, obj.get('current_password'), obj.get('new_password'), self.auth_ip, self.auth_ua)
                return self.send(200, {'message': '密码已修改，请重新登录。'}, extra={'Set-Cookie': cookie('', self.secure_cookie, True)})
            if path == '/api/admin/user-status':
                if type(obj.get('enabled')) is not bool:
                    raise ValueError('账号状态格式不正确。')
                AUTH.set_enabled(self.user, int(obj.get('user_id')), obj['enabled'], self.auth_ip, self.auth_ua)
                return self.send(200, {'message': '账号状态已更新。'})
            if path == '/api/settings':
                if SETTINGS_READONLY:
                    return self.send(403, {'error': '公网部署的航班接口密钥由服务器环境变量管理。'})
                key = obj.get('key', '')
                if not isinstance(key, str) or len(key) > 256:
                    raise ValueError('密钥格式不正确。')
                KEY = key.strip()
                with LOCK:
                    FLIGHT_CACHE.clear()
                return self.send(200, {'configured': bool(KEY), 'message': '密钥仅存于本次服务内存中，关闭服务后清除。'})
            if path == '/api/weather/refresh':
                value, cached = refresh_weather()
                return self.send(200, {'data': value, 'cached': cached})
            if path == '/api/predict':
                if 'route' in obj or 'airline' in obj:
                    raise ValueError('本系统预测机场一小时平均延误，不接受航线或单航班条件。')
                airport = airport_arg(obj.get('airport', 'PEK'))
                if obj.get('horizon_minutes', 60) != 60:
                    raise ValueError('当前模型的预报窗口固定为 60 分钟。')
                raw_start = obj.get('window_start')
                if not isinstance(raw_start, str):
                    raise ValueError('请提供带时区的 window_start。')
                try:
                    aware_start = datetime.fromisoformat(raw_start.replace('Z', '+00:00'))
                except ValueError:
                    raise ValueError('window_start 必须是 ISO 8601 时间。') from None
                if aware_start.tzinfo is None:
                    raise ValueError('window_start 必须包含时区；北京时间请使用 +08:00。')
                local_aware = aware_start.astimezone(timezone(timedelta(hours=8)))
                if local_aware.minute or local_aware.second or local_aware.microsecond:
                    raise ValueError('一小时窗口必须从整点开始。')
                date_arg(local_aware.strftime('%Y-%m-%d'), start='2019-01-01', end='2030-12-31')
                window_start = local_aware.replace(tzinfo=None)

                def optional(name, low, high):
                    value = obj.get(name)
                    if value in [None, '']:
                        return None
                    value = float(value)
                    if not math.isfinite(value) or not low <= value <= high:
                        raise ValueError(f'{name} 超出允许范围。')
                    return value

                mode = obj.get('space_mode', 'manual')
                if mode not in ['auto', 'manual']:
                    raise ValueError('空间天气模式不正确。')
                context = None
                if mode == 'auto':
                    context = auto_parameters(window_start)
                    dst = context['dst'] if context['usable'] else None
                    roti = context['roti'] if context['usable'] else None
                else:
                    dst = optional('dst', 0, 200)
                    roti = optional('roti', 0, 2)
                    if (dst is None) != (roti is None):
                        raise ValueError('情景分析需同时输入 Dst 变化率和 ROTI。')
                result = m.estimate(MODEL, airport, window_start, dst, roti)
                return self.send(200, {
                    **result,
                    'mode': 'automatic' if mode == 'auto' else ('scenario' if dst is not None else 'historical_model'),
                    'auto_parameters': context,
                    'outside_space_training': dst is not None and (dst > 48 or roti > .2),
                    'notice': '历史研究模型给出 PEK 整体一小时平均离港延误；不是单个航班或航线的预计延误。区间按机场小时残差校准。',
                })
            if path == '/api/live':
                airport = airport_arg(obj.get('airport', 'PEK'))
                with LOCK:
                    cached = FLIGHT_CACHE.get(airport)
                if cached and time.time() - cached[0] < 300:
                    return self.send(200, {**cached[1], 'cached': True})
                response = attach_window_forecasts(fetch_airport(KEY))
                with LOCK:
                    FLIGHT_CACHE[airport] = (time.time(), response)
                return self.send(200, response)
            self.send(404, {'error': '接口不存在。'})
        except AuthError as exc:
            self.send(exc.status, {'error': str(exc)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send(400, {'error': str(exc)})
        except SourceError as exc:
            self.send(502, {'error': str(exc), 'last_weather': WEATHER if parsed.path == '/api/weather/refresh' else None})
        except Exception:
            LOG.exception('POST %s failed', parsed.path)
            self.send(500, {'error': '处理失败，请稍后重试。'})


def main():
    global META, MODEL, TEST, WEATHER, FORECASTS, HISTORICAL_SPACE, EVENT_COMPARISON, AUTH, PROXY_SECRET
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(message)s')
    try:
        configure_runtime()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    host = args.host or os.environ.get('HOST', '127.0.0.1')
    try:
        port = args.port if args.port is not None else int(os.environ.get('PORT', '8765'))
    except ValueError as exc:
        raise SystemExit('PORT 必须是整数。') from exc
    public_bind = host not in {'127.0.0.1', 'localhost', '::1'}
    if public_bind and not ALLOWED_HOSTS:
        raise SystemExit('公网监听需要设置 PUBLIC_ORIGIN（推荐）或 ALLOWED_HOSTS。')

    for required in ('metadata.json', 'model.joblib', 'test_windows.pkl', 'event_arrival_comparison.json'):
        if not (m.DATA / required).exists():
            raise SystemExit(f'缺少运行资产 data/{required}，请先准备模型数据。')

    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    AUTH = AuthStore(root / 'accounts.sqlite3')
    admin_password = os.environ.get('ADMIN_INITIAL_PASSWORD') or None
    if not AUTH.has_admin():
        if public_bind and not admin_password:
            raise SystemExit('首次公网部署必须设置 ADMIN_INITIAL_PASSWORD（至少 10 个字符）。')
        credentials = None if admin_password else root / '管理员初始账户.txt'
        try:
            AUTH.bootstrap_admin(credentials, admin_password)
        except AuthError as exc:
            raise SystemExit('ADMIN_INITIAL_PASSWORD 不符合密码规则：' + str(exc)) from exc

    proxy_secret = root / 'gateway_secret.txt'
    if not proxy_secret.exists():
        proxy_secret.write_text(secrets.token_urlsafe(32), encoding='utf-8')
    PROXY_SECRET = proxy_secret.read_text(encoding='utf-8').strip()
    META = json.loads((m.DATA / 'metadata.json').read_text(encoding='utf-8'))
    MODEL = m.joblib.load(m.DATA / 'model.joblib')
    TEST = m.pd.read_pickle(m.DATA / 'test_windows.pkl')
    EVENT_COMPARISON = json.loads(
        (m.DATA / 'event_arrival_comparison.json').read_text(encoding='utf-8')
    )
    try:
        validate_assets(META, MODEL, TEST)
        validate_event_comparison(EVENT_COMPARISON)
    except ValueError as exc:
        raise SystemExit('模型资产校验失败：' + str(exc)) from exc
    HISTORICAL_SPACE = load_historical_space()

    saved = root / 'issued_forecasts.json'
    if saved.exists():
        try:
            FORECASTS = json.loads(saved.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            LOG.warning('已忽略无法读取的 issued_forecasts.json。')
            FORECASTS = {}
    weather_file = root / 'latest_space_weather.json'
    if weather_file.exists():
        try:
            WEATHER = json.loads(weather_file.read_text(encoding='utf-8'))
            weather_view()
        except (OSError, ValueError, KeyError):
            LOG.warning('已忽略无法读取的空间天气缓存。')
            WEATHER = None

    server = AppServer((host, port), Handler)
    display = PUBLIC_ORIGIN or f'http://{host}:{port}'
    LOG.info('航迹服务已启动：%s（状态目录：%s）', display, root)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
