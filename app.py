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
import local_db
from collections import defaultdict

from logging.handlers import RotatingFileHandler

# Set up logging with automatic size rotation (max 5MB, keep 2 backups) to prevent disk overflow
log_file_path = 'app.log'
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

@app.before_request
def start_scheduler():
    global _scheduler_started
    if not _scheduler_started:
        with _scheduler_lock:
            if not _scheduler_started:
                # Initialize local high-speed SQLite database
                local_db.init_db()
                local_db.start_background_sync(days=14)

                scheduler = BackgroundScheduler(timezone="UTC")
                scheduler.add_job(func=update_job, trigger="interval", minutes=1)
                # Background sync SQLite with Google Sheets every 2 hours to keep completely up to date
                scheduler.add_job(func=lambda: local_db.sync_from_google_sheets(days=3), trigger="interval", hours=2)
                # Daily X auto-post jobs at 20:00, 22:00, 00:00, and 02:00 JST (11:00, 13:00, 15:00, 17:00 UTC)
                scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=11, minute=0, args=["20:00"])
                scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=13, minute=0, args=["22:00"])
                scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=15, minute=0, args=["00:00"])
                scheduler.add_job(func=run_scheduled_x_post, trigger="cron", hour=17, minute=0, args=["02:00"])
                # Daily X human-like tips at 12:00 and 18:00 JST (03:00 and 09:00 UTC)
                def _tip_12():
                    import tips_poster
                    tips_poster.run_scheduled_tip_post(slot_label="12:00")
                def _tip_18():
                    import tips_poster
                    tips_poster.run_scheduled_tip_post(slot_label="18:00")
                scheduler.add_job(func=_tip_12, trigger="cron", hour=3, minute=0)
                scheduler.add_job(func=_tip_18, trigger="cron", hour=9, minute=0)
                # Daily note promotion check at 13:00 and 20:30 JST (04:00 and 11:30 UTC)
                def _note_check_job():
                    try:
                        import note_poster
                        note_poster.check_and_post_new_note()
                    except Exception as err:
                        import logging
                        logging.error(f"Scheduled note promotion check failed: {err}")
                scheduler.add_job(func=_note_check_job, trigger="cron", hour=4, minute=0)
                scheduler.add_job(func=_note_check_job, trigger="cron", hour=11, minute=30)
                # Daily events crawler at 10:00 and 17:00 JST (01:00 and 08:00 UTC)
                def _crawl_events_job():
                    try:
                        import events
                        events.fetch_all_events(force_refresh=True)
                    except Exception as err:
                        import logging
                        logging.error(f"Scheduled events crawl failed: {err}")
                scheduler.add_job(func=_crawl_events_job, trigger="cron", hour="1,8", minute=0)
                scheduler.start()
                threading.Thread(target=update_job, daemon=True).start()
                threading.Thread(target=_crawl_events_job, daemon=True).start()
                _scheduler_started = True
                import logging
                logging.info("APScheduler & local SQLite high-speed cache started successfully")

