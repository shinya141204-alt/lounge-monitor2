from flask import Flask, render_template, jsonify, request, send_from_directory, Response, redirect, session, url_for, abort, make_response
from werkzeug.exceptions import HTTPException
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import monitor
import atexit
import datetime
import threading
import logger
import os
import sys
import json
import mimetypes
import re
import time
import local_db
import scheduled_articles
import turbo_store
import request_guards
import zlib
from collections import defaultdict, OrderedDict
from werkzeug.utils import safe_join

from logging.handlers import RotatingFileHandler

# Set up logging with automatic size rotation (max 5MB, keep 2 backups) to prevent disk overflow
# (absolute path so the app also works when started from another working directory, e.g. tests)
log_file_path = os.environ.get('APP_LOG_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.log')
_app_role_env = (os.environ.get('APP_ROLE') or '').strip().lower()
_is_fly_multiworker_web = bool(
    os.environ.get('FLY_APP_NAME')
    and not os.environ.get('APP_LOG_PATH')
    and _app_role_env == 'web'
)
_is_fly_stdout_only = bool(
    os.environ.get('FLY_APP_NAME')
    and not os.environ.get('APP_LOG_PATH')
    and _app_role_env in ('web', 'worker')
)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))

root_logger = logging.getLogger('')
root_logger.setLevel(logging.INFO)
# Clear any default handlers to avoid duplicates
root_logger.handlers.clear()
if not _is_fly_stdout_only:
    file_handler = RotatingFileHandler(log_file_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    root_logger.addHandler(file_handler)
root_logger.addHandler(console)

import gzip
import hashlib
import hmac
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# x_host=0: Fly.io does not rewrite X-Forwarded-Host, so trusting x_host=1 would let clients spoof request.host / url_root
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
def _load_secure_config(env_key: str, fallback_value: str, is_sensitive: bool = True) -> str:
    """Load configuration with fallback safety, value sanitization, and security warning logging."""
    val = os.environ.get(env_key)
    if val is not None and val.strip():
        return val.strip()

    if is_sensitive:
        is_fly_prod = bool(os.environ.get('FLY_APP_NAME'))
        if is_fly_prod:
            # Never fall back to the publicly-known default values in production.
            # An unguessable random value keeps the app running while effectively
            # disabling the corresponding login / token until the secret is configured.
            import secrets as _secrets
            logging.error(
                f"[SECURITY] Environment variable '{env_key}' is not set in production. "
                f"Using a random one-time value instead of the default (login/token disabled). "
                f"Configure it via `flyctl secrets set {env_key}=...`."
            )
            return _secrets.token_urlsafe(48)
        logging.warning(
            f"[SECURITY NOTICE] Environment variable '{env_key}' is not set. "
            f"Using default fallback value (local development only)."
        )
    return fallback_value


app.secret_key = _load_secure_config('SECRET_KEY', 'aiseki-navi-staff-monitor-secret-key-2026')
app.permanent_session_lifetime = datetime.timedelta(days=7)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('FLY_APP_NAME') or os.environ.get('ENABLE_HTTPS'):
    app.config['SESSION_COOKIE_SECURE'] = True
STAFF_PASSCODE = _load_secure_config('STAFF_PASSCODE', '5555')
MASTER_PASSCODE = _load_secure_config('MASTER_PASSCODE', '2918')
ADMIN_API_SECRET = _load_secure_config('ADMIN_API_SECRET', 'lounge2026')

_login_failures_lock = threading.Lock()
_login_failures = defaultdict(list)  # ip -> [epoch_ts, ...]
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SEC = 300


def get_client_ip() -> str:
    """Resolve true client IP: prefer Fly.io's edge-verified `Fly-Client-IP` in production, else `request.remote_addr`."""
    if os.environ.get('FLY_APP_NAME'):
        fly_ip = (request.headers.get('Fly-Client-IP') or '').strip()
        if fly_ip:
            return fly_ip[:64]
    return (request.remote_addr or '0.0.0.0').strip()[:64]


def _compute_session_auth_gen(is_master: bool) -> str:
    """Compute a non-reversible HMAC generation fingerprint so rotating passcodes or tapping Force-Logout revokes active cookies."""
    import ops_governance
    if is_master:
        m_epoch = ops_governance.get_master_session_epoch(allow_blocking=True)
        msg = f'auth_gen:master:{MASTER_PASSCODE}:{m_epoch}'.encode('utf-8')
    else:
        eff_staff = ops_governance.get_effective_staff_passcode(STAFF_PASSCODE)
        epoch = ops_governance.get_staff_session_epoch()
        msg = f'auth_gen:staff:{eff_staff}:{epoch}'.encode('utf-8')
    return hmac.new(str(app.secret_key).encode('utf-8'), msg, hashlib.sha256).hexdigest()[:20]


@app.before_request
def enforce_session_passcode_generation():
    """Revoke staff/master sessions immediately if passcodes were rotated or if a legacy 90d cookie lacks `auth_gen` in production."""
    if request.endpoint == 'static' or request.path.startswith('/static/'):
        return
    if not (session.get('is_staff') or session.get('is_master')):
        return
    tok = session.get('auth_gen')
    is_m = bool(session.get('is_master'))
    if tok:
        if not request_guards.safe_compare_digest(tok, _compute_session_auth_gen(is_m)):
            session.clear()
    elif os.environ.get('FLY_APP_NAME'):
        session.clear()


def _is_login_rate_limited(ip_addr: str, scope: str = 'staff') -> bool:
    norm_ip = turbo_store.normalize_ip_for_rate_limit(ip_addr)
    if turbo_store.is_shared_bus_active():
        return turbo_store.get_rate_limit_count(f'staff_login:{scope}', norm_ip, LOGIN_WINDOW_SEC) >= LOGIN_MAX_ATTEMPTS
    now_t = time.time()
    k = (scope, norm_ip)
    with _login_failures_lock:
        recent = [t for t in _login_failures.get(k, []) if now_t - t < LOGIN_WINDOW_SEC]
        if recent:
            _login_failures[k] = recent
        else:
            _login_failures.pop(k, None)
        return len(recent) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip_addr: str, scope: str = 'staff') -> None:
    norm_ip = turbo_store.normalize_ip_for_rate_limit(ip_addr)
    if turbo_store.is_shared_bus_active():
        turbo_store.record_rate_limit_hit(f'staff_login:{scope}', norm_ip, LOGIN_WINDOW_SEC)
        return
    now_t = time.time()
    k = (scope, norm_ip)
    with _login_failures_lock:
        if len(_login_failures) > 512:
            stale_ips = [
                key for key, ts_list in _login_failures.items()
                if not ts_list or (now_t - ts_list[-1] >= LOGIN_WINDOW_SEC)
            ]
            for key in stale_ips:
                _login_failures.pop(key, None)
        recent = [t for t in _login_failures.get(k, []) if now_t - t < LOGIN_WINDOW_SEC]
        recent.append(now_t)
        _login_failures[k] = recent


def _clear_login_failures(ip_addr: str, scope: str = 'staff') -> None:
    norm_ip = turbo_store.normalize_ip_for_rate_limit(ip_addr)
    if turbo_store.is_shared_bus_active():
        turbo_store.clear_rate_limit(f'staff_login:{scope}', norm_ip)
    with _login_failures_lock:
        _login_failures.pop((scope, norm_ip), None)


@app.context_processor
def inject_auth_flags():
    """Automatically inject staff/master session flags (and unread inquiry badge for master) into every template."""
    import portal_guides
    is_m = bool(session.get('is_master'))
    unread = {'unread_total': 0, 'unread_priority': 0}
    if is_m:
        try:
            import ops_governance
            unread = ops_governance.get_unread_counts()
        except Exception:
            pass
    return {
        'is_staff': bool(session.get('is_staff')),
        'is_master': is_m,
        'ops_unread_total': unread.get('unread_total', 0),
        'ops_unread_priority': unread.get('unread_priority', 0),
        'area_iso_maps': portal_guides.AREA_3D_CROSS_SECTIONS,
    }


def _get_admin_secret_from_request() -> str:
    """Accept `X-Admin-Secret` header only (query `?secret=` removed so secrets never leak into access logs or history)."""
    return (request.headers.get('X-Admin-Secret') or '').strip()


def _secret_header_ok() -> bool:
    secret = _get_admin_secret_from_request()
    if not secret:
        return False
    ip = get_client_ip()
    if _is_login_rate_limited(ip, scope='master'):
        return False
    expected_secret = (os.environ.get('ADMIN_API_SECRET') or '').strip() or ADMIN_API_SECRET
    if request_guards.safe_compare_digest(secret, expected_secret):
        _clear_login_failures(ip, scope='master')
        return True
    _record_login_failure(ip, scope='master')
    return False


def is_admin_authorized():
    """Verify administrative permission either via authenticated staff/master session or secret token."""
    if session.get('is_staff') or session.get('is_master'):
        return True
    return _secret_header_ok()


def is_master_authorized():
    """Verify master/owner permission via master session or secret token."""
    if session.get('is_master'):
        return True
    return _secret_header_ok()


def _is_json_request() -> bool:
    """Determine whether the current request expects a JSON error response."""
    if request.path.startswith('/api/'):
        return True
    accept = request.headers.get('Accept', '')
    if 'application/json' in accept:
        return True
    if request.is_json:
        return True
    return False


def _render_http_error(e, code: int, default_name: str, title: str, message: str):
    if _is_json_request():
        return jsonify({'status': 'error', 'error': getattr(e, 'name', None) or default_name, 'code': code}), code
    return render_template('error.html', code=code, title=title, message=message), code


@app.errorhandler(404)
def handle_404_error(e):
    """Handle 404 Not Found error with JSON for API requests and friendly HTML for web pages."""
    return _render_http_error(e, 404, 'Not Found', 'ページが見つかりませんでした', 'お探しのページは削除されたか、URLが変更された可能性があります。')


@app.errorhandler(500)
def handle_500_error(e):
    """Handle 500 Internal Server Error with JSON for API requests and friendly HTML for web pages."""
    logging.exception(f"[HTTP 500] Internal Server Error on {request.path}: {e}")
    return _render_http_error(e, 500, 'Internal Server Error', 'システムエラーが発生しました', '一時的にサーバーで問題が発生しています。しばらく待ってから再度お試しください。')


@app.errorhandler(400)
def handle_400_error(e):
    """Handle 400 Bad Request error with JSON for API requests and friendly HTML for web pages."""
    return _render_http_error(e, 400, 'Bad Request', '不正なリクエストです', 'リクエストの形式が正しくありません。入力内容をご確認の上、再度お試しください。')


request_guards.register_request_guards(app, _render_http_error)


@app.after_request
def compress_response(response):
    """Set cache headers and automatically gzip-compress JSON/HTML/CSS/JS/XML/SVG/CSV/text responses."""
    is_static_req = (request.endpoint == 'static') or request.path.startswith('/static/')
    is_staff_sess = False if is_static_req else bool(session.get('is_staff') or session.get('is_master'))

    # 0. Baseline security headers (HSTS only when served over HTTPS behind Fly's proxy)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    is_embed = request.path.startswith('/embed/')
    if not is_embed:
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Content-Security-Policy', request_guards.build_csp_header(request.path, is_embed=is_embed))
    response.headers.setdefault('Content-Security-Policy-Report-Only', request_guards.build_csp_report_only_header(request.path, is_embed=is_embed))
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=(self), payment=()')
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')

    # 1. Unified Cache-Control headers (ISSUE-01 / F5.2)
    if is_static_req and response.status_code in (200, 304):
        if 'Cache-Control' not in response.headers:
            v_param = request.args.get('v') or ''
            if re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', v_param):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                response.headers['Cache-Control'] = 'public, max-age=86400'
        return response
    elif request.path in ('/sw.js', '/api/status'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    elif is_staff_sess or request.path.startswith('/staff'):
        if is_staff_sess:
            response.headers['X-Staff-View'] = '1'
        response.headers['Cache-Control'] = 'private, no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
    elif (
        response.content_type and 'text/html' in response.content_type and response.status_code == 200
        and not is_embed
        and 'no-store' not in (response.headers.get('Cache-Control') or '')
    ):
        if (
            os.environ.get('ENABLE_PUBLIC_EDGE_CACHE') == '1'
            and request.method == 'GET'
            and 'Set-Cookie' not in response.headers
            and not request.cookies.get('an_src')
        ):
            response.headers['Cache-Control'] = 'public, max-age=0, s-maxage=15, stale-while-revalidate=45'
            response.vary.add('Cookie')
        else:
            # Allow browser back/forward cache (bfcache) for instant 0ms back-navigation while revalidating normal loads
            response.headers['Cache-Control'] = 'private, no-cache, must-revalidate, max-age=0'

    # 2. Vary: Accept-Encoding + Gzip compression on compressible representations
    if response.status_code < 200 or response.status_code >= 300 or response.direct_passthrough:
        return response
    ctype = (response.content_type or '').lower()
    is_compressible_mime = (
        'application/json' in ctype
        or 'text/html' in ctype
        or 'text/css' in ctype
        or 'application/javascript' in ctype
        or 'text/javascript' in ctype
        or 'application/xml' in ctype
        or 'text/xml' in ctype
        or 'image/svg+xml' in ctype
        or 'text/csv' in ctype
        or 'text/plain' in ctype
    )
    if not is_compressible_mime:
        return response
    response.vary.add('Accept-Encoding')
    if (
        'Content-Encoding' in response.headers
        or 'gzip' not in _parse_accept_encoding(request.headers.get('Accept-Encoding', ''))
    ):
        return response
    data = response.get_data()
    if len(data) > 500:
        compressed = gzip.compress(data, compresslevel=4)
        response.set_data(compressed)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = len(compressed)

    return response


def _parse_accept_encoding(header_val: str) -> set:
    """Parse Accept-Encoding into lowercased encoding tokens, excluding q=0 entries (RFC 9110)."""
    accepted = set()
    for raw_item in (header_val or '').split(','):
        item = raw_item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(';')]
        enc = parts[0].lower()
        if not enc:
            continue
        q_zero = False
        for param in parts[1:]:
            if '=' in param:
                k, v = param.split('=', 1)
                if k.strip().lower() == 'q':
                    try:
                        if float(v.strip()) <= 0.0:
                            q_zero = True
                    except ValueError:
                        pass
        if not q_zero:
            accepted.add(enc)
    return accepted


