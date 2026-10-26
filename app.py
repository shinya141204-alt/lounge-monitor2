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
from collections import defaultdict

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

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'aiseki-navi-staff-monitor-secret-key-2026')
app.permanent_session_lifetime = datetime.timedelta(days=90)
STAFF_PASSCODE = os.environ.get('STAFF_PASSCODE', '5555')
ADMIN_API_SECRET = os.environ.get('ADMIN_API_SECRET', 'lounge2026')

def is_admin_authorized():
    """Verify administrative permission either via authenticated staff session or secret token."""
    if session.get('is_staff'):
        return True
    secret = request.args.get('secret', '').strip()
    if secret and secret == ADMIN_API_SECRET:
        return True
    return False

@app.after_request
def compress_response(response):
    """Set cache headers and automatically gzip-compress JSON/HTML/CSS/JS responses."""
    # 1. Cache headers (must run regardless of gzip compression)
    if request.path.startswith('/static/') and response.status_code == 200:
        if any(request.path.lower().endswith(ext) for ext in ('.js', '.css', '.jpg', '.jpeg', '.png', '.webp', '.svg', '.gif', '.ico', '.woff2')):
            response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    elif response.content_type and 'text/html' in response.content_type and response.status_code == 200:
        if 'Cache-Control' not in response.headers:
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'

    # 2. Gzip compression
    if (
        response.status_code < 200
        or response.status_code >= 300
        or response.direct_passthrough
        or 'Content-Encoding' in response.headers
        or 'gzip' not in request.headers.get('Accept-Encoding', '').lower()
    ):
        return response
    if response.content_type and (
        'application/json' in response.content_type
        or 'text/html' in response.content_type
        or 'text/css' in response.content_type
        or 'application/javascript' in response.content_type
    ):
        data = response.get_data()
        if len(data) > 500:
            compressed = gzip.compress(data, compresslevel=5)
            response.set_data(compressed)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = len(compressed)
            response.headers['Vary'] = 'Accept-Encoding'

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
            local_db.start_background_sync(days=14, on_recent_synced=refresh_momentum_from_db)
            threading.Thread(target=local_db.restore_votes_from_sheets, kwargs={'hours': 24}, daemon=True).start()

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
            scheduler.add_job(func=_flush_analytics, trigger="interval", minutes=10)
            scheduler.add_job(func=lambda: local_db.sync_from_google_sheets(days=3), trigger="interval", hours=2)
            scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=11, minute=0, args=["20:00"])
            scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=13, minute=0, args=["22:00"])
            scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=15, minute=0, args=["00:00"])
            scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=17, minute=0, args=["02:00"])
            def _tip_12():
                import tips_poster
                tips_poster.run_scheduled_tip_post(slot_label="12:00")
            def _tip_18():
                import tips_poster
                tips_poster.run_scheduled_tip_post(slot_label="18:00")
            scheduler.add_job(func=_tip_12, trigger="cron", hour=3, minute=0)
            scheduler.add_job(func=_tip_18, trigger="cron", hour=9, minute=0)
            def _note_check_job():
                try:
                    import note_poster
                    note_poster.check_and_post_new_note()
                except Exception as err:
                    logging.error(f"Scheduled note promotion check failed: {err}")
            scheduler.add_job(func=_note_check_job, trigger="cron", hour=4, minute=0)
            scheduler.add_job(func=_note_check_job, trigger="cron", hour=11, minute=30)
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
            scheduler.add_job(func=run_scheduled_weekly_x_post, trigger="cron", day_of_week="sun", hour=10, minute=0)
            scheduler.start()
            trigger_background_fetch_if_needed()
            threading.Thread(target=_crawl_events_job, daemon=True).start()
            logging.info("APScheduler & local SQLite high-speed cache started successfully")
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


local_db.register_sync_complete_callback(clear_analysis_and_report_caches)


@app.route('/api/analysis/<path:store_name>')
def get_store_analysis(store_name):
    """Returns aggregated hourly and weekly data for a specific store."""
    import store_analysis
    payload, status_code = store_analysis.compute_store_analysis_payload(
        store_name,
        analysis_cache=analysis_cache,
        cache_duration=ANALYSIS_CACHE_DURATION,
    )
    return jsonify(payload), status_code


