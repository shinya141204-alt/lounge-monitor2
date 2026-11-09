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
import re
import time
import local_db
import scheduled_articles
import turbo_store
import request_guards
import zlib
from collections import defaultdict, OrderedDict

from logging.handlers import RotatingFileHandler

# Set up logging with automatic size rotation (max 5MB, keep 2 backups) to prevent disk overflow
# (absolute path so the app also works when started from another working directory, e.g. tests)
log_file_path = os.environ.get('APP_LOG_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.log')
_is_fly_multiworker_web = bool(
    os.environ.get('FLY_APP_NAME')
    and not os.environ.get('APP_LOG_PATH')
    and (os.environ.get('APP_ROLE') or '').strip().lower() == 'web'
)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))

root_logger = logging.getLogger('')
root_logger.setLevel(logging.INFO)
# Clear any default handlers to avoid duplicates
root_logger.handlers.clear()
if not _is_fly_multiworker_web:
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
        msg = f'auth_gen:master:{MASTER_PASSCODE}'.encode('utf-8')
    else:
        eff_staff = ops_governance.get_effective_staff_passcode(STAFF_PASSCODE)
        epoch = ops_governance.get_staff_session_epoch()
        msg = f'auth_gen:staff:{eff_staff}:{epoch}'.encode('utf-8')
    return hmac.new(str(app.secret_key).encode('utf-8'), msg, hashlib.sha256).hexdigest()[:20]


@app.before_request
def enforce_session_passcode_generation():
    """Revoke staff/master sessions immediately if passcodes were rotated or if a legacy 90d cookie lacks `auth_gen` in production."""
    if not (session.get('is_staff') or session.get('is_master')):
        return
    tok = session.get('auth_gen')
    is_m = bool(session.get('is_master'))
    if tok:
        if not hmac.compare_digest(str(tok), _compute_session_auth_gen(is_m)):
            session.clear()
    elif os.environ.get('FLY_APP_NAME'):
        session.clear()


def _is_login_rate_limited(ip_addr: str) -> bool:
    if turbo_store.is_shared_bus_active():
        return turbo_store.get_rate_limit_count('staff_login', ip_addr, LOGIN_WINDOW_SEC) >= LOGIN_MAX_ATTEMPTS
    now_t = time.time()
    with _login_failures_lock:
        recent = [t for t in _login_failures.get(ip_addr, []) if now_t - t < LOGIN_WINDOW_SEC]
        if recent:
            _login_failures[ip_addr] = recent
        else:
            _login_failures.pop(ip_addr, None)
        return len(recent) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip_addr: str) -> None:
    if turbo_store.is_shared_bus_active():
        turbo_store.record_rate_limit_hit('staff_login', ip_addr, LOGIN_WINDOW_SEC)
        return
    now_t = time.time()
    with _login_failures_lock:
        if len(_login_failures) > 512:
            stale_ips = [
                k for k, ts_list in _login_failures.items()
                if not ts_list or (now_t - ts_list[-1] >= LOGIN_WINDOW_SEC)
            ]
            for k in stale_ips:
                _login_failures.pop(k, None)
        recent = [t for t in _login_failures.get(ip_addr, []) if now_t - t < LOGIN_WINDOW_SEC]
        recent.append(now_t)
        _login_failures[ip_addr] = recent


def _clear_login_failures(ip_addr: str) -> None:
    if turbo_store.is_shared_bus_active():
        turbo_store.clear_rate_limit('staff_login', ip_addr)
    with _login_failures_lock:
        _login_failures.pop(ip_addr, None)


@app.context_processor
def inject_auth_flags():
    """Automatically inject staff/master session flags (and unread inquiry badge for master) into every template."""
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
    }


def _get_admin_secret_from_request() -> str:
    """Accept `X-Admin-Secret` header only (query `?secret=` removed so secrets never leak into access logs or history)."""
    return (request.headers.get('X-Admin-Secret') or '').strip()


def _secret_header_ok() -> bool:
    secret = _get_admin_secret_from_request()
    if not secret:
        return False
    ip = get_client_ip()
    if _is_login_rate_limited(ip):
        return False
    if hmac.compare_digest(secret, ADMIN_API_SECRET):
        return True
    _record_login_failure(ip)
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
    """Set cache headers and automatically gzip-compress JSON/HTML/CSS/JS/XML/SVG responses."""
    is_staff_sess = bool(session.get('is_staff') or session.get('is_master'))

    # 0. Baseline security headers (HSTS only when served over HTTPS behind Fly's proxy)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    if not request.path.startswith('/embed/'):
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Content-Security-Policy', "base-uri 'self'; object-src 'none'; frame-ancestors 'self'")
    else:
        response.headers.setdefault('Content-Security-Policy', "base-uri 'self'; object-src 'none'; frame-ancestors *")
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=(self), payment=()')
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    # 1. Unified Cache-Control headers (ISSUE-01 / F5.2)
    if request.path.startswith('/static/') and response.status_code == 200:
        if request.args.get('v'):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            response.headers['Cache-Control'] = 'public, max-age=86400'
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
        and not request.path.startswith('/embed/')
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

    # 2. Gzip compression
    if (
        response.status_code < 200
        or response.status_code >= 300
        or response.direct_passthrough
        or 'Content-Encoding' in response.headers
        or 'gzip' not in request.headers.get('Accept-Encoding', '').lower()
    ):
        return response
    ctype = response.content_type or ''
    if (
        'application/json' in ctype
        or 'text/html' in ctype
        or 'text/css' in ctype
        or 'application/javascript' in ctype
        or 'application/xml' in ctype
        or 'text/xml' in ctype
        or 'image/svg+xml' in ctype
    ):
        data = response.get_data()
        if len(data) > 500:
            compressed = gzip.compress(data, compresslevel=4)
            response.set_data(compressed)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = len(compressed)
            prev_vary = response.headers.get('Vary', '')
            if not prev_vary:
                response.headers['Vary'] = 'Accept-Encoding'
            elif 'accept-encoding' not in prev_vary.lower():
                response.headers['Vary'] = f"{prev_vary}, Accept-Encoding"

    return response

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


