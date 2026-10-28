from flask import Flask, render_template, jsonify, request, send_from_directory, Response, redirect, session, url_for, abort, make_response
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import monitor
import atexit
import datetime
import threading
import logger
import statistics
import os
import sys
import json
import time
import local_db
import scheduled_articles
import zlib
from collections import defaultdict, OrderedDict

from logging.handlers import RotatingFileHandler

# Set up logging with automatic size rotation (max 5MB, keep 2 backups) to prevent disk overflow
# (absolute path so the app also works when started from another working directory, e.g. tests)
log_file_path = os.environ.get('APP_LOG_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.log')
file_handler = RotatingFileHandler(log_file_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))

root_logger = logging.getLogger('')
root_logger.setLevel(logging.INFO)
# Clear any default handlers to avoid duplicates
root_logger.handlers.clear()
root_logger.addHandler(file_handler)
root_logger.addHandler(console)

import gzip
import hmac
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY', 'aiseki-navi-staff-monitor-secret-key-2026')
app.permanent_session_lifetime = datetime.timedelta(days=90)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
STAFF_PASSCODE = os.environ.get('STAFF_PASSCODE', '5555')
MASTER_PASSCODE = os.environ.get('MASTER_PASSCODE', '2918')
ADMIN_API_SECRET = os.environ.get('ADMIN_API_SECRET', 'lounge2026')

_login_failures_lock = threading.Lock()
_login_failures = defaultdict(list)  # ip -> [epoch_ts, ...]
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SEC = 300


def _is_login_rate_limited(ip_addr: str) -> bool:
    now_t = time.time()
    with _login_failures_lock:
        recent = [t for t in _login_failures.get(ip_addr, []) if now_t - t < LOGIN_WINDOW_SEC]
        if recent:
            _login_failures[ip_addr] = recent
        else:
            _login_failures.pop(ip_addr, None)
        return len(recent) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip_addr: str) -> None:
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
    with _login_failures_lock:
        _login_failures.pop(ip_addr, None)


@app.context_processor
def inject_auth_flags():
    """Automatically inject staff/master session flags into every template (hides AdSense on all pages for staff/master)."""
    return {
        'is_staff': bool(session.get('is_staff')),
        'is_master': bool(session.get('is_master')),
    }


def is_admin_authorized():
    """Verify administrative permission either via authenticated staff/master session or secret token."""
    if session.get('is_staff') or session.get('is_master'):
        return True
    secret = request.args.get('secret', '').strip()
    if secret and hmac.compare_digest(secret, ADMIN_API_SECRET):
        return True
    return False


def is_master_authorized():
    """Verify master/owner permission via master session or secret token."""
    if session.get('is_master'):
        return True
    secret = request.args.get('secret', '').strip()
    if secret and hmac.compare_digest(secret, ADMIN_API_SECRET):
        return True
    return False


@app.after_request
def compress_response(response):
    """Set cache headers and automatically gzip-compress JSON/HTML/CSS/JS/XML/SVG responses."""
    is_staff_sess = bool(session.get('is_staff') or session.get('is_master'))
    # 1. Cache headers (must run regardless of gzip compression)
    if request.path.startswith('/static/') and response.status_code == 200:
        if request.args.get('v'):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            response.headers['Cache-Control'] = 'public, max-age=86400'
    else:
        if is_staff_sess:
            response.headers['X-Staff-View'] = '1'
            response.headers['Cache-Control'] = 'private, no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
        elif response.content_type and 'text/html' in response.content_type and response.status_code == 200:
            if 'Cache-Control' not in response.headers:
                if request.path.startswith('/staff'):
                    response.headers['Cache-Control'] = 'private, no-store, no-cache, must-revalidate, max-age=0'
                    response.headers['Pragma'] = 'no-cache'
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
            compressed = gzip.compress(data, compresslevel=5)
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

def _ensure_scheduler_started_async():
    global _scheduler_started
    if os.environ.get('DISABLE_SCHEDULER') == '1' or _scheduler_started:
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def _boot():
        try:
            local_db.init_db()
            restore_latest_data_if_empty()

            scheduler = BackgroundScheduler(timezone="UTC", job_defaults={
                'misfire_grace_time': 600,
                'coalesce': True,
                'max_instances': 1,
            })
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
            # Human-paced anti-spam schedule with randomized jitter (90s-540s) so posts never fire at :00:00 sharp:
            # - X Live: 20:00 & 22:00 daily (00:00 late-night removed on Mon-Thu/Sun, kept only Fri/Sat nights; 02:00 removed completely)
            # - X Tips: 18:00 pre-outing prime slot only (12:00 midday removed to cut daily post volume by half)
            scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=11, minute=2, jitter=420, args=["20:00"])
            scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=13, minute=3, jitter=420, args=["22:00"])
            scheduler.add_job(func=run_scheduled_x_post, trigger="cron", day_of_week="fri,sat", hour=15, minute=4, jitter=420, args=["00:00"])
            def _tip_18():
                import tips_poster
                tips_poster.run_scheduled_tip_post(slot_label="18:00")
            scheduler.add_job(func=_tip_18, trigger="cron", hour=9, minute=3, jitter=480)
            def _note_check_job():
                try:
                    import note_poster
                    note_poster.check_and_post_new_note()
                except Exception as err:
                    logging.error(f"Scheduled note promotion check failed: {err}")
            # Check note RSS during calm midday (12:15 JST) and pre-evening (17:15 JST) windows to avoid 20:00-22:00 X rush
            scheduler.add_job(func=_note_check_job, trigger="cron", hour="3,8", minute=15, jitter=300)
            def _threads_daily_job():
                try:
                    import threads_poster
                    with data_lock:
                        cur_stores = list(latest_data.get('full_data') or [])
                        is_seed = bool(latest_data.get('is_seed'))
                    if cur_stores and not is_seed:
                        threads_poster.execute_scheduled_threads_post(cur_stores, slot_label="22:00")
                except Exception as err:
                    logging.error(f"Scheduled Threads post failed: {err}")
            scheduler.add_job(func=_threads_daily_job, trigger="cron", hour=13, minute=8, jitter=300)
            def _ig_carousel_job(slot_lbl="21:45"):
                try:
                    import instagram_poster
                    with data_lock:
                        cur_stores = list(latest_data.get('full_data') or [])
                        is_seed = bool(latest_data.get('is_seed'))
                    if not is_seed:
                        instagram_poster.execute_scheduled_instagram_post(cur_stores, mode="carousel", slot_label=slot_lbl)
                except Exception as err:
                    logging.error(f"Scheduled Instagram carousel post failed: {err}")
            # Friday pre-outing forecast (18:45 JST = 09:45 UTC) + Tue/Sun evening report (21:45 JST = 12:45 UTC)
            scheduler.add_job(func=_ig_carousel_job, trigger="cron", day_of_week="fri", hour=9, minute=45, jitter=300, args=["18:45"])
            scheduler.add_job(func=_ig_carousel_job, trigger="cron", day_of_week="tue,sun", hour=12, minute=45, jitter=300, args=["21:45"])
            # Plan E: Women-perspective 'Tonight's Zero-Wait & Ladies Perks' alerts on Threads (Wed/Fri/Sat 19:30 JST) & Instagram (Thu/Sat 19:45 JST)
            def _threads_ladies_job():
                try:
                    import threads_poster
                    with data_lock:
                        cur_stores = list(latest_data.get('full_data') or [])
                        is_seed = bool(latest_data.get('is_seed'))
                    if cur_stores and not is_seed:
                        threads_poster.execute_scheduled_ladies_threads_post(cur_stores, slot_label="19:30")
                except Exception as err:
                    logging.error(f"Scheduled Threads ladies post failed: {err}")
            scheduler.add_job(func=_threads_ladies_job, trigger="cron", day_of_week="wed,fri,sat", hour=10, minute=30, jitter=240)
            def _ig_ladies_job():
                try:
                    import instagram_poster
                    with data_lock:
                        cur_stores = list(latest_data.get('full_data') or [])
                        is_seed = bool(latest_data.get('is_seed'))
                    if cur_stores and not is_seed:
                        instagram_poster.execute_scheduled_ladies_instagram_post(cur_stores, slot_label="19:45")
                except Exception as err:
                    logging.error(f"Scheduled Instagram ladies post failed: {err}")
            scheduler.add_job(func=_ig_ladies_job, trigger="cron", day_of_week="thu,sat", hour=10, minute=45, jitter=240)
            def _crawl_events_job():
                try:
                    import events
                    import data_audit
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
            scheduler.add_job(func=_scheduled_articles_release_job, trigger="interval", minutes=15)
            def _bonus_alert_check_job():
                try:
                    import bonus_alert
                    with data_lock:
                        cur_stores = list(latest_data.get('full_data') or [])
                        is_seed = bool(latest_data.get('is_seed'))
                    if cur_stores and not is_seed:
                        bonus_alert.check_and_post_bonus_alert(cur_stores)
                except Exception as err:
                    logging.error(f"Bonus alert check failed: {err}")
            scheduler.add_job(func=_bonus_alert_check_job, trigger="interval", minutes=10)
            scheduler.add_job(func=run_scheduled_weekly_x_post, trigger="cron", day_of_week="sun", hour=10, minute=0, jitter=300)
            def _pdca_autonomous_job():
                try:
                    import pdca_engine
                    with data_lock:
                        cur_stores = list(latest_data.get('full_data') or [])
                    pdca_engine.run_pdca_cycle(
                        live_stores=cur_stores,
                        days=14,
                        apply_actions=True,
                        use_ai=True,
                        record_log=True,
                    )
                except Exception as err:
                    logging.error(f"Autonomous PDCA cycle failed: {err}")
            # Run daily before evening SNS rush (17:35 JST = 08:35 UTC) and after late-night close (03:35 JST = 18:35 UTC)
            scheduler.add_job(func=_pdca_autonomous_job, trigger="cron", hour="8,18", minute=35, jitter=120)
            def _sheets_retention_job():
                try:
                    import archive_data
                    archive_data.run_scheduled_retention_job()
                except Exception as err:
                    logging.error(f"Scheduled Google Sheets retention job failed: {err}")
            # Run daily during quiet early-morning off-hours (06:20 JST = 21:20 UTC) to prevent 10M-cell ceiling
            scheduler.add_job(func=_sheets_retention_job, trigger="cron", hour=21, minute=20, jitter=120)
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

    import store_master
    master_map = store_master.load()
    if clean_name not in master_map:
        with data_lock:
            live_names = {s.get('name') for s in (latest_data.get('full_data') or [])}
        cached_proc = (analysis_cache.get('processed_per_store') or {}).get(clean_name)
        if clean_name not in live_names and cached_proc is None:
            return jsonify({'error': 'Store not found'}), 404

    import store_analysis
    raw_entry = (analysis_cache.get('raw_data_by_store') or {}).get(clean_name)
    proc_entry = (analysis_cache.get('processed_per_store') or {}).get(clean_name)
    raw_ts = raw_entry.get('last_fetched') if isinstance(raw_entry, dict) else None
    if proc_entry is not None and raw_ts is not None:
        return _cached_json_response(
            ('analysis', local_db.DB_PATH, clean_name, id(proc_entry), raw_ts),
            lambda: store_analysis.compute_store_analysis_payload(
                clean_name,
                analysis_cache=analysis_cache,
                cache_duration=ANALYSIS_CACHE_DURATION,
            ),
            ttl_sec=60.0,
        )
    payload, status_code = store_analysis.compute_store_analysis_payload(
        clean_name,
        analysis_cache=analysis_cache,
        cache_duration=ANALYSIS_CACHE_DURATION,
    )
    if status_code == 200:
        new_proc = (analysis_cache.get('processed_per_store') or {}).get(clean_name)
        new_raw = (analysis_cache.get('raw_data_by_store') or {}).get(clean_name)
        new_ts = new_raw.get('last_fetched') if isinstance(new_raw, dict) else None
        if new_proc is not None:
            return _cached_json_response(
                ('analysis', local_db.DB_PATH, clean_name, id(new_proc), new_ts),
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
LATEST_STATUS_SNAPSHOT_FILE = os.environ.get('LATEST_STATUS_SNAPSHOT_FILE') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'latest_status_cache.json')