# Global storage for the latest data (thread-safe)
data_lock = threading.Lock()
latest_data = {
    'top_store': None,
    'last_updated': None,
    'full_data': []
}
LATEST_STATUS_SNAPSHOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'latest_status_cache.json')


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
            with open(LATEST_STATUS_SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            full_data = saved.get('full_data') or []
            if full_data:
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
            import store_master
            master = store_master.load()
            items = []
            latest_ts = None
            for sname, vals in snap.items():
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
                    'votes': {'smooth': 0, 'waiting': 0, 'lively': 0},
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


def _enrich_for_ssr(stores):
    """Attach display fields (brand_code, brand_cls, display_title, area_label, ladies_pct, men_pct)
    for instant server-side HTML card rendering."""
    import store_master
    import store_hours
    master = store_master.load()
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
        enriched.append(item)
    return enriched


def get_initial_status_payload():
    """Return status dict for SSR embedding in index.html if available."""
    restore_latest_data_if_empty()
    with data_lock:
        if latest_data.get('full_data'):
            return {
                'timestamp': latest_data['last_updated'],
                'ranking': _enrich_for_ssr(latest_data['full_data']),
                'status': 'success',
                'is_seed': bool(latest_data.get('is_seed'))
            }
    return None

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
# (reduces load on the official sites while nothing is happening)
def fetch_interval_sec(now_jst=None):
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return 600 if 7 <= now_jst.hour < 17 else 60

_last_fetch_at = None

# Rolling in-memory snapshots for 15-min momentum calculation
rolling_snapshots = []

def refresh_momentum_from_db():
    """Recompute women_diff / men_diff on in-memory latest_data once background Google Sheets sync completes."""
    try:
        baseline_map = local_db.get_snapshot_around(10, 25)
        if not baseline_map:
            return
        updated_any = False
        with data_lock:
            full_data = latest_data.get('full_data') or []
            if not full_data or latest_data.get('is_seed'):
                return
            for s in full_data:
                prev = baseline_map.get(s.get('name'))
                if prev:
                    s['women_diff'] = int(s.get('women', 0)) - int(prev.get('women', 0))
                    s['men_diff'] = int(s.get('men', 0)) - int(prev.get('men', 0))
                    updated_any = True
        if updated_any:
            save_latest_status_snapshot()
            print("[app] Momentum (15-min women_diff/men_diff) refreshed from newly synced SQLite baseline.")
    except Exception as e:
        print(f"[app] refresh_momentum_from_db error: {e}", file=sys.stderr)

def update_job(force=False):
    global latest_data, _last_update_error, rolling_snapshots, _last_fetch_at
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

            with data_lock:
                current_data = [] if latest_data.get('is_seed') else latest_data.get('full_data', [])
                
                # Merge new data with current data
                merged_map = {s['name']: s for s in current_data}
                for s in data:
                    sname = s['name']
                    prev = baseline_map.get(sname)
                    if prev:
                        s['women_diff'] = int(s.get('women', 0)) - int(prev.get('women', 0))
                        s['men_diff'] = int(s.get('men', 0)) - int(prev.get('men', 0))
                    else:
                        s['women_diff'] = 0
                        s['men_diff'] = 0
                    s['votes'] = votes_map.get(sname, {'smooth': 0, 'waiting': 0, 'lively': 0})
                    merged_map[sname] = s
                
                merged_data = list(merged_map.values())
                sorted_data = sorted(merged_data, key=lambda x: (x.get('region') != 'Korea', x['women'], x['men']), reverse=True)
                top_store = sorted_data[0] if sorted_data else None
                
                latest_data['top_store'] = top_store
                latest_data['full_data'] = sorted_data
                latest_data['last_updated'] = jst_now.strftime("%Y-%m-%d %H:%M:%S")
                latest_data['is_seed'] = False
            save_latest_status_snapshot()
            print(f"Data updated. Top store: {top_store['name'] if top_store else 'None'}")
            _last_update_error = None
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
                def _log_and_refresh_analysis(items):
                    logger.log_data(items)
                    try:
                        analysis_cache.get('processed_per_store', {}).clear()
                        analysis_cache.get('raw_data_by_store', {}).clear()
                    except Exception:
                        pass
                threading.Thread(target=_log_and_refresh_analysis, args=(data,), daemon=True).start()
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

@app.route('/ogp/live.png')
def dynamic_live_ogp_image():
    """Dynamically render a 1200x630 live TOP3 ranking OGP card image."""
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    s_param = (request.args.get('s') or '').strip()
    with data_lock:
        stores = list(latest_data.get('full_data') or [])
    try:
        import ogp_generator
        png_bytes = ogp_generator.generate_live_ogp_png(stores, s_param=s_param)
        from flask import Response
        resp = Response(png_bytes, mimetype='image/png')
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    except Exception as e:
        logging.error(f"Dynamic OGP generation failed, falling back to static image: {e}")
        return send_from_directory('static', 'note-eyecatch.jpg', mimetype='image/jpeg')

@app.route('/ogp/weekly.png')
def dynamic_weekly_ogp_image():
    """Dynamically render a 1200x630 weekly ranking & open-data infographic OGP card image."""
    try:
        import weekly_report
        import ogp_generator
        report = weekly_report.generate_weekly_report()
        png_bytes = ogp_generator.generate_weekly_ogp_png(report if report and 'error' not in report else {})
        from flask import Response
        resp = Response(png_bytes, mimetype='image/png')
        resp.headers['Cache-Control'] = 'public, max-age=1800'
        return resp
    except Exception as e:
        logging.error(f"Weekly OGP generation failed, falling back to static image: {e}")
        return send_from_directory('static', 'note-eyecatch.jpg', mimetype='image/jpeg')

@app.route('/guide')
@app.route('/guide/strategy')
def guide_article():
    """Official comprehensive lounge strategy editorial manual."""
    return render_template('guide_article.html')

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
    'dating_Casual': os.environ.get('AFFILIATE_URL_DATING_CASUAL') or os.environ.get('AFFILIATE_URL_DATING') or 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
    'dating_casual': os.environ.get('AFFILIATE_URL_DATING_CASUAL') or os.environ.get('AFFILIATE_URL_DATING') or 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
}