# Global storage for analysis cache
analysis_cache = {
    'last_fetched': None,
    'raw_data': None,
    'processed_per_store': {} 
}
ANALYSIS_CACHE_DURATION = 900  # Cache for 15 minutes



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
    
    # Check if processed data is still valid in in-memory cache
    if store_name in analysis_cache.get('processed_per_store', {}):
        cached = analysis_cache.get('raw_data_by_store', {}).get(store_name)
        if cached and (now - cached['last_fetched']).total_seconds() < ANALYSIS_CACHE_DURATION:
            return jsonify(analysis_cache['processed_per_store'][store_name])

    # 1. Fast Path: Query local high-speed SQLite database first (sub-millisecond)
    try:
        sqlite_history = local_db.get_store_history(store_name, days=30)
        if sqlite_history and len(sqlite_history) >= 3:
            target_data = sqlite_history
            analysis_cache['raw_data_by_store'][store_name] = {
                'data': target_data,
                'last_fetched': now
            }
    except Exception as e:
        print(f"[Analysis] SQLite query error: {e}", file=sys.stderr)

    # 2. Fallback: Query Google Sheets via gviz API if local SQLite had no data
    if target_data is None:
        try:
            print(f"Fetching historical data for {store_name} from Google Sheets via gviz...")
            access_token = logger.get_access_token()
            spreadsheet_id = logger.get_spreadsheet_id()
            if not access_token or not spreadsheet_id:
                return jsonify({"error": "Failed to authenticate"}), 500
            
            import json
            import urllib.parse
            
            safe_store_name = store_name.replace("'", "\\'")
            query = f"SELECT A, B, C, D WHERE B='{safe_store_name}'"
            url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={urllib.parse.quote(query)}"
            headers = {"Authorization": f"Bearer {access_token}"}
            
            response = logger.http_session.get(url, headers=headers, timeout=15)
            if response.status_code != 200:
                print(f"Failed to fetch data via gviz: HTTP {response.status_code}")
                return jsonify({"error": "Failed to load data"}), 500
                
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
                
                analysis_cache['raw_data_by_store'][store_name] = {
                    'data': target_data,
                    'last_fetched': now
                }
                
                if store_name in analysis_cache['processed_per_store']:
                    del analysis_cache['processed_per_store'][store_name]
                    
                # Backfill fetched records into SQLite so subsequent clicks are instantaneous
                try:
                    for d in target_data:
                        local_db.insert_logs([{'name': d['name'], 'men': d['men'], 'women': d['women'], 'source': ''}], d['ts'])
                except Exception:
                    pass

                print(f"Fetched and cached {len(target_data)} rows for {store_name}.")
            else:
                print("Invalid response from gviz API")
                return jsonify({"error": "Failed to parse data"}), 500
                
        except Exception as e:
            print(f"Error fetching sheet data for {store_name}: {e}", file=__import__('sys').stderr)
            return jsonify({"error": "Failed to load data"}), 500

    if not target_data:
        return jsonify({"error": "Store not found"}), 404
        
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
    seen_hours = set()
    
    for d in sorted_target:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            # Guard against future timestamp anomalies
            if dt > now_jst or not is_business_hours(dt):
                continue
            business_date = get_business_dt(dt).strftime("%Y-%m-%d")
            hour_key = f"{business_date} {dt.strftime('%H')}"
            if hour_key in seen_hours:
                continue
            seen_hours.add(hour_key)
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
    for region, keywords in REGIONS.items():
        for keyword in keywords:
            if keyword in store_name:
                return region
    return 'Other'

def update_job():
    global latest_data, _last_update_error
    print(f"[{datetime.datetime.now()}] Updating data...")
    try:
        data = monitor.get_all_data()
        if data:
            for store in data:
                store['region'] = detect_region(store['name'])

            with data_lock:
                current_data = latest_data.get('full_data', [])
                
                # Merge new data with current data
                merged_map = {s['name']: s for s in current_data}
                for s in data:
                    merged_map[s['name']] = s
                
                merged_data = list(merged_map.values())
                sorted_data = sorted(merged_data, key=lambda x: (x['women'], x['men']), reverse=True)
                top_store = sorted_data[0] if sorted_data else None
                
                latest_data['top_store'] = top_store
                latest_data['full_data'] = sorted_data
                jst_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
                latest_data['last_updated'] = jst_now.strftime("%Y-%m-%d %H:%M:%S")
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
                threading.Thread(target=logger.log_data, args=(data,)).start()
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
    return render_template('index.html', is_staff=False)

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
    'taxi': 'https://aiseki-navi.com/#taxi-coupon',
    'taxi_dispatch': 'https://aiseki-navi.com/#taxi-coupon',
}