def save_latest_status_snapshot():
    """Persist latest_data to disk so server restarts/redeploys have 0ms cold-start latency."""
    try:
        with data_lock:
            if not latest_data.get('full_data') or latest_data.get('is_seed'):
                return
            payload = {
                'last_updated': latest_data.get('last_updated'),
                'full_data': latest_data.get('full_data', [])
            }
        os.makedirs(os.path.dirname(LATEST_STATUS_SNAPSHOT_FILE), exist_ok=True)
        tmp_path = LATEST_STATUS_SNAPSHOT_FILE + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            import json
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, LATEST_STATUS_SNAPSHOT_FILE)
    except Exception:
        pass


def restore_latest_data_if_empty():
    """Immediately populate latest_data from disk snapshot, SQLite, or store_master on cold start
    so users NEVER see a blocking loading screen even right after a Render redeploy."""
    with data_lock:
        if latest_data.get('full_data'):
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

    # Fallback 2: Restore from latest SQLite logs if available (up to 7 days)
    try:
        snap = local_db.get_snapshot_around(0, 10080)
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
    'ag': 'brand-ag',
    'JIS': 'brand-jis',
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
_sitemap_xml_cache = {'key': None, 'xml': None}
_CACHE_QUERY_ALLOWLIST = ('s', 'area', 'slot', 'channel', 'switch', 'days', 'format')


def _normalize_cache_query_args() -> tuple:
    if not request.args:
        return ()
    return tuple(
        (k, (request.args.get(k) or '')[:64])
        for k in _CACHE_QUERY_ALLOWLIST
        if k in request.args
    )


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
    gz_bytes = gzip.compress(raw_bytes, compresslevel=6) if (status_code == 200 and len(raw_bytes) > 500) else None
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
    return (sh_sig, ev_sig, db_sig, wr_sig)


def _is_static_editorial_route(path: str) -> bool:
    return path == '/guide' or path.startswith('/guide/') or path in ('/grooming', '/privacy', '/columns')


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
        gz_bytes = gzip.compress(raw_bytes, compresslevel=6) if len(raw_bytes) > 500 else None
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
    if bm:
        usual_w = bm.get('usual_women')
        usual_m = bm.get('usual_men')
        slot_label = bm.get('usual_slot_label') or '普段同時間帯'
        core_short = bm.get('core_time_short') or '20-22時'
        core_wpct = bm.get('core_time_wpct') or 58
        mid_pct = bm.get('midnight_retention_pct')
        mid_label = bm.get('midnight_label_short')
    else:
        usual_w = None
        usual_m = None
        slot_label = '普段同時間帯'
        core_short = '20-22時'
        core_wpct = 58
        club_kw = ("梅田", "茶屋町", "心斎橋", "難波", "渋谷", "新宿", "栄", "錦", "福岡", "札幌", "熊本", "UMEDA", "CHAYAMACHI", "NAMBA", "SHIBUYA", "SHINJUKU", "FUKUOKA", "SAPPORO")
        is_club = any(k in (sname or '').upper() for k in club_kw)
        mid_pct = 82 if is_club else 60
        mid_label = '深夜安定' if is_club else '終電前型'

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
        sname = s.get('name', '')
        info = master.get(sname, {})
        brand = info.get('brand') or ('ag' if 'ag' in sname else sname.split(' ')[0])
        if brand == 'YATAKOI':
            brand = 'ヤタコイ'
        disp = info.get('display_name') or sname
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
        item['ssr_brand'] = brand
        item['ssr_brand_cls'] = _BRAND_CLS_MAP.get(brand, 'brand-other')
        item['ssr_title'] = f"{brand} {disp}" if disp_counts[disp] > 1 else disp
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
    return enriched


def get_initial_status_payload():
    """Return status dict for SSR embedding in index.html if available (memoized per live snapshot & 15m bucket)."""
    restore_latest_data_if_empty()
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    bucket_15m = f"{now_jst.strftime('%Y%m%d%H')}_{(now_jst.minute // 15) * 15:02d}"
    _, ev_upd = _get_cached_events_nonblocking()
    env_probe = _runtime_env_probe()
    with data_lock:
        full_d = latest_data.get('full_data')
        if not full_d:
            return None
        last_u = latest_data.get('last_updated')
        is_s = bool(latest_data.get('is_seed'))
        raw_copy = list(full_d)
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
_last_data_audit_mono = 0.0


def prewarm_ssr_and_status_caches():
    """Pre-populate SSR payload, /api/status, and primary HTML route caches on background update thread so user GET never misses."""
    global _prewarm_in_progress
    if _prewarm_in_progress:
        return
    _prewarm_in_progress = True
    try:
        get_initial_status_payload()
        if os.environ.get('DISABLE_SCHEDULER') == '1' or app.testing:
            return
        headers = {
            'User-Agent': 'aiseki-internal-prewarm-bot/1.0',
            'Accept-Encoding': 'gzip',
        }
        with app.test_client() as client:
            for path in ('/api/status', '/', '/for-women', '/price-comparison', '/areas'):
                try:
                    client.get(path, headers=headers)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        _prewarm_in_progress = False

# Region Definitions
REGIONS = {
    'Hokkaido': ['Sapporo', '札幌', 'SAPPORO'],
    'Tohoku': ['Sendai', '仙台', 'ag仙台'],
    'Kanto': ['Shibuya', 'Ebisu', 'Shinjuku', 'Ueno', 'Kashiwa', 'Machida', 'Yokohama', 'Omiya', 'Utsunomiya', 'Takasaki', '渋谷', '恵比寿', '新宿', '上野', '柏', '町田', '横浜', '大宮', '宇都宮', '高崎', 'OMIYA', 'SHINJUKU', 'NISHISHINJUKU'],
    'Chubu': ['Nagoya', 'Shizuoka', 'Hamamatsu', 'Kanazawa', '名古屋', '静岡', '浜松', '金沢', '錦', '栄'],
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
            snap = local_db.get_snapshot_around(0, 10080)
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

def update_job(force=False):
    global latest_data, _last_update_error, rolling_snapshots, _last_fetch_at, _last_data_audit_mono
    with data_lock:
        has_live_data = bool(latest_data.get('full_data')) and not latest_data.get('is_seed')
    if not force and has_live_data and _last_fetch_at is not None:
        elapsed = (datetime.datetime.now() - _last_fetch_at).total_seconds()
        if elapsed < fetch_interval_sec() - 5:
            return
    _last_fetch_at = datetime.datetime.now()
    print(f"[{datetime.datetime.now()}] Updating data...")
    try:
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

            # Fetch recent crowd votes (last 4 hours)
            try:
                votes_map = local_db.get_recent_votes(hours=4)
            except Exception:
                votes_map = {}

            import store_master
            valid_master = store_master.load()
            now_epoch = time.time()

            with data_lock:
                current_data = latest_data.get('full_data', [])

                # Guarantee all 53 canonical stores in store_master.json remain present even if one scraper
                # transiently times out on a cold-start deploy (prevents Oriental Lounge / JIS vanishing for 10m!)
                merged_map = {}
                if valid_master:
                    recent_db_snap = {}
                    try:
                        recent_db_snap = local_db.get_snapshot_around(0, 30) or {}
                    except Exception:
                        recent_db_snap = {}
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
                
                merged_data = list(merged_map.values())
                sorted_data = sorted(merged_data, key=lambda x: (x.get('region') != 'Korea', x['women'], x['men']), reverse=True)
                top_store = sorted_data[0] if sorted_data else None
                
                latest_data['top_store'] = top_store
                latest_data['full_data'] = sorted_data
                latest_data['last_updated'] = jst_now.strftime("%Y-%m-%d %H:%M:%S")
                latest_data['is_seed'] = False
            save_latest_status_snapshot()
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
            with _ogp_bytes_cache_lock:
                _ogp_bytes_cache.clear()
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
            
            # --- Logging Optimization ---
            total_guests = sum(d.get('men', 0) + d.get('women', 0) for d in data)
            is_off_hours = 7 <= jst_now.hour < 17
            
            last_logged_str = latest_data.get('last_logged')
            is_logging_time = True
            if last_logged_str:
                last_logged = datetime.datetime.strptime(last_logged_str, "%Y-%m-%d %H:%M:%S")
                if (jst_now - last_logged).total_seconds() < 600:
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



@app.route('/')
def index():
    trigger_background_fetch_if_needed()
    def _render():
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        s_param = (request.args.get('s') or '').strip()
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
        return render_template(
            'index.html',
            is_staff=False,
            initial_status=get_initial_status_payload(),
            og_meta=og_meta,
            og_image_url=og_image_url,
            published_scheduled=scheduled_articles.get_published_articles(),
        )
    return _get_cached_html_or_render('index_pub', _render)

@app.route('/ogp/live.png')
def dynamic_live_ogp_image():
    """Dynamically render a 1200x630 live TOP3 ranking OGP card image (memoized in RAM)."""
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    s_param = (request.args.get('s') or '').strip()[:40]
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    bucket_10m = f"{now_jst.strftime('%Y%m%d%H')}_{(now_jst.minute // 10) * 10:02d}"
    with data_lock:
        stores = list(latest_data.get('full_data') or [])
        last_u = latest_data.get('last_updated')
        is_s = bool(latest_data.get('is_seed'))
    ogp_key = ('live', s_param, bucket_10m, _live_data_signature(stores, last_u, is_s))
    now_mono = time.monotonic()
    with _ogp_bytes_cache_lock:
        hit = _ogp_bytes_cache.get(ogp_key)
        if hit is not None and (now_mono - hit[0] < 120.0):
            _ogp_bytes_cache.move_to_end(ogp_key)
            resp = Response(hit[1], mimetype='image/png')
            resp.headers['Cache-Control'] = 'public, max-age=300'
            return resp
    try:
        import ogp_generator
        png_bytes = ogp_generator.generate_live_ogp_png(stores, s_param=s_param)
        with _ogp_bytes_cache_lock:
            _ogp_bytes_cache[ogp_key] = (now_mono, png_bytes)
            _ogp_bytes_cache.move_to_end(ogp_key)
            while len(_ogp_bytes_cache) > _OGP_BYTES_CACHE_MAX:
                _ogp_bytes_cache.popitem(last=False)
        resp = Response(png_bytes, mimetype='image/png')
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    except Exception as e:
        logging.error(f"Dynamic OGP generation failed, falling back to static image: {e}")
        return send_from_directory('static', 'note-eyecatch.jpg', mimetype='image/jpeg')

@app.route('/ogp/weekly.png')
def dynamic_weekly_ogp_image():
    """Dynamically render a 1200x630 weekly ranking & open-data infographic OGP card image (memoized in RAM)."""
    try:
        import weekly_report
        import ogp_generator
        report = weekly_report.generate_weekly_report()
        valid_rep = report if isinstance(report, dict) and 'error' not in report else {}
        ogp_key = (
            'weekly',
            valid_rep.get('generated_at'),
            len(valid_rep.get('overall_ranking') or []),
            local_db.DB_PATH,
        )
        now_mono = time.monotonic()
        with _ogp_bytes_cache_lock:
            hit = _ogp_bytes_cache.get(ogp_key)
            if hit is not None and (now_mono - hit[0] < 600.0):
                _ogp_bytes_cache.move_to_end(ogp_key)
                resp = Response(hit[1], mimetype='image/png')
                resp.headers['Cache-Control'] = 'public, max-age=1800'
                return resp
        png_bytes = ogp_generator.generate_weekly_ogp_png(valid_rep)
        with _ogp_bytes_cache_lock:
            _ogp_bytes_cache[ogp_key] = (now_mono, png_bytes)
            _ogp_bytes_cache.move_to_end(ogp_key)
            while len(_ogp_bytes_cache) > _OGP_BYTES_CACHE_MAX:
                _ogp_bytes_cache.popitem(last=False)
        resp = Response(png_bytes, mimetype='image/png')
        resp.headers['Cache-Control'] = 'public, max-age=1800'
        return resp
    except Exception as e:
        logging.error(f"Weekly OGP generation failed, falling back to static image: {e}")
        return send_from_directory('static', 'note-eyecatch.jpg', mimetype='image/jpeg')

@app.route('/guide')
def guide_article():
    """Official comprehensive lounge strategy editorial manual."""
    return _get_cached_html_or_render('guide_article', lambda: render_template('guide_article.html'))


@app.route('/guide/strategy')
def guide_strategy_legacy_redirect():
    """301 Permanent redirect from legacy /guide/strategy to canonical /guide."""
    return redirect('/guide', code=301)


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
    'women_dating_bachelor': os.environ.get('AFFILIATE_URL_WOMEN_DATING') or os.environ.get('AFFILIATE_URL_DATING_BACHELOR') or 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
    'women_dating': os.environ.get('AFFILIATE_URL_WOMEN_DATING') or os.environ.get('AFFILIATE_URL_DATING_BACHELOR') or 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
    'women_beauty_clinic': os.environ.get('AFFILIATE_URL_WOMEN_CLINIC') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
    'women_clinic': os.environ.get('AFFILIATE_URL_WOMEN_CLINIC') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
    'women_hair_removal': os.environ.get('AFFILIATE_URL_WOMEN_CLINIC') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
    'women_cosme_salon': os.environ.get('AFFILIATE_URL_WOMEN_COSME') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
    'women_cosme': os.environ.get('AFFILIATE_URL_WOMEN_COSME') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
    'women_skincare': os.environ.get('AFFILIATE_URL_WOMEN_COSME') or 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
}