@app.route('/link/<path:item_id>')
def affiliate_redirect(item_id):
    """Safe internal redirect shielding affiliate links from ad-blockers."""
    clean_id = item_id.strip().lower()
    target_url = AFFILIATE_REDIRECTS.get(clean_id)
    if not target_url:
        return redirect(url_for('index'))
    if not _is_bot_request():
        try:
            import analytics
            ref_path = urllib_parse.urlparse(request.referrer or '').path or '-'
            analytics.track('affiliate_click', f"{clean_id} @ {ref_path}")
        except Exception:
            pass
    return redirect(target_url, code=302)

# --- First-party analytics (no personal data) ---
import urllib.parse as urllib_parse

_BOT_MARKERS = ('bot', 'spider', 'crawl', 'slurp', 'facebookexternalhit', 'preview', 'headless', 'python-requests', 'curl')
_TRACKED_PAGE_PREFIXES = ('/guide', '/columns', '/areas', '/area/', '/grooming', '/stats', '/price-comparison', '/oriental-lounge', '/ag', '/jis', '/store/')
_LIVE_NO_CACHE_PREFIXES = ('/price-comparison', '/store/', '/areas', '/area/', '/oriental-lounge', '/ag', '/jis')

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
            if (request.path in ('/', '/sw.js', '/api/status')
                    or request.path.startswith(_LIVE_NO_CACHE_PREFIXES)):
                response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
            if (request.method == 'GET' and response.status_code == 200
                    and (request.path == '/' or request.path.startswith(_TRACKED_PAGE_PREFIXES))
                    and not session.get('is_staff')
                    and not _is_bot_request()):
                import analytics
                analytics.track('page_view', request.path)
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
    """Staff-only analytics summary (page views, affiliate clicks, button clicks)."""
    if not session.get('is_staff'):
        return redirect(url_for('staff_monitor'))
    import analytics
    days = max(1, min(90, request.args.get('days', 7, type=int)))
    flushed_rows = 0
    if request.args.get('flush') == '1':
        try:
            flushed_rows = analytics.flush()
        except Exception:
            pass
    data = analytics.summary(days=days)
    if request.args.get('format') == 'json':
        return jsonify(data)
    return render_template('staff_analytics.html', data=data, days=days, flushed=request.args.get('flush') == '1', flushed_rows=flushed_rows)