def _ensure_scheduler_started_async():
    global _scheduler_started, _web_boot_initialized
    if is_web_only_role():
        if not _web_boot_initialized:
            with _scheduler_lock:
                if not _web_boot_initialized:
                    _web_boot_initialized = True
                    if os.environ.get('DISABLE_SCHEDULER') != '1':
                        _start_web_analytics_flusher()
                    def _init_web_only():
                        try:
                            local_db.init_db()
                            restore_latest_data_if_empty()
                            if os.environ.get('DISABLE_SCHEDULER') != '1':
                                local_db.refresh_forecast_caches_async(days=21)
                                local_db.trigger_forecast_backfill_async(days=21, min_existing=80, max_rows=4500)
                        except Exception as e:
                            logging.warning(f"Web-only lightweight boot warning: {e}")
                    threading.Thread(target=_init_web_only, daemon=True).start()
        return
    if os.environ.get('DISABLE_SCHEDULER') == '1' or _scheduler_started:
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def _boot():
        try:
            from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
            local_db.init_db()
            restore_latest_data_if_empty()
            local_db.refresh_forecast_caches_async(days=21)
            local_db.trigger_forecast_backfill_async(days=21, min_existing=80, max_rows=4500)

            scheduler = BackgroundScheduler(
                timezone="UTC",
                executors={
                    'default': APSThreadPoolExecutor(8),
                    'sns_ai': APSThreadPoolExecutor(4),
                },
                job_defaults={
                    'misfire_grace_time': 600,
                    'coalesce': True,
                    'max_instances': 1,
                },
            )
            scheduler.add_job(func=update_job, trigger="interval", minutes=1)
            def _flush_analytics():
                try:
                    import analytics
                    analytics.flush()
                except Exception as err:
                    logging.error(f"analytics flush failed: {err}")
            atexit.register(_flush_analytics)
            scheduler.add_job(func=_flush_analytics, trigger="interval", minutes=10)
            scheduler.add_job(func=lambda: local_db.sync_from_google_sheets(days=2), trigger="interval", hours=2)
            # Human-paced anti-spam schedule with idempotent retry windows within each target hour:
            # First run posts with natural jitter; if a transient API/network hiccup occurs, the follow-up tick
            # within the same hour automatically retries (and exits in <0.1ms once `job_state.is_done` is True).
            import bonus_alert, data_audit, events, instagram_poster, note_poster, ops_governance, pdca_engine, sns_editorial, threads_poster, tiktok_poster, tips_poster
            from apscheduler.triggers.combining import OrTrigger
            from apscheduler.triggers.cron import CronTrigger
            def _visible_live_stores():
                with data_lock:
                    cur_copy, cur_seed = list(latest_data.get('full_data') or []), bool(latest_data.get('is_seed'))
                return ops_governance.filter_visible_stores(cur_copy), cur_seed
            def _x_sched_wrap(slot_lbl="20:00"):
                if not sns_editorial.should_fire_scheduled_slot_now("x", slot_lbl):
                    return
                cur_stores, _ = _visible_live_stores()
                skip_pace, pace_reason = sns_editorial.should_skip_slot_for_human_pacing("x", slot_lbl, stores=cur_stores)
                if skip_pace:
                    logging.info(f"Skipping X {slot_lbl} for human pacing: {pace_reason}")
                    return
                run_scheduled_x_post(slot_lbl)
            scheduler.add_job(func=_x_sched_wrap, trigger="cron", hour=11, minute="2,6,10,14,18,22,27", jitter=75, args=["20:00"], executor="sns_ai")
            scheduler.add_job(func=_x_sched_wrap, trigger="cron", hour=13, minute="2,6,10,14,18,22,27", jitter=75, args=["22:00"], executor="sns_ai")
            scheduler.add_job(func=_x_sched_wrap, trigger="cron", day_of_week="fri,sat", hour=15, minute="2,6,10,14,18,22,27", jitter=75, args=["00:00"], executor="sns_ai")
            def _tip_18():
                if not sns_editorial.should_fire_scheduled_slot_now("x_tip", "18:00"):
                    return
                skip_pace, pace_reason = sns_editorial.should_skip_slot_for_human_pacing("x_tip", "18:00")
                if skip_pace:
                    logging.info(f"Skipping X tip 18:00 for human pacing: {pace_reason}")
                    return
                tips_poster.run_scheduled_tip_post(slot_label="18:00")
            scheduler.add_job(func=_tip_18, trigger="cron", hour=9, minute="2,6,10,14,18,23,28", jitter=75, executor="sns_ai")
            def _note_check_job():
                try:
                    note_poster.check_and_post_new_note()
                except Exception as err:
                    logging.error(f"Scheduled note promotion check failed: {err}")
            # Check note RSS during calm midday (12:20 JST) and mid-afternoon (15:20 JST) windows, well clear of 18:00 Tips & 20:00-22:00 X rush
            scheduler.add_job(func=_note_check_job, trigger="cron", hour="3,6", minute=20, jitter=240, executor="sns_ai")
            def _threads_daily_job():
                try:
                    if not sns_editorial.should_fire_scheduled_slot_now("threads", "22:00"):
                        return
                    cur_stores, is_seed = _visible_live_stores()
                    if cur_stores and not is_seed:
                        skip_pace, pace_reason = sns_editorial.should_skip_slot_for_human_pacing("threads", "22:00", stores=cur_stores)
                        if skip_pace:
                            logging.info(f"Skipping Threads 22:00 for human pacing: {pace_reason}")
                            return
                        threads_poster.execute_scheduled_threads_post(cur_stores, slot_label="22:00", image_url=threads_poster.select_scheduled_threads_image_url())
                except Exception as err:
                    logging.error(f"Scheduled Threads post failed: {err}")
            scheduler.add_job(func=_threads_daily_job, trigger="cron", hour=13, minute="5,9,13,17,21,26,32", jitter=75, executor="sns_ai")
            # Plan E: Women-perspective 'Tonight's Zero-Wait & Ladies Perks' alerts on Threads (Wed/Fri/Sat 19:30 JST)
            # Note: IG Carousel and IG Ladies single-image feed are disabled in favor of Plan B IG Reels (daily 20:00 + active-night 22:50 JST).
            def _threads_ladies_job():
                try:
                    if not sns_editorial.should_fire_scheduled_slot_now("threads_ladies", "19:30"):
                        return
                    cur_stores, is_seed = _visible_live_stores()
                    if cur_stores and not is_seed:
                        threads_poster.execute_scheduled_ladies_threads_post(cur_stores, slot_label="19:30")
                except Exception as err:
                    logging.error(f"Scheduled Threads ladies post failed: {err}")
            scheduler.add_job(func=_threads_ladies_job, trigger="cron", day_of_week="wed,fri,sat", hour=10, minute="31,35,39,44,49", jitter=60, executor="sns_ai")
            def _ig_reel_scheduled_job(slot_lbl="20:00"):
                try:
                    if not sns_editorial.should_fire_scheduled_slot_now("ig_reel", slot_lbl) or not instagram_poster.is_reel_auto_schedule_enabled(allow_blocking=False):
                        return
                    cur_stores, is_seed = _visible_live_stores()
                    if cur_stores and not is_seed:
                        instagram_poster.publish_live_reel_to_instagram(cur_stores, reel_type="auto", slot_label=slot_lbl, force=False)
                except Exception as err:
                    logging.error(f"Scheduled Instagram Reel post failed: {err}")
            scheduler.add_job(func=_ig_reel_scheduled_job, trigger="cron", hour=11, minute="4,10,16,22,28,34,40", jitter=50, args=["20:00"], executor="sns_ai")
            scheduler.add_job(func=_ig_reel_scheduled_job, trigger=OrTrigger([CronTrigger(hour=13, minute="54", jitter=45, timezone="UTC"), CronTrigger(hour=14, minute="0,6,12,18,24,30", jitter=50, timezone="UTC")]), args=["22:50"], executor="sns_ai")
            def _tiktok_inbox_job(slot_lbl="20:35"):
                try:
                    if not tiktok_poster.is_tiktok_auto_schedule_enabled(allow_blocking=False) or not tiktok_poster.is_tiktok_token_configured() or not sns_editorial.should_fire_scheduled_slot_now("tiktok", slot_lbl):
                        return
                    cur_stores, is_seed = _visible_live_stores()
                    if cur_stores and not is_seed:
                        tiktok_poster.execute_scheduled_tiktok_inbox_post(cur_stores, slot_label=slot_lbl, force=False)
                except Exception as err:
                    logging.error(f"Scheduled TikTok Inbox post failed: {err}")
            scheduler.add_job(func=_tiktok_inbox_job, trigger="cron", day_of_week="wed,fri,sat", hour=11, minute="39,45,51,57", jitter=50, args=["20:35"], executor="sns_ai")
            def _crawl_events_job():
                try:
                    evs = events.fetch_all_events(force_refresh=True)
                    with data_lock:
                        cur_stores = list(latest_data.get('full_data') or [])
                    data_audit.run_data_audit(live_stores=cur_stores, all_events=evs, send_alerts=True)
                except Exception as err:
                    logging.error(f"Scheduled events crawl failed: {err}")
            scheduler.add_job(func=_crawl_events_job, trigger="cron", hour="1,8,20", minute=0)
            def _scheduled_articles_release_job():
                try:
                    scheduled_articles.check_and_announce_newly_published()
                except Exception as err:
                    logging.error(f"Scheduled articles release check failed: {err}")
            scheduler.add_job(func=_scheduled_articles_release_job, trigger="interval", minutes=15, executor="sns_ai")
            def _bonus_alert_check_job():
                try:
                    cur_stores, is_seed = _visible_live_stores()
                    if cur_stores and not is_seed:
                        bonus_alert.check_and_post_bonus_alert(cur_stores)
                except Exception as err:
                    logging.error(f"Bonus alert check failed: {err}")
            scheduler.add_job(func=_bonus_alert_check_job, trigger="interval", minutes=10, executor="sns_ai")
            def _x_weekly_sun_fallback_job():
                try:
                    if sns_editorial.should_fire_scheduled_slot_now("x_weekly", "19:00"):
                        run_scheduled_weekly_x_post(force=False)
                except Exception as err:
                    logging.error(f"Scheduled weekly X post failed: {err}")
            scheduler.add_job(func=_x_weekly_sun_fallback_job, trigger="cron", day_of_week="sun", hour=10, minute="2,7,12,18,24", jitter=75, executor="sns_ai")
            def _pdca_autonomous_job():
                try:
                    cur_stores, _ = _visible_live_stores()
                    pdca_engine.run_pdca_cycle(
                        live_stores=cur_stores,
                        days=14,
                        apply_actions=True,
                        use_ai=False,
                        use_ai_for_replenish=True,
                        record_log=True,
                    )
                except Exception as err:
                    logging.error(f"Autonomous PDCA cycle failed: {err}")
            # Run daily before evening SNS rush (17:35 JST = 08:35 UTC) and after late-night close (03:35 JST = 18:35 UTC)
            scheduler.add_job(func=_pdca_autonomous_job, trigger="cron", hour="8,18", minute=35, jitter=120, executor="sns_ai")
            def _sheets_retention_job():
                try:
                    import archive_data
                    archive_data.run_scheduled_retention_job()
                except Exception as err:
                    logging.error(f"Scheduled Google Sheets retention job failed: {err}")
            # Run daily during quiet early-morning off-hours (06:20 JST = 21:20 UTC) to prevent 10M-cell ceiling
            scheduler.add_job(func=_sheets_retention_job, trigger="cron", hour=21, minute=20, jitter=120)
            def _daily_token_and_db_maintenance_job():
                try:
                    import threads_poster
                    import instagram_poster
                    import tiktok_poster
                    if hasattr(threads_poster, 'maybe_refresh_threads_token'):
                        threads_poster.maybe_refresh_threads_token()
                    if hasattr(instagram_poster, 'maybe_refresh_ig_token'):
                        instagram_poster.maybe_refresh_ig_token()
                    if hasattr(tiktok_poster, 'refresh_tiktok_token_if_needed'):
                        tiktok_poster.refresh_tiktok_token_if_needed(force=False)
                except Exception as err:
                    logging.warning(f"Daily Meta/TikTok token auto-refresh check warning: {err}")
                try:
                    import arena_calendar
                    arena_calendar.get_arena_schedule_events(allow_blocking=True, force_refresh=True)
                except Exception as err:
                    logging.warning(f"Daily arena schedule refresh warning: {err}")
                try:
                    local_db.rollup_store_hourly_stats(days_back=4)
                    local_db.prune_old_logs(monitor_days=90, vote_days=14)
                except Exception as err:
                    logging.warning(f"Daily SQLite rollup/prune warning: {err}")
            # Run daily at 06:35 JST (21:35 UTC) so Meta/TikTok tokens renew even on quiet weekdays and SQLite stays lean
            scheduler.add_job(func=_daily_token_and_db_maintenance_job, trigger="cron", hour=21, minute=35, jitter=120, executor="sns_ai")
            scheduler.start()
            # Priority #1 at t=0s: scrape live headcounts first without competing with SQLite backfill or Sheets reads
            trigger_background_fetch_if_needed()

            def _staggered_background_warmup():
                try:
                    import events as _ev_boot
                    has_disk_events = os.path.exists(_ev_boot.CACHE_FILE)
                    if has_disk_events:
                        _ev_boot.fetch_all_events(force_refresh=False, allow_blocking=False)

                    # Stage 1 (t+5s): Sync 10 days of SQLite crowd history (sufficient for 7d reports & store analytics)
                    time.sleep(5)
                    local_db.sync_from_google_sheets(days=10, on_recent_synced=refresh_momentum_from_db)

                    # Stage 2: Restore crowd votes & warm Job State / Analytics caches sequentially (zero lock contention)
                    time.sleep(2)
                    local_db.restore_votes_from_sheets(hours=24)
                    try:
                        import job_state as _js
                        import analytics as _an
                        import sns_editorial as _se
                        import affiliate_manager as _afm
                        import archive_data as _ard
                        _js.all_keys()
                        _afm._load_overrides(allow_blocking=True)
                        _ard.get_retention_status(allow_blocking=True)
                        _an._refresh_sheet_rows_async()
                        _se.fetch_live_weather_context(allow_blocking=True)
                    except Exception:
                        pass

                    # Stage 3: Crawl 20+ external event pages & seed PDCA directives after user traffic has settled
                    time.sleep(30 if has_disk_events else 12)
                    _crawl_events_job()
                    import pdca_engine as _pdca_boot
                    if not _pdca_boot.get_active_directives(allow_blocking=True):
                        with data_lock:
                            boot_stores = list(latest_data.get('full_data') or [])
                        _pdca_boot.run_pdca_cycle(live_stores=boot_stores, days=14, apply_actions=True, use_ai=False, record_log=True)
                except Exception as err:
                    logging.error(f"Staggered background warmup error: {err}")

            threading.Thread(target=_staggered_background_warmup, daemon=True).start()
            logging.info("APScheduler & staggered background warmup started successfully")
        except Exception as e:
            logging.error(f"Scheduler boot error: {e}")

    threading.Thread(target=_boot, daemon=True).start()