@app.route('/link/<path:item_id>')
def affiliate_redirect(item_id):
    """Safe internal redirect shielding affiliate links from ad-blockers."""
    clean_id = item_id.strip().lower()
    target_url = AFFILIATE_REDIRECTS.get(clean_id)
    if not target_url:
        return redirect(url_for('index'))
    return redirect(target_url, code=302)

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
        return render_template('index.html', is_staff=True)

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
    
    with data_lock:
        last_updated = latest_data.get('last_updated')
        has_data = bool(latest_data.get('full_data'))
    
    is_stale = False
    if last_updated:
        last_time = datetime.datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S")
        current_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        if (current_jst - last_time).total_seconds() > 90:
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
                update_job()
            finally:
                with _fetch_lock:
                    _is_fetching = False
                    _fetch_started_at = None
        threading.Thread(target=background_fetch).start()

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/api/status')
def get_status():
    trigger_background_fetch_if_needed()
    with data_lock:
        if latest_data.get('full_data'):
            return jsonify({
                'timestamp': latest_data['last_updated'],
                'ranking': latest_data['full_data'],
                'status': 'success'
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

@app.route('/api/debug_ps')
def debug_ps():
    if request.args.get('secret') != 'lounge2026':
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'pid': os.getpid(),
        'has_data': bool(latest_data['full_data']),
        'last_updated': latest_data['last_updated'],
        'threads': [t.name for t in threading.enumerate()]
    })

@app.route('/api/logs')
def view_logs():
    if request.args.get('secret') != 'lounge2026':
        return jsonify({'error': 'Unauthorized'}), 401
    if os.path.exists('app.log'):
        with open('app.log', 'r') as f:
            content = f.read()
        return Response(content, mimetype='text/plain')
    return "No logs found.", 404

@app.route('/api/thread_status')
def get_thread_status():
    """Diagnostic endpoint to check the health of the background fetcher and logger"""
    if request.args.get('secret') != 'lounge2026':
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
            evs = events.get_events_for_store(store)
        else:
            evs = events.fetch_all_events(force_refresh=force)
        return jsonify({
            'status': 'success',
            'count': len(evs),
            'events': evs
        })
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/robots.txt')
def robots_txt():
    content = "User-agent: *\nAllow: /\nDisallow: /api/debug_ps\nDisallow: /api/logs\nDisallow: /api/thread_status\nDisallow: /staff\nSitemap: https://aiseki-navi.com/sitemap.xml\n"
    return Response(content, mimetype='text/plain')

@app.route('/sitemap.xml')
def sitemap_xml():
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d')
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://aiseki-navi.com/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
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
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://aiseki-navi.com/guide/brand-comparison</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.85</priority>
  </url>
