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
from sources import SourceError, get_space_weather, fetch_route
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
ROUTES = m.ROUTES
META = {}
MODEL = None
TEST = None
HISTORICAL_SPACE = None
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


def auto_parameters(scheduled):
    """Only exact historical feature-hour matches are fed to the validated model."""
    stamp = (m.pd.Timestamp(scheduled) - m.pd.Timedelta(hours=8)).floor('h') - m.pd.Timedelta(hours=2)
    result = {'source': '自动匹配', 'feature_hour_utc': stamp.isoformat() + 'Z', 'dst': None, 'roti': None, 'usable': False}
    if HISTORICAL_SPACE is not None and stamp in HISTORICAL_SPACE.index:
        row = HISTORICAL_SPACE.loc[stamp]
        result.update(
            dst=clean(row['abs_dDst_dt_nT_per_h']),
            roti=clean(row['ROTI_TECU_per_min']),
            source='本地历史 Dst 与北京站 TEC',
        )
        result['usable'] = result['dst'] is not None and result['roti'] is not None
        result['reason'] = '已自动匹配起飞前历史参数。' if result['usable'] else '对应历史小时存在缺测，使用基础航班模型。'
    else:
        result['reason'] = '该时刻没有可匹配的北京站 ROTI；最新 Dst 与 Kp 仅供观测参考，不能当作该时刻的未来参数。使用基础航班模型。'
    return result


def attach_forecasts(response):
    """Distinguish an issued-before-departure forecast from retrospective computation."""
    now = datetime.now(timezone.utc)
    with LOCK:
        for flight in response['flights']:
            flight['prediction'] = None
            flight['prediction_kind'] = 'unavailable'
            flight['prediction_issued_at'] = None
            if flight['status'] == 'cancelled':
                continue
            try:
                scheduled = datetime.fromisoformat(flight['scheduled_departure'].replace('Z', '+00:00'))
                if scheduled.tzinfo is None:
                    continue
                identity = flight['flight_no'] + '|' + scheduled.astimezone(timezone.utc).isoformat()
                saved = FORECASTS.get(identity)
                if saved:
                    flight.update(prediction=saved['prediction'], prediction_kind='predeparture', prediction_issued_at=saved['issued_at'])
                    continue
                local = scheduled.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
                prediction = m.estimate(MODEL, response['route'], local, flight['airline'] or '??')
                kind = 'predeparture' if scheduled > now and flight['status'] == 'scheduled' else 'retrospective'
                flight.update(prediction=prediction, prediction_kind=kind, prediction_issued_at=now.isoformat())
                if kind == 'predeparture':
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
    if isinstance(obj, (m.pd.Timestamp, datetime)):
        return obj.isoformat()
    if obj is m.pd.NaT:
        return None
    if hasattr(obj, 'item'):
        return clean(obj.item())
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def route_arg(value):
    if value not in ROUTES:
        raise ValueError('请选择支持的首都机场出港航线。')
    return value


def date_arg(value, start='2019-01-01', end='2019-12-31'):
    try:
        day = datetime.strptime(value, '%Y-%m-%d').strftime('%Y-%m-%d')
    except (TypeError, ValueError):
        raise ValueError('日期格式必须为 YYYY-MM-DD。') from None
    if not start <= day <= end:
        raise ValueError(f'日期须在 {start} 至 {end} 之间。')
    return day