@app.route('/link/<path:item_id>')
def affiliate_redirect(item_id):
    """Safe internal redirect shielding affiliate links from ad-blockers, with live runtime override resolution."""
    import affiliate_manager
    clean_id = item_id.strip().lower()
    target_url = affiliate_manager.resolve_affiliate_url(clean_id, allow_blocking=False) or AFFILIATE_REDIRECTS.get(clean_id)
    if not target_url:
        return redirect(url_for('index'))
    if not _is_bot_request():
        try:
            import analytics
            parsed_ref = urllib_parse.urlparse(request.referrer or '')
            ref_path = parsed_ref.path or '-'
            analytics.track('affiliate_click', f"{clean_id} @ {ref_path}")
            ref_s = (urllib_parse.parse_qs(parsed_ref.query).get('s') or [''])[0].strip()
            raw_inflow = ref_s or (request.cookies.get('an_src') or '').strip()
            if raw_inflow:
                inflow_tag = analytics.normalize_inflow_source(raw_inflow)
                if inflow_tag:
                    analytics.track('sns_inflow', f"cv:{inflow_tag}")
        except Exception:
            pass
    return redirect(target_url, code=302)

# --- First-party analytics (no personal data) ---
import urllib.parse as urllib_parse

_BOT_MARKERS = ('bot', 'spider', 'crawl', 'slurp', 'facebookexternalhit', 'preview', 'headless', 'python-requests', 'curl')
_TRACKED_PAGE_PREFIXES = ('/guide', '/columns', '/areas', '/area/', '/grooming', '/for-women', '/stats', '/price-comparison', '/oriental-lounge', '/ag', '/jis', '/store/')
_LIVE_NO_CACHE_PREFIXES = ('/price-comparison', '/for-women', '/store/', '/areas', '/area/', '/oriental-lounge', '/ag', '/jis')

@app.template_filter('seo_store_name')
def _jinja_seo_store_name(name):
    import store_master
    return store_master.seo_name(name)

@app.template_filter('store_slug')
def _jinja_store_slug(name):
    import store_master
    return store_master.slug_of(name)

def _is_bot_request():
    ua = (request.headers.get('User-Agent') or '').lower()
    return (not ua) or any(m in ua for m in _BOT_MARKERS)

@app.after_request
def track_page_views(response):
    try:
        if request.path.startswith('/static/'):
            if request.args.get('v'):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                response.headers['Cache-Control'] = 'public, max-age=86400'
        else:
            if request.path in ('/sw.js', '/api/status'):
                response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
            elif (request.path == '/' or request.path.startswith(_LIVE_NO_CACHE_PREFIXES)) and not session.get('is_staff'):
                # Allow browser back/forward cache (bfcache) for instant 0ms back-navigation while revalidating normal visits
                response.headers['Cache-Control'] = 'private, no-cache, must-revalidate, max-age=0'
            if (request.method == 'GET' and response.status_code == 200
                    and (request.path == '/' or request.path.startswith(_TRACKED_PAGE_PREFIXES))
                    and not session.get('is_staff')
                    and not _is_bot_request()):
                import analytics
                analytics.track('page_view', request.path)
                raw_s = (request.args.get('s') or '').strip()
                if raw_s:
                    inflow_tag = analytics.normalize_inflow_source(raw_s)
                    if inflow_tag:
                        analytics.track('sns_inflow', f"{inflow_tag} @ {request.path}")
                        response.set_cookie('an_src', inflow_tag, max_age=86400, path='/', samesite='Lax')
    except Exception:
        pass
    return response

@app.route('/api/track', methods=['POST'])
def track_event():
    """Receives navigator.sendBeacon events from the frontend (event + short label only)."""
    if _is_bot_request():
        return ('', 204)
    payload = request.get_json(silent=True, force=True) or {}
    import analytics
    analytics.track(str(payload.get('event', ''))[:30], str(payload.get('label', ''))[:80])
    return ('', 204)

@app.route('/staff/analytics')
def staff_analytics():
    """Master-only analytics summary (page views, affiliate clicks, button clicks, affiliate slot manager & Sheets retention)."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import analytics
    import affiliate_manager
    import archive_data
    days = max(1, min(90, request.args.get('days', 7, type=int)))
    is_json_req = request.args.get('format') == 'json'
    force_flush = request.args.get('flush') == '1'
    flushed_rows = 0
    if force_flush:
        try:
            flushed_rows = analytics.flush()
        except Exception:
            pass
    data = analytics.summary(days=days, allow_blocking=(is_json_req or force_flush))
    if is_json_req:
        return jsonify(data)
    aff_status = affiliate_manager.get_affiliate_slots_status(analytics_data=data, allow_blocking=False)
    retention_status = archive_data.get_retention_status(allow_blocking=False)
    return render_template(
        'staff_analytics.html',
        data=data,
        days=days,
        flushed=force_flush,
        flushed_rows=flushed_rows,
        aff_status=aff_status,
        retention_status=retention_status,
    )


@app.route('/api/affiliate/config', methods=['GET', 'POST'])
def api_affiliate_config():
    """Master-only API to inspect or live-update affiliate destination URLs without redeploying."""
    if not is_master_authorized():
        return jsonify({'ok': False, 'error': 'マスター権限が必要です'}), 401
    import analytics
    import affiliate_manager

    if request.method == 'GET':
        days = max(1, min(90, request.args.get('days', 7, type=int)))
        ana_data = analytics.summary(days=days, allow_blocking=False)
        return jsonify({
            'ok': True,
            **affiliate_manager.get_affiliate_slots_status(analytics_data=ana_data, allow_blocking=True),
        })

    payload = request.get_json(silent=True, force=True) or {}
    slot_id = str(payload.get('slot_id') or '').strip()
    target_url = str(payload.get('url') or '').strip()
    memo = str(payload.get('memo') or '').strip()
    days = max(1, min(90, int(payload.get('days') or 7)))

    res = affiliate_manager.set_affiliate_slot_override(slot_id=slot_id, target_url=target_url, memo=memo)
    if not res.get('ok'):
        return jsonify(res), 400

    ana_data = analytics.summary(days=days, allow_blocking=False)
    status_now = affiliate_manager.get_affiliate_slots_status(analytics_data=ana_data, allow_blocking=False)
    return jsonify({
        **res,
        'aff_status': status_now,
    })


@app.route('/api/sheets/retention', methods=['GET', 'POST'])
def api_sheets_retention():
    """Master-only API to inspect Google Sheets 10M-cell capacity or run compression/rotation immediately."""
    if not is_master_authorized():
        return jsonify({'ok': False, 'error': 'マスター権限が必要です'}), 401
    import archive_data

    if request.method == 'GET':
        return jsonify(archive_data.get_retention_status(allow_blocking=True))

    payload = request.get_json(silent=True, force=True) or {}
    dry_run = bool(payload.get('dry_run', False))
    res = archive_data.run_full_sheets_retention(dry_run=dry_run)
    status_code = 200 if res.get('ok') else (409 if res.get('busy') else 500)
    return jsonify(res), status_code

@app.route('/staff', methods=['GET', 'POST'])
def staff_monitor():
    """Internal staff dashboard (5555 = field data only, 2918 = master full management)."""
    error = None
    client_ip = (request.remote_addr or '0.0.0.0').strip()

    if request.method == 'POST':
        if _is_login_rate_limited(client_ip):
            return render_template('staff_login.html', error='試行回数が上限に達しました。5分ほど待ってから再度お試しください'), 429
        entered = request.form.get('passcode', '').strip()
        if entered and hmac.compare_digest(entered, MASTER_PASSCODE):
            _clear_login_failures(client_ip)
            session['is_staff'] = True
            session['is_master'] = True
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        elif entered and hmac.compare_digest(entered, STAFF_PASSCODE):
            _clear_login_failures(client_ip)
            session['is_staff'] = True
            session['is_master'] = False
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        else:
            _record_login_failure(client_ip)
            error = 'パスコードが正しくありません'

    # Allow one-touch login via query param ?passcode=5555 or ?passcode=2918 (rate-limited as well)
    query_passcode = request.args.get('passcode', '').strip()
    if query_passcode:
        if _is_login_rate_limited(client_ip):
            return render_template('staff_login.html', error='試行回数が上限に達しました。5分ほど待ってから再度お試しください'), 429
        if hmac.compare_digest(query_passcode, MASTER_PASSCODE):
            _clear_login_failures(client_ip)
            session['is_staff'] = True
            session['is_master'] = True
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        elif hmac.compare_digest(query_passcode, STAFF_PASSCODE):
            _clear_login_failures(client_ip)
            session['is_staff'] = True
            session['is_master'] = False
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        else:
            _record_login_failure(client_ip)
            error = 'パスコードが正しくありません'

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
            return render_template(
                'index.html',
                is_staff=True,
                is_master=is_master,
                initial_status=get_initial_status_payload(),
                published_scheduled=scheduled_articles.get_published_articles(),
                scheduled_queue=sched_queue,
                data_audit_report=audit_report,
                editorial_preview=editorial_preview,
            )
        return _get_cached_html_or_render(('staff_dash', is_master), _render_staff)

    return render_template('staff_login.html', error=error)

@app.route('/api/data-audit')
def api_data_audit():
    """Master-only endpoint returning automated price & business hours drift audit report."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import data_audit
    with data_lock:
        cur_live = list(latest_data.get('full_data') or [])
    report = data_audit.run_data_audit(live_stores=cur_live, send_alerts=False)
    return jsonify(report)