@app.before_request
def start_scheduler():
    _ensure_scheduler_started_async()
    if is_web_only_role() and not request.path.startswith('/static/') and request.path != '/api/health':
        sync_latest_data_from_shared_bus()

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


@app.route('/api/analysis/<path:store_name>')
def get_store_analysis(store_name):
    """Returns aggregated hourly and weekly data for a specific store."""
    clean_name = (store_name or '').strip()
    if not clean_name or len(clean_name) > 80 or any(ord(ch) < 32 for ch in clean_name):
        return jsonify({'error': 'Store not found'}), 404

    import ops_governance
    import store_master
    internal_name = store_master.to_internal_store_name(clean_name)
    if ops_governance.is_store_hidden(internal_name):
        return jsonify({'error': 'Store not found'}), 404
    master_map = store_master.load()
    if internal_name not in master_map:
        with data_lock:
            live_names = {s.get('name') for s in (latest_data.get('full_data') or [])}
        cached_proc = (analysis_cache.get('processed_per_store') or {}).get(internal_name)
        if internal_name not in live_names and cached_proc is None:
            return jsonify({'error': 'Store not found'}), 404
    clean_name = internal_name


    import store_analysis
    raw_entry = (analysis_cache.get('raw_data_by_store') or {}).get(clean_name)
    proc_entry = (analysis_cache.get('processed_per_store') or {}).get(clean_name)
    raw_ts = raw_entry.get('last_fetched') if isinstance(raw_entry, dict) else None
    def _safe_analysis_call():
        p, code = store_analysis.compute_store_analysis_payload(
            clean_name,
            analysis_cache=analysis_cache,
            cache_duration=ANALYSIS_CACHE_DURATION,
        )
        if isinstance(p, dict) and 'store_name' in p:
            p = dict(p)
            p['store_name'] = store_master.seo_name(internal_name)
        return p, code

    if proc_entry is not None and raw_ts is not None:
        return _cached_json_response(
            ('analysis', local_db.DB_PATH, clean_name, raw_ts),
            _safe_analysis_call,
            ttl_sec=60.0,
        )
    payload, status_code = _safe_analysis_call()
    if status_code == 200:
        new_proc = (analysis_cache.get('processed_per_store') or {}).get(clean_name)
        new_raw = (analysis_cache.get('raw_data_by_store') or {}).get(clean_name)
        new_ts = new_raw.get('last_fetched') if isinstance(new_raw, dict) else None
        if new_proc is not None and new_ts is not None:
            return _cached_json_response(
                ('analysis', local_db.DB_PATH, clean_name, new_ts),
                lambda: (payload, 200),
                ttl_sec=60.0,
            )
    return jsonify(payload), status_code


# Global storage for the latest data (thread-safe)
data_lock = threading.Lock()
latest_data = {
    'top_store': None,
    'last_updated': None,
    'full_data': []
}
_shared_source_health = {}
LATEST_STATUS_SNAPSHOT_FILE = os.environ.get('LATEST_STATUS_SNAPSHOT_FILE') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'latest_status_cache.json')


def sync_latest_data_from_shared_bus(force_l1: bool = False) -> bool:
    """Pull the latest 53-store snapshot published by `worker.py` via Redis / shared bus in <0.5ms."""
    global _shared_source_health
    if not turbo_store.is_shared_bus_active():
        return False
    if not force_l1 and request_guards.should_skip_duplicate_bus_sync(bool(latest_data.get('full_data'))):
        return True
    snap = turbo_store.get_live_snapshot(bypass_l1=force_l1)
    if not snap or not isinstance(snap.get('full_data'), list) or not snap['full_data']:
        return False
    import ops_governance
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
    if changed:
        clear_fast_http_caches(clear_editorial=False)
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
                'full_data': latest_data.get('full_data', [])
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
        tmp_path = LATEST_STATUS_SNAPSHOT_FILE + '.tmp'
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