def backtest(route, compare, start, end):
    start = date_arg(start)
    end = date_arg(end)
    if start > end:
        raise ValueError('开始日期不能晚于结束日期。')
    days = TEST.scheduled.dt.strftime('%Y-%m-%d')
    selected = TEST[days.between(start, end) & TEST.destination.isin([route, compare])].copy()
    series = []
    summary = []
    for code in dict.fromkeys([route, compare]):
        data = selected[selected.destination == code]
        if not len(data):
            summary.append({'route': code, 'n': 0})
            continue
        summary.append({
            'route': code,
            'n': len(data),
            'actual_mean': data.arrival_DT.mean(),
            'predicted_mean': data.predicted_DT.mean(),
            'mae': (data.arrival_DT - data.predicted_DT).abs().mean(),
            'delay15': data.arrival_DT.gt(15).mean(),
            'coverage': data.arrival_DT.between(data.lower, data.upper).mean(),
        })
        aggregate = data.groupby(data.scheduled.dt.strftime('%Y-%m-%d')).agg(
            actual=('arrival_DT', 'mean'), predicted=('predicted_DT', 'mean'), n=('arrival_DT', 'size')
        )
        series.append({'route': code, 'points': [{'date': index, **row.to_dict()} for index, row in aggregate.iterrows()]})
    focus = selected[selected.destination == route].sort_values('scheduled')
    records = focus[['flight_no', 'destination', 'scheduled', 'scheduled_arrival', 'arrival_DT', 'predicted_DT', 'lower', 'upper']].head(100).to_dict('records')
    return clean({
        'start': start,
        'end': end,
        'summary': summary,
        'series': series,
        'records': records,
        'record_total': len(focus),
        'record_limit': 100,
        'selected_model': META['selected'],
    })


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
    dst_file = m.SOURCE / 'dst_hourly.csv'
    roti_file = m.SOURCE / 'roti_hourly.csv'
    if dst_file.exists() and roti_file.exists():
        dst = m.pd.read_csv(dst_file, parse_dates=['hour_utc']).set_index('hour_utc')
        roti = m.pd.read_csv(roti_file, parse_dates=['hour_utc']).set_index('hour_utc')
        return dst[['abs_dDst_dt_nT_per_h']].join(roti[['ROTI_TECU_per_min']], how='inner')
    LOG.warning('历史空间天气运行数据缺失；自动预测将安全回退到基础模型。')
    return None


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
        except (BrokenPipeError, ConnectionResetError):
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
                    'today': datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d'),
                    'tomorrow': (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).strftime('%Y-%m-%d'),
                })
            if parsed.path == '/api/weather':
                return self.send(200, {'data': weather_view(), 'configured': True})
            if parsed.path in {'/api/backtest', '/api/export'}:
                route = route_arg(get('route', 'SHA'))
                compare = route_arg(get('compare', 'CAN'))
                start = date_arg(get('start', '2019-09-01'))
                end = date_arg(get('end', '2019-09-30'))
                if start > end:
                    raise ValueError('开始日期不能晚于结束日期。')
                if parsed.path == '/api/backtest':
                    return self.send(200, backtest(route, compare, start, end))
                days = TEST.scheduled.dt.strftime('%Y-%m-%d')
                data = TEST[days.between(start, end) & TEST.destination.eq(route)]
                columns = ['flight_no', 'destination', 'scheduled', 'scheduled_arrival', 'arrival_DT', 'predicted_DT', 'lower', 'upper']
                csv_text = data[columns].to_csv(index=False)
                return self.send(200, ('\ufeff' + csv_text).encode('utf-8'), 'text/csv; charset=utf-8', {'Content-Disposition': f'attachment; filename="PEK_{route}_{start}_{end}.csv"'})

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
                route = route_arg(obj.get('route', 'SHA'))
                day = date_arg(obj.get('date'), start='2019-01-01', end='2030-12-31')
                clock = obj.get('time', '10:00')
                try:
                    scheduled = datetime.strptime(day + ' ' + clock, '%Y-%m-%d %H:%M')
                except ValueError:
                    raise ValueError('起飞时间格式不正确。') from None
                airline = obj.get('airline', 'CA')
                if not isinstance(airline, str) or len(airline) != 2 or not airline.isalnum():
                    raise ValueError('请输入两位航空公司代码。')

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
                    context = auto_parameters(scheduled)
                    dst = context['dst'] if context['usable'] else None
                    roti = context['roti'] if context['usable'] else None
                else:
                    dst = optional('dst', 0, 200)
                    roti = optional('roti', 0, 2)
                    if (dst is None) != (roti is None):
                        raise ValueError('情景分析需同时输入 Dst 变化率和 ROTI。')
                result = m.estimate(MODEL, route, scheduled, airline.upper(), dst, roti)
                return self.send(200, {
                    **result,
                    'route': route,
                    'scheduled': scheduled.isoformat(),
                    'mode': 'automatic' if mode == 'auto' else ('scenario' if dst is not None else 'historical_model'),
                    'auto_parameters': context,
                    'outside_space_training': dst is not None and (dst > 48 or roti > .2),
                    'notice': '历史模型研究估计，不是航空公司发布的预计到达时间。区间为单航班残差校准区间。',
                })
            if path == '/api/live':
                route = route_arg(obj.get('route', 'SHA'))
                with LOCK:
                    cached = FLIGHT_CACHE.get(route)
                if cached and time.time() - cached[0] < 300:
                    return self.send(200, {**cached[1], 'cached': True})
                response = attach_forecasts(fetch_route(route, KEY))
                with LOCK:
                    FLIGHT_CACHE[route] = (time.time(), response)
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
    global META, MODEL, TEST, WEATHER, FORECASTS, HISTORICAL_SPACE, AUTH, PROXY_SECRET
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

    for required in ('metadata.json', 'model.joblib', 'test_flights.pkl'):
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
    TEST = m.pd.read_pickle(m.DATA / 'test_flights.pkl')
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