@app.route('/staff/logout')
def staff_logout():
    """Clear staff and master authentication and return to staff login."""
    session.pop('is_staff', None)
    session.pop('is_master', None)
    return redirect(url_for('staff_monitor'))


_sns_kit_ctx_cache = {'ctx': None, 'key': None, 'ts': 0.0}
_sns_slide_bytes_cache = OrderedDict()  # (slide_num, is_jpg, ctx_sig) -> (ts, bytes)
_sns_zip_bytes_cache = {'key': None, 'ts': 0.0, 'bytes': None}


def _sns_context_sig(ctx: dict) -> tuple:
    if not isinstance(ctx, dict):
        return ()
    top_sig = tuple(
        (p.get('raw_name') or p.get('short_name'), p.get('peak_women'), p.get('avg_wpct'))
        for p in (ctx.get('top_picks') or [])[:5]
    )
    return (ctx.get('weekend_label'), ctx.get('generated_at'), top_sig)


def _get_sns_kit_context(fast=False):
    import ig_carousel_generator
    import weekly_report
    now_t = time.time()
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    live_top_sig = tuple((s.get('name'), int(s.get('women') or 0), int(s.get('men') or 0)) for s in live_s[:8])
    cache_key = (local_db.DB_PATH, live_top_sig, datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d%H'))
    with _sns_cache_lock:
        if _sns_kit_ctx_cache['ctx'] is not None and _sns_kit_ctx_cache['key'] == cache_key and (now_t - _sns_kit_ctx_cache['ts'] < 300):
            return _sns_kit_ctx_cache['ctx']

    rep = None
    try:
        # Unify context across IG editorial captions and carousel slides so rankings/numbers always match
        cached_rep = (getattr(weekly_report, '_report_cache', None) or {}).get('data')
        if fast and isinstance(cached_rep, dict) and 'error' not in cached_rep:
            rep = cached_rep
        else:
            res = weekly_report.generate_weekly_report()
            if isinstance(res, dict) and 'error' not in res:
                rep = res
    except Exception:
        rep = None

    ctx = ig_carousel_generator.build_forecast_context(report=rep, live_stores=live_s)
    with _sns_cache_lock:
        _sns_kit_ctx_cache['ctx'] = ctx
        _sns_kit_ctx_cache['key'] = cache_key
        _sns_kit_ctx_cache['ts'] = now_t
    return ctx


def _render_cached_ig_slide_bytes(slide_num: int, is_jpg: bool, ctx: dict) -> bytes:
    import ig_carousel_generator
    clamped = max(1, min(5, int(slide_num)))
    cache_key = (clamped, bool(is_jpg), _sns_context_sig(ctx))
    now_t = time.time()
    with _sns_cache_lock:
        cached = _sns_slide_bytes_cache.get(cache_key)
        if cached is not None and (now_t - cached[0] < 300):
            _sns_slide_bytes_cache.move_to_end(cache_key)
            return cached[1]

    if is_jpg and hasattr(ig_carousel_generator, 'render_slide_jpeg'):
        img_bytes = ig_carousel_generator.render_slide_jpeg(clamped, ctx=ctx)
    elif is_jpg:
        import io
        from PIL import Image
        png_bytes = ig_carousel_generator.render_slide_png(clamped, ctx=ctx)
        img = Image.open(io.BytesIO(png_bytes)).convert('RGB')
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=92, optimize=True)
        img_bytes = out.getvalue()
    else:
        img_bytes = ig_carousel_generator.render_slide_png(clamped, ctx=ctx)

    with _sns_cache_lock:
        _sns_slide_bytes_cache[cache_key] = (now_t, img_bytes)
        _sns_slide_bytes_cache.move_to_end(cache_key)
        while len(_sns_slide_bytes_cache) > 20:
            _sns_slide_bytes_cache.popitem(last=False)
    return img_bytes


@app.route('/staff/sns-editorial')
def staff_sns_editorial():
    """Master-only 4-role SNS Editorial Room board across X, Threads, and Instagram."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import sns_editorial
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    channel = (request.args.get('channel') or 'x').strip().lower()
    if channel not in ('x', 'threads', 'instagram'):
        channel = 'x'
    slot_label = (request.args.get('slot') or '22:00').strip()
    if slot_label not in ('20:00', '22:00', '00:00', '02:00'):
        slot_label = '22:00'
    tone_mode = (request.args.get('tone') or '').strip().lower() or None
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    ig_ctx = _get_sns_kit_context(fast=True) if channel == 'instagram' else None
    force_ai = request.args.get('refresh_ai') == '1'
    is_json_req = request.args.get('format') == 'json'
    editorial = sns_editorial.run_editorial_pipeline(
        live_s,
        slot_label=slot_label,
        channel=channel,
        ig_ctx=ig_ctx,
        record_history=False,
        use_ai=True,
        force_ai_refresh=force_ai,
        ai_cache_only=(not is_json_req),
        tone_mode=tone_mode,
    )
    if is_json_req:
        return jsonify(editorial)
    return render_template(
        'staff_sns_editorial.html',
        editorial=editorial,
        slot_label=slot_label,
        channel=channel,
        tone_mode=editorial.get('tone_mode'),
    )


@app.route('/api/sns-editorial')
def api_sns_editorial():
    """Master-only JSON API returning the 4-role SNS editorial proposals and scores for X, Threads, or Instagram."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import sns_editorial
    restore_latest_data_if_empty()
    channel = (request.args.get('channel') or 'x').strip().lower()
    if channel not in ('x', 'threads', 'instagram'):
        channel = 'x'
    slot_label = (request.args.get('slot') or '22:00').strip()
    tone_mode = (request.args.get('tone') or '').strip().lower() or None
    force_ai = request.args.get('refresh_ai') == '1'
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    ig_ctx = _get_sns_kit_context(fast=True) if channel == 'instagram' else None
    return jsonify(sns_editorial.run_editorial_pipeline(
        live_s,
        slot_label=slot_label,
        channel=channel,
        ig_ctx=ig_ctx,
        record_history=False,
        use_ai=True,
        force_ai_refresh=force_ai,
        tone_mode=tone_mode,
    ))


@app.route('/api/editorial/tone-mode', methods=['POST'])
def api_editorial_tone_mode():
    """Master-only endpoint to persist the default writing tone & perspective across SNS and Column editorial rooms."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import sns_editorial
    import column_editorial
    payload = request.get_json(silent=True) or {}
    raw_tm = (payload.get('tone_mode') or '').strip().lower()
    norm_tm = sns_editorial.normalize_tone_mode(raw_tm)
    if not norm_tm:
        return jsonify({'ok': False, 'error': '有効な執筆トーン（spicy_female / spicy / female_real / standard）を指定してください。'}), 400
    saved_tm = sns_editorial.set_default_editorial_tone_mode(norm_tm)
    column_editorial.clear_column_ai_cache()
    clear_fast_http_caches()
    meta = sns_editorial.EDITORIAL_TONE_MODES[saved_tm]
    return jsonify({
        'ok': True,
        'tone_mode': saved_tm,
        'label': meta['label'],
        'short_label': meta['short_label'],
        'message': f"自動投稿＆編集部の標準トーンを『{meta['label']}』に保存しました！",
    })


@app.route('/api/sns-editorial/gemini-key', methods=['POST'])
def api_sns_editorial_gemini_key():
    """Master-only endpoint to securely store or clear the Gemini API key in obfuscated job_state."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import sns_editorial
    payload = request.get_json(silent=True) or {}
    action = (payload.get('action') or 'save').strip().lower()
    if action == 'clear':
        sns_editorial.set_gemini_api_key('')
        try:
            import column_editorial
            column_editorial.clear_column_ai_cache()
        except Exception:
            pass
        clear_fast_http_caches()
        return jsonify({
            'ok': True,
            'message': '登録済みのGemini APIキーを削除しました（Render環境変数がある場合はそちらが使用されます）。',
            'gemini_status': sns_editorial.get_gemini_status(),
        })

    api_key = (payload.get('api_key') or '').strip()
    if not api_key or len(api_key) < 16:
        return jsonify({'ok': False, 'error': '有効なGemini APIキーを入力してください（AIza... で始まる文字列）'}), 400

    sns_editorial.set_gemini_api_key(api_key)
    try:
        import column_editorial
        column_editorial.clear_column_ai_cache()
    except Exception:
        pass
    clear_fast_http_caches()
    return jsonify({
        'ok': True,
        'message': 'Gemini APIキーを暗号化保存しました！自動でAIアドリブ執筆＆鬼デスク数値検閲が有効になります。',
        'gemini_status': sns_editorial.get_gemini_status(),
    })