_BRAND_CLS_MAP = {
    'OLG': 'brand-olg',
    'オリエンタルラウンジ': 'brand-olg',
    'ag': 'brand-ag',
    'JIS': 'brand-jis',
    'ASOBIBAR': 'brand-asobibar',
    'XIX': 'brand-xix',
    'ALFA': 'brand-alfa',
    'ヤタコイ': 'brand-yatakoi',
    'YATAKOI': 'brand-yatakoi',
    'CLOVERS': 'brand-clovers',
}

_ssr_payload_cache_lock = threading.Lock()
_ssr_payload_cache = {'key': None, 'payload': None}
_status_json_cache_lock = threading.Lock()
_status_json_cache = {'key': None, 'raw': None, 'gzip': None, 'etag': None}
_api_json_cache_lock = threading.Lock()
_api_json_cache = OrderedDict()
_page_html_cache_lock = threading.Lock()
_page_html_cache = OrderedDict()
_editorial_html_cache = OrderedDict()
_PAGE_HTML_CACHE_MAX = 128
_EDITORIAL_HTML_CACHE_MAX = 64
_ogp_bytes_cache_lock = threading.Lock()
_ogp_bytes_cache = OrderedDict()
_OGP_BYTES_CACHE_MAX = 24
_sns_cache_lock = threading.Lock()
_staff_editorial_preview_cache = {'ts': 0.0, 'sig': None, 'data': None}
_aux_doc_cache_lock = threading.Lock()
_sitemap_xml_cache = {'key': None, 'xml': None}
_llms_txt_cache = {'key': None, 'short': None, 'full': None, 'facts': None}
_CACHE_QUERY_ALLOWLIST = ('s', 'area', 'slot', 'channel', 'switch', 'days', 'format')


def _normalize_cache_query_args() -> tuple:
    if not request.args:
        return ()
    out = []
    for k in _CACHE_QUERY_ALLOWLIST:
        if k not in request.args:
            continue
        raw_v = (request.args.get(k) or '').strip()
        if k == 's':
            if re.fullmatch(r'\d{8}_\d{4}', raw_v):
                norm_v = raw_v
            else:
                import analytics
                norm_v = analytics.normalize_inflow_source(raw_v)
            if norm_v:
                out.append(('s', norm_v))
            continue
        out.append((k, raw_v[:64]))
    return tuple(out)


def clear_fast_http_caches(clear_editorial: bool = True):
    """Clear short-lived SSR/HTML/status caches when underlying data mutates."""
    with _ssr_payload_cache_lock:
        _ssr_payload_cache['key'] = None
        _ssr_payload_cache['payload'] = None
    with _status_json_cache_lock:
        _status_json_cache['key'] = None
        _status_json_cache['raw'] = None
        _status_json_cache['gzip'] = None
        _status_json_cache['etag'] = None
    with _api_json_cache_lock:
        _api_json_cache.clear()
    with _page_html_cache_lock:
        _page_html_cache.clear()
        if clear_editorial:
            _editorial_html_cache.clear()
    with _ogp_bytes_cache_lock:
        _ogp_bytes_cache.clear()
    with _aux_doc_cache_lock:
        _llms_txt_cache['key'] = None
        _llms_txt_cache['short'] = None
        _llms_txt_cache['full'] = None
        _llms_txt_cache['facts'] = None
    if clear_editorial:
        try:
            from routes import portal_bp as _pb_mod
            _pb_mod.clear_monthly_csv_cache()
        except Exception:
            pass
        if clear_editorial:
            _sitemap_xml_cache['key'] = None
            _sitemap_xml_cache['xml'] = None
    if clear_editorial:
        with _sns_cache_lock:
            _staff_editorial_preview_cache['ts'] = 0.0
            _staff_editorial_preview_cache['sig'] = None
            _staff_editorial_preview_cache['data'] = None
            _sns_kit_ctx_cache['ctx'] = None
            _sns_kit_ctx_cache['key'] = None
            _sns_kit_ctx_cache['ts'] = 0.0
            _sns_slide_bytes_cache.clear()
            _sns_zip_bytes_cache['key'] = None
            _sns_zip_bytes_cache['ts'] = 0.0
            _sns_zip_bytes_cache['bytes'] = None


def _build_precompressed_response(raw_bytes: bytes, gz_bytes: bytes, content_type: str, status_code: int = 200):
    wants_gzip = (gz_bytes is not None) and ('gzip' in request.headers.get('Accept-Encoding', '').lower())
    if wants_gzip:
        resp = Response(gz_bytes, status=status_code, content_type=content_type)
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['Content-Length'] = len(gz_bytes)
        resp.headers['Vary'] = 'Accept-Encoding'
        return resp
    return Response(raw_bytes, status=status_code, content_type=content_type)


def _cached_json_response(cache_key, build_dict_fn, ttl_sec=30.0):
    now_mono = time.monotonic()
    with _api_json_cache_lock:
        hit = _api_json_cache.get(cache_key)
        if hit is not None and (now_mono - hit[0] < ttl_sec):
            _api_json_cache.move_to_end(cache_key)
            return _build_precompressed_response(hit[1], hit[2], 'application/json', hit[3])
    payload, status_code = build_dict_fn()
    raw_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    gz_bytes = gzip.compress(raw_bytes, compresslevel=4) if (status_code == 200 and len(raw_bytes) > 500) else None
    if status_code == 200:
        with _api_json_cache_lock:
            _api_json_cache[cache_key] = (now_mono, raw_bytes, gz_bytes, status_code)
            _api_json_cache.move_to_end(cache_key)
            while len(_api_json_cache) > 96:
                _api_json_cache.popitem(last=False)
    return _build_precompressed_response(raw_bytes, gz_bytes, 'application/json', status_code)


def _live_data_signature(stores, last_updated=None, is_seed=False):
    return (
        last_updated,
        bool(is_seed),
        tuple(
            (
                s.get('name'),
                int(s.get('women') or 0),
                int(s.get('men') or 0),
                int(s.get('women_diff') or 0),
                int(s.get('men_diff') or 0),
                int((s.get('votes') or {}).get('smooth') or 0),
                int((s.get('votes') or {}).get('waiting') or 0),
                int((s.get('votes') or {}).get('lively') or 0),
                int((s.get('pos_telemetry') or {}).get('waiting_groups') or 0),
                int((s.get('pos_telemetry') or {}).get('matched_groups') or 0),
            )
            for s in (stores or [])
        ),
    )


def _get_cached_events_nonblocking():
    """Return events from memory or disk cache without spawning network threads."""
    try:
        import events as _ev
        mem_evs = _ev._events_memory_cache.get('events')
        if mem_evs:
            return mem_evs, _ev._events_memory_cache.get('updated_at')
        if os.path.exists(_ev.CACHE_FILE):
            with open(_ev.CACHE_FILE, 'r', encoding='utf-8') as f:
                disk_data = json.load(f)
            if disk_data.get('events'):
                disk_data['events'] = _ev._ensure_regional_events(disk_data['events'])
                _ev._events_memory_cache = disk_data
                return disk_data['events'], disk_data.get('updated_at')
    except Exception:
        pass
    return [], None


def _runtime_env_probe():
    """Lightweight microsecond probe so business-hour transitions or test mocks immediately invalidate HTML/SSR caches."""
    sh_sig = None
    ev_sig = None
    db_sig = id(getattr(local_db, 'get_store_7day_analytics', None))
    wr_mod = sys.modules.get('weekly_report')
    wr_sig = id(getattr(wr_mod, 'generate_weekly_report', None)) if wr_mod else None
    try:
        import store_hours as _sh
        hp = _sh.get_business_hours_status('OLG 新宿', total_guests=0)
        sh_sig = (id(_sh.get_business_hours_status), hp.get('is_open_now'), hp.get('today_hours_label'))
    except Exception:
        pass
    try:
        import events as _ev
        ev_sig = (id(_ev.fetch_all_events), id(_ev.get_events_for_store))
    except Exception:
        pass
    gov_sig = None
    try:
        import ops_governance as _og
        gov_sig = (tuple(sorted(_og.get_hidden_brands())), tuple(sorted(_og.get_hidden_stores())))
    except Exception:
        pass
    return (sh_sig, ev_sig, db_sig, wr_sig, gov_sig)


