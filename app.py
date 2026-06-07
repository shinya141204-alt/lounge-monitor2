from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import monitor
import atexit
import datetime
import threading
import logger
import statistics
import weekly_report
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
        
        # Parse relevant fields only to save memory
        # Row format: Timestamp, Store Name, Men, Women, Source
        data = []
        for row in all_values[1:]: # Skip header
            if len(row) >= 4:
                try:
                    data.append({
                        'ts': row[0],
                        'name': row[1],
                        'men': int(row[2]) if row[2] else 0,
                        'women': int(row[3]) if row[3] else 0
                    })
                except:
                    continue
        
        analysis_cache['raw_data'] = data
        analysis_cache['last_fetched'] = now
        analysis_cache['processed_per_store'] = {} # Clear processed cache
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
        
    # Check if we computed this store recently (in-memory optimization)
    # Since we cleared processed_per_store on fetch, this corresponds to the current raw_data version
    if store_name in analysis_cache['processed_per_store']:
        return jsonify(analysis_cache['processed_per_store'][store_name])
        
    # Filter for target store
    target_data = [d for d in data if store_name in d['name']] # Loose match or exact? Using loose for "OLG 大阪駅前" vs full name matches logic elsewhere
    
    if not target_data:
        return jsonify({"error": "Store not found"}), 404
        
    # Aggregation containers
    hourly_women = defaultdict(list)
    weekday_women = defaultdict(list)
    hourly_women_by_day = defaultdict(lambda: defaultdict(list))
    
    for d in target_data:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            
            # Hourly (0-23)
            hourly_women[dt.hour].append(d['women'])
            
            # Weekday (0=Mon, 6=Sun)
            weekday_women[dt.weekday()].append(d['women'])
            
            # Hourly by specific weekday
            hourly_women_by_day[dt.weekday()][dt.hour].append(d['women'])
        except:
            continue
            
    # Calculate averages & Raw Data
    # Hourly: Ensure 0-23 keys exist
    hourly_avg = []
    
    hourly_result = {}
    hourly_raw_display = {} # For Scatter Plot
    
    for h in range(24):
        vals = hourly_women.get(h, [])
        hourly_result[h] = round(statistics.mean(vals), 1) if vals else 0
        hourly_raw_display[h] = vals # List of all values
        
    # Weekday: 0-6
    weekday_result = {}
    weekdays_str = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for i in range(7):
        vals = weekday_women.get(i, [])
        weekday_result[weekdays_str[i]] = round(statistics.mean(vals), 1) if vals else 0
        
    # Hourly by Weekday
    # Result: { "Mon": { "0": 10, ... }, ... }
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

    # All Available Data (sampled at hourly intervals for performance)
    recent_data_women = []
    recent_data_men = []
    
    # Sort strictly by time for line graph
    sorted_target = sorted(target_data, key=lambda x: x['ts'])
    
    # Keep track of which hours we've already included (to sample 1 point per hour)
    seen_hours = set()
    
    def get_business_date(dt):
        """Get business date: 0:00-6:59 counts as previous day (business hours 18:00-7:00)"""
        if dt.hour <= 6:
            # Before 7AM = previous business day
            return (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            return dt.strftime("%Y-%m-%d")
    
    def is_business_hours(dt):
        """Check if time is within business hours (18:00-6:59)"""
        # Business hours: 18:00-23:59 or 0:00-6:59
        return dt.hour >= 18 or dt.hour <= 6
    
    for d in sorted_target:
        try:
            # Parse timestamp to check if this is a new hour
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            
            # Skip non-business hours (6:00-17:59)
            if not is_business_hours(dt):
                continue
            
            # Get business date for grouping
            business_date = get_business_date(dt)
            hour_key = f"{business_date} {dt.strftime('%H')}"  # e.g. "2024-01-28 02" (even if calendar is 1/29)
            
            # Skip if we already have data for this hour
            if hour_key in seen_hours:
                continue
            seen_hours.add(hour_key)
            
            # Store with business date for proper frontend grouping
            business_ts = f"{business_date} {dt.strftime('%H:%M:%S')}"
            
            # Format TS for easier JS parsing
            recent_data_women.append({
                'x': business_ts,
                'y': d['women']
            })
            recent_data_men.append({
                'x': business_ts,
                'y': d['men']
            })
        except:
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
    
    # Cache it
    analysis_cache['processed_per_store'][store_name] = result
    
    return jsonify(result)

# ... (debug route and main)


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
    """Detects the region based on the store name."""
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
            # Add region info
            for store in data:
                store['region'] = detect_region(store['name'])

            # Sort data by women count descending, then men count descending
            sorted_data = sorted(data, key=lambda x: (x['women'], x['men']), reverse=True)
            top_store = sorted_data[0] if sorted_data else None
            
            with data_lock:
                latest_data['top_store'] = top_store
                latest_data['full_data'] = sorted_data
                # Store as JST (UTC+9)
                jst_now = datetime.datetime.now() + datetime.timedelta(hours=9)
                latest_data['last_updated'] = jst_now.strftime("%Y-%m-%d %H:%M:%S")
            print(f"Data updated. Top store: {top_store['name'] if top_store else 'None'}")
            _last_update_error = None
            
            # --- Logging Optimization ---
            # 1. Check for Zero Data (Prevention)
            total_guests = sum(d.get('men', 0) + d.get('women', 0) for d in data)
            
            # 2. Check for Time (Business Hours: 17:00 - 07:00 JST)
            # If it is between 07:00 and 16:59, we consider it "hours to skip"
            # jst_now is already defined above
            is_off_hours = 7 <= jst_now.hour < 17
            
            # 3. Check for Frequency (Every 10 minutes)
            # To save spreadsheet space, we only log if it's been at least 10 minutes since the last log.
            # We store the last logged time in latest_data.
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
                # Log to Google Sheets (in background)
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

# Weekly archival job - runs every Sunday at 4:00 AM JST (19:00 UTC Saturday)
def run_archival():
    """Run data archival to move old data to archive sheet."""
    try:
        import archive_data
        print(f"[{datetime.datetime.now()}] Running scheduled data archival...")
        archive_data.archive_old_data(dry_run=False)
        
        # Clear the analysis cache after archival to refresh data
        global analysis_cache
        analysis_cache['raw_data'] = None
        analysis_cache['last_fetched'] = None
        analysis_cache['processed_per_store'] = {}
        print("Analysis cache cleared after archival.")
    except Exception as e:
        print(f"ERROR during archival: {e}")

scheduler.add_job(func=run_archival, trigger="cron", day_of_week="sun", hour=19, minute=0)  # 19:00 UTC = 4:00 AM JST
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
    
    # Read latest_data under lock (quick read only)
    with data_lock:
        last_updated = latest_data.get('last_updated')
        has_data = bool(latest_data.get('full_data'))
    
    # Check staleness (older than 90 seconds)
    is_stale = False
    if last_updated:
        last_time = datetime.datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S")
        current_jst = datetime.datetime.now() + datetime.timedelta(hours=9)
        if (current_jst - last_time).total_seconds() > 90:
            is_stale = True

    # If data is missing or stale, trigger a background update (atomic check-and-set)
    if not has_data or is_stale:
        with _fetch_lock:
            if _is_fetching:
                return  # Already fetching
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

@app.route('/api/status')
def get_status():
    # Trigger fetch OUTSIDE data_lock to avoid deadlock
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
    return jsonify({
        'last_updated': latest_data['last_updated'],
        'has_full_data': bool(latest_data['full_data']),
        'is_fetching': _is_fetching,
        'last_error': _last_update_error,
        'logger_error': logger.last_error
    })

@app.route('/api/debug')
def debug_status():
    # Run full data fetch to see if parsing works
    try:
        data = monitor.get_all_data()
        return jsonify({
            "count": len(data),
            "data": data,
            "connection_test": monitor.debug_connections()
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": "In get_all_data"})

@app.route('/api/weekly-report')
def get_weekly_report():
    try:
        report = weekly_report.generate_weekly_report()
        if "error" in report:
            return jsonify({"status": "error", "error": report["error"]}), 500
        return jsonify({"status": "success", "data": report})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)