@app.route('/api/sns-editorial/platform-token', methods=['POST'])
def api_sns_editorial_platform_token():
    """Master-only endpoint to verify, exchange (short->60d long-lived), save, or clear Threads / Instagram tokens without redeploying."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import sns_editorial
    import threads_poster
    import instagram_poster

    payload = request.get_json(silent=True) or {}
    ch = (payload.get('channel') or 'threads').strip().lower()
    if ch not in ('threads', 'instagram'):
        return jsonify({'ok': False, 'error': 'channel は threads または instagram を指定してください。'}), 400

    action = (payload.get('action') or 'save').strip().lower()
    if action == 'clear':
        res = (
            threads_poster.save_and_prepare_threads_token('')
            if ch == 'threads'
            else instagram_poster.save_and_prepare_ig_token('')
        )
        clear_fast_http_caches()
        return jsonify({
            **res,
            'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
        })

    if action == 'verify':
        ver = (
            threads_poster.verify_threads_token()
            if ch == 'threads'
            else instagram_poster.verify_ig_token()
        )
        if ver.get('ok'):
            sns_editorial.record_sns_delivery_status(ch, 'token_refreshed', detail='verified')
            uname = ver.get('username') or ver.get('id') or ch
            return jsonify({
                'ok': True,
                'verify': ver,
                'message': f"疎通確認OK！ @{uname} と正常に接続されています（投稿は行っていません）。",
                'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
            })
        err_msg = ver.get('error') or 'トークンが無効または未設定です'
        if ver.get('status') == 'error':
            sns_editorial.record_sns_delivery_status(ch, 'token_error', detail=err_msg)
        return jsonify({
            'ok': False,
            'verify': ver,
            'error': f"疎通エラー：{err_msg}",
            'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
        }), 400

    raw_token = (payload.get('access_token') or '').strip()
    app_secret = (payload.get('app_secret') or '').strip()
    if not raw_token or len(raw_token) < 20:
        return jsonify({'ok': False, 'error': '有効なアクセストークンを貼り付けてください。'}), 400

    res = (
        threads_poster.save_and_prepare_threads_token(raw_token, app_secret=app_secret)
        if ch == 'threads'
        else instagram_poster.save_and_prepare_ig_token(raw_token, app_secret=app_secret)
    )
    clear_fast_http_caches()
    status_code = 200 if res.get('ok') else 400
    return jsonify({
        **res,
        'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
    }), status_code


@app.route('/staff/column-editorial')
def staff_column_editorial():
    """Master-only 4-role Column & note.com Editorial Room board."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import column_editorial
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    theme_key = (request.args.get('theme') or 'area_hopping').strip().lower()
    custom_topic = (request.args.get('topic') or '').strip()
    tone_mode = (request.args.get('tone') or '').strip().lower() or None
    export_slug = (request.args.get('export') or '').strip()
    force_ai = request.args.get('refresh_ai') == '1'
    is_json_req = request.args.get('format') == 'json'
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    editorial = column_editorial.run_column_editorial_pipeline(
        live_s,
        theme_key=theme_key,
        custom_topic=custom_topic,
        use_ai=True,
        force_ai_refresh=force_ai,
        ai_cache_only=(not is_json_req),
        tone_mode=tone_mode,
    )
    if is_json_req:
        return jsonify(editorial)
    return render_template(
        'staff_column_editorial.html',
        editorial=editorial,
        custom_topic=custom_topic,
        tone_mode=editorial.get('tone_mode'),
        export_slug=export_slug,
    )


@app.route('/api/column-editorial')
def api_column_editorial():
    """Master-only JSON API returning 4-role column & note proposals, scores, and audit table."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import column_editorial
    restore_latest_data_if_empty()
    theme_key = (request.args.get('theme') or 'area_hopping').strip().lower()
    custom_topic = (request.args.get('topic') or '').strip()
    tone_mode = (request.args.get('tone') or '').strip().lower() or None
    force_ai = request.args.get('refresh_ai') == '1'
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    return jsonify(column_editorial.run_column_editorial_pipeline(
        live_stores=live_s,
        theme_key=theme_key,
        custom_topic=custom_topic,
        use_ai=True,
        force_ai_refresh=force_ai,
        ai_cache_only=False,
        tone_mode=tone_mode,
    ))


@app.route('/api/column-editorial/publish', methods=['POST'])
def api_column_editorial_publish():
    """Master-only endpoint to schedule or immediately publish an editorial column to the live site."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import column_editorial
    payload = request.get_json(silent=True) or {}
    candidate = payload.get('candidate') or {}
    if not isinstance(candidate, dict):
        return jsonify({'ok': False, 'error': '有効な記事データがありません。'}), 400
    publish_mode = (payload.get('publish_mode') or 'schedule').strip().lower()
    publish_at_str = (payload.get('publish_at') or '').strip()
    custom_slug = (payload.get('slug') or '').strip()
    eyecatch_key = (payload.get('eyecatch_key') or '').strip()
    res = column_editorial.save_editorial_column(
        candidate=candidate,
        publish_mode=publish_mode,
        publish_at_str=publish_at_str,
        custom_slug=custom_slug,
        eyecatch_key=eyecatch_key,
    )
    if not res.get('ok'):
        return jsonify(res), 400
    clear_fast_http_caches()
    return jsonify(res)


@app.route('/api/column-editorial/delete', methods=['POST'])
def api_column_editorial_delete():
    """Master-only endpoint to delete a dynamically registered editorial column."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import column_editorial
    payload = request.get_json(silent=True) or {}
    slug = (payload.get('slug') or '').strip()
    res = column_editorial.delete_editorial_column(slug)
    if not res.get('ok'):
        return jsonify(res), 400
    clear_fast_http_caches()
    return jsonify(res)


@app.route('/api/column-editorial/export-note')
def api_column_editorial_export_note():
    """Master-only endpoint converting any existing/scheduled/dynamic column into ready-to-paste note.com format."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import column_editorial
    slug = (request.args.get('slug') or '').strip()
    res = column_editorial.export_column_for_note(slug, site_url='https://aiseki-navi.com')
    if not res.get('ok'):
        return jsonify(res), 404
    return jsonify(res)


@app.route('/staff/pdca')
def staff_pdca():
    """Master-only Autonomous Closed-Loop PDCA Strategist board."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import pdca_engine
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    days = max(1, min(90, request.args.get('days', 14, type=int)))
    is_json_req = request.args.get('format') == 'json'
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    has_existing = bool(pdca_engine.get_active_directives(allow_blocking=False))
    if not has_existing and not is_json_req:
        def _seed_pdca_async(stores_snap):
            try:
                if not pdca_engine.get_active_directives(allow_blocking=True):
                    pdca_engine.run_pdca_cycle(
                        live_stores=stores_snap,
                        days=days,
                        apply_actions=True,
                        use_ai=False,
                        record_log=True,
                    )
            except Exception:
                pass
        threading.Thread(target=_seed_pdca_async, args=(live_s,), daemon=True).start()
    pdca_data = pdca_engine.run_pdca_cycle(
        live_stores=live_s,
        days=days,
        apply_actions=(not has_existing and is_json_req),
        use_ai=True,
        ai_cache_only=(not is_json_req),
        record_log=(not has_existing and is_json_req),
    )
    if is_json_req:
        return jsonify(pdca_data)
    return render_template('staff_pdca.html', pdca=pdca_data, days=days)


@app.route('/api/pdca')
def api_pdca():
    """Master-only JSON API returning PDCA diagnostics, active directives, and AI Chief Strategist briefing."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import pdca_engine
    restore_latest_data_if_empty()
    days = max(1, min(90, request.args.get('days', 14, type=int)))
    force_ai = request.args.get('refresh_ai') == '1'
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    return jsonify(pdca_engine.run_pdca_cycle(
        live_stores=live_s,
        days=days,
        apply_actions=False,
        use_ai=True,
        ai_cache_only=False,
        force_ai_refresh=force_ai,
        record_log=False,
    ))


@app.route('/api/pdca/settings', methods=['POST'])
def api_pdca_settings():
    """Master-only endpoint to toggle autonomous SNS optimization and low-stock column auto-replenishment."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import pdca_engine
    payload = request.get_json(silent=True) or {}
    auto_sns = payload.get('auto_optimize_sns') if 'auto_optimize_sns' in payload else None
    auto_col = payload.get('auto_replenish_columns') if 'auto_replenish_columns' in payload else None
    updated = pdca_engine.update_pdca_settings(
        auto_optimize_sns=auto_sns,
        auto_replenish_columns=auto_col,
    )
    return jsonify({
        'ok': True,
        'settings': updated,
        'active_directives': pdca_engine.get_active_directives(allow_blocking=False),
    })


@app.route('/api/pdca/run', methods=['POST'])
def api_pdca_run():
    """Master-only manual trigger to execute the full PDCA cycle immediately and update editorial directives."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import pdca_engine
    restore_latest_data_if_empty()
    payload = request.get_json(silent=True) or {}
    days = max(1, min(90, int(payload.get('days') or 14)))
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    res = pdca_engine.run_pdca_cycle(
        live_stores=live_s,
        days=days,
        apply_actions=True,
        use_ai=True,
        ai_cache_only=False,
        force_ai_refresh=bool(payload.get('refresh_ai')),
        record_log=True,
    )
    clear_fast_http_caches()
    return jsonify({'ok': True, 'pdca': res})


@app.route('/staff/sns-kit')
def staff_sns_kit():
    """Master-only one-click Instagram / Threads weekend forecast carousel & caption generator."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import ig_carousel_generator
    ctx = _get_sns_kit_context()
    captions = ig_carousel_generator.build_captions(ctx)
    cache_bust = int(time.time()) // 300
    return render_template('staff_sns_kit.html', ctx=ctx, captions=captions, cache_bust=cache_bust)


@app.route('/staff/sns-kit/slide/<int:slide_num>.png')
def staff_sns_kit_slide(slide_num):
    """Render individual 1080x1350 PNG slide for master mobile save or preview (memoized in RAM)."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    ctx = _get_sns_kit_context()
    png_bytes = _render_cached_ig_slide_bytes(slide_num, is_jpg=False, ctx=ctx)
    headers = {'Cache-Control': 'private, max-age=300'}
    if request.args.get('download') == '1':
        headers['Content-Disposition'] = f'attachment; filename="aiseki_navi_slide_0{max(1, min(5, slide_num))}.png"'
    return Response(png_bytes, mimetype='image/png', headers=headers)