def _is_static_editorial_route(path: str) -> bool:
    return path == '/guide' or path.startswith('/guide/') or path in ('/grooming', '/privacy', '/about', '/columns')


def _get_cached_html_or_render(cache_extra_key, render_fn, ttl_sec=660.0):
    """Fast thread-safe memoizer for rendered HTML strings keyed on session auth + live data signature."""
    is_editorial = _is_static_editorial_route(request.path)
    pub_slugs = tuple(a.get('slug') for a in scheduled_articles.get_published_articles())
    norm_q = _normalize_cache_query_args()
    is_staff = bool(session.get('is_staff'))
    is_master = bool(session.get('is_master'))

    if is_editorial:
        eff_ttl = max(ttl_sec, 300.0)
        full_key = (
            request.path,
            norm_q,
            is_staff,
            is_master,
            pub_slugs,
            _runtime_env_probe(),
            cache_extra_key,
        )
        target_cache = _editorial_html_cache
        max_entries = _EDITORIAL_HTML_CACHE_MAX
    else:
        eff_ttl = ttl_sec
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        bucket_15m = f"{now_jst.strftime('%Y%m%d%H')}_{(now_jst.minute // 15) * 15:02d}"
        with data_lock:
            cur_stores = latest_data.get('full_data') or []
            last_upd = latest_data.get('last_updated')
            is_seed = bool(latest_data.get('is_seed'))
            live_sig = _live_data_signature(cur_stores, last_upd, is_seed)
        _, ev_upd = _get_cached_events_nonblocking()
        full_key = (
            request.path,
            norm_q,
            is_staff,
            is_master,
            local_db.DB_PATH,
            bucket_15m,
            pub_slugs,
            ev_upd,
            _runtime_env_probe(),
            live_sig,
            cache_extra_key,
        )
        target_cache = _page_html_cache
        max_entries = _PAGE_HTML_CACHE_MAX

    now_mono = time.monotonic()
    with _page_html_cache_lock:
        hit = target_cache.get(full_key)
        if hit is not None and (now_mono - hit[0] < eff_ttl):
            target_cache.move_to_end(full_key)
            return _build_precompressed_response(hit[1], hit[2], 'text/html; charset=utf-8')
    rendered = render_fn()
    if isinstance(rendered, str):
        raw_bytes = rendered.encode('utf-8')
        gz_bytes = gzip.compress(raw_bytes, compresslevel=4) if len(raw_bytes) > 500 else None
        with _page_html_cache_lock:
            target_cache[full_key] = (now_mono, raw_bytes, gz_bytes)
            target_cache.move_to_end(full_key)
            while len(target_cache) > max_entries:
                target_cache.popitem(last=False)
        return _build_precompressed_response(raw_bytes, gz_bytes, 'text/html; charset=utf-8')
    return rendered


def _attach_store_benchmarks(item: dict, sname: str, w: int, m: int, benchmarks: dict) -> None:
    """Attach usual same-weekday/hour comparison and core-time / post-last-train retention fields."""
    bm = benchmarks.get(sname) if benchmarks else None
    is_korea = (item.get('region') == 'Korea') or ('カンナム' in sname) or ('ホンデ' in sname)
    # No fabricated fallbacks: unmeasured core-time / midnight retention stay None (never '20-22時' / 58 / 82 / 60)
    usual_w = bm.get('usual_women') if bm else None
    usual_m = bm.get('usual_men') if bm else None
    slot_label = (bm.get('usual_slot_label') if bm else None) or '普段同時間帯'
    has_core = bool(bm and bm.get('has_measured_core'))
    has_mid = bool(bm and bm.get('has_measured_midnight'))
    core_short = bm.get('core_time_short') if has_core else None
    core_wpct = bm.get('core_time_wpct') if has_core else None
    mid_pct = bm.get('midnight_retention_pct') if has_mid else None
    mid_label = bm.get('midnight_label_short') if (has_mid and mid_pct is not None) else None

    tot = w + m
    if tot > 0 and usual_w is not None:
        w_diff_usual = int(round(w - usual_w))
    else:
        w_diff_usual = None
    if tot > 0 and usual_m is not None:
        m_diff_usual = int(round(m - usual_m))
    else:
        m_diff_usual = None

    item['usual_women'] = usual_w
    item['usual_men'] = usual_m
    item['usual_w_diff'] = w_diff_usual
    item['usual_m_diff'] = m_diff_usual
    item['usual_slot_label'] = slot_label
    item['core_time_short'] = None if is_korea else core_short
    item['core_time_wpct'] = None if is_korea else core_wpct
    item['midnight_retention_pct'] = None if is_korea else mid_pct
    item['midnight_label_short'] = None if is_korea else mid_label
    item['has_measured_core'] = False if (is_korea or not bm) else bool(bm.get('has_measured_core'))
    item['has_measured_midnight'] = False if (is_korea or not bm) else bool(bm.get('has_measured_midnight'))


def _enrich_for_ssr(stores):
    """Attach display fields (brand_code, brand_cls, display_title, area_label, ladies_pct, men_pct, event badge)
    for instant server-side HTML card rendering."""
    import store_master
    import store_hours
    master = store_master.load()
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

    # Count duplicate display_names so same-city stores (e.g. 新宿) get brand prefix
    disp_counts = defaultdict(int)
    temp = []
    for s in stores:
        sname = (s.get('name') or '').strip()
        info = master.get(sname, {})
        if info:
            brand = _jinja_brand_ja(info.get('brand') or '')
            disp = info.get('display_name') or sname
        else:
            if re.match(r'^(?:OLG\s+ag|ORIENTAL\s+ag|ag)(?:\s+|(?=[^a-zA-Z0-9])|$)', sname, flags=re.IGNORECASE):
                brand = 'ag'
                disp = re.sub(r'^(?:OLG\s+ag|ORIENTAL\s+ag|ag)\s*', '', sname, flags=re.IGNORECASE).strip() or 'ag'
            elif re.match(r'^(?:OLG|ORIENTAL)(?:\s+|$)', sname, flags=re.IGNORECASE) or str(s.get('source') or '').upper() in ('OLG', 'ORIENTAL'):
                brand = 'オリエンタルラウンジ'
                disp = re.sub(r'^(?:OLG|ORIENTAL)(?:\s+|$)', '', sname, flags=re.IGNORECASE).strip() or 'オリエンタルラウンジ'
            elif re.match(r'^(?:ヤタコイ|YATAKOI)(?:\s+|$)', sname, flags=re.IGNORECASE) or str(s.get('source') or '').upper() == 'YATAKOI':
                brand = 'ヤタコイ'
                disp = re.sub(r'^(?:ヤタコイ|YATAKOI)(?:\s+|$)', '', sname, flags=re.IGNORECASE).strip() or 'ヤタコイ'
            else:
                raw_prefix = re.split(r'\s+', sname)[0] if sname else ''
                brand = _jinja_brand_ja(str(s.get('source') or raw_prefix))
                disp = re.sub(r'^(?:JIS|ASOBIBAR|XIX|ALFA|CLOVERS)(?:\s+|$)', '', sname, flags=re.IGNORECASE).strip() or sname
        area = info.get('area_label') or ''
        disp_counts[disp] += 1
        temp.append((s, brand, disp, area))

    enriched = []
    dom_rank = 0
    for s, brand, disp, area in temp:
        w = int(s.get('women') or 0)
        m = int(s.get('men') or 0)
        tot = w + m
        l_pct = round((w / tot) * 100) if tot > 0 else 0
        m_pct = (100 - l_pct) if tot > 0 else 0
        item = dict(s)
        sname = s.get('name', '')
        h_info = store_hours.get_business_hours_status(sname, total_guests=tot)
        is_korea = (s.get('region') == 'Korea') or ('カンナム' in sname) or ('ホンデ' in sname)
        if not is_korea:
            dom_rank += 1
            item['ssr_rank'] = dom_rank
        else:
            item['ssr_rank'] = None
        is_dup = disp_counts[disp] > 1
        item['ssr_brand'] = brand
        item['ssr_brand_cls'] = _BRAND_CLS_MAP.get(brand, 'brand-other')
        item['ssr_is_dup'] = is_dup
        item['ssr_disp'] = disp
        item['ssr_title'] = f"{brand} {disp}" if is_dup else disp
        item['ssr_area'] = area
        item['ssr_ladies_pct'] = l_pct
        item['ssr_men_pct'] = m_pct
        item['is_open_now'] = h_info['is_open_now']
        item['today_hours_label'] = h_info['today_hours_label']
        _attach_store_benchmarks(item, sname, w, m, benchmarks)
        ev_badge = ''
        ev_title = ''
        if ev_mod is not None and sname:
            try:
                matched_evs = ev_mod.get_events_for_store(
                    sname,
                    store_only=True,
                    allow_blocking=False,
                    preloaded_events=preloaded_evs,
                )
                if matched_evs:
                    has_disc = any(e.get('tag') == '割引・特典' for e in matched_evs)
                    ev_badge = '特典' if has_disc else 'フェア'
                    ev_title = matched_evs[0].get('title') or ''
            except Exception:
                pass
        item['ssr_event_badge'] = ev_badge
        item['ssr_event_title'] = ev_title
        enriched.append(item)
    try:
        import moat_intelligence
        moat_intelligence.enrich_stores_with_moat_signals(enriched, allow_weather_blocking=False)
    except Exception:
        pass
    import store_master
    for item in enriched:
        raw_n = item.get('name', '')
        s_seo = store_master.seo_name(raw_n)
        item['seo_name'] = s_seo
        item['name'] = s_seo
        if 'slug' not in item or not item['slug']:
            item['slug'] = store_master.slug_of(raw_n)
    return enriched