@app.route('/staff', methods=['GET', 'POST'])
def staff_monitor():
    """Internal staff dashboard with all advertisements and promotions removed."""
    error = None
    if request.method == 'POST':
        entered = request.form.get('passcode', '').strip()
        if entered == STAFF_PASSCODE:
            session['is_staff'] = True
            session.permanent = True
            return redirect(url_for('staff_monitor'))
        else:
            error = 'パスコードが正しくありません'

    # Allow one-touch login via query param ?passcode=5555
    query_passcode = request.args.get('passcode', '').strip()
    if query_passcode == STAFF_PASSCODE:
        session['is_staff'] = True
        session.permanent = True
        return redirect(url_for('staff_monitor'))

    # Check if already authenticated in session
    if session.get('is_staff'):
        trigger_background_fetch_if_needed()
        import data_audit
        with data_lock:
            cur_live = list(latest_data.get('full_data') or [])
        audit_report = data_audit.run_data_audit(live_stores=cur_live, send_alerts=False)
        return render_template(
            'index.html',
            is_staff=True,
            initial_status=get_initial_status_payload(),
            published_scheduled=scheduled_articles.get_published_articles(),
            scheduled_queue=scheduled_articles.get_all_with_status(),
            data_audit_report=audit_report,
        )

    return render_template('staff_login.html', error=error)

@app.route('/api/data-audit')
def api_data_audit():
    """Staff/Admin endpoint returning automated price & business hours drift audit report."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import data_audit
    with data_lock:
        cur_live = list(latest_data.get('full_data') or [])
    report = data_audit.run_data_audit(live_stores=cur_live, send_alerts=False)
    return jsonify(report)

@app.route('/staff/logout')
def staff_logout():
    """Clear staff authentication and return to staff login."""
    session.pop('is_staff', None)
    return redirect(url_for('staff_monitor'))


def _get_sns_kit_context():
    import ig_carousel_generator
    import weekly_report
    rep = None
    try:
        rep = weekly_report.generate_weekly_report()
    except Exception:
        rep = None
    with data_lock:
        live_s = list(latest_data.get('full_data') or [])
    return ig_carousel_generator.build_forecast_context(report=rep, live_stores=live_s)


@app.route('/staff/sns-kit')
def staff_sns_kit():
    """Staff-only one-click Instagram / Threads weekend forecast carousel & caption generator."""
    if not session.get('is_staff'):
        return redirect(url_for('staff_monitor'))
    import ig_carousel_generator
    ctx = _get_sns_kit_context()
    captions = ig_carousel_generator.build_captions(ctx)
    cache_bust = int(time.time()) // 300
    return render_template('staff_sns_kit.html', ctx=ctx, captions=captions, cache_bust=cache_bust)


@app.route('/staff/sns-kit/slide/<int:slide_num>.png')
def staff_sns_kit_slide(slide_num):
    """Render individual 1080x1350 PNG slide for staff mobile save or preview."""
    if not session.get('is_staff'):
        return redirect(url_for('staff_monitor'))
    import ig_carousel_generator
    ctx = _get_sns_kit_context()
    png_bytes = ig_carousel_generator.render_slide_png(slide_num, ctx=ctx)
    headers = {'Cache-Control': 'private, max-age=300'}
    if request.args.get('download') == '1':
        headers['Content-Disposition'] = f'attachment; filename="aiseki_navi_slide_0{max(1, min(5, slide_num))}.png"'
    return Response(png_bytes, mimetype='image/png', headers=headers)


@app.route('/staff/sns-kit/download.zip')
def staff_sns_kit_zip():
    """Download all 5 portrait PNG slides + Instagram/Threads captions in one ZIP."""
    if not session.get('is_staff'):
        return redirect(url_for('staff_monitor'))
    import ig_carousel_generator
    ctx = _get_sns_kit_context()
    zip_bytes = ig_carousel_generator.render_all_slides_zip(ctx=ctx)
    today_s = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d')
    return Response(
        zip_bytes,
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="aiseki_navi_weekend_kit_{today_s}.zip"'},
    )

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
    with data_lock:
        if latest_data.get('full_data'):
            return jsonify({
                'timestamp': latest_data['last_updated'],
                'ranking': latest_data['full_data'],
                'status': 'success',
                'is_seed': bool(latest_data.get('is_seed'))
            })
        else:
            return jsonify({
                'timestamp': None,
                'ranking': [],
                'status': 'loading'
            })

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
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'pid': os.getpid(),
        'has_data': bool(latest_data['full_data']),
        'last_updated': latest_data['last_updated'],
        'threads': [t.name for t in threading.enumerate()]
    })

@app.route('/api/logs')
def view_logs():
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    if os.path.exists('app.log'):
        with open('app.log', 'r') as f:
            content = f.read()
        return Response(content, mimetype='text/plain')
    return "No logs found.", 404

@app.route('/api/thread_status')
def get_thread_status():
    """Diagnostic endpoint to check the health of the background fetcher and logger"""
    if not is_admin_authorized():
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
        return jsonify({"status": "success", "data": report})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/api/events')
def get_events():
    """Returns active events and promotions for lounge stores."""
    try:
        import events
        store = request.args.get('store', '').strip()
        force = request.args.get('refresh', '').lower() == 'true'
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

    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '0.0.0.0').split(',')[0].strip()
    ip_hash = hashlib.sha256(f"{client_ip}_aiseki_salt".encode('utf-8')).hexdigest()[:16]
    raw_voter = str(payload.get('voter_id', '')).strip()[:64]
    voter_hash = hashlib.sha256(f"v_{raw_voter}_aiseki_salt".encode('utf-8')).hexdigest()[:16] if raw_voter else ''

    ok, reason = local_db.add_crowd_vote(store_name, vote_type, ip_hash, voter_id=voter_hash)
    if ok:
        try:
            import analytics
            analytics.track('vote', vote_type)
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
    content = "User-agent: *\nAllow: /\nDisallow: /api/debug_ps\nDisallow: /api/logs\nDisallow: /api/thread_status\nDisallow: /staff\nSitemap: https://aiseki-navi.com/sitemap.xml\n"
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

@app.route('/store/<store_slug>')
def store_detail_page(store_slug):
    import store_master
    import store_hours
    import urllib.parse
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
    with data_lock:
        raw_stores = list(latest_data.get('full_data') or [])
        last_updated = latest_data.get('last_updated') or '--:--'

    sname = store_master.store_by_slug(store_slug, live_stores=raw_stores)
    if not sname:
        return redirect(url_for('index'))

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
        brand_slug = 'oriental-lounge'
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

    store_analytics = None
    try:
        store_analytics = local_db.get_store_7day_analytics(sname)
    except Exception:
        pass

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

@app.route('/sitemap.xml')
def sitemap_xml():
    import store_master
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d')
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
{scheduled_urls}{area_urls}
{store_urls}
  <url>
    <loc>https://aiseki-navi.com/privacy</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
</urlset>"""
    return Response(xml, mimetype='application/xml')