@app.route('/staff/sns-kit/download.zip')
def staff_sns_kit_zip():
    """Download all 5 portrait PNG slides + Instagram/Threads captions in one ZIP (memoized in RAM)."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import ig_carousel_generator
    ctx = _get_sns_kit_context()
    ctx_sig = _sns_context_sig(ctx)
    now_t = time.time()
    with _sns_cache_lock:
        if _sns_zip_bytes_cache.get('bytes') is not None and _sns_zip_bytes_cache.get('key') == ctx_sig and (now_t - _sns_zip_bytes_cache.get('ts', 0.0) < 300):
            zip_bytes = _sns_zip_bytes_cache['bytes']
        else:
            zip_bytes = None
    if zip_bytes is None:
        zip_bytes = ig_carousel_generator.render_all_slides_zip(ctx=ctx)
        with _sns_cache_lock:
            _sns_zip_bytes_cache['key'] = ctx_sig
            _sns_zip_bytes_cache['ts'] = now_t
            _sns_zip_bytes_cache['bytes'] = zip_bytes
    today_s = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d')
    return Response(
        zip_bytes,
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="aiseki_navi_weekend_kit_{today_s}.zip"'},
    )


@app.route('/ogp/ig-slide/<int:slide_num>.png')
@app.route('/ogp/ig-slide/<int:slide_num>.jpg')
def public_ig_slide_png(slide_num):
    """Publicly accessible 1080x1350 JPEG/PNG slide endpoint so Meta Instagram Graph API servers can fetch carousel images."""
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    clamped = max(1, min(5, slide_num))
    is_jpg = request.path.endswith('.jpg')
    try:
        ctx = _get_sns_kit_context(fast=False)
        img_bytes = _render_cached_ig_slide_bytes(clamped, is_jpg=is_jpg, ctx=ctx)
        resp = Response(img_bytes, mimetype='image/jpeg' if is_jpg else 'image/png')
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    except Exception as e:
        logging.error(f"Public IG slide generation failed: {e}")
        abort(503)

_is_fetching = False
_fetch_started_at = None
_last_update_error = None
_fetch_lock = threading.Lock()

def trigger_background_fetch_if_needed():
    """Check if data is stale and trigger a background fetch if needed.
    MUST be called WITHOUT holding data_lock to avoid deadlock."""
    global _is_fetching, _fetch_started_at
    if os.environ.get('DISABLE_SCHEDULER') == '1':
        return
    
    with data_lock:
        last_updated = latest_data.get('last_updated')
        has_data = bool(latest_data.get('full_data')) and not latest_data.get('is_seed')
    
    is_stale = False
    if last_updated and has_data:
        last_time = datetime.datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S")
        current_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        if (current_jst - last_time).total_seconds() > fetch_interval_sec() + 30:
            is_stale = True

    if not has_data or is_stale:
        with _fetch_lock:
            # If a fetch has been running for more than 200s, assume it's hung and reset
            if _is_fetching and _fetch_started_at:
                elapsed = (datetime.datetime.now() - _fetch_started_at).total_seconds()
                if elapsed > 200:
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
                update_job(force=not has_data)
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
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    now_hr = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d%H')
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
        c_key = (_live_data_signature(raw_copy, last_u, is_s), now_hr)

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
        _attach_store_benchmarks(item, sname, w, m, benchmarks)
        enriched_ranking.append(item)

    raw_bytes = json.dumps({
        'timestamp': last_u,
        'ranking': enriched_ranking,
        'status': 'success',
        'is_seed': is_s,
    }, ensure_ascii=False).encode('utf-8')
    gz_bytes = gzip.compress(raw_bytes, compresslevel=6) if len(raw_bytes) > 500 else None
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
    with data_lock:
        last_updated = latest_data.get('last_updated')
        store_count = len(latest_data.get('full_data') or [])
    age_sec = None
    if last_updated:
        last_time = datetime.datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S")
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        age_sec = int((now_jst - last_time).total_seconds())
    snap = health.snapshot()
    down = [k for k, v in snap['sources'].items() if v.get('down')]
    stale = age_sec is None or age_sec > fetch_interval_sec() * 3 + 120
    ok = not stale and not down
    body = {
        'status': 'ok' if ok else 'degraded',
        'last_updated': last_updated,
        'data_age_sec': age_sec,
        'store_count': store_count,
        'down_sources': down,
        'webhook_configured': snap['webhook_configured'],
    }
    if is_admin_authorized():
        body['detail'] = snap
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
        report = weekly_report.generate_weekly_report()
        if "error" in report:
            return jsonify({"status": "error", "error": report["error"]}), 500
        lf = weekly_report._report_cache.get('last_fetched')
        return _cached_json_response(
            ('weekly_report', local_db.DB_PATH, lf, id(report)),
            lambda: ({"status": "success", "data": report}, 200),
            ttl_sec=60.0,
        )
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/api/events')
def get_events():
    """Returns active events and promotions for lounge stores."""
    try:
        import events
        store = request.args.get('store', '').strip()
        force = (request.args.get('refresh', '').lower() == 'true') and is_master_authorized()
        if not force:
            ev_upd = events._events_memory_cache.get('updated_at')
            ev_cnt = len(events._events_memory_cache.get('events') or [])
            if ev_cnt > 0:
                return _cached_json_response(
                    ('events', store, ev_upd, ev_cnt),
                    lambda: (
                        (lambda evs: {'status': 'success', 'count': len(evs), 'events': evs})(
                            events.get_events_for_store(store, allow_blocking=False) if store else events.fetch_all_events(force_refresh=False, allow_blocking=False)
                        ),
                        200,
                    ),
                    ttl_sec=60.0,
                )
        if store:
            evs = events.get_events_for_store(store, allow_blocking=force)
        else:
            evs = events.fetch_all_events(force_refresh=force, allow_blocking=force)
        return jsonify({
            'status': 'success',
            'count': len(evs),
            'events': evs
        })
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/api/votes/<path:store_name>')
def get_store_votes(store_name):
    """Returns user crowd votes in the last 4 hours for a specific store."""
    counts = local_db.get_recent_votes(store_name=store_name, hours=4)
    return jsonify({'status': 'success', 'store_name': store_name, 'votes': counts})

@app.route('/api/vote', methods=['POST'])
def post_store_vote():
    """Submits a one-tap live crowd vote from a user."""
    import hashlib
    payload = request.get_json(silent=True) or {}
    store_name = str(payload.get('store_name', '')).strip()
    vote_type = str(payload.get('vote_type', '')).strip()
    if not store_name or vote_type not in ('smooth', 'waiting', 'lively'):
        return jsonify({'status': 'error', 'error': 'Invalid parameters'}), 400
    if len(store_name) > 60:
        return jsonify({'status': 'error', 'error': 'Invalid store'}), 400
    # Only accept votes for stores that actually exist in the current ranking (prevents junk rows)
    with data_lock:
        known_names = {s.get('name') for s in latest_data.get('full_data', [])}
    if known_names and store_name not in known_names:
        return jsonify({'status': 'error', 'error': 'Unknown store'}), 400

    client_ip = (request.remote_addr or '0.0.0.0').strip()
    ip_hash = hashlib.sha256(f"{client_ip}_aiseki_salt".encode('utf-8')).hexdigest()[:16]
    raw_voter = str(payload.get('voter_id', '')).strip()[:64]
    voter_hash = hashlib.sha256(f"v_{raw_voter}_aiseki_salt".encode('utf-8')).hexdigest()[:16] if raw_voter else ''

    ok, reason = local_db.add_crowd_vote(store_name, vote_type, ip_hash, voter_id=voter_hash)
    if ok:
        try:
            import analytics
            analytics.track('vote', f"{store_name}:{vote_type}")
        except Exception:
            pass
    counts = local_db.get_recent_votes(store_name=store_name, hours=4)

    # Also update in-memory latest_data so subsequent /api/status calls reflect immediately
    with data_lock:
        for s in latest_data.get('full_data', []):
            if s.get('name') == store_name:
                s['votes'] = counts
                break

    return jsonify({
        'status': 'success' if ok else 'duplicate',
        'reason': reason,
        'store_name': store_name,
        'votes': counts
    })

@app.route('/robots.txt')
def robots_txt():
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/debug_ps\n"
        "Disallow: /api/logs\n"
        "Disallow: /api/thread_status\n"
        "Disallow: /api/data-audit\n"
        "Disallow: /api/trigger-\n"
        "Disallow: /staff\n"
        "Sitemap: https://aiseki-navi.com/sitemap.xml\n"
    )
    return Response(content, mimetype='text/plain')

@app.route('/ads.txt')
def ads_txt():
    content = "google.com, pub-3043764334238627, DIRECT, f08c47fec0942fa0\n"
    return Response(content, mimetype='text/plain')

from portal_guides import AREA_GUIDES, BRAND_GUIDES, get_stores_for_area

@app.route('/oriental-lounge')
def brand_oriental_lounge():
    return _render_brand_page('oriental-lounge')

@app.route('/ag')
def brand_ag():
    return _render_brand_page('ag')

@app.route('/jis')
def brand_jis():
    return _render_brand_page('jis')

def _render_brand_page(brand_slug):
    import store_master
    import store_hours
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    brand = BRAND_GUIDES.get(brand_slug)
    if not brand:
        return redirect(url_for('index'))

    def _render():
        with data_lock:
            raw_stores = list(latest_data.get('full_data') or [])
            last_updated = latest_data.get('last_updated') or '--:--'

        master = store_master.load()
        live_map = {s.get('name'): s for s in raw_stores}
        all_names = list(dict.fromkeys(list(live_map.keys()) + list(master.keys())))

        matched = []
        for sname in all_names:
            if store_master.brand_key_of(sname) == brand_slug:
                s_live = live_map.get(sname, {})
                info = master.get(sname, {})
                reg = info.get('region') or s_live.get('region') or detect_region(sname)
                w_cnt = int(s_live.get('women', 0))
                m_cnt = int(s_live.get('men', 0))
                h_info = store_hours.get_business_hours_status(sname, total_guests=(w_cnt + m_cnt))
                matched.append({
                    'name': sname,
                    'seo_name': store_master.seo_name(sname),
                    'slug': store_master.slug_of(sname),
                    'region': reg,
                    'area_label': info.get('area_label') or s_live.get('area') or '全国',
                    'women': w_cnt,
                    'men': m_cnt,
                    'women_diff': int(s_live.get('women_diff', 0) or 0),
                    'men_diff': int(s_live.get('men_diff', 0) or 0),
                    'is_open_now': h_info['is_open_now'],
                    'today_hours_label': h_info['today_hours_label'],
                    'hours_label': h_info['hours_label'],
                })

        matched.sort(key=lambda x: (x.get('region') != 'Korea', x['women'], x['men'], bool(x.get('is_open_now'))), reverse=True)
        dom_rank = 0
        for item in matched:
            if item.get('region') != 'Korea':
                dom_rank += 1
                item['rank_num'] = dom_rank
            else:
                item['rank_num'] = None
        total_w = sum(x['women'] for x in matched)
        total_m = sum(x['men'] for x in matched)

        return render_template(
            'brand_page.html',
            brand=brand,
            stores=matched,
            store_count=len(matched),
            total_women=total_w,
            total_men=total_m,
            last_updated=last_updated
        )
    return _get_cached_html_or_render(('brand', brand_slug), _render)

@app.route('/store/<store_slug>')
def store_detail_page(store_slug):
    import store_master
    import store_hours
    import urllib.parse
    legacy_target = store_master.legacy_redirect_for_slug(store_slug)
    if legacy_target:
        return redirect(legacy_target, code=301)

    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    with data_lock:
        raw_stores = list(latest_data.get('full_data') or [])
        last_updated = latest_data.get('last_updated') or '--:--'

    sname = store_master.store_by_slug(store_slug, live_stores=raw_stores)
    if not sname:
        abort(404)

    def _render():
        store_analytics = None
        try:
            store_analytics = local_db.get_store_7day_analytics(sname)
        except Exception:
            pass
        info = store_master.get(sname) or {}
        live_map = {s.get('name'): s for s in raw_stores}
        s_live = live_map.get(sname, {})

        women = int(s_live.get('women', 0))
        men = int(s_live.get('men', 0))
        tot = women + men
        wpct = round((women / tot) * 100) if tot > 0 else 50
        hours_info = store_hours.get_business_hours_status(sname, total_guests=tot)

        bkey = store_master.brand_key_of(sname)
        raw_brand = info.get('brand') or ''
        if bkey == 'oriental-lounge':
            brand_label = 'オリエンタルラウンジ'
            brand_slug = 'oriental-lounge'
            official_url = 'https://oriental-lounge.com/'
        elif bkey == 'ag':
            brand_label = 'ag（アグ）'
            brand_slug = 'ag'
            official_url = 'https://ag-ree.jp/'
        elif bkey == 'jis':
            brand_label = 'JIS（ジス）'
            brand_slug = 'jis'
            official_url = 'https://jis.bar/'
        else:
            brand_label = raw_brand or '相席ラウンジ'
            brand_slug = ''
            official_url = {
                'ALFA': 'https://aiseki-hiroshima.com/',
                'CLOVERS': 'https://bar-clovers.com/',
                'XIX': 'https://xix-thelounge.com/',
                'YATAKOI': 'https://yatakoi.com/',
                'ヤタコイ': 'https://yatakoi.com/',
            }.get(raw_brand, 'https://oriental-lounge.com/')

        if tot == 0:
            if hours_info['is_open_now']:
                crowd_comment = f"営業中｜現在 0 : 0（本日 {hours_info['today_hours_label']}）"
            else:
                crowd_comment = f"営業時間外（本日 {hours_info['today_hours_label']}）"
        elif women < 10:
            if women >= men:
                crowd_comment = f'少人数・立ち上がり中（女性{women}名｜基準10名未満）'
            else:
                crowd_comment = f'少人数・男性先行（女性{women}名 / 男性{men}名）'
        elif women > men:
            crowd_comment = f'女性優位・好バランス（女性+{women - men}名）'
        elif women == men:
            crowd_comment = '男女比1:1 バランス良好'
        else:
            crowd_comment = '男性先行・近隣店舗との比較推奨'

        map_anchor = info.get('map_anchor') or store_master.seo_name(sname)
        map_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(map_anchor)}"

        area_slug = info.get('area_slug')
        area_info = AREA_GUIDES.get(area_slug) if area_slug else None

        nearby_stores = []
        if area_slug:
            for other_s in get_stores_for_area(area_slug, raw_stores):
                oname = other_s.get('name')
                if oname and oname != sname:
                    ow = int(other_s.get('women', 0))
                    om = int(other_s.get('men', 0))
                    otot = ow + om
                    oh_info = store_hours.get_business_hours_status(oname, total_guests=otot)
                    nearby_stores.append({
                        'name': oname,
                        'seo_name': store_master.seo_name(oname),
                        'slug': store_master.slug_of(oname),
                        'women': ow,
                        'men': om,
                        'women_diff': int(other_s.get('women_diff', 0) or 0),
                        'men_diff': int(other_s.get('men_diff', 0) or 0),
                        'wpct': round((ow / otot) * 100) if otot > 0 else 0,
                        'is_open_now': oh_info['is_open_now'],
                        'today_hours_label': oh_info['today_hours_label'],
                        'hours_label': oh_info['hours_label'],
                        'hours_spec': oh_info['schedule_spec'],
                    })

        store_obj = {
            'name': sname,
            'seo_name': store_master.seo_name(sname),
            'slug': store_master.slug_of(sname),
            'area_label': info.get('area_label') or s_live.get('area') or '全国',
            'brand_label': brand_label,
            'brand_slug': brand_slug,
            'official_url': official_url,
            'women': women,
            'men': men,
            'women_diff': int(s_live.get('women_diff', 0) or 0),
            'men_diff': int(s_live.get('men_diff', 0) or 0),
            'total': tot,
            'wpct': wpct if tot > 0 else 0,
            'crowd_comment': crowd_comment,
            'map_url': map_url,
            'is_open_now': hours_info['is_open_now'],
            'today_hours_label': hours_info['today_hours_label'],
            'hours_label': hours_info['hours_label'],
            'hours_spec': hours_info['schedule_spec'],
        }
        try:
            _attach_store_benchmarks(store_obj, sname, women, men, local_db.get_all_stores_live_benchmarks(allow_blocking=False))
        except Exception:
            _attach_store_benchmarks(store_obj, sname, women, men, {})

        nearby_comparison = None
        try:
            import price_comparison
            nearby_comparison = price_comparison.get_nearby_comparison_for_store(sname, live_stores=raw_stores)
        except Exception:
            pass

        return render_template(
            'store_detail.html',
            store=store_obj,
            store_analytics=store_analytics,
            area_info=area_info,
            nearby_stores=nearby_stores,
            nearby_comparison=nearby_comparison,
            jp_holidays=sorted(store_hours.JP_HOLIDAYS),
            last_updated=last_updated
        )
    return _get_cached_html_or_render(('store', store_slug), _render)

@app.route('/sitemap.xml')
def sitemap_xml():
    import store_master
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d')
    pub_items = scheduled_articles.get_published_articles()
    smap_key = (today, tuple(it.get('slug') for it in pub_items))
    if _sitemap_xml_cache['key'] == smap_key and _sitemap_xml_cache['xml'] is not None:
        return Response(_sitemap_xml_cache['xml'], mimetype='application/xml')
    brand_urls = "\n".join([
        f"""  <url>
    <loc>https://aiseki-navi.com/{bslug}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>0.95</priority>
  </url>""" for bslug in BRAND_GUIDES.keys()
    ])
    area_urls = "\n".join([
        f"""  <url>
    <loc>https://aiseki-navi.com/area/{slug}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>0.85</priority>
  </url>""" for slug in AREA_GUIDES.keys()
    ])
    master_stores = store_master.load()
    store_slugs = [store_master.slug_of(sname) for sname in master_stores.keys()]
    store_urls = "\n".join([
        f"""  <url>
    <loc>https://aiseki-navi.com/store/{sslug}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>0.85</priority>
  </url>""" for sslug in store_slugs if sslug
    ])
    scheduled_urls = "".join([
        f"""  <url>
    <loc>https://aiseki-navi.com{item['path']}</loc>
    <lastmod>{item['lastmod']}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>\n""" for item in scheduled_articles.get_published_articles()
    ])
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://aiseki-navi.com/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>
{brand_urls}
  <url>
    <loc>https://aiseki-navi.com/areas</loc>
    <lastmod>{today}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/columns</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/stats</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/price-comparison</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/for-women</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/grooming</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/weekday-strategy</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/brand-comparison</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/shinjuku</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/solo-vs-pair</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/second-venue</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/first-10-minutes</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/club-area-midnight</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/weekend-strategy</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/beauty-area-stage</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/male-lead-mechanics</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/price-event-comparison</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/last-train-cliff</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>
{scheduled_urls}{area_urls}
{store_urls}
  <url>
    <loc>https://aiseki-navi.com/privacy</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
</urlset>"""
    _sitemap_xml_cache['key'] = smap_key
    _sitemap_xml_cache['xml'] = xml
    return Response(xml, mimetype='application/xml')