def get_initial_status_payload():
    """Return status dict for SSR embedding in index.html if available (memoized per live snapshot & 15m bucket)."""
    restore_latest_data_if_empty()
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    bucket_15m = f"{now_jst.strftime('%Y%m%d%H')}_{(now_jst.minute // 15) * 15:02d}"
    _, ev_upd = _get_cached_events_nonblocking()
    env_probe = _runtime_env_probe()
    import ops_governance
    with data_lock:
        full_d = latest_data.get('full_data')
        if not full_d:
            return None
        last_u = latest_data.get('last_updated')
        is_s = bool(latest_data.get('is_seed'))
        raw_copy = list(full_d)
    raw_copy = ops_governance.filter_visible_stores(raw_copy)
    c_key = (_live_data_signature(raw_copy, last_u, is_s), bucket_15m, ev_upd, env_probe)

    with _ssr_payload_cache_lock:
        if _ssr_payload_cache['key'] == c_key and _ssr_payload_cache['payload'] is not None:
            return _ssr_payload_cache['payload']

    payload = {
        'timestamp': last_u,
        'ranking': _enrich_for_ssr(raw_copy),
        'status': 'success',
        'is_seed': is_s,
    }
    with _ssr_payload_cache_lock:
        _ssr_payload_cache['key'] = c_key
        _ssr_payload_cache['payload'] = payload
    return payload


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
            paths = ('/api/status', '/', '/for-women', '/price-comparison') if do_html else ('/api/status', '/')
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
                clear_fast_http_caches(clear_editorial=False)
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
            clear_fast_http_caches(clear_editorial=False)
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
            clear_fast_http_caches(clear_editorial=False)
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

    def _render():
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        s_param = explicit_s
        if not s_param:
            bucket_min = (now_jst.minute // 15) * 15
            s_param = f"{now_jst.strftime('%Y%m%d_%H')}{bucket_min:02d}"
        og_meta = None
        og_image_url = f"https://aiseki-navi.com/ogp/live.png?s={s_param}"
        try:
            import ogp_generator
            og_meta = ogp_generator.get_ogp_meta_for_slot(s_param, now_jst=now_jst)
        except Exception:
            pass
        initial_status = get_initial_status_payload()
        return render_template(
            'index.html',
            is_staff=False,
            initial_status=initial_status,
            initial_status_client=_client_status_payload(initial_status),
            deferred_card_pool=_build_deferred_card_pool(initial_status, is_staff=False),
            og_meta=og_meta,
            og_image_url=og_image_url,
            published_scheduled=scheduled_articles.get_published_articles(),
            has_sns_inflow=has_sns_inflow,
            bachelor_shares_instant=bachelor_shares_instant,
            monetized_women_dating=monetized_women_dating,
            monetized_women_clinic=monetized_women_clinic,
        )
    cache_sig = (
        'index_pub',
        has_sns_inflow,
        bachelor_shares_instant,
        monetized_women_dating,
        monetized_women_clinic,
    )
    return _get_cached_html_or_render(cache_sig, _render)


from ssr_payload import (
    CLIENT_STATUS_DROP_KEYS as _CLIENT_STATUS_DROP_KEYS,
    build_deferred_card_pool as _build_deferred_card_pool,
    client_status_payload as _client_status_payload,
)


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

_BARE_INTERNAL_FROM_SLUGS = frozenset({
    'concierge', 'grooming', 'for-women', 'guide', 'stats', 'columns', 'areas', 'price-comparison',
})

@app.route('/link/<path:item_id>')
def affiliate_redirect(item_id):
    """Safe internal redirect shielding affiliate links from ad-blockers, with live runtime override resolution."""
    import affiliate_manager
    clean_id = item_id.strip().lower()
    target_url = affiliate_manager.resolve_affiliate_url(clean_id, allow_blocking=False) or AFFILIATE_REDIRECTS.get(clean_id)
    if not target_url:
        return redirect(url_for('index'))
    is_staff_probe = (
        (request.args.get('staff_test') or '').strip().lower() in ('1', 'true')
        or bool(session.get('is_staff') or session.get('is_master'))
    )
    if not is_staff_probe and (_is_bot_request() or _is_prefetch_request()):
        hub = '/for-women' if clean_id.startswith('women_') else '/grooming'
        resp_bot = redirect(hub, code=302)
        resp_bot.headers['X-Robots-Tag'] = 'noindex, nofollow'
        resp_bot.headers['Cache-Control'] = 'private, no-store'
        return resp_bot
    client_ip = get_client_ip()
    if (
        request.method == 'GET'
        and not is_staff_probe
        and turbo_store.check_sliding_rate_limit('aff_clk_ip', client_ip, limit=30, window_sec=600)
        and turbo_store.check_sliding_rate_limit(
            'aff_clk_human',
            f"{client_ip}|{(request.headers.get('User-Agent') or '')[:80]}",
            limit=10,
            window_sec=600,
        )
    ):
        try:
            import analytics
            parsed_ref = urllib_parse.urlparse(request.referrer or '')
            raw_from = (request.args.get('from') or '').strip()
            unquoted_from = urllib_parse.unquote(raw_from).strip()
            if unquoted_from in _BARE_INTERNAL_FROM_SLUGS:
                unquoted_from = '/' + unquoted_from
            ref_path = ''
            if unquoted_from.startswith('/') and not unquoted_from.startswith('//') and '\\' not in raw_from and '\\' not in unquoted_from:
                parsed_from = urllib_parse.urlparse(unquoted_from)
                clean_from = (parsed_from.path or '').strip()
                if (
                    not parsed_from.scheme
                    and not parsed_from.netloc
                    and '..' not in clean_from
                    and re.fullmatch(r'/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?)?', clean_from)
                ):
                    ref_path = clean_from[:80]
            if not ref_path:
                ref_host = (parsed_ref.hostname or '').lower()
                req_host = (request.host or '').split(':', 1)[0].lower()
                same_host = (
                    (not parsed_ref.netloc)
                    or (ref_host == req_host)
                    or (ref_host in ('aiseki-navi.com', 'www.aiseki-navi.com', 'aiseki-navi-tokyo.fly.dev'))
                )
                fallback_ref = re.sub(r'[\r\n\x00@\\]', '', (parsed_ref.path or '').strip())[:80] if same_host else ''
                ref_path = fallback_ref if (fallback_ref.startswith('/') and not fallback_ref.startswith('//')) else '-'
            analytics.track('affiliate_click', f"{clean_id} @ {ref_path}")
            explicit_s = (request.args.get('s') or '').strip()
            ref_s = explicit_s or (urllib_parse.parse_qs(parsed_ref.query).get('s') or [''])[0].strip()
            raw_inflow = ref_s or (request.cookies.get('an_src') or '').strip()
            if raw_inflow:
                inflow_tag = analytics.normalize_inflow_source(raw_inflow)
                if inflow_tag:
                    analytics.track('sns_inflow', f"cv:{inflow_tag}")
        except Exception:
            pass
    resp = redirect(target_url, code=302)
    resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return resp

# --- First-party analytics (no personal data) ---
import urllib.parse as urllib_parse

_BOT_MARKERS = (
    'bot', 'spider', 'crawl', 'slurp', 'facebookexternalhit', 'preview', 'headless', 'python-requests', 'curl',
    # AI assistants / fetchers & ad/SEO/headless scanners whose UA does not always contain "bot"
    'chatgpt-user', 'perplexity-user', 'claude-user', 'claude-web', 'anthropic-ai', 'mistralai-user',
    'meta-externalfetcher', 'meta-externalagent', 'cohere-ai', 'google-extended', 'applebot-extended',
    'mediapartners-google', 'adsbot', 'apis-google', 'feedfetcher-google', 'google-read-aloud',
    'google-inspectiontool', 'googleother', 'storebot-google',
    'lighthouse', 'chrome-lighthouse', 'phantomjs', 'playwright', 'puppeteer', 'selenium',
    'semrush', 'ahrefs', 'petal', 'censys', 'zgrab',
    'bytespider', 'gptbot', 'oai-searchbot', 'python-httpx', 'aiohttp', 'go-http-client', 'wget', 'scrapy',
    'axios', 'node-fetch', 'undici', 'okhttp', 'libwww-perl', 'java/',
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
    return s


_BADGE_EMOJI_PREFIX_RE = re.compile(r'^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2197\u2198\uFE0E\uFE0F\s]+')


@app.template_filter('clean_badge')
def _jinja_clean_badge(val):
    if val is None:
        return ''
    s = _BADGE_EMOJI_PREFIX_RE.sub('', str(val))
    s = re.sub(r'^推定:\s*', '', s)
    return s.strip()


@app.template_filter('merge_brand_stats')
def _jinja_merge_brand_stats(by_brand):
    if not isinstance(by_brand, dict):
        return {}
    merged = {}
    for raw_brand, bdata in by_brand.items():
        if not isinstance(bdata, dict):
            continue
        disp = _jinja_brand_ja(raw_brand)
        sc = int(bdata.get('store_count', 0) or 0)
        aw = float(bdata.get('total_avg_women', 0) or 0)
        am = float(bdata.get('total_avg_men', 0) or 0)
        if disp not in merged:
            merged[disp] = {
                'store_count': sc,
                'total_avg_women': aw,
                'total_avg_men': am,
                '_w_int': isinstance(bdata.get('total_avg_women'), int),
                '_m_int': isinstance(bdata.get('total_avg_men'), int),
            }
        else:
            merged[disp]['store_count'] += sc
            merged[disp]['total_avg_women'] += aw
            merged[disp]['total_avg_men'] += am
            merged[disp]['_w_int'] = merged[disp]['_w_int'] and isinstance(bdata.get('total_avg_women'), int)
            merged[disp]['_m_int'] = merged[disp]['_m_int'] and isinstance(bdata.get('total_avg_men'), int)
    out = {}
    for disp, m in merged.items():
        out[disp] = {
            'store_count': m['store_count'],
            'total_avg_women': int(round(m['total_avg_women'])) if m['_w_int'] else round(m['total_avg_women'], 1),
            'total_avg_men': int(round(m['total_avg_men'])) if m['_m_int'] else round(m['total_avg_men'], 1),
        }
    return out

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
            if (ref_qs.get('staff_test') or [''])[0].strip().lower() in ('1', 'true'):
                return True
        except Exception:
            pass
    return False


@app.after_request
def track_page_views(response):
    try:
        if (request.method == 'GET' and response.status_code == 200
                and (request.path == '/' or request.path.startswith(_TRACKED_PAGE_PREFIXES))
                and not request.path.endswith('.csv')
                and not session.get('is_staff')
                and not session.get('is_master')
                and not _is_staff_test_request()
                and not _is_bot_request()
                and not _is_prefetch_request()):
            import analytics
            analytics.track('page_view', request.path)
            raw_s = (request.args.get('s') or '').strip()
            if raw_s:
                inflow_tag = analytics.normalize_inflow_source(raw_s)
                if inflow_tag:
                    analytics.track('sns_inflow', f"{inflow_tag} @ {request.path}")
                    response.set_cookie(
                        'an_src',
                        inflow_tag,
                        max_age=86400,
                        path='/',
                        samesite='Lax',
                        secure=bool(request.is_secure),
                        httponly=True,
                    )
    except Exception:
        pass
    return response


_BEACON_ALLOWED_EVENTS = frozenset({
    'affiliate_impression', 'cushion_click', 'store_open', 'map_cafe', 'map_bar',
    'official_click', 'sns_click', 'share', 'vote',
    'hashigo_intent',
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
    payload = request.get_json(silent=True, force=True) or {}
    ev_name = re.sub(r'[^A-Za-z0-9_.:-]', '', str(payload.get('event', '')).strip())[:30]
    if ev_name not in _BEACON_ALLOWED_EVENTS:
        return ('', 204)
    import analytics
    analytics.track(ev_name, str(payload.get('label', '')).strip()[:96])
    return ('', 204)


@app.route('/staff', methods=['GET', 'POST'])
def staff_monitor():
    """Internal staff dashboard (staff passcode = field data only, master passcode = full management)."""
    error = None
    client_ip = get_client_ip()

    if request.method == 'POST':
        if _is_login_rate_limited(client_ip):
            return render_template('staff_login.html', error='試行回数が上限に達しました。5分ほど待ってから再度お試しください'), 429
        import ops_governance
        eff_staff_passcode = ops_governance.get_effective_staff_passcode(STAFF_PASSCODE)
        entered = request.form.get('passcode', '').strip()
        if entered and hmac.compare_digest(entered, MASTER_PASSCODE):
            _clear_login_failures(client_ip)
            session['is_staff'] = True
            session['is_master'] = True
            session['auth_gen'] = _compute_session_auth_gen(True)
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        elif entered and hmac.compare_digest(entered, eff_staff_passcode):
            _clear_login_failures(client_ip)
            session['is_staff'] = True
            session['is_master'] = False
            session['auth_gen'] = _compute_session_auth_gen(False)
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        else:
            _record_login_failure(client_ip)
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
            return render_template(
                'index.html',
                is_staff=True,
                is_master=is_master,
                initial_status=staff_init_status,
                initial_status_client=_client_status_payload(staff_init_status),
                deferred_card_pool=_build_deferred_card_pool(staff_init_status, is_staff=True),
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
        if sync_latest_data_from_shared_bus():
            return
        if turbo_store.is_shared_bus_active():
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
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/api/status')
def get_status():
    import store_hours
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    now_bucket = f"{now_jst.strftime('%Y%m%d%H')}_{(now_jst.minute // 10) * 10:02d}"
    try:
        _hp = store_hours.get_business_hours_status('OLG 新宿', total_guests=0)
        _sh_sig = (id(store_hours.get_business_hours_status), _hp.get('is_open_now'), _hp.get('today_hours_label'))
    except Exception:
        _sh_sig = None
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
    c_key = (_live_data_signature(raw_copy, last_u, is_s), now_bucket, _sh_sig)

    inm = (request.headers.get('If-None-Match') or '').strip()
    with _status_json_cache_lock:
        if _status_json_cache['key'] == c_key and _status_json_cache['raw'] is not None:
            etag_cached = _status_json_cache.get('etag') or ''
            if inm and etag_cached and inm == etag_cached:
                resp_304 = Response(status=304)
                resp_304.headers['ETag'] = etag_cached
                return resp_304
            resp_hit = _build_precompressed_response(_status_json_cache['raw'], _status_json_cache['gzip'], 'application/json')
            if etag_cached:
                resp_hit.headers['ETag'] = etag_cached
            return resp_hit

    try:
        benchmarks = local_db.get_all_stores_live_benchmarks(allow_blocking=False)
    except Exception:
        benchmarks = {}

    enriched_ranking = []
    for s in raw_copy:
        item = dict(s)
        sname = item.get('name', '')
        w = int(item.get('women') or 0)
        m = int(item.get('men') or 0)
        if 'is_open_now' not in item or item.get('is_open_now') is None:
            h_info = store_hours.get_business_hours_status(sname, total_guests=w + m)
            item['is_open_now'] = h_info['is_open_now']
            item['today_hours_label'] = h_info['today_hours_label']
        elif 'today_hours_label' not in item:
            h_info = store_hours.get_business_hours_status(sname, total_guests=w + m)
            item['today_hours_label'] = h_info['today_hours_label']
        _attach_store_benchmarks(item, sname, w, m, benchmarks)
        enriched_ranking.append(item)
    try:
        import moat_intelligence
        moat_intelligence.enrich_stores_with_moat_signals(enriched_ranking, allow_weather_blocking=False)
    except Exception:
        pass

    import store_master
    for item in enriched_ranking:
        raw_n = item.get('name', '')
        s_seo = store_master.seo_name(raw_n)
        item['seo_name'] = s_seo
        item['name'] = s_seo
        if 'slug' not in item or not item['slug']:
            item['slug'] = store_master.slug_of(raw_n)


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
        return resp_304
    resp_fresh = _build_precompressed_response(raw_bytes, gz_bytes, 'application/json')
    resp_fresh.headers['ETag'] = etag_val
    return resp_fresh

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
    if _is_fly_multiworker_web:
        return Response(
            "Production web workers stream logs directly to stdout (use `flyctl logs -a aiseki-navi-tokyo`).\n",
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

@app.route('/api/weekly-report')
def get_weekly_report():
    try:
        import weekly_report
        import store_master

        def _clean_report(rep):
            if not isinstance(rep, dict):
                return rep
            rep_clean = dict(rep)
            for k in ('stores', 'anaba_stores'):
                new_list = []
                for s in rep_clean.get(k, []):
                    item = dict(s)
                    if item.get('brand') == 'OLG':
                        item['brand'] = 'オリエンタルラウンジ'
                    if 'name' in item:
                        item['name'] = store_master.seo_name(item['name'])
                    new_list.append(item)
                rep_clean[k] = new_list
            if 'by_brand' in rep_clean and isinstance(rep_clean['by_brand'], dict):
                cleaned_bb = {}
                for b_name, b_data in rep_clean['by_brand'].items():
                    safe_b = 'オリエンタルラウンジ' if b_name == 'OLG' else b_name
                    cleaned_bb[safe_b] = b_data
                rep_clean['by_brand'] = cleaned_bb
            return rep_clean

        def _build_weekly():
            report = weekly_report.generate_weekly_report()
            if not isinstance(report, dict) or "error" in report:
                return {"status": "error", "error": (report or {}).get("error") if isinstance(report, dict) else "invalid_report"}, 500
            return {"status": "success", "data": _clean_report(report)}, 200

        # Cache key is decided BEFORE generation: a hit returns pre-gzipped bytes with zero report generation / cleanup copies
        lf = weekly_report._report_cache.get('last_fetched')
        if lf is None:  # report not generated yet (or just invalidated): never cache under an anonymous key
            payload, code = _build_weekly()
            return jsonify(payload), code
        gen_sig = id(getattr(weekly_report, 'generate_weekly_report', None))
        return _cached_json_response(('weekly_report', local_db.DB_PATH, lf, gen_sig), _build_weekly, ttl_sec=60.0)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/api/events')
def get_events():
    """Returns active events and promotions for lounge stores."""
    try:
        import events
        import store_master

        def _clean_evs(ev_list):
            out = []
            for ev in (ev_list or []):
                item = dict(ev)
                if item.get('brand') == 'OLG':
                    item['brand'] = 'オリエンタルラウンジ'
                if 'target_stores' in item and isinstance(item['target_stores'], list):
                    item['target_stores'] = [store_master.seo_name(s) for s in item['target_stores']]
                out.append(item)
            return out

        store = request.args.get('store', '').strip()
        internal_store = store_master.to_internal_store_name(store) if store else ''
        force = (
            (request.args.get('refresh', '').lower() == 'true')
            and is_master_authorized()
            and (request.headers.get('Sec-Fetch-Site') or '').lower() != 'cross-site'
        )
        if not force:
            ev_upd = events._events_memory_cache.get('updated_at')
            ev_cnt = len(events._events_memory_cache.get('events') or [])
            if ev_cnt > 0:
                return _cached_json_response(
                    ('events', internal_store, ev_upd, ev_cnt),
                    lambda: (
                        (lambda evs: {'status': 'success', 'count': len(evs), 'events': _clean_evs(evs)})(
                            events.get_events_for_store(internal_store, allow_blocking=False) if internal_store else events.fetch_all_events(force_refresh=False, allow_blocking=False)
                        ),
                        200,
                    ),
                    ttl_sec=60.0,
                )
        if internal_store:
            evs = events.get_events_for_store(internal_store, allow_blocking=force)
        else:
            evs = events.fetch_all_events(force_refresh=force, allow_blocking=force)
        cleaned = _clean_evs(evs)
        return jsonify({
            'status': 'success',
            'count': len(cleaned),
            'events': cleaned
        })
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/api/votes/<path:store_name>')
def get_store_votes(store_name):
    """Returns user crowd votes in the last 4 hours for a specific store."""
    import store_master
    import ops_governance
    if not store_name or len(store_name) > 80 or any(ord(ch) < 32 for ch in store_name):
        return jsonify({'status': 'error', 'error': 'Invalid store name'}), 400
    internal_name = store_master.to_internal_store_name(store_name)
    if ops_governance.is_store_hidden(internal_name or store_name):
        return jsonify({'status': 'error', 'error': 'Store not found'}), 404
    counts = local_db.get_recent_votes(store_name=internal_name, hours=4)
    disp_name = store_master.seo_name(internal_name) if internal_name else store_name
    return jsonify({'status': 'success', 'store_name': disp_name, 'votes': counts})

@app.route('/api/vote', methods=['POST'])
def post_store_vote():
    """Submits a one-tap live crowd vote from a user."""
    import hashlib
    import store_master
    payload = request.get_json(silent=True) or {}
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
    # Only accept votes for stores that actually exist in the current ranking (prevents junk rows)
    with data_lock:
        known_names = {s.get('name') for s in latest_data.get('full_data', [])}
    if known_names and store_name not in known_names:
        return jsonify({'status': 'error', 'error': 'Unknown store'}), 400
    if _is_staff_test_request(include_referrer=True):
        counts = local_db.get_recent_votes(store_name=store_name, hours=4)
        return jsonify({
            'status': 'success',
            'reason': 'staff_test_noop',
            'store_name': store_master.seo_name(store_name),
            'votes': counts,
        })

    client_ip = get_client_ip()
    sec_bytes = str(app.secret_key).encode('utf-8')
    ip_hash = hmac.new(sec_bytes, f"vote_ip:{client_ip}".encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    raw_voter = str(payload.get('voter_id', '')).strip()[:64]
    voter_hash = hmac.new(sec_bytes, f"vote_id:{raw_voter}".encode('utf-8'), hashlib.sha256).hexdigest()[:16] if raw_voter else ''

    raw_wait = payload.get('wait_minutes')
    wait_minutes = None
    if raw_wait is not None and raw_wait != '':
        try:
            w_int = int(raw_wait)
            if w_int in (0, 15, 30, 45):
                wait_minutes = w_int
        except (TypeError, ValueError):
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
        clear_fast_http_caches(clear_editorial=False)

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
    payload = request.get_json(silent=True) or {}
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
    if _is_staff_test_request(include_referrer=True):
        watch_info = local_db.get_watch_intent_counts(store_name=store_name, hours=6)
        return jsonify({
            'status': 'success',
            'reason': 'staff_test_noop',
            'store_name': store_master.seo_name(store_name),
            'target_cond': target_cond,
            'watching_count': int(watch_info.get('watching_count', 0)),
        })

    client_ip = get_client_ip()
    sec_bytes = str(app.secret_key).encode('utf-8')
    raw_voter = str(payload.get('voter_id', '')).strip()[:64]
    ip_hash = hmac.new(sec_bytes, f"watch_ip:{client_ip}".encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    voter_hash = (
        hmac.new(sec_bytes, f"watch_id:{client_ip}:{raw_voter}".encode('utf-8'), hashlib.sha256).hexdigest()[:16]
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
    run_scheduled_x_post,
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