@app.route('/columns')
def columns_index():
    """Portal page listing all editorial guides, comparisons, and area features."""
    is_staff = bool(session.get('is_staff'))
    return render_template(
        'columns.html',
        area_guides=AREA_GUIDES,
        published_scheduled=scheduled_articles.get_published_articles(),
        upcoming_scheduled=scheduled_articles.get_upcoming_articles() if is_staff else [],
        is_staff=is_staff,
    )

@app.route('/stats')
def stats_whitepaper():
    """SEO-indexed primary statistical data whitepaper & weekly ranking page."""
    trigger_background_fetch_if_needed()
    report_data = None
    try:
        import weekly_report
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

@app.route('/areas')
@app.route('/area/<area_slug>')
def area_portal(area_slug=None):
    """Area portal page showing live crowd stats and local lounge guides."""
    trigger_background_fetch_if_needed()
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

@app.route('/price-comparison')
def price_comparison_page():
    """Urban cluster cross-brand price, men's event & live cost-performance comparison board."""
    import price_comparison
    restore_latest_data_if_empty()
    trigger_background_fetch_if_needed()
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

@app.route('/grooming')
def grooming_guide():
    """Dedicated editorial guide for men's grooming, fashion, and backup dating apps."""
    return render_template('grooming.html')

@app.route('/privacy')
def privacy_page():
    """Dedicated legal, privacy policy, and operator information page."""
    return render_template('privacy.html')

@app.route('/guide/weekday-strategy')
def guide_weekday_strategy():
    """Editorial guide for weekday lounge strategy."""
    return render_template('guide_weekday.html')

@app.route('/guide/brand-comparison')
@app.route('/guide/compare')
def guide_brand_comparison():
    """Editorial guide comparing Oriental Lounge, ag, and JIS."""
    return render_template('guide_comparison.html')

@app.route('/guide/shinjuku')
def guide_shinjuku():
    """Editorial guide comparing the 4 Shinjuku lounge stores."""
    return render_template('guide_shinjuku.html')

@app.route('/guide/solo-vs-pair')
def guide_solo_vs_pair():
    """Editorial guide comparing solo vs 2-person lounge visits."""
    return render_template('guide_solo_pair.html')

@app.route('/guide/second-venue')
def guide_second_venue():
    """Editorial guide on inviting to a second venue smoothly."""
    return render_template('guide_second_venue.html')