@app.route('/columns')
def columns_index():
    """Portal page listing all editorial guides, comparisons, and area features."""
    is_staff = bool(session.get('is_staff'))
    is_master = bool(session.get('is_master'))
    pub_items = scheduled_articles.get_published_articles()
    up_items = scheduled_articles.get_upcoming_articles() if is_staff else []
    cache_sig = (
        'columns',
        is_staff,
        is_master,
        tuple(it.get('slug') for it in pub_items),
        tuple(it.get('slug') for it in up_items),
    )
    return _get_cached_html_or_render(
        cache_sig,
        lambda: render_template(
            'columns.html',
            area_guides=AREA_GUIDES,
            published_scheduled=pub_items,
            upcoming_scheduled=up_items,
            is_staff=is_staff,
            is_master=is_master,
        ),
    )

@app.route('/stats')
def stats_whitepaper():
    """SEO-indexed primary statistical data whitepaper & weekly ranking page."""
    import weekly_report
    trigger_background_fetch_if_needed()
    cached_rep = (getattr(weekly_report, '_report_cache', None) or {}).get('data')
    rep_sig = cached_rep.get('generated_at') if isinstance(cached_rep, dict) else None

    def _render():
        report_data = None
        try:
            res = weekly_report.generate_weekly_report()
            if res and 'error' not in res:
                report_data = res
        except Exception:
            report_data = None
        with data_lock:
            live_stores = list(latest_data.get('full_data') or [])
            last_updated = latest_data.get('last_updated') or '--:--'
        return render_template(
            'stats_report.html',
            report=report_data,
            live_stores=live_stores,
            last_updated=last_updated
        )
    return _get_cached_html_or_render(('stats', rep_sig), _render)

@app.route('/areas')
@app.route('/area/<area_slug>')
def area_portal(area_slug=None):
    """Area portal page showing live crowd stats and local lounge guides."""
    if area_slug is not None and area_slug not in AREA_GUIDES:
        abort(404)
    trigger_background_fetch_if_needed()
    def _render():
        with data_lock:
            stores = list(latest_data.get('full_data') or [])
            last_updated = latest_data.get('last_updated') or '--:--'

        area_summaries = []
        for slug, info in AREA_GUIDES.items():
            matched = get_stores_for_area(slug, stores)
            total_w = sum(s.get('women', 0) for s in matched)
            total_m = sum(s.get('men', 0) for s in matched)
            area_summaries.append({
                'slug': slug,
                'info': info,
                'stores': matched,
                'store_count': len(matched),
                'total_women': total_w,
                'total_men': total_m,
            })

        selected_area = None
        if area_slug and area_slug in AREA_GUIDES:
            for item in area_summaries:
                if item['slug'] == area_slug:
                    selected_area = item
                    break

        return render_template(
            'areas.html',
            area_summaries=area_summaries,
            selected_area=selected_area,
            last_updated=last_updated
        )
    return _get_cached_html_or_render(('areas', area_slug), _render)

@app.route('/price-comparison')
def price_comparison_page():
    """Urban cluster cross-brand price, men's event & live cost-performance comparison board."""
    import price_comparison
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    def _render():
        with data_lock:
            live_stores = list(latest_data.get('full_data') or [])
            last_updated = latest_data.get('last_updated') or '--:--'

        data = price_comparison.build_area_comparison_clusters(live_stores=live_stores)
        valid_ids = {c['id'] for c in data['clusters']}
        req_area = (request.args.get('area') or 'shinjuku').strip().lower()
        selected_area = req_area if req_area in valid_ids else 'shinjuku'

        return render_template(
            'price_comparison.html',
            data=data,
            selected_area=selected_area,
            last_updated=last_updated,
            is_staff=bool(session.get('is_staff')),
        )
    return _get_cached_html_or_render('price_comp', _render)

@app.route('/for-women')
def for_women_guide():
    """Dedicated SEO & real-time zero-wait lounge guide for female pairs (Plan C & D)."""
    import ladies_guide
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()

    def _render():
        with data_lock:
            live_stores = list(latest_data.get('full_data') or [])
            last_updated = latest_data.get('last_updated') or '--:--'
        board = ladies_guide.build_ladies_live_board(live_stores=live_stores)
        return render_template(
            'for_women.html',
            board=board,
            last_updated=last_updated,
            is_staff=bool(session.get('is_staff')),
        )

    return _get_cached_html_or_render('for_women', _render)


