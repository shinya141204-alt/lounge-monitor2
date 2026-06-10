from flask import Flask, render_template, jsonify, request, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
import monitor
import atexit
import datetime
import threading
import logger
import statistics
from collections import defaultdict

app = Flask(__name__)

# Global storage for analysis cache
analysis_cache = {
    'last_fetched': None,
    'raw_data': None,
    'processed_per_store': {} 
}
ANALYSIS_CACHE_DURATION = 900  # Cache for 15 minutes

def get_sheet_data():
    """Fetches data from Google Sheets with caching."""
    global analysis_cache
    
    now = datetime.datetime.now()
    
    # Check cache validity
    if analysis_cache['raw_data'] and analysis_cache['last_fetched']:
        time_diff = (now - analysis_cache['last_fetched']).total_seconds()
        if time_diff < ANALYSIS_CACHE_DURATION:
            return analysis_cache['raw_data']
            
    # Fetch new data
    try:
        print("Fetching historical data from Google Sheets...")
        client = logger.get_client()
        if not client:
            return None
            
        sheet = client.open('Lounge Monitor Data').sheet1
        all_values = sheet.get_all_values()
        
        data = []
        for row in all_values[1:]:
            if len(row) >= 4:
                try:
                    data.append({
                        'ts': row[0],
                        'name': row[1],
                        'men': int(row[2]) if row[2] else 0,
                        'women': int(row[3]) if row[3] else 0
                    })
                except Exception as e:
                    print(f"Error parsing historical data: {e}", file=__import__('sys').stderr)
                    continue
        
        analysis_cache['raw_data'] = data
        analysis_cache['last_fetched'] = now
        analysis_cache['processed_per_store'] = {}
        print(f"Cached {len(data)} rows of historical data.")
        return data
        
    except Exception as e:
        print(f"Error fetching sheet data: {e}")
        return None

@app.route('/api/analysis/<path:store_name>')
def get_store_analysis(store_name):
    """Returns aggregated hourly and weekly data for a specific store."""
    data = get_sheet_data()
    if not data:
        return jsonify({"error": "Failed to load data"}), 500
        
    if store_name in analysis_cache['processed_per_store']:
        return jsonify(analysis_cache['processed_per_store'][store_name])
        
    target_data = [d for d in data if store_name in d['name']]
    
    if not target_data:
        return jsonify({"error": "Store not found"}), 404
        
    hourly_women = defaultdict(list)
    weekday_women = defaultdict(list)
    hourly_women_by_day = defaultdict(lambda: defaultdict(list))
    
    for d in target_data:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            hourly_women[dt.hour].append(d['women'])
            weekday_women[dt.weekday()].append(d['women'])
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
    weekdays_str = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for i in range(7):
        vals = weekday_women.get(i, [])
        weekday_result[weekdays_str[i]] = round(statistics.mean(vals), 1) if vals else 0
        
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
        import traceback
        _last_update_error = traceback.format_exc()
        print(f"Error during update: {e}")

# Create scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=update_job, trigger="interval", minutes=1)
scheduler.start()

# Determine initial data immediately in a separate thread so startup isn't blocked
threading.Thread(target=update_job).start()

# Shutdown scheduler on exit
atexit.register(lambda: scheduler.shutdown())

@app.route('/')
def index():
    return render_template('index.html')

_is_fetching = False
_last_update_error = None
_fetch_lock = threading.Lock()

def trigger_background_fetch_if_needed():
    """Check if data is stale and trigger a background fetch if needed.
    MUST be called WITHOUT holding data_lock to avoid deadlock."""
    global _is_fetching
    
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
            if _is_fetching:
                return
            _is_fetching = True
        
        print("Data missing or stale. Triggering background fetch in worker...")
        def background_fetch():
            global _is_fetching
            try:
                update_job()
            finally:
                with _fetch_lock:
                    _is_fetching = False
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
    trigger_background_fetch_if_needed()
    return jsonify({'status': 'ok'})

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
        'logger_error': str(logger.last_error) if logger.last_error else None
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