_STATIC_PRECOMPRESS_EXTS = frozenset({'.css', '.js', '.svg', '.json'})
_STATIC_MIME_FALLBACKS = {
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.json': 'application/json; charset=utf-8',
    '.webp': 'image/webp',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.woff2': 'font/woff2',
    '.mp4': 'video/mp4',
}


def serve_static_asset(filename):
    """Pre-compressed (.br/.gz) + ETag + 304 + strict Cache-Control static asset handler."""
    abs_path = safe_join(app.static_folder, filename)
    if not abs_path or not os.path.isfile(abs_path):
        abort(404)
    ext = os.path.splitext(abs_path)[1].lower()
    ae_tokens = _parse_accept_encoding(request.headers.get('Accept-Encoding') or '')
    chosen_path = abs_path
    enc = ''
    if ext in _STATIC_PRECOMPRESS_EXTS:
        if 'br' in ae_tokens and os.path.isfile(abs_path + '.br'):
            chosen_path = abs_path + '.br'
            enc = 'br'
        elif 'gzip' in ae_tokens and os.path.isfile(abs_path + '.gz'):
            chosen_path = abs_path + '.gz'
            enc = 'gzip'

    st = os.stat(chosen_path)
    etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}-{enc or "raw"}"'
    v_param = request.args.get('v') or ''
    cc_val = 'public, max-age=31536000, immutable' if re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', v_param) else 'public, max-age=86400'
    ctype = _STATIC_MIME_FALLBACKS.get(ext) or mimetypes.guess_type(abs_path)[0] or 'application/octet-stream'

    inm = (request.headers.get('If-None-Match') or '').strip()
    if inm and (inm == etag or inm == '*'):
        resp304 = Response(b'', status=304, mimetype=ctype)
        resp304.headers['ETag'] = etag
        resp304.headers['Cache-Control'] = cc_val
        resp304.vary.add('Accept-Encoding')
        resp304.headers['X-Content-Type-Options'] = 'nosniff'
        return resp304

    with open(chosen_path, 'rb') as f:
        body = f.read()
    resp = Response(body, status=200, content_type=ctype)
    if enc:
        resp.headers['Content-Encoding'] = enc
    resp.headers['Content-Length'] = len(body)
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = cc_val
    resp.vary.add('Accept-Encoding')
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


app.view_functions['static'] = serve_static_asset


_csp_report_lock = threading.Lock()
_csp_report_counts = {}


@app.route('/api/csp-report', methods=['POST'])
def receive_csp_report():
    """Rate-limited, non-blocking CSP Report-Only violation sink (in-memory counters + sampled log only)."""
    if not turbo_store.check_sliding_rate_limit('csp_report_ip', get_client_ip(), limit=30, window_sec=60):
        return ('', 204)
    try:
        raw = request.get_data(cache=False)[:16384]
        data = json.loads(raw.decode('utf-8', errors='ignore')) if raw else {}
        rep = data.get('csp-report') if isinstance(data, dict) else None
        if not isinstance(rep, dict) and isinstance(data, list) and data:
            rep = (data[0] or {}).get('body') if isinstance(data[0], dict) else None
        if isinstance(rep, dict):
            _clean_log = lambda s, lim: re.sub(r'[\r\n\t\x00-\x1f\x7f]+', ' ', str(s or ''))[:lim]
            directive = _clean_log(rep.get('violated-directive') or rep.get('effectiveDirective') or 'unknown', 64)
            blocked = _clean_log(rep.get('blocked-uri') or rep.get('blockedURL') or '', 120)
            doc_uri = _clean_log(rep.get('document-uri') or rep.get('documentURL') or '', 120)
            k = (directive, blocked)
            with _csp_report_lock:
                if len(_csp_report_counts) > 128 and k not in _csp_report_counts:
                    _csp_report_counts.clear()
                cnt = _csp_report_counts.get(k, 0) + 1
                _csp_report_counts[k] = cnt
            if cnt <= 3 or (cnt % 50) == 0:
                logging.info(f"[CSP-Report] directive={directive} blocked={blocked} doc={doc_uri} count={cnt}")
    except Exception:
        pass
    return ('', 204)

@app.before_request
def redirect_to_custom_domain():
    """Redirect traffic from old .onrender.com domain to custom domain aiseki-navi.com (301 Permanent)."""
    host = request.host.split(':')[0].lower()
    if host.endswith('onrender.com'):
        url = 'https://aiseki-navi.com' + request.path
        if request.query_string:
            url += '?' + request.query_string.decode('utf-8', errors='ignore')
        return redirect(url, code=301)

_scheduler_started = False
_scheduler_lock = threading.Lock()
_web_boot_initialized = False
_web_analytics_flusher_started = False


def get_app_role() -> str:
    """Return active process role: 'web' (HTTP-only Gunicorn worker), 'worker' (background engine), or 'all' (legacy single-process)."""
    role = (os.environ.get('APP_ROLE') or 'all').strip().lower()
    return role if role in ('web', 'worker', 'all') else 'all'


def is_web_only_role() -> bool:
    return get_app_role() == 'web'


def _flush_web_analytics_on_exit():
    try:
        import analytics
        analytics.flush(force_sheets=True)
    except Exception as err:
        logging.warning(f"Web analytics exit flush warning: {err}")


def _start_web_analytics_flusher():
    """Start lightweight per-worker analytics flusher in APP_ROLE=web (60s Postgres sync + 10m Sheets append + atexit)."""
    global _web_analytics_flusher_started
    if _web_analytics_flusher_started:
        return False
    _web_analytics_flusher_started = True
    atexit.register(_flush_web_analytics_on_exit)

    def _web_analytics_loop():
        tick = 0
        while True:
            time.sleep(60)
            tick += 1
            try:
                import analytics
                analytics.flush(force_sheets=(tick % 10 == 0))
            except Exception as err:
                logging.warning(f"Web analytics periodic flush warning: {err}")

    threading.Thread(target=_web_analytics_loop, name="web-analytics-flusher", daemon=True).start()
    return True


_web_prewarm_watcher_started = False


def _start_web_prewarm_watcher():
    """Poll shared Redis snapshot publication in APP_ROLE=web and prewarm /, /api/status cleanly."""
    global _web_prewarm_watcher_started
    if _web_prewarm_watcher_started:
        return False
    _web_prewarm_watcher_started = True

    def _watcher_loop():
        jitter = 0.2 + (os.getpid() % 5) * 0.3
        time.sleep(jitter)
        last_pub_epoch = None
        while True:
            try:
                if turbo_store.is_shared_bus_active():
                    snap = turbo_store.get_live_snapshot(bypass_l1=False)
                    pub_ep = snap.get('published_at_epoch') if isinstance(snap, dict) else None
                    if pub_ep is not None and pub_ep != last_pub_epoch:
                        last_pub_epoch = pub_ep
                        sync_latest_data_from_shared_bus()
                        prewarm_ssr_and_status_caches()
            except Exception as err:
                logging.debug(f"Web prewarm watcher tick skipped: {err}")
            time.sleep(2.5)

    threading.Thread(target=_watcher_loop, name="web-prewarm-watcher", daemon=True).start()
    return True


def _ensure_scheduler_started_async():
    global _scheduler_started, _web_boot_initialized
    if is_web_only_role():
        if not _web_boot_initialized:
            with _scheduler_lock:
                if not _web_boot_initialized:
                    _web_boot_initialized = True
                    if os.environ.get('DISABLE_SCHEDULER') != '1':
                        _start_web_analytics_flusher()
                        if not app.testing:
                            _start_web_prewarm_watcher()

                    def _init_web_only():
                        global _web_boot_initialized
                        try:
                            local_db.init_db()
                        except Exception as e:
                            logging.warning(f"Web-only init_db warning (will retry): {e}")
                            with _scheduler_lock:
                                _web_boot_initialized = False
                            return
                        try:
                            restore_latest_data_if_empty()
                        except Exception as e:
                            logging.warning(f"Web-only restore warning: {e}")
                        if os.environ.get('DISABLE_SCHEDULER') != '1':
                            try:
                                local_db.refresh_forecast_caches_async(days=21)
                            except Exception as e:
                                logging.warning(f"Web-only forecast cache warm warning: {e}")

                    threading.Thread(target=_init_web_only, daemon=True).start()
        return
    if os.environ.get('DISABLE_SCHEDULER') == '1' or _scheduler_started:
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def _boot():
        import scheduler_jobs
        scheduler_jobs.boot_legacy_scheduler(sys.modules[__name__])

    threading.Thread(target=_boot, daemon=True).start()


@app.before_request
def start_scheduler():
    _ensure_scheduler_started_async()
    if is_web_only_role() and not request.path.startswith('/static/') and request.path != '/api/health':
        try:
            sync_latest_data_from_shared_bus()
        except Exception as e:
            logging.warning(f"Web-only bus sync warning: {e}")

# Global storage for analysis cache
analysis_cache = {
    'last_fetched': None,
    'raw_data': None,
    'processed_per_store': {} 
}
ANALYSIS_CACHE_DURATION = 900  # Cache for 15 minutes


def clear_analysis_and_report_caches():
    """Invalidates in-memory store analysis and weekly report caches when SQLite sync completes."""
    try:
        analysis_cache.get('processed_per_store', {}).clear()
        analysis_cache.get('raw_data_by_store', {}).clear()
    except Exception:
        pass
    try:
        import weekly_report
        weekly_report._report_cache['data'] = None
        weekly_report._report_cache['last_fetched'] = None
    except Exception:
        pass
    try:
        clear_fast_http_caches()
    except Exception:
        pass


local_db.register_sync_complete_callback(clear_analysis_and_report_caches)


# Global storage for the latest data (thread-safe)
data_lock = threading.Lock()
latest_data = {
    'top_store': None,
    'last_updated': None,
    'full_data': []
}
_shared_source_health = {}
_last_bus_snap_token = None
LATEST_STATUS_SNAPSHOT_FILE = os.environ.get('LATEST_STATUS_SNAPSHOT_FILE') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'latest_status_cache.json')


def sync_latest_data_from_shared_bus(force_l1: bool = False) -> bool:
    """Pull the latest 53-store snapshot published by `worker.py` via Redis / shared bus in <0.5ms."""
    global _shared_source_health, _last_bus_snap_token
    if not turbo_store.is_shared_bus_active():
        return False
    if not force_l1 and request_guards.should_skip_duplicate_bus_sync(bool(latest_data.get('full_data'))):
        return True
    snap = turbo_store.get_live_snapshot(bypass_l1=force_l1)
    if not snap or not isinstance(snap.get('full_data'), list) or not snap['full_data']:
        return False
    import ops_governance
    gov_sig = (tuple(sorted(ops_governance.get_hidden_brands())), tuple(sorted(ops_governance.get_hidden_stores())))
    snap_token = (snap.get('published_at_epoch'), snap.get('last_updated'), bool(snap.get('is_seed')), gov_sig)
    if not force_l1 and snap_token == _last_bus_snap_token and latest_data.get('full_data'):
        request_guards.mark_bus_synced_for_request()
        return True
    snap_stores = ops_governance.filter_visible_stores(snap['full_data'])
    snap_updated = snap.get('last_updated')
    snap_seed = bool(snap.get('is_seed'))
    if isinstance(snap.get('source_health'), dict) and snap['source_health']:
        _shared_source_health = snap['source_health']

    changed = False
    with data_lock:
        cur_stores = latest_data.get('full_data') or []
        cur_sig = _live_data_signature(cur_stores, latest_data.get('last_updated'), bool(latest_data.get('is_seed')))
        new_sig = _live_data_signature(snap_stores, snap_updated, snap_seed)
        if cur_sig != new_sig:
            latest_data['full_data'] = list(snap_stores)
            latest_data['top_store'] = snap_stores[0] if snap_stores else None
            latest_data['last_updated'] = snap_updated
            latest_data['is_seed'] = snap_seed
            changed = True
    _last_bus_snap_token = snap_token
    if changed:
        invalidate_live_caches(clear_editorial=False)
    request_guards.mark_bus_synced_for_request()
    return True