@app.route('/ladies')
def ladies_legacy_redirect():
    """301 Permanent redirect from short alias /ladies to canonical /for-women."""
    return redirect('/for-women', code=301)


@app.route('/grooming')
def grooming_guide():
    """Dedicated editorial guide for men's grooming, fashion, and backup dating apps."""
    return _get_cached_html_or_render('grooming', lambda: render_template('grooming.html'))

@app.route('/privacy')
def privacy_page():
    """Dedicated legal, privacy policy, and operator information page."""
    return _get_cached_html_or_render('privacy', lambda: render_template('privacy.html'))

@app.route('/guide/weekday-strategy')
def guide_weekday_strategy():
    """Editorial guide for weekday lounge strategy."""
    return _get_cached_html_or_render('guide_weekday', lambda: render_template('guide_weekday.html'))

@app.route('/guide/brand-comparison')
def guide_brand_comparison():
    """Editorial guide comparing Oriental Lounge, ag, and JIS."""
    return _get_cached_html_or_render('guide_comparison', lambda: render_template('guide_comparison.html'))


@app.route('/guide/compare')
def guide_compare_legacy_redirect():
    """301 Permanent redirect from legacy /guide/compare to canonical /guide/brand-comparison."""
    return redirect('/guide/brand-comparison', code=301)


@app.route('/guide/shinjuku')
def guide_shinjuku():
    """Editorial guide comparing the 3 Shinjuku lounge stores."""
    return _get_cached_html_or_render('guide_shinjuku', lambda: render_template('guide_shinjuku.html'))

@app.route('/guide/solo-vs-pair')
def guide_solo_vs_pair():
    """Editorial guide comparing solo vs 2-person lounge visits."""
    return _get_cached_html_or_render('guide_solo_pair', lambda: render_template('guide_solo_pair.html'))

@app.route('/guide/second-venue')
def guide_second_venue():
    """Editorial guide on inviting to a second venue smoothly."""
    return _get_cached_html_or_render('guide_second_venue', lambda: render_template('guide_second_venue.html'))

@app.route('/guide/first-10-minutes')
def guide_first_10min():
    """Editorial guide on first 10 minutes conversation and grooming."""
    return _get_cached_html_or_render('guide_first_10min', lambda: render_template('guide_first_10min.html'))

@app.route('/guide/club-area-midnight')
def guide_club_area_midnight():
    """Editorial data study on dance club proximity and midnight 2nd-wave female inflows."""
    return _get_cached_html_or_render('guide_club_midnight', lambda: render_template('guide_club_midnight.html'))

@app.route('/guide/weekend-strategy')
def guide_weekend_strategy():
    """Editorial data study guide for Friday/Saturday peak hours."""
    return _get_cached_html_or_render('guide_weekend', lambda: render_template('guide_weekend.html'))

@app.route('/guide/beauty-area-stage')
def guide_beauty_area_stage():
    """Editorial data study on beauty salon districts, early-evening surges, and men's grooming."""
    return _get_cached_html_or_render('guide_beauty_stage', lambda: render_template('guide_beauty_stage.html'))

@app.route('/guide/male-lead-mechanics')
def guide_male_lead_mechanics():
    """Editorial data study on why male-leading stores attract more women and why women no longer wait."""
    return _get_cached_html_or_render('guide_male_lead', lambda: render_template('guide_male_lead.html'))


@app.route('/guide/price-event-comparison')
def guide_price_event_comparison():
    """Editorial guide explaining the 51-store price, 4-time-slot, solo rule, event & minimum 10-women comparison board."""
    return _get_cached_html_or_render('guide_price_comparison', lambda: render_template('guide_price_comparison.html'))


@app.route('/guide/last-train-cliff')
def guide_last_train_cliff():
    """Editorial story & 9.6-million-point data study on the 23:20 last-train cliff, 00:30 second wave, and post-23:00 brand reversal."""
    return _get_cached_html_or_render('guide_last_train_cliff', lambda: render_template('guide_last_train_cliff.html'))


def _render_scheduled_guide(slug: str):
    """Render a scheduled editorial guide if published, or allow noindex preview for authenticated staff."""
    item = scheduled_articles.get_article_by_slug(slug)
    if not item:
        abort(404)
    if scheduled_articles.is_published(item):
        return _get_cached_html_or_render(
            ('sched_guide', slug, item.get('created_at') or item.get('publish_at')),
            lambda: render_template(item['template'], is_scheduled_preview=False, scheduled_item=item),
        )
    if is_admin_authorized():
        resp = make_response(render_template(item['template'], is_scheduled_preview=True, scheduled_item=item))
        resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return resp
    abort(404)


@app.route('/guide/local-vs-urban')
def guide_local_vs_urban():
    """Scheduled editorial data study comparing local 1-store cities vs urban flagship clusters."""
    return _render_scheduled_guide('local-vs-urban')


@app.route('/guide/cost-break-even')
def guide_cost_break_even():
    """Scheduled editorial data study on stay-duration cost break-even and store-hopping."""
    return _render_scheduled_guide('cost-break-even')


@app.route('/guide/rain-payday')
def guide_rain_payday():
    """Scheduled editorial data study on rainy nights, pre-payday windows, and 3-day-weekend Sundays."""
    return _render_scheduled_guide('rain-payday')


@app.route('/guide/<slug>')
def guide_dynamic_scheduled(slug):
    """Dynamic route serving scheduled or live columns published from the Column Editorial Room."""
    item = scheduled_articles.get_article_by_slug(slug)
    if not item:
        abort(404)
    return _render_scheduled_guide(slug)

@app.route('/api/sns-post')
def generate_sns_post_api():
    """Returns ready-to-post viral X/SNS text from current live data."""
    import sns_workflows
    post_type = request.args.get('type', 'all')
    base_url = request.url_root.rstrip('/')
    return jsonify(sns_workflows.build_sns_post_payload(post_type, base_url, latest_data))


def run_scheduled_x_post(slot_label="20:00", force=False):
    """Execute scheduled X auto-post with duplicate prevention that survives restarts/redeploys."""
    import sns_workflows
    return sns_workflows.execute_scheduled_x_post(latest_data, slot_label=slot_label, force=force)


@app.route('/api/trigger-x-post')
def trigger_x_post_endpoint():
    """Endpoint to test or externally trigger X auto-posting (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    slot = request.args.get('slot', '20:00')
    force = request.args.get('force', '0') == '1'
    res = run_scheduled_x_post(slot_label=slot, force=force)
    return jsonify(res)


@app.route('/api/trigger-x-tip')
def trigger_x_tip_endpoint():
    """Endpoint to test or externally trigger X human-like tips posting (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    slot = request.args.get('slot', '12:00')
    force = request.args.get('force', '0') == '1'
    idx = request.args.get('idx', type=int)
    import tips_poster
    res = tips_poster.run_scheduled_tip_post(slot_label=slot, force=force, tip_index=idx)
    return jsonify(res)


@app.route('/api/trigger-note-share')
def trigger_note_share_endpoint():
    """Endpoint to test or externally trigger note promotion posting (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    idx = request.args.get('idx', 0, type=int)
    import note_poster
    res = note_poster.check_and_post_new_note(force=force, article_index=idx)
    return jsonify(res)


@app.route('/api/trigger-x-pinned')
def trigger_x_pinned_endpoint():
    """Endpoint to post the 2-post Pinned Tweet thread (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import x_poster
    main_text, reply_text = x_poster.build_pinned_tweet_thread()
    res = x_poster.post_to_x(main_text, reply_text=reply_text)
    res["tweet_text"] = main_text
    res["reply_text"] = reply_text
    return jsonify(res)


@app.route('/api/trigger-x-column')
def trigger_x_column_endpoint():
    """Endpoint to post a 2-post Column Promotion thread (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    idx = request.args.get('idx', 0, type=int)
    import x_poster
    main_text, reply_text, item = x_poster.build_column_tweet_thread(col_index=idx)
    media_bytes, media_mime = x_poster.load_column_eyecatch_bytes(item)
    res = x_poster.post_to_x(
        main_text,
        reply_text=reply_text,
        media_bytes=media_bytes,
        media_mime_type=media_mime,
    )
    res["column"] = item
    res["tweet_text"] = main_text
    res["reply_text"] = reply_text
    return jsonify(res)


def run_scheduled_weekly_x_post(force=False):
    """Execute weekly summary X auto-post (with dynamic /ogp/weekly.png card attached to 1st post)."""
    import sns_workflows
    return sns_workflows.execute_scheduled_weekly_x_post(force=force)


@app.route('/api/trigger-x-weekly')
def trigger_x_weekly_endpoint():
    """Endpoint to test or externally trigger the Weekly Summary X post (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    res = run_scheduled_weekly_x_post(force=force)
    return jsonify(res)


@app.route('/api/trigger-x-bonus')
def trigger_x_bonus_endpoint():
    """Endpoint to test or externally trigger the Bonus-Time Guerrilla Alert X post (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import bonus_alert
    force = request.args.get('force', '0') == '1'
    with data_lock:
        stores = list(latest_data.get('full_data') or [])
    res = bonus_alert.check_and_post_bonus_alert(stores, force=force)
    return jsonify(res)


@app.route('/api/trigger-threads-post')
def trigger_threads_post_endpoint():
    """Endpoint to test or externally trigger Meta Threads auto-post (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import threads_poster
    slot = request.args.get('slot', '20:00')
    force = request.args.get('force', '0') == '1'
    with data_lock:
        stores = list(latest_data.get('full_data') or [])
    res = threads_poster.execute_scheduled_threads_post(stores, slot_label=slot, force=force)
    return jsonify(res)


@app.route('/api/trigger-ig-post')
def trigger_ig_post_endpoint():
    """Endpoint to test or externally trigger Meta Instagram Graph API auto-post (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import instagram_poster
    mode = request.args.get('mode', 'carousel')
    slot = request.args.get('slot', '22:00')
    force = request.args.get('force', '0') == '1'
    with data_lock:
        stores = list(latest_data.get('full_data') or [])
    res = instagram_poster.execute_scheduled_instagram_post(stores, mode=mode, slot_label=slot, force=force)
    return jsonify(res)


@app.route('/api/trigger-ladies-sns')
def trigger_ladies_sns_endpoint():
    """Endpoint to test or externally trigger Plan E Women-Perspective Zero-Wait & Perks SNS alerts (master only)."""
    if not is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    channel = (request.args.get('channel') or 'threads').strip().lower()
    force = request.args.get('force', '0') == '1'
    with data_lock:
        stores = list(latest_data.get('full_data') or [])
    if channel in ('ig', 'instagram'):
        import instagram_poster
        slot = request.args.get('slot', '19:45')
        res = instagram_poster.execute_scheduled_ladies_instagram_post(stores, slot_label=slot, force=force)
    else:
        import threads_poster
        slot = request.args.get('slot', '19:30')
        res = threads_poster.execute_scheduled_ladies_threads_post(stores, slot_label=slot, force=force)
    return jsonify(res)


# Start background services immediately on module import (unless disabled for unit tests)
_ensure_scheduler_started_async()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)