</urlset>"""
    return Response(xml, mimetype='application/xml')

@app.route('/guide/weekday-strategy')
def guide_weekday_strategy():
    """Editorial guide for weekday lounge strategy."""
    return render_template('guide_weekday.html')

@app.route('/guide/brand-comparison')
@app.route('/guide/compare')
def guide_brand_comparison():
    """Editorial guide comparing Oriental Lounge, ag, and JIS."""
    return render_template('guide_comparison.html')

@app.route('/api/sns-post')
def generate_sns_post_api():
    """Returns ready-to-post viral X/SNS text from current live data."""
    post_type = request.args.get('type', 'all').lower()
    base_url = request.url_root.rstrip('/')
    stores = latest_data.get('full_data') or []
    ts = latest_data.get('last_updated') or ''
    time_part = ts.split(' ')[1] if ' ' in ts else '現在'

    if not stores:
        return jsonify({'status': 'loading', 'text': f'全国の相席ラウンジ人数ランキング「相席ナビ」\n{base_url}/'})

    if post_type == 'kanto':
        filtered = [s for s in stores if s.get('region') == 'Kanto']
        header = f"🗼【{time_part} 関東速報】いま女性が多い相席ラウンジTOP5！"
        tags = "#相席ナビ #相席ラウンジ #新宿 #渋谷 #オリエンタルラウンジ"
    elif post_type == 'kansai':
        filtered = [s for s in stores if s.get('region') == 'Kinki']
        header = f"🐙【{time_part} 関西速報】いま女性が多い相席ラウンジTOP5！"
        tags = "#相席ナビ #相席ラウンジ #梅田 #難波 #心斎橋"
    elif post_type == 'anaba':
        # High female ratio & at least 10 women
        candidates = [s for s in stores if s.get('women', 0) >= 10]
        candidates.sort(key=lambda x: (x.get('women', 0) / max(1, x.get('women', 0) + x.get('men', 0)), x.get('women', 0)), reverse=True)
        filtered = candidates
        header = f"💎【{time_part} 穴場速報】いま女性比率が高くて狙い目の相席ラウンジTOP3！"
        tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS"
    else:
        filtered = list(stores)
        header = f"🔥【{time_part} 全国速報】いま盛り上がってる相席ラウンジTOP5！"
        tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS #ag"

    limit = 5
    medals = ['🥇 1位', '🥈 2位', '🥉 3位', '4️⃣ 4位', '5️⃣ 5位']
    lines = [header, ""]
    for i, s in enumerate(filtered[:limit]):
        w = s.get('women', 0)
        m = s.get('men', 0)
        tot = w + m
        pct = round((w / tot) * 100) if tot > 0 else 0
        if post_type == 'anaba':
            lines.append(f"{medals[i]}：{s.get('name')}（女性比率{pct}% ｜ 👩女性{w}名 / 👨男性{m}名）")
        else:
            lines.append(f"{medals[i]}：{s.get('name')}（👩女性{w}名 / 👨男性{m}名・女性比率{pct}%）")

    lines.extend([
        "",
        "💡【相席ナビからの立ち回りアドバイス】",
        "満席で男性の入店待ちが発生している店舗を避け、女性比率の高い穴場店舗へハシゴ移動するのが最も勝率の高い立ち回りです！",
        "",
        "📖【2026最新】オリラジ・ag・JIS 3大ブランドの違い比較：",
        f"{base_url}/guide/brand-comparison",
        "",
        f"👇全国80店舗の今わかるリアルタイム人数＆男女比グラフ",
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
    """Execute scheduled X auto-post with duplicate prevention across workers."""
    import x_poster
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    dedup_key = f"{now_jst.strftime('%Y-%m-%d')}_{slot_label}"
    lock_file = "/tmp/aiseki_navi_last_x_post.txt"

    if not force:
        try:
            if os.path.exists(lock_file):
                with open(lock_file, "r") as f:
                    if f.read().strip() == dedup_key:
                        logging.info(f"X auto-post already sent for {dedup_key}, skipping.")
                        return {"status": "skipped", "reason": "Already posted for this slot"}
        except Exception:
            pass

    stores = latest_data.get('full_data') or []
    if not stores:
        try:
            stores, _ = monitor.get_all_data()
        except Exception as e:
            logging.error(f"Failed to fetch stores for X post: {e}")
            return {"status": "error", "detail": str(e)}

    text = x_poster.build_scheduled_tweet_text(stores, slot_label=slot_label)
    if not text:
        return {"status": "error", "detail": "Empty tweet text"}

    result = x_poster.post_to_x(text)
    if result.get("status") == "success" and not force:
        try:
            with open(lock_file, "w") as f:
                f.write(dedup_key)
        except Exception:
            pass
    result["tweet_text"] = text
    return result

@app.route('/api/trigger-x-post')
def trigger_x_post_endpoint():
    """Endpoint to test or externally trigger X auto-posting."""
    if request.args.get('secret') != 'lounge2026':
        return jsonify({'error': 'Unauthorized'}), 401
    slot = request.args.get('slot', '20:00')
    force = request.args.get('force', '0') == '1'
    res = run_scheduled_x_post(slot_label=slot, force=force)
    return jsonify(res)

@app.route('/api/trigger-x-tip')
def trigger_x_tip_endpoint():
    """Endpoint to test or externally trigger X human-like tips posting."""
    if request.args.get('secret') != 'lounge2026':
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
    if request.args.get('secret') != 'lounge2026':
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    idx = request.args.get('idx', 0, type=int)
    import note_poster
    res = note_poster.check_and_post_new_note(force=force, article_index=idx)
    return jsonify(res)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)