@app.route('/guide/first-10-minutes')
def guide_first_10min():
    """Editorial guide on first 10 minutes conversation and grooming."""
    return render_template('guide_first_10min.html')

@app.route('/guide/club-area-midnight')
def guide_club_area_midnight():
    """Editorial data study on dance club proximity and midnight 2nd-wave female inflows."""
    return render_template('guide_club_midnight.html')

@app.route('/guide/weekend-strategy')
def guide_weekend_strategy():
    """Editorial data strategy guide for Friday/Saturday peak hours."""
    return render_template('guide_weekend.html')

@app.route('/guide/beauty-area-stage')
def guide_beauty_area_stage():
    """Editorial data study on beauty salon districts, early-evening surges, and men's grooming."""
    return render_template('guide_beauty_stage.html')

@app.route('/guide/male-lead-mechanics')
def guide_male_lead_mechanics():
    """Editorial data study on why male-leading stores attract more women and why women no longer wait."""
    return render_template('guide_male_lead.html')


@app.route('/guide/price-event-comparison')
def guide_price_event_comparison():
    """Editorial guide explaining the 51-store price, 4-time-slot, solo rule, event & minimum 10-women comparison board."""
    return render_template('guide_price_comparison.html')


def _render_scheduled_guide(slug: str):
    """Render a scheduled editorial guide if published, or allow noindex preview for authenticated staff."""
    item = scheduled_articles.get_article_by_slug(slug)
    if not item:
        abort(404)
    if scheduled_articles.is_published(item):
        return render_template(item['template'], is_scheduled_preview=False, scheduled_item=item)
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
    """Endpoint to test or externally trigger X auto-posting."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    slot = request.args.get('slot', '20:00')
    force = request.args.get('force', '0') == '1'
    res = run_scheduled_x_post(slot_label=slot, force=force)
    return jsonify(res)


@app.route('/api/trigger-x-tip')
def trigger_x_tip_endpoint():
    """Endpoint to test or externally trigger X human-like tips posting."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    slot = request.args.get('slot', '12:00')
    force = request.args.get('force', '0') == '1'
    idx = request.args.get('idx', type=int)
    import tips_poster
    res = tips_poster.run_scheduled_tip_post(slot_label=slot, force=force, tip_index=idx)
    return jsonify(res)


@app.route('/api/trigger-note-share')
def trigger_note_share_endpoint():
    """Endpoint to test or externally trigger note promotion posting."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    idx = request.args.get('idx', 0, type=int)
    import note_poster
    res = note_poster.check_and_post_new_note(force=force, article_index=idx)
    return jsonify(res)


@app.route('/api/trigger-x-pinned')
def trigger_x_pinned_endpoint():
    """Endpoint to post the 2-post Pinned Tweet thread (1st URL-free + 2nd URL reply) to X."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import x_poster
    main_text, reply_text = x_poster.build_pinned_tweet_thread()
    res = x_poster.post_to_x(main_text, reply_text=reply_text)
    res["tweet_text"] = main_text
    res["reply_text"] = reply_text
    return jsonify(res)


@app.route('/api/trigger-x-column')
def trigger_x_column_endpoint():
    """Endpoint to post a 2-post Column Promotion thread (1st URL-free + eyecatch image + 2nd URL reply) to X."""
    if not is_admin_authorized():
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
    """Endpoint to test or externally trigger the Weekly Summary X post."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    res = run_scheduled_weekly_x_post(force=force)
    return jsonify(res)

@app.route('/api/trigger-x-bonus')
def trigger_x_bonus_endpoint():
    """Endpoint to test or externally trigger the Bonus-Time Guerrilla Alert X post."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import bonus_alert
    force = request.args.get('force', '0') == '1'
    with data_lock:
        stores = list(latest_data.get('full_data') or [])
    res = bonus_alert.check_and_post_bonus_alert(stores, force=force)
    return jsonify(res)


@app.route('/api/trigger-threads-post')
def trigger_threads_post_endpoint():
    """Endpoint to test or externally trigger Meta Threads auto-post."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import threads_poster
    slot = request.args.get('slot', '20:00')
    force = request.args.get('force', '0') == '1'
    with data_lock:
        stores = list(latest_data.get('full_data') or [])
    res = threads_poster.execute_scheduled_threads_post(stores, slot_label=slot, force=force)
    return jsonify(res)


# Start background services immediately on module import (unless disabled for unit tests)
_ensure_scheduler_started_async()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)