def save_latest_status_snapshot():
    """Persist latest_data to disk and publish to shared Redis bus so all web workers have 0ms latency."""
    try:
        with data_lock:
            if not latest_data.get('full_data') or latest_data.get('is_seed'):
                return
            payload = {
                'last_updated': latest_data.get('last_updated'),
                'full_data': [dict(s) for s in (latest_data.get('full_data') or [])]
            }
        try:
            import health as _hlth
            turbo_store.publish_live_snapshot(
                full_data=payload['full_data'],
                last_updated=payload['last_updated'],
                is_seed=False,
                source_health=_hlth.snapshot(),
            )
        except Exception:
            pass
        os.makedirs(os.path.dirname(LATEST_STATUS_SNAPSHOT_FILE), exist_ok=True)
        tmp_path = f"{LATEST_STATUS_SNAPSHOT_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            import json
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, LATEST_STATUS_SNAPSHOT_FILE)
    except Exception:
        pass


def restore_latest_data_if_empty():
    """Immediately populate latest_data from shared Redis bus, disk snapshot, DB, or store_master on cold start
    so users NEVER see a blocking loading screen even right after a redeploy."""
    if is_web_only_role() and sync_latest_data_from_shared_bus():
        return True
    with data_lock:
        if latest_data.get('full_data'):
            return True
    if sync_latest_data_from_shared_bus(force_l1=True):
        return True
    try:
        if os.path.exists(LATEST_STATUS_SNAPSHOT_FILE):
            import json
            import store_master
            master = store_master.load()
            with open(LATEST_STATUS_SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            full_data = [
                s for s in (saved.get('full_data') or [])
                if not master or s.get('name') in master
            ]
            if full_data:
                full_data.sort(
                    key=lambda x: (x.get('region') != 'Korea', int(x.get('women') or 0), int(x.get('men') or 0)),
                    reverse=True,
                )
                with data_lock:
                    if not latest_data.get('full_data'):
                        latest_data['full_data'] = full_data
                        latest_data['top_store'] = full_data[0]
                        latest_data['last_updated'] = saved.get('last_updated')
                        latest_data['is_seed'] = False
                return True
    except Exception:
        pass

    # Fallback 2: Restore from latest SQLite logs if available (staged 3h -> 24h -> 7d to avoid scanning 7 days every cold start)
    try:
        snap = request_guards.staged_snapshot_around(local_db)
        if snap and len(snap) >= 10:
            baseline_snap = local_db.get_snapshot_around(10, 25) or {}
            try:
                votes_map = local_db.get_recent_votes(hours=4) or {}
            except Exception:
                votes_map = {}
            import store_master
            master = store_master.load()
            items = []
            latest_ts = None
            for sname, vals in snap.items():
                if master and sname not in master:
                    continue
                info = master.get(sname, {})
                source = (info.get('brand') or 'oriental').lower()
                if source == 'olg' or source == 'ag':
                    source = 'oriental'
                cur_m = int(vals.get('men', 0))
                cur_w = int(vals.get('women', 0))
                prev = baseline_snap.get(sname)
                w_diff = (cur_w - int(prev.get('women', 0))) if prev else 0
                m_diff = (cur_m - int(prev.get('men', 0))) if prev else 0
                items.append({
                    'name': sname,
                    'men': cur_m,
                    'women': cur_w,
                    'source': source,
                    'region': info.get('region') or detect_region(sname),
                    'women_diff': w_diff,
                    'men_diff': m_diff,
                    'votes': votes_map.get(sname, {'smooth': 0, 'waiting': 0, 'lively': 0}),
                })
                if vals.get('ts') and (not latest_ts or vals['ts'] > latest_ts):
                    latest_ts = vals['ts']
            items.sort(key=lambda x: (x.get('region') != 'Korea', x['women'], x['men']), reverse=True)
            with data_lock:
                if not latest_data.get('full_data'):
                    latest_data['full_data'] = items
                    latest_data['top_store'] = items[0]
                    latest_data['last_updated'] = latest_ts
                    latest_data['is_seed'] = False
            return True
    except Exception:
        pass

    # Fallback 3: Ephemeral Render container just deployed -> build instant 53-store list from store_master.json
    try:
        import store_master
        master = store_master.load()
        if master:
            jst_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
            items = []
            for sname, info in master.items():
                brand = (info.get('brand') or 'OLG').upper()
                source = 'oriental' if brand in ('OLG', 'AG') else brand.lower()
                items.append({
                    'name': sname,
                    'men': 0,
                    'women': 0,
                    'source': source,
                    'region': info.get('region') or detect_region(sname),
                    'women_diff': 0,
                    'men_diff': 0,
                    'votes': {'smooth': 0, 'waiting': 0, 'lively': 0},
                })
            items.sort(key=lambda x: (x.get('region') != 'Korea'), reverse=True)
            with data_lock:
                if not latest_data.get('full_data'):
                    latest_data['full_data'] = items
                    latest_data['top_store'] = items[0]
                    latest_data['last_updated'] = jst_now.strftime("%Y-%m-%d %H:%M:%S")
                    latest_data['is_seed'] = True
            return True
    except Exception:
        pass
    return False


from cache_core import (
    _CACHE_QUERY_ALLOWLIST,
    _EDITORIAL_HTML_CACHE_MAX,
    _OGP_BYTES_CACHE_MAX,
    _PAGE_HTML_CACHE_MAX,
    _api_json_cache,
    _api_json_cache_lock,
    _aux_doc_cache_lock,
    _build_precompressed_response,
    _cached_json_response,
    _editorial_html_cache,
    _get_cached_events_nonblocking,
    _get_cached_html_or_render,
    _gov_sig,
    _is_static_editorial_route,
    _live_data_signature,
    _llms_txt_cache,
    _normalize_cache_query_args,
    _ogp_bytes_cache,
    _ogp_bytes_cache_lock,
    _page_html_cache,
    _page_html_cache_lock,
    _runtime_env_probe,
    _sitemap_xml_cache,
    _sns_cache_lock,
    _ssr_inflight,
    _ssr_payload_cache,
    _ssr_payload_cache_lock,
    _staff_editorial_preview_cache,
    _status_inflight,
    _status_json_cache,
    _status_json_cache_lock,
    clear_fast_http_caches,
    get_live_gen,
    invalidate_live_caches,
    is_fast_cache_tokens_enabled,
)
from ssr_payload import (
    BRAND_CLS_MAP as _BRAND_CLS_MAP,
    CLIENT_STATUS_DROP_KEYS as _CLIENT_STATUS_DROP_KEYS,
    attach_store_benchmarks as _attach_store_benchmarks,
    build_deferred_card_pool as _build_deferred_card_pool,
    build_top_provenance_meta as _build_top_provenance_meta,
    client_status_payload as _client_status_payload,
    enrich_for_ssr as _ssr_enrich_impl,
    enrich_stores_for_status as _enrich_stores_for_status,
    finalize_enriched_ranking as _finalize_enriched_ranking,
)


def _enrich_for_ssr(stores, is_seed: bool = False):
    """Attach display fields for instant server-side HTML card rendering."""
    try:
        benchmarks = local_db.get_all_stores_live_benchmarks(allow_blocking=False)
    except Exception:
        benchmarks = {}
    preloaded_evs, _ = _get_cached_events_nonblocking()
    ev_mod = None
    if preloaded_evs:
        try:
            import events as _ev_m
            ev_mod = _ev_m
        except Exception:
            ev_mod = None
    with data_lock:
        lu_ssr = latest_data.get('last_updated')
    return _ssr_enrich_impl(
        stores,
        is_seed=bool(is_seed),
        last_updated=lu_ssr,
        benchmarks=benchmarks,
        preloaded_evs=preloaded_evs,
        ev_mod=ev_mod,
        local_db_mod=local_db,
        is_testing=bool(app.testing),
        brand_ja_fn=_jinja_brand_ja,
    )


def get_initial_status_payload():
    """Return status dict for SSR embedding in index.html if available (memoized + Single-Flight)."""
    restore_latest_data_if_empty()
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    bucket_15m = now_jst.strftime('%Y%m%d%H%M')
    _, ev_upd = _get_cached_events_nonblocking()
    import ops_governance
    with data_lock:
        full_d = latest_data.get('full_data')
        if not full_d:
            return None
        last_u = latest_data.get('last_updated')
        is_s = bool(latest_data.get('is_seed'))
        raw_copy = list(full_d)
    raw_copy = ops_governance.filter_visible_stores(raw_copy)
    if is_fast_cache_tokens_enabled():
        c_key = (get_live_gen(), bucket_15m, last_u, is_s, len(raw_copy), ev_upd, _gov_sig(), getattr(local_db, 'DB_PATH', None))
    else:
        c_key = (_live_data_signature(raw_copy, last_u, is_s), bucket_15m, ev_upd, _runtime_env_probe())

    ev = None
    is_leader = False
    with _ssr_payload_cache_lock:
        if _ssr_payload_cache['key'] == c_key and _ssr_payload_cache['payload'] is not None:
            return _ssr_payload_cache['payload']
        inflight = _ssr_inflight.get(c_key)
        if inflight is not None and (time.monotonic() - inflight[1] < 10.0):
            ev = inflight[0]
        else:
            ev = threading.Event()
            _ssr_inflight[c_key] = (ev, time.monotonic())
            is_leader = True

    if not is_leader and ev is not None:
        ev.wait(timeout=0.5)
        with _ssr_payload_cache_lock:
            if _ssr_payload_cache['key'] == c_key and _ssr_payload_cache['payload'] is not None:
                return _ssr_payload_cache['payload']

    try:
        payload = {
            'timestamp': last_u,
            'ranking': _enrich_for_ssr(raw_copy, is_seed=is_s),
            'status': 'success',
            'is_seed': is_s,
        }
        with _ssr_payload_cache_lock:
            _ssr_payload_cache['key'] = c_key
            _ssr_payload_cache['payload'] = payload
        return payload
    finally:
        if is_leader:
            with _ssr_payload_cache_lock:
                cur = _ssr_inflight.get(c_key)
                if cur is not None and cur[0] is ev:
                    _ssr_inflight.pop(c_key, None)
            ev.set()


_prewarm_in_progress = False
_last_html_prewarm_mono = 0.0
_last_data_audit_mono = 0.0


def prewarm_ssr_and_status_caches():
    """Pre-populate SSR payload immediately and warm primary gzip caches asynchronously without per-minute CPU spikes."""
    global _prewarm_in_progress, _last_html_prewarm_mono
    try:
        get_initial_status_payload()
    except Exception:
        pass
    if get_app_role() == 'worker':  # worker serves no HTTP traffic: skip HTML/gzip warm-up CPU entirely
        return
    if os.environ.get('DISABLE_SCHEDULER') == '1' or app.testing or _prewarm_in_progress:
        return
    now_mono = time.monotonic()
    # Warm `/api/status` every cycle (tiny JSON) and full `/` HTML every 180s in a background thread
    warm_full_html = (now_mono - _last_html_prewarm_mono) >= 180.0
    if warm_full_html:
        _last_html_prewarm_mono = now_mono
    _prewarm_in_progress = True

    def _bg_warm(do_html: bool):
        global _prewarm_in_progress
        try:
            headers = {
                'User-Agent': 'aiseki-internal-prewarm-bot/1.0',
                'Accept-Encoding': 'gzip',
            }
            paths = ('/api/status', '/', '/for-women', '/price-comparison', '/stats', '/stats') if do_html else ('/api/status', '/')
            with app.test_client() as client:
                for path in paths:
                    try:
                        client.get(path, headers=headers)
                    except Exception:
                        pass
        finally:
            _prewarm_in_progress = False

    threading.Thread(target=_bg_warm, args=(warm_full_html,), daemon=True).start()

# Region Definitions
REGIONS = {
    'Hokkaido': ['Sapporo', '札幌', 'SAPPORO'],
    'Tohoku': ['Sendai', '仙台', 'ag仙台'],
    'Kanto': ['Shibuya', 'Ebisu', 'Shinjuku', 'Ueno', 'Kinshicho', 'Kashiwa', 'Machida', 'Yokohama', 'Omiya', 'Utsunomiya', 'Takasaki', '渋谷', '恵比寿', '新宿', '上野', '錦糸町', '柏', '町田', '横浜', '大宮', '宇都宮', '高崎', 'OMIYA', 'SHINJUKU', 'NISHISHINJUKU', 'KINSHICHO'],
    'Chubu': ['Nagoya', 'Shizuoka', 'Hamamatsu', 'Kanazawa', '名古屋', '静岡', '浜松', '金沢', '名古屋 錦', '名古屋錦', '栄'],
    'Kinki': ['Osaka', 'Umeda', 'Tenma', 'Shinsaibashi', 'Namba', 'Kyoto', 'Kobe', 'Chayamachi', '大阪', '梅田', '天満', '心斎橋', '難波', '京都', '神戸', '茶屋町', 'UMEDA', 'NAMBA', 'CHAYAMACHI'],
    'Chugoku': ['Okayama', 'Hiroshima', '岡山', '広島', 'OKAYAMA', 'HIROSHIMA', 'CLOVERS'],
    'Shikoku': ['Matsuyama', '松山', 'MATSUYAMA'],
    'Kyushu': ['Fukuoka', 'Kokura', 'Nagasaki', 'Oita', 'Kumamoto', 'Miyazaki', 'Kagoshima', 'Okinawa', '福岡', '小倉', '長崎', '大分', '熊本', '宮崎', '鹿児島', '沖縄', 'FUKUOKA', 'KUMAMOTO'],
    'Korea': ['Seoul', 'Gangnam', 'Hongdae', 'ソウル', 'カンナム', 'ホンデ']
}

def detect_region(store_name):
    # 1. Store master (single source of truth, e.g. fixes 'OLG 錦糸町' being matched by '錦' → Chubu)
    try:
        import store_master
        r = store_master.region_of(store_name)
        if r:
            return r
    except Exception:
        pass
    # 2. Fallback keyword matching for stores not yet in the master
    for region, keywords in REGIONS.items():
        for keyword in keywords:
            if keyword in store_name:
                return region
    return 'Other'

# Fetch interval: every minute during business hours, every 10 minutes 07:00-17:00 JST
# UNLESS a major scraper (Oriental Lounge / JIS) had an error or store list is incomplete -> retry in 60s!
def fetch_interval_sec(now_jst=None):
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if not (7 <= now_jst.hour < 17):
        return 60
    errs = getattr(monitor, 'fetch_errors', None) or {}
    if 'get_oriental_data' in errs or 'get_jis_data' in errs:
        return 60
    with data_lock:
        cur_len = len(latest_data.get('full_data') or [])
    if cur_len < 50:
        return 60
    return 600

_last_fetch_at = None

# Rolling in-memory snapshots for 15-min momentum calculation
rolling_snapshots = []

def refresh_momentum_from_db():
    """Recompute women_diff / men_diff (and promote cold-start seed data to real counts) once fast Google Sheets sync completes."""
    try:
        baseline_map = local_db.get_snapshot_around(10, 25) or {}
        with data_lock:
            is_seed_now = bool(latest_data.get('is_seed')) or not latest_data.get('full_data')

        # If server booted on an ephemeral container with 0:0 seed data, immediately promote to the latest Google Sheets snapshot
        if is_seed_now:
            snap = request_guards.staged_snapshot_around(local_db)
            if snap and len(snap) >= 10:
                try:
                    votes_map = local_db.get_recent_votes(hours=4) or {}
                except Exception:
                    votes_map = {}
                import store_master
                master = store_master.load()
                items = []
                latest_ts = None
                for sname, info in (master.items() if master else [(k, {}) for k in snap.keys()]):
                    vals = snap.get(sname, {})
                    brand = (info.get('brand') or 'OLG').upper()
                    source = 'oriental' if brand in ('OLG', 'AG') else brand.lower()
                    cur_m = int(vals.get('men', 0))
                    cur_w = int(vals.get('women', 0))
                    prev = baseline_map.get(sname)
                    w_diff = (cur_w - int(prev.get('women', 0))) if prev else 0
                    m_diff = (cur_m - int(prev.get('men', 0))) if prev else 0
                    items.append({
                        'name': sname,
                        'men': cur_m,
                        'women': cur_w,
                        'source': source,
                        'region': info.get('region') or detect_region(sname),
                        'women_diff': w_diff,
                        'men_diff': m_diff,
                        'votes': votes_map.get(sname, {'smooth': 0, 'waiting': 0, 'lively': 0}),
                    })
                    if vals.get('ts') and (not latest_ts or vals['ts'] > latest_ts):
                        latest_ts = vals['ts']
                items.sort(key=lambda x: (x.get('region') != 'Korea', x['women'], x['men']), reverse=True)
                with data_lock:
                    if latest_data.get('is_seed') or not latest_data.get('full_data'):
                        latest_data['full_data'] = items
                        latest_data['top_store'] = items[0] if items else None
                        if latest_ts:
                            latest_data['last_updated'] = latest_ts
                        latest_data['is_seed'] = False
                save_latest_status_snapshot()
                invalidate_live_caches(clear_editorial=False)
                prewarm_ssr_and_status_caches()
                print("[app] Cold-start seed promoted to live counts from fast Google Sheets sync.")
                return

        recent_snap = {}
        try:
            recent_snap = local_db.get_snapshot_around(0, 30) or {}
        except Exception:
            recent_snap = {}

        if not baseline_map and not recent_snap:
            return
        updated_any = False
        with data_lock:
            full_data = latest_data.get('full_data') or []
            if not full_data or latest_data.get('is_seed'):
                return
            for s in full_data:
                sname = s.get('name')
                if float(s.get('fetched_at') or 0.0) <= 0.0 and sname in recent_snap:
                    s['women'] = int(recent_snap[sname].get('women', 0))
                    s['men'] = int(recent_snap[sname].get('men', 0))
                    updated_any = True
                prev = baseline_map.get(sname)
                if prev:
                    s['women_diff'] = int(s.get('women', 0)) - int(prev.get('women', 0))
                    s['men_diff'] = int(s.get('men', 0)) - int(prev.get('men', 0))
                    updated_any = True
            if updated_any:
                full_data.sort(key=lambda x: (x.get('region') != 'Korea', int(x.get('women') or 0), int(x.get('men') or 0)), reverse=True)
                latest_data['top_store'] = full_data[0] if full_data else None
        if updated_any:
            save_latest_status_snapshot()
            invalidate_live_caches(clear_editorial=False)
            prewarm_ssr_and_status_caches()
            print("[app] Momentum (15-min women_diff/men_diff) refreshed from newly synced SQLite baseline.")
    except Exception as e:
        print(f"[app] refresh_momentum_from_db error: {e}", file=sys.stderr)

_is_fetching = False
_fetch_started_at = None
_last_update_error = None
_fetch_lock = threading.Lock()


def update_job(force=False, _claimed=False):
    global latest_data, _last_update_error, rolling_snapshots, _last_fetch_at, _last_data_audit_mono, _is_fetching, _fetch_started_at
    claimed_here = False
    if not _claimed:
        with _fetch_lock:
            if _is_fetching:
                if _fetch_started_at and (datetime.datetime.now() - _fetch_started_at).total_seconds() > 150:
                    _is_fetching = False
                elif not force:
                    return
            if not _is_fetching:
                _is_fetching = True
                _fetch_started_at = datetime.datetime.now()
                claimed_here = True

    try:
        with data_lock:
            has_live_data = bool(latest_data.get('full_data')) and not latest_data.get('is_seed')
        if not force and has_live_data and _last_fetch_at is not None:
            elapsed = (datetime.datetime.now() - _last_fetch_at).total_seconds()
            if elapsed < fetch_interval_sec() - 5:
                return
        _last_fetch_at = datetime.datetime.now()
        print(f"[{datetime.datetime.now()}] Updating data...")
        data = monitor.get_all_data()
        if data:
            for store in data:
                store['region'] = detect_region(store['name'])

            jst_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)

            # Determine ~15 min ago (10-22 min) baseline from in-memory rolling_snapshots or SQLite (non-blocking)
            baseline_map = {}
            best_diff_from_15 = 999.0
            for snap in rolling_snapshots:
                age_min = (jst_now - snap['dt']).total_seconds() / 60.0
                if 10 <= age_min <= 22:
                    dist = abs(age_min - 15.0)
                    if dist < best_diff_from_15:
                        best_diff_from_15 = dist
                        baseline_map = snap['map']
            if not baseline_map:
                try:
                    baseline_map = local_db.get_snapshot_around(10, 25)
                except Exception:
                    baseline_map = {}

            # Record current snapshot if at least 2 mins since last snapshot
            if not rolling_snapshots or (jst_now - rolling_snapshots[-1]['dt']).total_seconds() >= 120:
                rolling_snapshots.append({
                    'dt': jst_now,
                    'map': {s['name']: {'men': s.get('men', 0), 'women': s.get('women', 0)} for s in data}
                })
                if len(rolling_snapshots) > 25:
                    rolling_snapshots[:] = rolling_snapshots[-25:]

            # Fetch recent crowd votes & recent DB fallback OUTSIDE `data_lock` so web threads never wait on SQLite
            try:
                votes_map = local_db.get_recent_votes(hours=4)
            except Exception:
                votes_map = {}

            import store_master
            valid_master = store_master.load()
            recent_db_snap = {}
            if valid_master:
                try:
                    recent_db_snap = local_db.get_snapshot_around(0, 30) or {}
                except Exception:
                    recent_db_snap = {}
            now_epoch = time.time()
            import ops_governance  # resolve hidden sets OUTSIDE data_lock (job_state I/O never blocks web threads)
            gov_hb, gov_hs = ops_governance.get_hidden_brands(), ops_governance.get_hidden_stores()

            with data_lock:
                current_data = latest_data.get('full_data', [])

                # Guarantee all 53 canonical stores in store_master.json remain present even if one scraper
                # transiently times out on a cold-start deploy (prevents Oriental Lounge / JIS vanishing for 10m!)
                merged_map = {}
                if valid_master:
                    for m_name, m_info in valid_master.items():
                        brand_u = (m_info.get('brand') or 'OLG').upper()
                        m_source = 'oriental' if brand_u in ('OLG', 'AG') else brand_u.lower()
                        db_vals = recent_db_snap.get(m_name) or {}
                        merged_map[m_name] = {
                            'name': m_name,
                            'men': int(db_vals.get('men', 0)),
                            'women': int(db_vals.get('women', 0)),
                            'source': m_source,
                            'region': m_info.get('region') or detect_region(m_name),
                            'women_diff': 0,
                            'men_diff': 0,
                            'votes': votes_map.get(m_name, {'smooth': 0, 'waiting': 0, 'lively': 0}),
                            'fetched_at': 0.0,
                        }

                for old_s in current_data:
                    oname = old_s.get('name')
                    if not oname or (valid_master and oname not in valid_master):
                        continue
                    old_copy = dict(old_s)
                    fetched_ts = float(old_copy.get('fetched_at') or 0.0)
                    if fetched_ts <= 0.0 or (now_epoch - fetched_ts > 1800):
                        # If stale >30m (or never live-fetched yet), fall back to recent <=30m DB snapshot or 0:0
                        base_entry = merged_map.get(oname)
                        old_copy['women'] = int(base_entry['women']) if base_entry else 0
                        old_copy['men'] = int(base_entry['men']) if base_entry else 0
                        old_copy['women_diff'] = 0
                        old_copy['men_diff'] = 0
                        old_copy.pop('pos_telemetry', None)
                    merged_map[oname] = old_copy

                for s in data:
                    sname = s['name']
                    if valid_master and sname not in valid_master:
                        continue
                    prev = baseline_map.get(sname)
                    if prev:
                        s['women_diff'] = int(s.get('women', 0)) - int(prev.get('women', 0))
                        s['men_diff'] = int(s.get('men', 0)) - int(prev.get('men', 0))
                    else:
                        s['women_diff'] = 0
                        s['men_diff'] = 0
                    s['votes'] = votes_map.get(sname, {'smooth': 0, 'waiting': 0, 'lively': 0})
                    s['fetched_at'] = now_epoch
                    merged_map[sname] = s

                merged_data = ops_governance.filter_visible_stores(list(merged_map.values()), hb=gov_hb, hs=gov_hs)
                sorted_data = sorted(merged_data, key=lambda x: (x.get('region') != 'Korea', x['women'], x['men']), reverse=True)
                top_store = sorted_data[0] if sorted_data else None

                latest_data['top_store'] = top_store
                latest_data['full_data'] = sorted_data
                latest_data['last_updated'] = jst_now.strftime("%Y-%m-%d %H:%M:%S")
                latest_data['is_seed'] = False
            try:
                import moat_intelligence
                moat_intelligence.calibrate_from_jis_pos(sorted_data)
                if os.environ.get('DISABLE_SCHEDULER') != '1' and not app.testing:
                    moat_intelligence.get_city_weather_map(allow_blocking=True)
            except Exception:
                pass
            save_latest_status_snapshot()
            invalidate_live_caches(clear_editorial=False)
            prewarm_ssr_and_status_caches()
            print(f"Data updated. Top store: {top_store['name'] if top_store else 'None'}")
            _last_update_error = None
            now_audit_mono = time.monotonic()
            if (now_audit_mono - _last_data_audit_mono) >= 300.0:
                _last_data_audit_mono = now_audit_mono
                try:
                    import data_audit
                    data_audit.run_data_audit(live_stores=sorted_data, send_alerts=True)
                except Exception:
                    pass
                try:
                    local_db.resolve_due_forecasts_only(data, jst_now.strftime("%Y-%m-%d %H:%M:%S"))
                except Exception:
                    pass

            # --- Logging Optimization ---
            total_guests = sum(d.get('men', 0) + d.get('women', 0) for d in data)
            is_off_hours = 7 <= jst_now.hour < 17

            with data_lock:
                last_logged = request_guards.parse_jst_ts(latest_data.get('last_logged'))
            is_logging_time = True
            if last_logged is not None and (jst_now.replace(tzinfo=None) - last_logged).total_seconds() < 600:
                is_logging_time = False

            if total_guests == 0:
                print(f"Skipping logging: Total guest count is 0.")
            elif is_off_hours:
                print(f"Skipping logging: Current time ({jst_now.strftime('%H:%M')}) is out of business hours (17:00-07:00).")
            elif not is_logging_time:
                print(f"Skipping logging: Interval optimization (Less than 10 mins since last log).")
            else:
                with data_lock:
                    latest_data['last_logged'] = jst_now.strftime("%Y-%m-%d %H:%M:%S")
                threading.Thread(target=logger.log_data, args=(data,), daemon=True).start()
                print("Logging data to Google Sheets...")
        else:
            print("No data retrieved.")
            _last_update_error = "No data retrieved from monitor.get_all_data()"
    except Exception as e:
        print(f"[{datetime.datetime.now()}] Update failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        _last_update_error = e
    finally:
        if claimed_here:
            with _fetch_lock:
                _is_fetching = False
                _fetch_started_at = None



@app.route('/')
def index():
    import affiliate_manager
    trigger_background_fetch_if_needed()
    import analytics
    raw_explicit_s = (request.args.get('s') or '').strip()
    norm_explicit_s = analytics.normalize_inflow_source(raw_explicit_s)
    explicit_s = raw_explicit_s if re.fullmatch(r'\d{8}_\d{4}', raw_explicit_s) else norm_explicit_s
    cookie_src = analytics.normalize_inflow_source(request.cookies.get('an_src') or '')
    has_sns_inflow = bool(norm_explicit_s or cookie_src)
    bachelor_shares_instant = affiliate_manager.are_slots_sharing_url('dating_bachelor', 'dating_instant', allow_blocking=False)
    monetized_women_dating = affiliate_manager.is_slot_monetized('women_dating_bachelor', allow_blocking=False)
    monetized_women_clinic = affiliate_manager.is_slot_monetized('women_beauty_clinic', allow_blocking=False)

    raw_share_area = (request.args.get('area') or '').strip().lower()
    safe_share_area = raw_share_area if raw_share_area in ('shinjuku', 'shibuya', 'umeda', 'namba', 'nagoya', 'fukuoka') else ''

    def _render():
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        bucket_s = f"{now_jst.strftime('%Y%m%d_%H')}{(now_jst.minute // 15) * 15:02d}"
        s_param = f"{explicit_s}_{bucket_s}" if explicit_s and not re.search(r'\d{8}', explicit_s) else (explicit_s or bucket_s)
        is_top3_share = (request.args.get('share') or '').strip().lower() == 'top3'
        area_qs = f"&area={safe_share_area}" if safe_share_area else ''
        og_meta = None
        og_image_url = (
            f"https://aiseki-navi.com/ogp/top3.png?s={s_param}{area_qs}"
            if is_top3_share
            else f"https://aiseki-navi.com/ogp/live.png?s={s_param}"
        )
        try:
            import ogp_generator
            og_meta = ogp_generator.get_ogp_meta_for_slot(s_param, now_jst=now_jst)
        except Exception:
            pass
        initial_status = get_initial_status_payload()
        top_provenance = _build_top_provenance_meta(
            initial_status, local_db_mod=local_db, is_testing=bool(app.testing)
        )
        return render_template(
            'index.html',
            is_staff=False,
            initial_status=initial_status,
            initial_status_client=_client_status_payload(initial_status),
            deferred_card_pool=_build_deferred_card_pool(initial_status, is_staff=False),
            top_provenance=top_provenance,
            og_meta=og_meta,
            og_image_url=og_image_url,
            published_scheduled=scheduled_articles.get_published_articles(),
            has_sns_inflow=has_sns_inflow,
            bachelor_shares_instant=bachelor_shares_instant,
            monetized_women_dating=monetized_women_dating,
            monetized_women_clinic=monetized_women_clinic,
        )
    is_top3_share_sig = (request.args.get('share') or '').strip().lower() == 'top3'
    cache_sig = (
        'index_pub',
        has_sns_inflow,
        is_top3_share_sig,
        safe_share_area if is_top3_share_sig else '',
        bachelor_shares_instant,
        monetized_women_dating,
        monetized_women_clinic,
    )
    return _get_cached_html_or_render(cache_sig, _render)


def _redirect_preserving_query(target_path: str, code: int = 301):
    """Redirect to canonical internal path while preserving safe query parameters (such as SNS `?s=` attribution)."""
    raw_qs = (request.query_string or b'').decode('utf-8', 'ignore').strip()
    clean_qs = re.sub(r'[\r\n\x00]', '', raw_qs)[:240]
    if clean_qs:
        sep = '&' if '?' in target_path else '?'
        return redirect(f"{target_path}{sep}{clean_qs}", code=code)
    return redirect(target_path, code=code)


AFFILIATE_REDIRECTS = {
    'spu': os.environ.get('AFFILIATE_URL_SPU') or 'https://h.accesstrade.net/sp/cc?rk=0100oo8500p144',
    'spu_styleup': os.environ.get('AFFILIATE_URL_SPU') or 'https://h.accesstrade.net/sp/cc?rk=0100oo8500p144',
    'orbis': os.environ.get('AFFILIATE_URL_ORBIS') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
    'mens_skincare_clinic': os.environ.get('AFFILIATE_URL_ORBIS') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
    'hair_removal': os.environ.get('AFFILIATE_URL_HAIR_REMOVAL') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
    'mens_hair_removal': os.environ.get('AFFILIATE_URL_HAIR_REMOVAL') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
    'hair_removal_trial': os.environ.get('AFFILIATE_URL_HAIR_REMOVAL_TRIAL') or os.environ.get('AFFILIATE_URL_HAIR_REMOVAL') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
    'dating': os.environ.get('AFFILIATE_URL_DATING') or 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
    'dating_instant': os.environ.get('AFFILIATE_URL_DATING_INSTANT') or os.environ.get('AFFILIATE_URL_DATING') or 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
    'dating_bachelor': os.environ.get('AFFILIATE_URL_DATING_BACHELOR') or os.environ.get('AFFILIATE_URL_DATING') or 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
    'dating_casual': os.environ.get('AFFILIATE_URL_DATING_CASUAL') or os.environ.get('AFFILIATE_URL_DATING') or 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
    'women_dating_bachelor': os.environ.get('AFFILIATE_URL_WOMEN_DATING') or 'https://www.bachelorapp.net/',
    'women_dating': os.environ.get('AFFILIATE_URL_WOMEN_DATING') or 'https://www.bachelorapp.net/',
    'women_beauty_clinic': os.environ.get('AFFILIATE_URL_WOMEN_CLINIC') or os.environ.get('AFFILIATE_URL_WOMEN_BEAUTY_CLINIC') or 'https://www.s-b-c.net/',
    'women_clinic': os.environ.get('AFFILIATE_URL_WOMEN_CLINIC') or os.environ.get('AFFILIATE_URL_WOMEN_BEAUTY_CLINIC') or 'https://www.s-b-c.net/',
    'women_hair_removal': os.environ.get('AFFILIATE_URL_WOMEN_CLINIC') or os.environ.get('AFFILIATE_URL_WOMEN_BEAUTY_CLINIC') or 'https://www.s-b-c.net/',
    'women_cosme_salon': os.environ.get('AFFILIATE_URL_WOMEN_COSME') or 'https://px.a8.net/svt/ejp?a8mat=4BERA3+6J68QA+5GZ4+5YZ75',
    'women_cosme': os.environ.get('AFFILIATE_URL_WOMEN_COSME') or 'https://px.a8.net/svt/ejp?a8mat=4BERA3+6J68QA+5GZ4+5YZ75',
    'women_skincare': os.environ.get('AFFILIATE_URL_WOMEN_COSME') or 'https://px.a8.net/svt/ejp?a8mat=4BERA3+6J68QA+5GZ4+5YZ75',
    'night_care_supplement': os.environ.get('AFFILIATE_URL_NIGHT_SUPPLEMENT') or os.environ.get('AFFILIATE_URL_SUPPLEMENT') or 'https://px.a8.net/svt/ejp?a8mat=4BELTJ+7RTR3M+3H28+5ZU29',
    'hangover_supplement': os.environ.get('AFFILIATE_URL_NIGHT_SUPPLEMENT') or os.environ.get('AFFILIATE_URL_SUPPLEMENT') or 'https://px.a8.net/svt/ejp?a8mat=4BELTJ+7RTR3M+3H28+5ZU29',
    'supplement': os.environ.get('AFFILIATE_URL_NIGHT_SUPPLEMENT') or os.environ.get('AFFILIATE_URL_SUPPLEMENT') or 'https://px.a8.net/svt/ejp?a8mat=4BELTJ+7RTR3M+3H28+5ZU29',
    'night_supplement': os.environ.get('AFFILIATE_URL_NIGHT_SUPPLEMENT') or os.environ.get('AFFILIATE_URL_SUPPLEMENT') or 'https://px.a8.net/svt/ejp?a8mat=4BELTJ+7RTR3M+3H28+5ZU29',
    'women_night_supplement': os.environ.get('AFFILIATE_URL_NIGHT_SUPPLEMENT') or os.environ.get('AFFILIATE_URL_SUPPLEMENT') or 'https://px.a8.net/svt/ejp?a8mat=4BELTJ+7RTR3M+3H28+5ZU29',
    'second_venue_reservation': os.environ.get('AFFILIATE_URL_SECOND_VENUE') or os.environ.get('AFFILIATE_URL_IKYU') or 'https://px.a8.net/svt/ejp?a8mat=4BGDZ4+9R9ROY+1OK+NVP2P',
    'ikyu_restaurant': os.environ.get('AFFILIATE_URL_SECOND_VENUE') or os.environ.get('AFFILIATE_URL_IKYU') or 'https://px.a8.net/svt/ejp?a8mat=4BGDZ4+9R9ROY+1OK+NVP2P',
    'ikyu': os.environ.get('AFFILIATE_URL_SECOND_VENUE') or os.environ.get('AFFILIATE_URL_IKYU') or 'https://px.a8.net/svt/ejp?a8mat=4BGDZ4+9R9ROY+1OK+NVP2P',
    'second_venue': os.environ.get('AFFILIATE_URL_SECOND_VENUE') or os.environ.get('AFFILIATE_URL_IKYU') or 'https://px.a8.net/svt/ejp?a8mat=4BGDZ4+9R9ROY+1OK+NVP2P',
    'women_second_venue': os.environ.get('AFFILIATE_URL_SECOND_VENUE') or os.environ.get('AFFILIATE_URL_IKYU') or 'https://px.a8.net/svt/ejp?a8mat=4BGDZ4+9R9ROY+1OK+NVP2P',
}

def issue_human_proof_cookie(secret_key: str, now_ts: int = None) -> str:
    """Issue a signed human-proof cookie value (`ts.hmac24`) preventing raw `an_hp=1` spoofing."""
    ts = int(time.time()) if now_ts is None else int(now_ts)
    sig = hmac.new(str(secret_key or '').encode('utf-8'), f'an_hp_v1:{ts}'.encode('utf-8'), hashlib.sha256).hexdigest()[:24]
    return f'{ts}.{sig}'


def verify_human_proof_cookie(secret_key: str, val: str, max_age_sec: int = 2592000, now_ts: int = None) -> bool:
    raw = str(val or '').strip()
    if '.' not in raw or len(raw) > 64:
        return False
    ts_s, sig_s = raw.split('.', 1)
    if not ts_s.isascii() or not ts_s.isdigit() or not re.fullmatch(r'[0-9a-f]{24}', sig_s):
        return False
    ts = int(ts_s)
    cur = int(time.time()) if now_ts is None else int(now_ts)
    if (cur - ts) < -10 or (cur - ts) > max(60, int(max_age_sec)):
        return False
    expected = issue_human_proof_cookie(secret_key, now_ts=ts)
    return request_guards.safe_compare_digest(raw, expected)


@app.route('/link/<path:item_id>', methods=['GET', 'POST'])
def affiliate_redirect(item_id):
    """Safe internal redirect shielding affiliate links from ad-blockers & non-JS crawlers."""
    import affiliate_manager
    import analytics
    clean_id = item_id.strip().lower()
    target_url = affiliate_manager.resolve_affiliate_url(clean_id, allow_blocking=False) or AFFILIATE_REDIRECTS.get(clean_id)
    if not target_url:
        return redirect(url_for('index'))
    is_auth_staff = is_admin_authorized()
    has_staff_test_flag = _is_staff_test_request(include_referrer=True)
    ua_low = (request.headers.get('User-Agent') or '').lower()
    is_audit_probe_ua = (
        ('aisekinavi-opssentinel' in ua_low or ua_low.startswith('curl/'))
        and (request.args.get('staff_test') or '').strip().lower() in ('1', 'true')
    )
    if not is_auth_staff and _is_prefetch_request():
        resp_bot = redirect('/for-women' if clean_id.startswith('women_') else '/grooming', code=302)
        resp_bot.headers['X-Robots-Tag'] = 'noindex, nofollow'
        resp_bot.headers['Cache-Control'] = 'private, no-store'
        return resp_bot
    if not is_auth_staff and _is_bot_request():
        if is_audit_probe_ua:
            if not turbo_store.check_sliding_rate_limit('aff_probe_ip', get_client_ip(), limit=20, window_sec=600):
                return ('Too Many Requests', 429)
            resp_probe = redirect(target_url, code=302)
            resp_probe.headers['X-Robots-Tag'] = 'noindex, nofollow'
            resp_probe.headers['Cache-Control'] = 'private, no-store'
            return resp_probe
        resp_bot = redirect('/for-women' if clean_id.startswith('women_') else '/grooming', code=302)
        resp_bot.headers['X-Robots-Tag'] = 'noindex, nofollow'
        resp_bot.headers['Cache-Control'] = 'private, no-store'
        return resp_bot
    skip_tracking = bool(is_auth_staff or has_staff_test_flag)
    ref_path = affiliate_manager.resolve_safe_ref_path(
        request.args.get('from', ''), request.referrer or '', request.host or ''
    )
    parsed_ref = urllib_parse.urlparse(request.referrer or '')
    explicit_s = (request.args.get('s') or '').strip()
    ref_s = explicit_s or (urllib_parse.parse_qs(parsed_ref.query).get('s') or [''])[0].strip()
    inflow_tag = analytics.normalize_inflow_source(ref_s or (request.cookies.get('an_src') or '').strip())
    require_hp = app.config.get('ENFORCE_AFFILIATE_HUMAN_PROOF')
    if require_hp is None:
        require_hp = not bool(app.config.get('TESTING'))
    if not is_auth_staff:
        is_post_ok = (
            request.method == 'POST'
            and affiliate_manager.verify_click_confirm_token(
                str(app.secret_key), clean_id, request.form.get('csrf_clk', '')
            )
        )
        has_valid_hp = verify_human_proof_cookie(str(app.secret_key), request.cookies.get('an_hp_sig', '')) or (request.cookies.get('an_hp') == '1')
        is_get_ok = request.method == 'GET' and ((not require_hp) or has_valid_hp)
        if not (is_post_ok or is_get_ok):
            qs_map = {}
            if ref_path and ref_path != '-':
                qs_map['from'] = ref_path
            if (explicit_s or ref_s) and inflow_tag:
                qs_map['s'] = inflow_tag
            act_url = f"/link/{urllib_parse.quote(clean_id, safe='_-')}" + (f"?{urllib_parse.urlencode(qs_map)}" if qs_map else '')
            tok = affiliate_manager.issue_click_confirm_token(str(app.secret_key), clean_id)
            body = affiliate_manager.render_click_interstitial_html(
                clean_id, act_url, tok, back_href=(ref_path if ref_path.startswith('/') else '/')
            )
            return (body, 200, {
                'Content-Type': 'text/html; charset=utf-8',
                'X-Robots-Tag': 'noindex, nofollow',
                'Cache-Control': 'private, no-store',
            })
    client_ip = get_client_ip()
    if not is_auth_staff:
        rl_ok = (
            turbo_store.check_sliding_rate_limit('aff_clk_ip', client_ip, limit=30, window_sec=600)
            and turbo_store.check_sliding_rate_limit(
                'aff_clk_human', f"{client_ip}|{(request.headers.get('User-Agent') or '')[:80]}", limit=10, window_sec=600
            )
        )
        if not rl_ok:
            resp_rl = redirect('/for-women' if clean_id.startswith('women_') else '/grooming', code=302)
            resp_rl.headers['X-Robots-Tag'] = 'noindex, nofollow'
            resp_rl.headers['Cache-Control'] = 'private, no-store'
            return resp_rl
    if not skip_tracking:
        try:
            analytics.track('affiliate_click', f"{clean_id} @ {ref_path}")
            if inflow_tag:
                analytics.track('sns_inflow', f"cv:{inflow_tag}")
                analytics.track('sns_inflow', f"cvslot:{inflow_tag} @ {clean_id[:36]}")
        except Exception:
            pass
    resp = redirect(target_url, code=(303 if request.method == 'POST' else 302))
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    resp.headers['Cache-Control'] = 'private, no-store'
    if request.method == 'POST' and not is_auth_staff:
        resp.set_cookie('an_hp', '1', max_age=2592000, path='/', samesite='Lax', secure=bool(request.is_secure))
        resp.set_cookie('an_hp_sig', issue_human_proof_cookie(str(app.secret_key)), max_age=2592000, path='/', samesite='Lax', secure=bool(request.is_secure), httponly=True)
    return resp

# --- First-party analytics (no personal data) ---
import urllib.parse as urllib_parse

_BOT_MARKERS = (
    'bot', 'spider', 'crawl', 'slurp', 'facebookexternalhit', 'preview', 'headless', 'python-requests', 'curl',
    'chatgpt-user', 'perplexity-user', 'claude-user', 'claude-web', 'anthropic-ai', 'mistralai-user',
    'meta-externalfetcher', 'meta-externalagent', 'cohere-ai', 'google-extended', 'applebot-extended',
    'mediapartners-google', 'adsbot', 'apis-google', 'feedfetcher-google', 'google-read-aloud',
    'google-inspectiontool', 'googleother', 'storebot-google',
    'lighthouse', 'chrome-lighthouse', 'phantomjs', 'playwright', 'puppeteer', 'selenium',
    'semrush', 'ahrefs', 'petal', 'censys', 'zgrab', 'mj12', 'screaming frog', 'siteaudit', 'barkrowler',
    'bytespider', 'gptbot', 'oai-searchbot', 'python-httpx', 'aiohttp', 'go-http-client', 'wget', 'scrapy',
    'axios', 'node-fetch', 'undici', 'okhttp', 'libwww-perl', 'java/', 'apache-httpclient', 'cfnetwork/',
    'postmanruntime', 'insomnia', 'uptimerobot', 'pingdom', 'datadog', 'statuscake', 'line-poker', 'slack-imgproxy',
)
_TRACKED_PAGE_PREFIXES = ('/guide', '/columns', '/areas', '/area/', '/concierge', '/grooming', '/for-women', '/stats', '/price-comparison', '/oriental-lounge', '/ag', '/jis', '/asobibar', '/store/')
_LIVE_NO_CACHE_PREFIXES = ('/price-comparison', '/for-women', '/concierge', '/store/', '/areas', '/area/', '/oriental-lounge', '/ag', '/jis', '/asobibar')

@app.template_filter('seo_store_name')
def _jinja_seo_store_name(name):
    import store_master
    return store_master.seo_name(name)

@app.template_filter('store_slug')
def _jinja_store_slug(name):
    import store_master
    return store_master.slug_of(name)

@app.template_filter('store_linkable_slug')
def _jinja_store_linkable_slug(name):
    return local_db._linkable_store_slug(name)

@app.template_filter('brand_ja')
def _jinja_brand_ja(val):
    if val is None:
        return ''
    s = str(val).strip()
    su = s.upper()
    if su in ('OLG', 'ORIENTAL') or s in ('オリエンタル', 'オリラジ'):
        return 'オリエンタルラウンジ'
    if su == 'YATAKOI':
        return 'ヤタコイ'
    if su == 'AG':
        return 'ag'
    if su in ('JIS', 'ASOBIBAR', 'XIX', 'ALFA', 'CLOVERS'):
        return su
    return s


_BADGE_EMOJI_PREFIX_RE = re.compile(r'^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2197\u2198\uFE0E\uFE0F\s]+')

@app.template_filter('clean_badge')
def _jinja_clean_badge(val):
    if val is None:
        return ''
    return re.sub(r'^推定:\s*', '', _BADGE_EMOJI_PREFIX_RE.sub('', str(val))).strip()

@app.template_filter('merge_brand_stats')
def _jinja_merge_brand_stats(by_brand):
    if not isinstance(by_brand, dict):
        return {}
    merged = {}
    for raw_brand, bdata in by_brand.items():
        if not isinstance(bdata, dict):
            continue
        disp = _jinja_brand_ja(raw_brand)
        sc, aw, am = int(bdata.get('store_count', 0) or 0), float(bdata.get('total_avg_women', 0) or 0), float(bdata.get('total_avg_men', 0) or 0)
        w_i, m_i = isinstance(bdata.get('total_avg_women'), int), isinstance(bdata.get('total_avg_men'), int)
        if disp not in merged:
            merged[disp] = {'store_count': sc, 'total_avg_women': aw, 'total_avg_men': am, '_w_int': w_i, '_m_int': m_i}
        else:
            m = merged[disp]
            m['store_count'] += sc
            m['total_avg_women'] += aw
            m['total_avg_men'] += am
            m['_w_int'], m['_m_int'] = m['_w_int'] and w_i, m['_m_int'] and m_i
    return {
        disp: {
            'store_count': m['store_count'],
            'total_avg_women': int(round(m['total_avg_women'])) if m['_w_int'] else round(m['total_avg_women'], 1),
            'total_avg_men': int(round(m['total_avg_men'])) if m['_m_int'] else round(m['total_avg_men'], 1),
        }
        for disp, m in merged.items()
    }

def _is_bot_request():
    ua = (request.headers.get('User-Agent') or '').lower()
    return (not ua) or any(m in ua for m in _BOT_MARKERS)

def _is_prefetch_request():
    if request.headers.get('X-Sw-Prefetch') == '1':
        return True
    purp = f"{request.headers.get('Sec-Purpose', '')} {request.headers.get('Purpose', '')} {request.headers.get('X-Moz', '')}".lower()
    return 'prefetch' in purp or 'preview' in purp

def _is_staff_test_request(include_referrer: bool = False) -> bool:
    if (request.args.get('staff_test') or '').strip().lower() in ('1', 'true'):
        return True
    if include_referrer and request.referrer:
        try:
            ref_qs = urllib_parse.parse_qs(urllib_parse.urlparse(request.referrer).query)
            return (ref_qs.get('staff_test') or [''])[0].strip().lower() in ('1', 'true')
        except Exception:
            pass
    return False


@app.after_request
def track_page_views(response):
    try:
        if request.endpoint == 'static' or request.path.startswith('/static/'):
            return response
        if (request.method == 'GET' and response.status_code == 200
                and (request.path == '/' or request.path.startswith(_TRACKED_PAGE_PREFIXES))
                and not request.path.endswith('.csv')
                and not session.get('is_staff')
                and not session.get('is_master')
                and not _is_staff_test_request(include_referrer=True)
                and not _is_bot_request()
                and not _is_prefetch_request()):
            import analytics
            pv_ok = True
            if not app.config.get('TESTING'):
                has_hp = verify_human_proof_cookie(str(app.secret_key), request.cookies.get('an_hp_sig', ''))
                pv_ok = turbo_store.check_sliding_rate_limit(
                    'pv_ip_hp' if has_hp else 'pv_ip_nohp',
                    get_client_ip(),
                    limit=(90 if has_hp else 18),
                    window_sec=600,
                )
            if pv_ok:
                analytics.track('page_view', request.path)
            raw_s = (request.args.get('s') or '').strip()
            inflow_tag = analytics.normalize_inflow_source(raw_s) if raw_s else ''
            if not inflow_tag and not request.cookies.get('an_src') and request.referrer:
                inflow_tag = analytics.classify_external_referrer_inflow(request.referrer, request.host)
            if inflow_tag:
                if pv_ok:
                    analytics.track('sns_inflow', f"{inflow_tag} @ {request.path}")
                response.set_cookie(
                    'an_src', inflow_tag, max_age=86400, path='/',
                    samesite='Lax', secure=bool(request.is_secure), httponly=True,
                )
    except Exception:
        pass
    return response


_BEACON_ALLOWED_EVENTS = frozenset({
    'human_pv', 'affiliate_impression', 'cushion_click', 'store_open', 'map_cafe', 'map_bar',
    'official_click', 'sns_click', 'share', 'vote', 'hashigo_intent',
    'co_view', 'sister_bridge_impression', 'sister_bridge_click',
})


@app.route('/api/track', methods=['POST'])
def track_event():
    """Receives navigator.sendBeacon events from the frontend (client-safe events only, rate-limited)."""
    if (
        _is_bot_request()
        or _is_staff_test_request(include_referrer=True)
        or session.get('is_staff')
        or session.get('is_master')
    ):
        return ('', 204)
    client_ip = get_client_ip()
    ua_key = f"{client_ip}|{(request.headers.get('User-Agent') or '')[:80]}"
    if (
        not turbo_store.check_sliding_rate_limit('api_track_ip', client_ip, limit=300, window_sec=60)
        or not turbo_store.check_sliding_rate_limit('api_track', ua_key, limit=120, window_sec=60)
    ):
        return ('', 204)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return ('', 204)
    ev_name = re.sub(r'[^A-Za-z0-9_.:-]', '', str(payload.get('event', '')).strip())[:30]
    if ev_name not in _BEACON_ALLOWED_EVENTS:
        return ('', 204)
    if ev_name == 'hashigo_intent' and not turbo_store.check_sliding_rate_limit('hashigo_intent_ip', client_ip, limit=10, window_sec=600):
        return ('', 204)
    if ev_name == 'human_pv' and not turbo_store.check_sliding_rate_limit('human_pv_ip', ua_key, limit=90, window_sec=600):
        return ('', 204)
    import analytics
    clean_lbl = str(payload.get('label', '')).strip()[:96]
    analytics.track(ev_name, clean_lbl)
    if ev_name == 'cushion_click':
        analytics.track_sns_cushion_attribution(clean_lbl, request.cookies.get('an_src'))
    resp = app.response_class('', status=204)
    if ev_name not in ('affiliate_impression', 'sister_bridge_impression', 'co_view'):
        if request.cookies.get('an_hp') != '1':
            resp.set_cookie('an_hp', '1', max_age=2592000, path='/', samesite='Lax', secure=bool(request.is_secure))
        if not verify_human_proof_cookie(str(app.secret_key), request.cookies.get('an_hp_sig', '')):
            resp.set_cookie('an_hp_sig', issue_human_proof_cookie(str(app.secret_key)), max_age=2592000, path='/', samesite='Lax', secure=bool(request.is_secure), httponly=True)
    return resp


@app.route('/staff', methods=['GET', 'POST'])
def staff_monitor():
    """Internal staff dashboard (staff passcode = field data only, master passcode = full management)."""
    error = None
    client_ip = get_client_ip()

    if request.method == 'POST':
        if _is_login_rate_limited(client_ip, scope='master') or _is_login_rate_limited(client_ip, scope='staff'):
            return render_template('staff_login.html', error='試行回数が上限に達しました。5分ほど待ってから再度お試しください'), 429
        import ops_governance
        eff_staff_passcode = ops_governance.get_effective_staff_passcode(STAFF_PASSCODE)
        entered = request.form.get('passcode', '').strip()
        if entered and request_guards.safe_compare_digest(entered, MASTER_PASSCODE):
            _clear_login_failures(client_ip, scope='master')
            _clear_login_failures(client_ip, scope='staff')
            session['is_staff'] = True
            session['is_master'] = True
            session['auth_gen'] = _compute_session_auth_gen(True)
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        elif entered and request_guards.safe_compare_digest(entered, eff_staff_passcode):
            _clear_login_failures(client_ip, scope='staff')
            session['is_staff'] = True
            session['is_master'] = False
            session['auth_gen'] = _compute_session_auth_gen(False)
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        else:
            _record_login_failure(client_ip, scope='staff')
            _record_login_failure(client_ip, scope='master')
            error = 'パスコードが正しくありません'

    # URL-query passcode login (?passcode=...) was removed: passcodes in URLs leak into
    # access logs, browser history and shared links. Use the POST form only.
    if request.args.get('passcode'):
        return redirect(url_for('staff_monitor'))

    # Allow explicit role switch (?switch=1) to re-enter passcode
    if request.args.get('switch') == '1':
        return render_template('staff_login.html', error=error)

    # Check if already authenticated in session
    if session.get('is_staff'):
        is_master = bool(session.get('is_master'))
        trigger_background_fetch_if_needed()
        def _render_staff():
            audit_report = None
            sched_queue = []
            editorial_preview = None
            if is_master:
                import data_audit
                import sns_editorial
                with data_lock:
                    cur_live = list(latest_data.get('full_data') or [])
                audit_report = data_audit.run_data_audit(live_stores=cur_live, send_alerts=False)
                sched_queue = scheduled_articles.get_all_with_status()
                try:
                    now_ep = time.monotonic()
                    top_sig = tuple((s.get('name'), int(s.get('women') or 0), int(s.get('men') or 0)) for s in cur_live[:5])
                    with _sns_cache_lock:
                        ep_hit = _staff_editorial_preview_cache['data'] if (
                            _staff_editorial_preview_cache['data'] is not None
                            and _staff_editorial_preview_cache['sig'] == top_sig
                            and (now_ep - _staff_editorial_preview_cache['ts'] < 120.0)
                        ) else None
                    if ep_hit is not None:
                        editorial_preview = ep_hit
                    else:
                        editorial_preview = sns_editorial.run_editorial_pipeline(
                            cur_live,
                            slot_label="22:00",
                            record_history=False,
                            use_ai=False,
                            ai_cache_only=True,
                        )
                        with _sns_cache_lock:
                            _staff_editorial_preview_cache['ts'] = now_ep
                            _staff_editorial_preview_cache['sig'] = top_sig
                            _staff_editorial_preview_cache['data'] = editorial_preview
                except Exception:
                    editorial_preview = None
            staff_init_status = get_initial_status_payload()
            staff_top_prov = _build_top_provenance_meta(
                staff_init_status, local_db_mod=local_db, is_testing=bool(app.testing)
            )
            return render_template(
                'index.html',
                is_staff=True,
                is_master=is_master,
                initial_status=staff_init_status,
                initial_status_client=_client_status_payload(staff_init_status),
                deferred_card_pool=_build_deferred_card_pool(staff_init_status, is_staff=True),
                top_provenance=staff_top_prov,
                published_scheduled=scheduled_articles.get_published_articles(),
                scheduled_queue=sched_queue,
                data_audit_report=audit_report,
                editorial_preview=editorial_preview,
            )
        return _get_cached_html_or_render(('staff_dash', is_master), _render_staff)

    return render_template('staff_login.html', error=error)


def trigger_background_fetch_if_needed():
    """Check if data is stale and trigger a background fetch if needed.
    In `APP_ROLE=web` mode, syncs from the shared Redis snapshot published by `worker.py` instead of scraping."""
    global _is_fetching, _fetch_started_at
    if is_web_only_role():
        sync_latest_data_from_shared_bus()
        return
    if os.environ.get('DISABLE_SCHEDULER') == '1':
        return

    with data_lock:
        last_updated = latest_data.get('last_updated')
        has_data = bool(latest_data.get('full_data')) and not latest_data.get('is_seed')

    is_stale = False
    if last_updated and has_data:
        last_time = request_guards.parse_jst_ts(last_updated)  # unparseable timestamp => treat as stale (never 500)
        current_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        if last_time is None or (current_jst - last_time).total_seconds() > fetch_interval_sec() + 30:
            is_stale = True

    if not has_data or is_stale:
        with _fetch_lock:
            # If a fetch has been running for more than 150s, assume it's hung and reset
            if _is_fetching and _fetch_started_at:
                elapsed = (datetime.datetime.now() - _fetch_started_at).total_seconds()
                if elapsed > 150:
                    print(f"WARNING: Previous fetch hung for {elapsed:.0f}s. Resetting flag.")
                    _is_fetching = False
                else:
                    return
            elif _is_fetching:
                return
            _is_fetching = True
            _fetch_started_at = datetime.datetime.now()

        print("Data missing or stale. Triggering background fetch in worker...")
        def background_fetch():
            global _is_fetching, _fetch_started_at
            try:
                update_job(force=not has_data, _claimed=True)
            finally:
                with _fetch_lock:
                    _is_fetching = False
                    _fetch_started_at = None
        threading.Thread(target=background_fetch, daemon=True).start()

@app.route('/sw.js')
def serve_sw():
    sw_path = safe_join(app.static_folder, 'sw.js')
    if not sw_path or not os.path.isfile(sw_path):
        abort(404)
    with open(sw_path, 'rb') as f:
        body = f.read()
    return Response(body, status=200, mimetype='application/javascript')

@app.route('/api/status')
def get_status():
    import store_hours
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    now_bucket = f"{now_jst.strftime('%Y%m%d%H')}_{(now_jst.minute // 10) * 10:02d}"
    import ops_governance
    with data_lock:
        full_d = latest_data.get('full_data')
        if not full_d:
            return jsonify({
                'timestamp': None,
                'ranking': [],
                'status': 'loading'
            })
        last_u = latest_data.get('last_updated')
        is_s = bool(latest_data.get('is_seed'))
        raw_copy = list(full_d)
    raw_copy = ops_governance.filter_visible_stores(raw_copy)
    if is_fast_cache_tokens_enabled():
        c_key = (get_live_gen(), now_bucket, last_u, is_s, len(raw_copy), _gov_sig(), getattr(local_db, 'DB_PATH', None))
    else:
        try:
            _hp = store_hours.get_business_hours_status('OLG 新宿', total_guests=0)
            _sh_sig = (id(store_hours.get_business_hours_status), _hp.get('is_open_now'), _hp.get('today_hours_label'))
        except Exception:
            _sh_sig = None
        c_key = (_live_data_signature(raw_copy, last_u, is_s), now_bucket, _sh_sig)

    inm = (request.headers.get('If-None-Match') or '').strip()

    def _serve_cached_status():
        etag_cached = _status_json_cache.get('etag') or ''
        if inm and etag_cached and inm == etag_cached:
            resp_304 = Response(status=304)
            resp_304.headers['ETag'] = etag_cached
            resp_304.vary.add('Accept-Encoding')
            return resp_304
        resp_hit = _build_precompressed_response(_status_json_cache['raw'], _status_json_cache['gzip'], 'application/json')
        if etag_cached:
            resp_hit.headers['ETag'] = etag_cached
        return resp_hit

    ev = None
    is_leader = False
    with _status_json_cache_lock:
        if _status_json_cache['key'] == c_key and _status_json_cache['raw'] is not None:
            return _serve_cached_status()
        inflight = _status_inflight.get(c_key)
        if inflight is not None and (time.monotonic() - inflight[1] < 10.0):
            ev = inflight[0]
        else:
            ev = threading.Event()
            _status_inflight[c_key] = (ev, time.monotonic())
            is_leader = True

    if not is_leader and ev is not None:
        ev.wait(timeout=0.5)
        with _status_json_cache_lock:
            if _status_json_cache['key'] == c_key and _status_json_cache['raw'] is not None:
                return _serve_cached_status()

    try:
        try:
            benchmarks = local_db.get_all_stores_live_benchmarks(allow_blocking=False)
        except Exception:
            benchmarks = {}

        enriched_ranking = _enrich_stores_for_status(
            raw_copy,
            last_updated=last_u,
            benchmarks=benchmarks,
            local_db_mod=local_db,
            is_testing=bool(app.testing),
            is_seed=is_s,
        )

        raw_bytes = json.dumps({
            'timestamp': last_u,
            'ranking': enriched_ranking,
            'status': 'success',
            'is_seed': is_s,
        }, ensure_ascii=False).encode('utf-8')
        gz_bytes = gzip.compress(raw_bytes, compresslevel=4) if len(raw_bytes) > 500 else None
        etag_val = f'W/"{zlib.crc32(raw_bytes):08x}-{len(raw_bytes):x}"'
        with _status_json_cache_lock:
            _status_json_cache['key'] = c_key
            _status_json_cache['raw'] = raw_bytes
            _status_json_cache['gzip'] = gz_bytes
            _status_json_cache['etag'] = etag_val
        if inm and inm == etag_val:
            resp_304 = Response(status=304)
            resp_304.headers['ETag'] = etag_val
            resp_304.vary.add('Accept-Encoding')
            return resp_304
        resp_fresh = _build_precompressed_response(raw_bytes, gz_bytes, 'application/json')
        resp_fresh.headers['ETag'] = etag_val
        return resp_fresh
    finally:
        if is_leader:
            with _status_json_cache_lock:
                cur = _status_inflight.get(c_key)
                if cur is not None and cur[0] is ev:
                    _status_inflight.pop(c_key, None)
            ev.set()

@app.route('/api/health')
def health_check():
    """Lightweight endpoint for keep-alive pings (cron-job.org etc.)"""
    return jsonify({'status': 'ok'})

@app.route('/api/health/deep')
def health_check_deep():
    """Data freshness + per-source scraper health. Returns 503 when data is stale (for external uptime monitors)."""
    import health
    if is_web_only_role():
        sync_latest_data_from_shared_bus()
    with data_lock:
        last_updated = latest_data.get('last_updated')
        store_count = len(latest_data.get('full_data') or [])
    age_sec = None
    last_time = request_guards.parse_jst_ts(last_updated)
    if last_time is not None:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        age_sec = int((now_jst - last_time).total_seconds())
    snap = health.snapshot()
    if not snap.get('sources') and isinstance(_shared_source_health, dict) and _shared_source_health.get('sources'):
        snap = _shared_source_health
    down = [k for k, v in (snap.get('sources') or {}).items() if isinstance(v, dict) and v.get('down')]
    stale = age_sec is None or age_sec > fetch_interval_sec() * 3 + 120
    ok = not stale and not down
    body = {
        'status': 'ok' if ok else 'degraded',
        'last_updated': last_updated,
        'data_age_sec': age_sec,
        'store_count': store_count,
        'down_sources': down,
        'webhook_configured': bool(snap.get('webhook_configured')),
    }
    if is_admin_authorized():
        body['detail'] = snap
        body['app_role'] = get_app_role()
        body['turbo'] = turbo_store.get_backend_status()
    return jsonify(body), (200 if ok else 503)

@app.route('/api/debug_ps')
def debug_ps():
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'pid': os.getpid(),
        'has_data': bool(latest_data['full_data']),
        'last_updated': latest_data['last_updated'],
        'threads': [t.name for t in threading.enumerate()]
    })

