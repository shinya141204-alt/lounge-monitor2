from flask import Flask, render_template, jsonify, request, send_from_directory, Response, redirect, session, url_for
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
import local_db
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
    """Automatically gzip-compress JSON and HTML responses for faster transfer."""
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

    # Cache static assets (JS, CSS, images, fonts) aggressively for instant mobile loading
    if request.path.startswith('/static/') and response.status_code == 200:
        if any(request.path.lower().endswith(ext) for ext in ('.js', '.css', '.jpg', '.jpeg', '.png', '.webp', '.svg', '.gif', '.ico', '.woff2')):
            response.headers['Cache-Control'] = 'public, max-age=604800, immutable'

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
                    events.fetch_all_events(force_refresh=True)
                except Exception as err:
                    logging.error(f"Scheduled events crawl failed: {err}")
            scheduler.add_job(func=_crawl_events_job, trigger="cron", hour="1,8", minute=0)
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
    global analysis_cache
    
    # Initialize cache format if needed
    if 'raw_data_by_store' not in analysis_cache:
        analysis_cache['raw_data_by_store'] = {}
    if 'processed_per_store' not in analysis_cache:
        analysis_cache['processed_per_store'] = {}
        
    now = datetime.datetime.now()
    target_data = None
    sqlite_history = []
    
    # Check if processed data is still valid in in-memory cache
    if store_name in analysis_cache.get('processed_per_store', {}):
        cached = analysis_cache.get('raw_data_by_store', {}).get(store_name)
        ttl = cached.get('ttl', ANALYSIS_CACHE_DURATION) if cached else ANALYSIS_CACHE_DURATION
        if cached and (now - cached['last_fetched']).total_seconds() < ttl:
            return jsonify(analysis_cache['processed_per_store'][store_name])

    # 1. Fast Path: Query local high-speed SQLite database first (sub-millisecond)
    try:
        sqlite_history = local_db.get_store_history(store_name, days=30) or []
        if len(sqlite_history) >= 3 and (not local_db.is_partial_recent_only() or local_db.has_multi_day_span(sqlite_history)):
            target_data = sqlite_history
            analysis_cache['raw_data_by_store'][store_name] = {
                'data': target_data,
                'last_fetched': now,
                'ttl': ANALYSIS_CACHE_DURATION
            }
    except Exception as e:
        print(f"[Analysis] SQLite query error: {e}", file=sys.stderr)

    # 2. Fallback: Query Google Sheets via gviz API if local SQLite had no data or only partial 2h boot data
    if target_data is None:
        try:
            access_token = logger.get_access_token()
            spreadsheet_id = logger.get_spreadsheet_id()
            if not access_token or not spreadsheet_id:
                if sqlite_history:
                    target_data = sqlite_history
                else:
                    return jsonify({"error": "Failed to authenticate"}), 500
            else:
                print(f"Fetching historical data for {store_name} from Google Sheets via gviz...")
                import json
                import urllib.parse
                
                safe_store_name = store_name.replace("'", "\\'")
                query = f"SELECT A, B, C, D WHERE B='{safe_store_name}'"
                url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={urllib.parse.quote(query)}"
                headers = {"Authorization": f"Bearer {access_token}"}
                
                response = logger.http_session.get(url, headers=headers, timeout=15)
                if response.status_code != 200:
                    print(f"Failed to fetch data via gviz: HTTP {response.status_code}")
                    if sqlite_history:
                        target_data = sqlite_history
                    else:
                        return jsonify({"error": "Failed to load data"}), 500
                else:
                    text = response.text
                    if "google.visualization.Query.setResponse(" in text:
                        json_str = text[text.find("{"):text.rfind("}")+1]
                        data_json = json.loads(json_str)
                        rows = data_json.get('table', {}).get('rows', [])
                        
                        target_data = []
                        for row in rows:
                            cols = row.get('c', [])
                            if len(cols) >= 4:
                                ts_val = (cols[0].get('v') or cols[0].get('f')) if cols[0] else None
                                name_val = cols[1].get('v') if cols[1] else None
                                men_val = cols[2].get('v') if cols[2] else 0
                                women_val = cols[3].get('v') if cols[3] else 0
                                
                                target_data.append({
                                    'ts': ts_val,
                                    'name': name_val,
                                    'men': int(men_val) if men_val is not None else 0,
                                    'women': int(women_val) if women_val is not None else 0
                                })
                        
                        # Backfill fetched records into SQLite in a single batch transaction so subsequent clicks are instantaneous
                        try:
                            local_db.insert_logs_with_ts(target_data)
                        except Exception:
                            pass

                        if sqlite_history and len(sqlite_history) > len(target_data):
                            target_data = local_db.get_store_history(store_name, days=30) or target_data

                        analysis_cache['raw_data_by_store'][store_name] = {
                            'data': target_data,
                            'last_fetched': now,
                            'ttl': ANALYSIS_CACHE_DURATION
                        }
                        
                        if store_name in analysis_cache['processed_per_store']:
                            del analysis_cache['processed_per_store'][store_name]

                        print(f"Fetched and cached {len(target_data)} rows for {store_name}.")
                    elif sqlite_history:
                        target_data = sqlite_history
                    else:
                        print("Invalid response from gviz API")
                        return jsonify({"error": "Failed to parse data"}), 500
                
        except Exception as e:
            print(f"Error fetching sheet data for {store_name}: {e}", file=__import__('sys').stderr)
            if sqlite_history:
                target_data = sqlite_history
            else:
                return jsonify({"error": "Failed to load data"}), 500

    if not target_data:
        return jsonify({"error": "Store not found"}), 404

    if store_name not in analysis_cache.get('raw_data_by_store', {}):
        is_complete = local_db.is_full_sync_completed() or local_db.has_multi_day_span(target_data)
        analysis_cache['raw_data_by_store'][store_name] = {
            'data': target_data,
            'last_fetched': now,
            'ttl': ANALYSIS_CACHE_DURATION if is_complete else 60
        }
        
    def get_business_dt(dt):
        """Treat 00:00 - 06:59 as belonging to the previous business day."""
        if dt.hour <= 6:
            return dt - datetime.timedelta(days=1)
        return dt

    def is_business_hours(dt):
        return dt.hour >= 18 or dt.hour <= 6

    def is_peak_hours(dt):
        """Peak lounge hours: 20:00 - 01:59"""
        return dt.hour >= 20 or dt.hour <= 1

    hourly_women = defaultdict(list)
    hourly_men = defaultdict(list)
    weekday_women = defaultdict(list)
    weekday_men = defaultdict(list)
    weekday_peak_women = defaultdict(list)
    weekday_peak_men = defaultdict(list)
    hourly_women_by_day = defaultdict(lambda: defaultdict(list))
    hourly_men_by_day = defaultdict(lambda: defaultdict(list))
    
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)

    for d in target_data:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            if dt > now_jst or not is_business_hours(dt):
                continue
            biz_dt = get_business_dt(dt)
            b_weekday = biz_dt.weekday()
            
            hourly_women[dt.hour].append(d['women'])
            hourly_men[dt.hour].append(d['men'])
            weekday_women[b_weekday].append(d['women'])
            weekday_men[b_weekday].append(d['men'])
            if is_peak_hours(dt):
                weekday_peak_women[b_weekday].append(d['women'])
                weekday_peak_men[b_weekday].append(d['men'])
            hourly_women_by_day[b_weekday][dt.hour].append(d['women'])
            hourly_men_by_day[b_weekday][dt.hour].append(d['men'])
        except Exception as e:
            continue
            
    hourly_result = {}
    hourly_men_result = {}
    for h in range(24):
        w_vals = hourly_women.get(h, [])
        m_vals = hourly_men.get(h, [])
        hourly_result[h] = round(statistics.mean(w_vals), 1) if w_vals else 0
        hourly_men_result[h] = round(statistics.mean(m_vals), 1) if m_vals else 0
        
    weekday_result = {}
    weekday_men_result = {}
    weekdays_str = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for i in range(7):
        # Prefer peak-hour averages if available so off-peak 18:00/05:00 zeros don't dilute the weekday impression
        w_vals = weekday_peak_women.get(i) or weekday_women.get(i, [])
        m_vals = weekday_peak_men.get(i) or weekday_men.get(i, [])
        weekday_result[weekdays_str[i]] = round(statistics.mean(w_vals), 1) if w_vals else 0
        weekday_men_result[weekdays_str[i]] = round(statistics.mean(m_vals), 1) if m_vals else 0
        
    hourly_by_weekday_result = {}
    hourly_men_by_weekday_result = {}
    
    for i in range(7):
        day_avg_w = {}
        day_avg_m = {}
        for h in range(24):
            w_vals = hourly_women_by_day[i].get(h, [])
            m_vals = hourly_men_by_day[i].get(h, [])
            day_avg_w[h] = round(statistics.mean(w_vals), 1) if w_vals else 0
            day_avg_m[h] = round(statistics.mean(m_vals), 1) if m_vals else 0
        hourly_by_weekday_result[weekdays_str[i]] = day_avg_w
        hourly_men_by_weekday_result[weekdays_str[i]] = day_avg_m

    recent_data_women = []
    recent_data_men = []
    sorted_target = sorted(target_data, key=lambda x: x['ts'])
    seen_minutes = set()
    
    for d in sorted_target:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            # Guard against future timestamp anomalies
            if dt > now_jst or not is_business_hours(dt):
                continue
            business_date = get_business_dt(dt).strftime("%Y-%m-%d")
            min_key = f"{business_date} {dt.strftime('%H:%M')}"
            if min_key in seen_minutes:
                continue
            seen_minutes.add(min_key)
            business_ts = f"{business_date} {dt.strftime('%H:%M:%S')}"
            recent_data_women.append({'x': business_ts, 'y': d['women']})
            recent_data_men.append({'x': business_ts, 'y': d['men']})
        except (ValueError, KeyError):
            continue
        
    result = {
        "store_name": store_name,
        "hourly": hourly_result,
        "hourly_men": hourly_men_result,
        "weekday": weekday_result,
        "weekday_men": weekday_men_result,
        "hourly_by_weekday": hourly_by_weekday_result,
        "hourly_men_by_weekday": hourly_men_by_weekday_result,
        "recent_trend": recent_data_women,
        "recent_trend_men": recent_data_men,
        "sample_count": len(target_data)
    }
    
    analysis_cache['processed_per_store'][store_name] = result
    return jsonify(result)


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
        is_korea = (s.get('region') == 'Korea') or ('カンナム' in s.get('name', '')) or ('ホンデ' in s.get('name', ''))
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
    'spu': 'https://h.accesstrade.net/sp/cc?rk=0100oo8500p144',
    'spu_styleup': 'https://h.accesstrade.net/sp/cc?rk=0100oo8500p144',
    'orbis': 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
    'mens_skincare_clinic': 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX',
    'hair_removal': 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
    'mens_hair_removal': 'https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D',
    'dating': 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
    'dating_instant': 'https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9',
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
_TRACKED_PAGE_PREFIXES = ('/guide', '/columns', '/areas', '/area/', '/grooming', '/stats', '/oriental-lounge', '/ag', '/jis', '/store/')

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
        elif (request.method == 'GET' and response.status_code == 200
                and (request.path == '/' or request.path.startswith(_TRACKED_PAGE_PREFIXES))
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
        return render_template('index.html', is_staff=True, initial_status=get_initial_status_payload())

    return render_template('staff_login.html', error=error)

@app.route('/staff/logout')
def staff_logout():
    """Clear staff authentication and return to staff login."""
    session.pop('is_staff', None)
    return redirect(url_for('staff_monitor'))

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

AREA_GUIDES = {
    'shinjuku': {
        'slug': 'shinjuku',
        'name': '新宿・西新宿エリア',
        'short_name': '新宿・西新宿',
        'icon': '🗼',
        'keywords': ['新宿', 'SHINJUKU', 'NISHISHINJUKU'],
        'region_code': 'Kanto',
        'lead': '日本一の激戦区。オリエンタルラウンジ・ag・JIS（東口・西口）の4店舗が徒歩圏内に集まっており、店舗ごとの男女比の差が一番出やすいエリアです。',
        'atmosphere': '歌舞伎町・東口側の「オリエンタルラウンジ新宿」「ag新宿」「JIS新宿」は20代前半〜後半まで圧倒的な集客力があります。一方、西口側の「JIS西新宿」は東口が満席のときでも落ち着いて入れることが多く、仕事帰りのOL層や大人っぽい雰囲気を求める方に人気です。',
        'best_time': '金・土曜の21:30以降は東口3店舗で男性の入店待ちが発生しやすいため、20:00〜21:00頃の早めに入るか、男女比を見ながら西新宿へ回るのがスムーズです。水曜・木曜の21時前後は待ち時間も少なく、かなり狙い目です。',
        'second_spot': '東口なら靖国通り・三丁目方面の落ち着いたバルやダイニングバー、西新宿なら野村ビル周辺や西口ガード近くの静かなバーが2軒目に移動しやすくて定番です。'
    },
    'shibuya': {
        'slug': 'shibuya',
        'name': '渋谷・恵比寿エリア',
        'short_name': '渋谷・恵比寿',
        'icon': '✨',
        'keywords': ['渋谷', '恵比寿', 'Shibuya', 'Ebisu'],
        'region_code': 'Kanto',
        'lead': 'トレンド感のある20代が集まる渋谷3店舗と、落ち着いた大人の街・恵比寿。同じ山手線沿線でもお店によって空気感がガラッと変わります。',
        'atmosphere': '「ag渋谷」は20代前半の学生やアパレル・美容系などノリの良い層が多く、「オリエンタルラウンジ渋谷本店・渋谷駅前」は20代半ばのOL層まで幅広く入っています。1駅隣の「オリエンタルラウンジ恵比寿」は30代前後の落ち着いた男女が多く、ゆっくり会話を楽しみたい夜に向いています。',
        'best_time': '渋谷駅前店と本店は徒歩数分の距離なので、行く直前に『相席ナビ』で女性比率が高い方を選ぶだけで待ち時間をかなり減らせます。恵比寿は木曜・金曜の20:30〜22:30頃が特にいい雰囲気です。',
        'second_spot': '渋谷なら道玄坂上の隠れ家カフェバーや宮益坂方面、恵比寿なら駅西口の恵比寿横丁周辺や少し歩いた裏路地のワインバーへ誘うと喜ばれます。'
    },
    'tokyo-east': {
        'slug': 'tokyo-east',
        'name': '上野・錦糸町・町田・東京近郊エリア',
        'short_name': '上野・錦糸町・町田',
        'icon': '🏮',
        'keywords': ['上野', '町田', 'Ueno', 'Machida'],
        'region_code': 'Kanto',
        'lead': '新宿や渋谷ほど気取らず、地元の常連や沿線ユーザーが集まってアットホームに盛り上がりやすい穴場エリアです。',
        'atmosphere': '上野は「オリエンタルラウンジ上野」と「ag上野」の2店舗があり、北千住・秋葉原・埼玉方面からのアクセスが良いため、気さくで話しやすい女性が多いのが特徴です。町田も小田急・横浜線沿線の20代が集まり、週末は都心に負けない盛り上がりを見せます。',
        'best_time': '19:30〜21:30頃の1軒目利用が多く、終電が都心部より少し早めな分、20時台に入店しておくとゆっくり話して2軒目にも誘いやすくなります。',
        'second_spot': '上野なら仲町通り周辺のきれいめな居酒屋やバー、町田なら原町田エリアのダイニングバーやカフェが徒歩すぐで使いやすいです。'
    },
    'kanto': {
        'slug': 'kanto',
        'name': '横浜・大宮・北関東エリア',
        'short_name': '横浜・大宮・北関東',
        'icon': '🌉',
        'keywords': ['横浜', '大宮', '柏', '宇都宮', '高崎', 'Yokohama', 'Omiya', 'OMIYA', 'Kashiwa', 'Utsunomiya', 'Takasaki'],
        'region_code': 'Kanto',
        'lead': 'わざわざ都心まで出なくても、各ターミナル駅前で非常に高い集客力を誇る関東の人気エリア群です。',
        'atmosphere': '特に大宮（オリエンタルラウンジ大宮・JIS大宮）と横浜（オリエンタルラウンジ横浜）は全国ランキングでも常に上位に入るマンモスエリアです。柏・宇都宮・高崎も地元エリアで唯一の大型ラウンジとして週末・平日問わず安定した来店があります。',
        'best_time': '大宮はオリエンタルラウンジとJISが近くにあるため、その日の人数バランスを見比べて選ぶのが鉄則です。20:00〜22:00頃が一番バランス良く座れます。',
        'second_spot': '横浜なら西口五番街〜鶴屋町方面のおしゃれなバル、大宮なら東口・南銀周辺の落ち着いた個室ダイニングやバーが定番ルートです。'
    },
    'osaka': {
        'slug': 'osaka',
        'name': '大阪（梅田・難波・心斎橋）・関西エリア',
        'short_name': '大阪・京都・神戸',
        'icon': '🐙',
        'keywords': ['大阪', '梅田', '茶屋町', '難波', '心斎橋', '天満', '京都', '神戸', 'Osaka', 'Umeda', 'UMEDA', 'Chayamachi', 'CHAYAMACHI', 'Namba', 'NAMBA', 'Shinsaibashi', 'Tenma', 'Kyoto', 'Kobe'],
        'region_code': 'Kinki',
        'lead': 'キタ（梅田・茶屋町・天満）とミナミ（難波・心斎橋）、さらに京都・神戸三宮まで、ノリの良さと店舗数の多さが魅力の関西エリア。',
        'atmosphere': '梅田・茶屋町エリアは「オリエンタルラウンジ梅田・茶屋町」「JIS梅田」「ヤタコイ茶屋町」が揃い、仕事帰りのOLから学生まで華やかな層が集まります。心斎橋・難波はさらにフレンドリーで盛り上がりやすく、京都・神戸三宮は少し落ち着いた上品な雰囲気があります。',
        'best_time': '関西エリアは金曜・土曜だけでなく水曜・木曜の夜もかなり人が入ります。梅田周辺は徒歩圏内に複数店舗があるため、待ち時間が出ている店舗を避けて移動するのがコツです。',
        'second_spot': '梅田ならお初天神裏参道や茶屋町の夜景が見えるバー、難波・心斎橋なら裏なんばや東心斎橋の隠れ家バーが2軒目にぴったりです。'
    },
    'nagoya': {
        'slug': 'nagoya',
        'name': '名古屋（栄・錦）・静岡・北陸エリア',
        'short_name': '名古屋・静岡・金沢',
        'icon': '🏯',
        'keywords': ['名古屋', '栄', '錦', '静岡', '浜松', '金沢', 'Nagoya', 'Shizuoka', 'Hamamatsu', 'Kanazawa'],
        'region_code': 'Chubu',
        'lead': '東海最大の歓楽街である名古屋（栄・錦）を中心に、静岡・浜松・金沢の人気店舗が集まる中部・北陸エリア。',
        'atmosphere': '名古屋は「オリエンタルラウンジ名古屋栄」「錦」「ag名古屋」の3店舗が同じ栄・錦エリアに集中しています。若い世代とワイワイ飲むならag名古屋、きれいめなOL層と落ち着いて飲むなら栄店・錦店と使い分けがしやすいのが魅力です。静岡・浜松・金沢も週末は100名近く集まることがある活気ある店舗です。',
        'best_time': '名古屋の3店舗はすべて徒歩数分圏内なので、向かう前に『相席ナビ』で3店舗の男女比を見比べるのが一番効果的です。20:30〜22:30頃がコアタイムです。',
        'second_spot': '栄・錦エリアなら住吉方面のカジュアルなバルや、錦三丁目の落ち着いたオーセンティックバーへ流れるのがスマートです。'
    },
    'fukuoka': {
        'slug': 'fukuoka',
        'name': '福岡（天神・小倉）・九州・沖縄エリア',
        'short_name': '福岡・九州・沖縄',
        'icon': '🍜',
        'keywords': ['福岡', '天神', '小倉', '長崎', '大分', '熊本', '宮崎', '鹿児島', '沖縄', 'Fukuoka', 'FUKUOKA', 'Kokura', 'Nagasaki', 'Oita', 'Kumamoto', 'KUMAMOTO', 'Miyazaki', 'Kagoshima', 'Okinawa'],
        'region_code': 'Kyushu',
        'lead': '全国屈指の盛り上がりを見せる福岡・天神エリアをはじめ、熊本・小倉・宮崎・鹿児島・沖縄那覇まで揃う九州エリア。',
        'atmosphere': '福岡・天神エリアには「オリエンタルラウンジ福岡」「ag福岡」「JIS福岡」の3大ブランドがすべて揃っており、平日でも女性が多く入ることで有名です。熊本もオリエンタルラウンジとJISの2店舗体制で活気があり、沖縄那覇は地元の方だけでなく旅行・出張客も多くて独特の開放感があります。',
        'best_time': '福岡・天神は20時台から女性の来店ペースが早く、21時〜23時頃にはかなり賑わいます。早めの時間に1軒目として入るのがおすすめです。',
        'second_spot': '天神・大名エリアはおしゃれな立ち飲みや隠れ家バー、屋台が徒歩すぐの場所に無数にあるため、2軒目への移動のしやすさは全国トップクラスです。'
    },
    'hokkaido-tohoku': {
        'slug': 'hokkaido-tohoku',
        'name': '札幌・仙台・岡山・広島・松山エリア',
        'short_name': '札幌・仙台・中四国',
        'icon': '❄️',
        'keywords': ['札幌', '仙台', '岡山', '広島', '松山', 'Sapporo', 'SAPPORO', 'Sendai', 'Okayama', 'OKAYAMA', 'Hiroshima', 'HIROSHIMA', 'CLOVERS', 'Matsuyama', 'MATSUYAMA'],
        'region_code': 'Hokkaido',
        'lead': 'すすきの（札幌）、国分町（仙台）、岡山、流川（広島）、大街道（松山）など、各地方の中心歓楽街で圧倒的な存在感を放つエリア。',
        'atmosphere': '札幌は「オリエンタルラウンジ札幌」「ag札幌」「JIS札幌」の3店舗、仙台は「オリエンタルラウンジ仙台」「ag仙台」の2店舗があり、その日の客層や人数で選べます。また岡山は「オリエンタルラウンジ」「XIX」、広島は「オリエンタルラウンジ」「ALFA」「CLOVERS」と地場の人気ブランドも揃っており、比較しながらお店を選べます。',
        'best_time': '金・土曜の21:00〜翌0:00はもちろん、木曜の20:30頃も女性比率が高くなりやすく、落ち着いて話せる穴場タイムです。',
        'second_spot': 'すすきの・国分町・流川など、どの店舗も歓楽街のど真ん中に位置しているため、お店を出て徒歩1〜2分圏内で2軒目のバーやカフェに困ることがありません。'
    }
}

def get_stores_for_area(area_slug, all_stores):
    area = AREA_GUIDES.get(area_slug)
    if not area:
        return []
    import store_master
    kws = area['keywords']
    matched = []
    for s in all_stores:
        sname = s.get('name', '')
        info = store_master.get(sname)
        if info is not None:
            # Store master decides the area (e.g. 'OLG 錦糸町' must not match Nagoya's '錦')
            if info.get('area_slug') == area_slug:
                matched.append(s)
        elif any(k in sname for k in kws):
            matched.append(s)
    matched.sort(key=lambda x: (x.get('women', 0), x.get('men', 0)), reverse=True)
    return matched

BRAND_GUIDES = {
    'oriental-lounge': {
        'slug': 'oriental-lounge',
        'name': 'オリエンタルラウンジ',
        'seo_title': 'オリエンタルラウンジ全店のリアルタイム人数・男女比・混雑状況一覧',
        'seo_description': 'オリエンタルラウンジ全店（新宿・渋谷・恵比寿・上野・梅田・心斎橋・難波・名古屋・福岡など）のリアルタイム男女人数・男女比・待ち時間傾向を毎分自動更新で一覧表示。今から入りやすい穴場店舗が3秒でわかります。',
        'seo_keywords': 'オリエンタルラウンジ 人数,オリエンタルラウンジ 男女比,オリエンタルラウンジ 混雑,オリエンタルラウンジ リアルタイム,オリエンタルラウンジ 待ち時間',
        'h1': '👑 オリエンタルラウンジ全店のリアルタイム人数・男女比一覧',
        'lead': '全国のオリエンタルラウンジ各店舗の現在人数（女性・男性）と女性比率をリアルタイム集計しています。店舗カードをタップすると、各店舗の混雑傾向と徒歩圏内のハシゴ候補店舗を確認できます。',
        'strategy_text_1': 'オリエンタルラウンジは20代半ば〜30代前半の社会人・OL層を中心に、落ち着いた高級感ある空間で会話を楽しめる王道ブランドです。金・土曜の21:00〜23:00は人気店舗で男性の入店待ちが発生しやすいため、入店前に本ページで「女性比率が55%以上の店舗」を選ぶのが鉄則です。',
        'strategy_text_2': '新宿・渋谷・梅田・名古屋・福岡など複数店舗が近いエリアでは、オリエンタルラウンジが男性過多の際に徒歩数分の「ag（アグ）」や「JIS（ジス）」へ移動するだけで、待ち時間ゼロで即相席できるケースが多くあります。',
        'faq_tip': '入店前に相席ナビでオリエンタルラウンジ各店の女性比率を確認し、女性が男性より多い店舗を選ぶか、平日の20時〜22時・週末の19時〜20時半の早い時間帯に入店するのが最も効果的です。'
    },
    'ag': {
        'slug': 'ag',
        'name': 'ag（アグ）',
        'seo_title': 'ag（アグ）全店のリアルタイム人数・男女比・混雑状況一覧',
        'seo_description': 'オリエンタルラウンジ系列の若手向け相席ラウンジ「ag（アグ）」全店（渋谷・上野・梅田・名古屋・広島・金沢・仙台・沖縄）のリアルタイム男女人数・男女比・混雑状況を毎分自動更新で無料公開中。',
        'seo_keywords': 'ag 人数,アグ 相席 人数,ag 男女比,ag 渋谷 人数,ag 梅田 人数,オリエンタルラウンジ ag 違い',
        'h1': '🥂 ag（アグ）全店のリアルタイム人数・男女比一覧',
        'lead': '「25歳以下（または若年層中心）」のカジュアルでノリの良い新世代ラウンジ『ag（アグ）』全店の現在人数ランキングです。深夜帯や2軒目・3軒目でも盛り上がりやすいのが特徴です。',
        'strategy_text_1': 'ag（アグ）は20代前半〜中盤の学生・アパレル・美容系などフットワークの軽い男女が多く、1軒目だけでなく22時〜24時以降の「2回転目」でも一気に人数が増える傾向があります。',
        'strategy_text_2': '同エリアにある「オリエンタルラウンジ」と徒歩数分で行き来できる店舗が多いため、まずは両方のリアルタイム人数を見比べて、女性が多い方から入店するのが最もスマートな立ち回りです。',
        'faq_tip': 'ag（アグ）は21時以降に急激に人が増えるため、入店直前にリアルタイム男女比をチェックし、同エリアのオリエンタルラウンジ本館と人数バランスを見比べてから向かうのがおすすめです。'
    },
    'jis': {
        'slug': 'jis',
        'name': 'JIS（ジス）',
        'seo_title': 'JIS（ジス）全店のリアルタイム人数・男女比・混雑状況一覧',
        'seo_description': '人気相席バー「JIS（ジス）」全店（新宿・西新宿・大宮・梅田・茶屋町・難波・福岡・熊本・松山・札幌）のリアルタイム男女人数・男女比・混雑状況を毎分自動更新で一覧表示。',
        'seo_keywords': 'JIS 人数,ジス 相席 人数,JIS 新宿 人数,JIS 梅田 人数,JIS 大宮 人数,相席ラウンジ 男女比',
        'h1': '🍸 JIS（ジス）全店のリアルタイム人数・男女比一覧',
        'lead': '札幌・大宮・新宿・西新宿・梅田・茶屋町・難波・福岡・熊本・松山で圧倒的な集客力を誇る『JIS（ジス）』全店のリアルタイム男女人数一覧です。',
        'strategy_text_1': 'JIS（ジス）は主要ターミナル駅前の大型店舗が多く、平日・週末を問わず安定した女性来店数を誇る人気ブランドです。特に新宿（歌舞伎町・西新宿）や大阪（梅田・茶屋町・難波）ではエリア内で複数店舗を展開しています。',
        'strategy_text_2': '東口側や本館が混雑している場合でも、もう一方の店舗（例：JIS西新宿やJIS茶屋町）は女性優位でスムーズに座れることが多いため、移動前の比較にぜひ活用してください。',
        'faq_tip': '新宿や梅田のように近隣に複数のJISやオリエンタルラウンジがあるエリアでは、入店前に各店舗の女性比率を見比べて55%以上の店舗を選ぶと待ち時間を回避できます。'
    }
}

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
            matched.append({
                'name': sname,
                'seo_name': store_master.seo_name(sname),
                'slug': store_master.slug_of(sname),
                'region': reg,
                'area_label': info.get('area_label') or s_live.get('area') or '全国',
                'women': int(s_live.get('women', 0)),
                'men': int(s_live.get('men', 0)),
                'women_diff': int(s_live.get('women_diff', 0) or 0),
                'men_diff': int(s_live.get('men_diff', 0) or 0),
            })

    matched.sort(key=lambda x: (x.get('region') != 'Korea', x['women'], x['men']), reverse=True)
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

    bkey = store_master.brand_key_of(sname)
    if bkey == 'oriental-lounge':
        brand_label = 'オリエンタルラウンジ'
        brand_slug = 'oriental-lounge'
    elif bkey == 'ag':
        brand_label = 'ag（アグ）'
        brand_slug = 'ag'
    elif bkey == 'jis':
        brand_label = 'JIS（ジス）'
        brand_slug = 'jis'
    else:
        brand_label = info.get('brand') or '相席ラウンジ'
        brand_slug = 'oriental-lounge'

    if tot == 0:
        crowd_comment = '営業時間外・オープン準備中'
    elif women > men:
        crowd_comment = f'女性優位・即相席チャンス🔥（女性+{women - men}名）'
    elif women == men:
        crowd_comment = '男女比1:1 バランス良好✨'
    else:
        crowd_comment = '男性多め・近隣店舗との比較推奨💡'

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
                nearby_stores.append({
                    'name': oname,
                    'seo_name': store_master.seo_name(oname),
                    'slug': store_master.slug_of(oname),
                    'women': ow,
                    'men': om,
                    'women_diff': int(other_s.get('women_diff', 0) or 0),
                    'men_diff': int(other_s.get('men_diff', 0) or 0),
                    'wpct': round((ow / otot) * 100) if otot > 0 else 0,
                })

    store_obj = {
        'name': sname,
        'seo_name': store_master.seo_name(sname),
        'slug': store_master.slug_of(sname),
        'area_label': info.get('area_label') or s_live.get('area') or '全国',
        'brand_label': brand_label,
        'brand_slug': brand_slug,
        'women': women,
        'men': men,
        'women_diff': int(s_live.get('women_diff', 0) or 0),
        'men_diff': int(s_live.get('men_diff', 0) or 0),
        'total': tot,
        'wpct': wpct if tot > 0 else 0,
        'crowd_comment': crowd_comment,
        'map_url': map_url,
    }

    store_analytics = None
    try:
        store_analytics = local_db.get_store_7day_analytics(sname)
    except Exception:
        pass

    return render_template(
        'store_detail.html',
        store=store_obj,
        store_analytics=store_analytics,
        area_info=area_info,
        nearby_stores=nearby_stores,
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
{area_urls}
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
    return render_template('columns.html', area_guides=AREA_GUIDES)

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

@app.route('/api/sns-post')
def generate_sns_post_api():
    """Returns ready-to-post viral X/SNS text from current live data."""
    post_type = request.args.get('type', 'all').lower()
    base_url = request.url_root.rstrip('/')
    stores = [s for s in (latest_data.get('full_data') or []) if s.get('region') != 'Korea']
    ts = latest_data.get('last_updated') or ''
    time_part = ts.split(' ')[1] if ' ' in ts else '現在'

    if not stores:
        return jsonify({'status': 'loading', 'text': f'全国の相席ラウンジ人数ランキング「相席ナビ」\n{base_url}/'})

    if post_type == 'kanto':
        filtered = [s for s in stores if s.get('region') == 'Kanto']
        header = f"{time_part}時点、関東でいま女性が多く入っている上位5店舗🗼"
        tags = "#相席ナビ #相席ラウンジ #新宿 #渋谷 #オリエンタルラウンジ"
    elif post_type == 'kansai':
        filtered = [s for s in stores if s.get('region') == 'Kinki']
        header = f"{time_part}時点、関西でいま女性が多く入っている上位5店舗🐙"
        tags = "#相席ナビ #相席ラウンジ #梅田 #難波 #心斎橋"
    elif post_type == 'anaba':
        # High female ratio & at least 10 women
        candidates = [s for s in stores if s.get('women', 0) >= 10]
        candidates.sort(key=lambda x: (x.get('women', 0) / max(1, x.get('women', 0) + x.get('men', 0)), x.get('women', 0)), reverse=True)
        filtered = candidates
        header = f"{time_part}時点、いま女性比率が高めで入りやすい穴場店舗✨"
        tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS"
    else:
        filtered = list(stores)
        header = f"{time_part}時点、全国でいま人が集まっている相席ラウンジTOP5🍻"
        tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS #ag"

    limit = 5
    medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣']
    lines = [header, ""]
    for i, s in enumerate(filtered[:limit]):
        w = s.get('women', 0)
        m = s.get('men', 0)
        tot = w + m
        pct = round((w / tot) * 100) if tot > 0 else 0
        if post_type == 'anaba':
            lines.append(f"{medals[i]} {s.get('name')}（女性比{pct}% ｜ 女性{w}名 / 男性{m}名）")
        else:
            lines.append(f"{medals[i]} {s.get('name')}（女性{w}名 / 男性{m}名・女性比{pct}%）")

    lines.extend([
        "",
        "💡 ひとことメモ",
        "1店舗目で男性待ちが長そうなときは、近くの女性比率が高めの店舗に回る方がスムーズに座れます。",
        "",
        "📖 オリエンタルラウンジ・ag・JISの違いまとめ：",
        f"{base_url}/guide/brand-comparison",
        "",
        f"👇全店舗の現在人数・男女比グラフ",
        f"{base_url}/",
        "",
        tags
    ])
    return jsonify({
        'status': 'success',
        'type': post_type,
        'timestamp': ts,
        'text': "\n".join(lines)
    })

def run_scheduled_x_post(slot_label="20:00", force=False):
    """Execute scheduled X auto-post with duplicate prevention that survives restarts/redeploys."""
    import x_poster
    import job_state
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    # Slots after midnight (00:00 / 02:00) belong to the previous business night
    biz_date = (now_jst - datetime.timedelta(hours=7)).strftime('%Y-%m-%d')
    dedup_key = f"x_post:{biz_date}_{slot_label}"

    if not force and job_state.is_done(dedup_key):
        logging.info(f"X auto-post already sent for {dedup_key}, skipping.")
        return {"status": "skipped", "reason": "Already posted for this slot"}

    stores = latest_data.get('full_data') or []
    if not stores:
        try:
            stores = monitor.get_all_data()
        except Exception as e:
            logging.error(f"Failed to fetch stores for X post: {e}")
            return {"status": "error", "detail": str(e)}

    text = x_poster.build_scheduled_tweet_text(stores, slot_label=slot_label)
    if not text:
        return {"status": "error", "detail": "Empty tweet text"}

    reply_text = x_poster.build_scheduled_reply_text(slot_label=slot_label)
    result = x_poster.post_to_x(text, reply_text=reply_text)
    if result.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
    elif result.get("status") == "error":
        try:
            import health
            health.send_alert(f"xpost:{dedup_key}", f"⚠️ X自動投稿（{slot_label}）に失敗しました: {str(result.get('detail') or result)[:200]}")
        except Exception:
            pass
    result["tweet_text"] = text
    result["reply_text"] = reply_text
    return result

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
    """Endpoint to post a 2-post Column Promotion thread (1st URL-free + 2nd URL reply) to X."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    idx = request.args.get('idx', 0, type=int)
    import x_poster
    main_text, reply_text, item = x_poster.build_column_tweet_thread(col_index=idx)
    res = x_poster.post_to_x(main_text, reply_text=reply_text)
    res["column"] = item
    res["tweet_text"] = main_text
    res["reply_text"] = reply_text
    return jsonify(res)

def run_scheduled_weekly_x_post(force=False):
    """Execute weekly summary X auto-post (with dynamic /ogp/weekly.png card)."""
    import x_poster
    import weekly_report
    import job_state
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    iso_year, iso_week, _ = now_jst.isocalendar()
    dedup_key = f"x_weekly:{iso_year}-W{iso_week:02d}"

    if not force and job_state.is_done(dedup_key):
        logging.info(f"Weekly X auto-post already sent for {dedup_key}, skipping.")
        return {"status": "skipped", "reason": "Already posted for this week"}

    try:
        report = weekly_report.generate_weekly_report()
    except Exception as e:
        logging.error(f"Failed to generate weekly report for X post: {e}")
        return {"status": "error", "detail": str(e)}

    if not report or "error" in report:
        return {"status": "error", "detail": str(report.get("error") if report else "Empty report")}

    main_text, reply_text = x_poster.build_weekly_report_tweet_thread(report)
    if not main_text:
        return {"status": "error", "detail": "Empty weekly tweet text"}

    result = x_poster.post_to_x(main_text, reply_text=reply_text)
    if result.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
    result["tweet_text"] = main_text
    result["reply_text"] = reply_text
    return result

@app.route('/api/trigger-x-weekly')
def trigger_x_weekly_endpoint():
    """Endpoint to test or externally trigger the Weekly Summary X post."""
    if not is_admin_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    res = run_scheduled_weekly_x_post(force=force)
    return jsonify(res)

# Start background services immediately on module import (unless disabled for unit tests)
_ensure_scheduler_started_async()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)


