from flask import Flask, render_template, jsonify, request, send_from_directory, Response
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import monitor
import atexit
import datetime
import threading
import logger
import statistics
import os
from collections import defaultdict

# Set up logging to a file so we can view it via API
log_file_path = 'app.log'
logging.basicConfig(
    filename=log_file_path,
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
# Also log to stderr for Render
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

app = Flask(__name__)

_scheduler_started = False
_scheduler_lock = threading.Lock()

@app.before_request
def start_scheduler():
    global _scheduler_started
    if not _scheduler_started:
        with _scheduler_lock:
            if not _scheduler_started:
                scheduler = BackgroundScheduler(timezone="UTC")
                scheduler.add_job(func=update_job, trigger="interval", minutes=1)
                scheduler.start()
                threading.Thread(target=update_job, daemon=True).start()
                _scheduler_started = True
                import logging
                logging.info("APScheduler started in worker process")

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
        
    now = datetime.datetime.now()
    target_data = None
    
    # Check cache
    if store_name in analysis_cache['raw_data_by_store']:
        cached = analysis_cache['raw_data_by_store'][store_name]
        if (now - cached['last_fetched']).total_seconds() < ANALYSIS_CACHE_DURATION:
            target_data = cached['data']
            
    if target_data is None:
        try:
            print(f"Fetching historical data for {store_name} from Google Sheets via gviz...")
            client = logger.get_client()
            access_token = logger.get_access_token()
            if not client or not access_token:
                return jsonify({"error": "Failed to authenticate"}), 500
                
            sheet = client.open('Lounge Monitor Data')
            spreadsheet_id = sheet.id
            
            import requests
            import json
            import urllib.parse
            
            query = f"SELECT * WHERE B='{store_name}'"
            url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={urllib.parse.quote(query)}"
            headers = {"Authorization": f"Bearer {access_token}"}
            
            response = requests.get(url, headers=headers)
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
                        # Extract string/number values correctly handling nulls
                        ts_val = cols[0].get('v') if cols[0] else None
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
                
                # Invalidate the processed cache for this store so it gets recomputed
                if 'processed_per_store' not in analysis_cache:
                    analysis_cache['processed_per_store'] = {}
                if store_name in analysis_cache['processed_per_store']:
                    del analysis_cache['processed_per_store'][store_name]
                    
                print(f"Fetched and cached {len(target_data)} rows for {store_name}.")
            else:
                print("Invalid response from gviz API")
                return jsonify({"error": "Failed to parse data"}), 500
                
        except Exception as e:
            print(f"Error fetching sheet data for {store_name}: {e}", file=__import__('sys').stderr)
            return jsonify({"error": "Failed to load data"}), 500

    if not target_data:
        return jsonify({"error": "Store not found"}), 404
        
    hourly_women = defaultdict(list)
    weekday_women = defaultdict(list)
    weekday_men = defaultdict(list)
    hourly_women_by_day = defaultdict(lambda: defaultdict(list))
    
    for d in target_data:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            hourly_women[dt.hour].append(d['women'])
            weekday_women[dt.weekday()].append(d['women'])
            weekday_men[dt.weekday()].append(d['men'])
            hourly_women_by_day[dt.weekday()][dt.hour].append(d['women'])
        except Exception as e:
            print(f"Error analyzing historical data: {e}", file=__import__('sys').stderr)
            continue
            
    hourly_result = {}
    hourly_raw_display = {}
    
    for h in range(24):
        vals = hourly_women.get(h, [])
        hourly_result[h] = round(statistics.mean(vals), 1) if vals else 0
        hourly_raw_display[h] = vals
        
    weekday_result = {}
    weekday_men_result = {}
    weekdays_str = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for i in range(7):
        w_vals = weekday_women.get(i, [])
        m_vals = weekday_men.get(i, [])
        weekday_result[weekdays_str[i]] = round(statistics.mean(w_vals), 1) if w_vals else 0
        weekday_men_result[weekdays_str[i]] = round(statistics.mean(m_vals), 1) if m_vals else 0
        
    hourly_by_weekday_result = {}
    hourly_raw_by_weekday_result = {}
    
    for i in range(7):
        day_avg = {}
        day_raw = {}
        for h in range(24):
            vals = hourly_women_by_day[i].get(h, [])
            day_avg[h] = round(statistics.mean(vals), 1) if vals else 0
            day_raw[h] = vals
        hourly_by_weekday_result[weekdays_str[i]] = day_avg
        hourly_raw_by_weekday_result[weekdays_str[i]] = day_raw

    recent_data_women = []
    recent_data_men = []
    sorted_target = sorted(target_data, key=lambda x: x['ts'])
    seen_hours = set()
    
    def get_business_date(dt):
        if dt.hour <= 6:
            return (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            return dt.strftime("%Y-%m-%d")
    
    def is_business_hours(dt):
        return dt.hour >= 18 or dt.hour <= 6
    
    for d in sorted_target:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            if not is_business_hours(dt):
                continue
            business_date = get_business_date(dt)
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
        "hourly_raw": hourly_raw_display,
        "weekday": weekday_result,
        "weekday_men": weekday_men_result,
        "hourly_by_weekday": hourly_by_weekday_result,
        "hourly_raw_by_weekday": hourly_raw_by_weekday_result,
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
                jst_now = datetime.datetime.now() + datetime.timedelta(hours=9)
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
    return render_template('index.html')

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
        current_jst = datetime.datetime.now() + datetime.timedelta(hours=9)
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
    import os
    import threading
    return jsonify({
        'pid': os.getpid(),
        'module_id': module_id,
        'has_data': bool(latest_data['full_data']),
        'last_updated': latest_data['last_updated'],
        'threads': [t.name for t in threading.enumerate()]
    })

@app.route('/api/logs')
def view_logs():
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)