@app.route('/api/logs')
def view_logs():
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    if os.path.exists(log_file_path):
        with open(log_file_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200 * 1024), os.SEEK_SET)
            content = f.read().decode('utf-8', errors='replace')
        return Response(content, mimetype='text/plain; charset=utf-8')
    if _is_fly_stdout_only:
        return Response(
            "Production workers stream logs directly to stdout (use `flyctl logs -a aiseki-navi-tokyo`).\n",
            mimetype='text/plain; charset=utf-8',
        )
    return "No logs found.", 404

@app.route('/api/thread_status')
def get_thread_status():
    """Diagnostic endpoint to check the health of the background fetcher and logger"""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'last_updated': latest_data['last_updated'],
        'has_full_data': bool(latest_data['full_data']),
        'is_fetching': _is_fetching,
        'last_error': str(_last_update_error) if _last_update_error else None,
        'logger_error': str(logger.last_error) if logger.last_error else None,
        'fetch_errors': getattr(monitor, 'fetch_errors', {})
    })

@app.route('/api/vote', methods=['POST'])
def post_store_vote():
    """Submits a one-tap live crowd vote from a user."""
    import hashlib
    import store_master
    raw_payload = request.get_json(silent=True)
    if raw_payload is not None and not isinstance(raw_payload, dict):
        return jsonify({'status': 'error', 'error': 'Invalid JSON payload'}), 400
    payload = raw_payload or {}
    raw_store = str(payload.get('store_name', '')).strip()
    vote_type = str(payload.get('vote_type', '')).strip()
    if not raw_store or vote_type not in ('smooth', 'waiting', 'lively'):
        return jsonify({'status': 'error', 'error': 'Invalid parameters'}), 400
    if len(raw_store) > 80:
        return jsonify({'status': 'error', 'error': 'Invalid store'}), 400
    import ops_governance
    store_name = store_master.to_internal_store_name(raw_store)
    if ops_governance.is_store_hidden(store_name):
        return jsonify({'status': 'error', 'error': 'Unknown store'}), 400
    # Only accept votes for stores that actually exist in the current ranking or store_master (prevents junk rows on cold boot)
    with data_lock:
        known_names = {s.get('name') for s in latest_data.get('full_data', [])}
    if known_names:
        if store_name not in known_names:
            return jsonify({'status': 'error', 'error': 'Unknown store'}), 400
    elif not store_master.get(store_name):
        return jsonify({'status': 'error', 'error': 'Unknown store'}), 400
    raw_st_flag = payload.get('staff_test')
    payload_staff_test = (raw_st_flag is True) or (str(raw_st_flag or '').strip().lower() in ('1', 'true'))
    if (
        _is_staff_test_request(include_referrer=True)
        or payload_staff_test
        or bool(session.get('is_staff'))
        or bool(session.get('is_master'))
    ):
        counts = local_db.get_recent_votes(store_name=store_name, hours=4)
        return jsonify({
            'status': 'success',
            'reason': 'staff_test_noop',
            'store_name': store_master.seo_name(store_name),
            'votes': counts,
        })

    client_ip = get_client_ip()
    norm_ip = turbo_store.normalize_ip_for_rate_limit(client_ip)
    sec_bytes = str(app.secret_key).encode('utf-8')
    ip_hash = hmac.new(sec_bytes, f"vote_ip:{norm_ip}".encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    raw_voter = str(payload.get('voter_id', '')).strip()[:64]
    voter_hash = hmac.new(sec_bytes, f"vote_id:{raw_voter}".encode('utf-8'), hashlib.sha256).hexdigest()[:16] if raw_voter else ''

    raw_wait = payload.get('wait_minutes')
    wait_minutes = None
    if raw_wait is not None and raw_wait != '' and not isinstance(raw_wait, bool):
        try:
            w_int = int(raw_wait)
            if w_int in (0, 15, 30, 45):
                wait_minutes = w_int
        except (TypeError, ValueError, OverflowError):
            wait_minutes = None

    ok, reason = local_db.add_crowd_vote(
        store_name, vote_type, ip_hash, voter_id=voter_hash, wait_minutes=wait_minutes
    )
    if ok:
        try:
            import analytics
            if reason == 'updated_wait':
                analytics.track('vote_wait', f"{store_name}:{wait_minutes}m")
            else:
                analytics.track('vote', f"{store_name}:{vote_type}")
                if wait_minutes is not None:
                    analytics.track('vote_wait', f"{store_name}:{wait_minutes}m")
        except Exception:
            pass
    counts = local_db.get_recent_votes(store_name=store_name, hours=4)

    # Also update in-memory latest_data so subsequent /api/status calls reflect immediately
    with data_lock:
        for s in latest_data.get('full_data', []):
            if s.get('name') == store_name:
                s['votes'] = counts
                break
    if ok:
        turbo_store.update_live_snapshot_store_votes(store_name, counts)
        invalidate_live_caches(clear_editorial=False)

    return jsonify({
        'status': 'success' if ok else 'duplicate',
        'reason': reason,
        'store_name': store_master.seo_name(store_name),
        'votes': counts
    })


@app.route('/api/watch-intent', methods=['POST'])
def post_watch_intent():
    """Records an anonymous first-party condition-watch intent from a user on /store/<slug>."""
    import hashlib
    import store_master
    raw_payload = request.get_json(silent=True)
    if raw_payload is not None and not isinstance(raw_payload, dict):
        return jsonify({'status': 'error', 'error': 'Invalid JSON payload'}), 400
    payload = raw_payload or {}
    raw_store = str(payload.get('store_name', '')).strip()
    target_cond = str(payload.get('target_cond', 'female_55')).strip()
    if not raw_store or target_cond not in ('female_55', 'zero_wait', 'evening_peak'):
        return jsonify({'status': 'error', 'error': 'Invalid parameters'}), 400
    if len(raw_store) > 80:
        return jsonify({'status': 'error', 'error': 'Invalid store'}), 400

    import ops_governance
    store_name = store_master.to_internal_store_name(raw_store)
    if ops_governance.is_store_hidden(store_name):
        return jsonify({'status': 'error', 'error': 'Unknown store'}), 400
    with data_lock:
        known_names = {s.get('name') for s in latest_data.get('full_data', [])}
    if known_names:
        if store_name not in known_names:
            return jsonify({'status': 'error', 'error': 'Unknown store'}), 400
    elif not store_master.get(store_name):
        return jsonify({'status': 'error', 'error': 'Unknown store'}), 400
    raw_st_flag = payload.get('staff_test')
    payload_staff_test = (raw_st_flag is True) or (str(raw_st_flag or '').strip().lower() in ('1', 'true'))
    if (
        _is_staff_test_request(include_referrer=True)
        or payload_staff_test
        or bool(session.get('is_staff'))
        or bool(session.get('is_master'))
    ):
        watch_info = local_db.get_watch_intent_counts(store_name=store_name, hours=6)
        return jsonify({
            'status': 'success',
            'reason': 'staff_test_noop',
            'store_name': store_master.seo_name(store_name),
            'target_cond': target_cond,
            'watching_count': int(watch_info.get('watching_count', 0)),
        })

    client_ip = get_client_ip()
    norm_ip = turbo_store.normalize_ip_for_rate_limit(client_ip)
    sec_bytes = str(app.secret_key).encode('utf-8')
    raw_voter = str(payload.get('voter_id', '')).strip()[:64]
    ip_hash = hmac.new(sec_bytes, f"watch_ip:{norm_ip}".encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    voter_hash = (
        hmac.new(sec_bytes, f"watch_id:{norm_ip}:{raw_voter}".encode('utf-8'), hashlib.sha256).hexdigest()[:16]
        if raw_voter else ''
    )

    ok, reason = local_db.add_watch_intent(
        store_name, target_cond=target_cond, ip_hash=ip_hash, voter_id=voter_hash
    )
    if reason == 'rate_limited':
        return jsonify({
            'status': 'rate_limited',
            'reason': 'rate_limited',
            'store_name': store_master.seo_name(store_name),
            'target_cond': target_cond,
        }), 429
    if ok:
        try:
            import analytics
            analytics.track('watch_intent', f"{store_name}:{target_cond}")
        except Exception:
            pass
    watch_info = local_db.get_watch_intent_counts(store_name=store_name, hours=6)
    return jsonify({
        'status': 'success' if ok else 'duplicate',
        'reason': reason,
        'store_name': store_master.seo_name(store_name),
        'target_cond': target_cond,
        'watching_count': int(watch_info.get('watching_count', 0)),
    })


# Backward-compatible symbol re-exports and modular domain Blueprint registration
from portal_guides import AREA_GUIDES, BRAND_GUIDES, get_stores_for_area
from routes.portal_bp import (
    STORE_PAGE_COMPARE_LIMIT,
    STORE_PAGE_NEARBY_LIMIT,
    _nearest_store_names,
    _nearest_stores_with_km,
    _trim_nearby_comparison,
)
from routes.seo_feeds_bp import _build_llms_txt_documents
from routes.sns_jobs_bp import (
    SUPPORTED_SNS_JOB_TYPES,
    _enqueue_sns_job_and_maybe_dispatch,
    _ensure_inline_sns_consumer_started,
    drain_sns_job_queue_once,
    execute_sns_job_payload,
    precompute_editorial_and_media_bundle,
    run_scheduled_weekly_x_post,
    run_scheduled_x_data_law_post,
    run_scheduled_x_post,
    run_scheduled_x_search_catcher_post,
)
from routes.staff_bp import (
    _get_sns_kit_context,
    _render_cached_ig_slide_bytes,
    _sns_context_sig,
    _sns_kit_ctx_cache,
    _sns_slide_bytes_cache,
    _sns_zip_bytes_cache,
)
from routes import register_blueprints

register_blueprints(app)


# Start background services immediately on module import (unless disabled for unit tests)
_ensure_scheduler_started_async()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)


