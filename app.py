from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import monitor
import atexit
import datetime
import threading
import logger

app = Flask(__name__)

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
    'Tohoku': ['Sendai', '仙台'],
    'Kanto': ['Shibuya', 'Ebisu', 'Shinjuku', 'Ueno', 'Kashiwa', 'Machida', 'Yokohama', 'Omiya', 'Utsunomiya', 'Takasaki', '渋谷', '恵比寿', '新宿', '上野', '柏', '町田', '横浜', '大宮', '宇都宮', '高崎', 'OMIYA', 'SHINJUKU', 'NISHISHINJUKU'],
    'Chubu': ['Nagoya', 'Shizuoka', 'Hamamatsu', 'Kanazawa', '名古屋', '静岡', '浜松', '金沢'],
    'Kinki': ['Osaka', 'Umeda', 'Tenma', 'Shinsaibashi', 'Namba', 'Kyoto', 'Kobe', 'Chayamachi', '大阪', '梅田', '天満', '心斎橋', '難波', '京都', '神戸', '茶屋町', 'UMEDA', 'NAMBA', 'CHAYAMACHI'],
    'Chugoku': ['Okayama', 'Hiroshima', '岡山', '広島', 'OKAYAMA', 'HIROSHIMA'],
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
    global latest_data
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
            
            # --- Logging Optimization ---
            # 1. Check for Zero Data (Prevention)
            total_guests = sum(d.get('men', 0) + d.get('women', 0) for d in data)
            
            # 2. Check for Time (Business Hours: 17:00 - 07:00 JST)
            # If it is between 07:00 and 16:59, we consider it "hours to skip"
            # jst_now is already defined above
            is_off_hours = 7 <= jst_now.hour < 17
            
            # 3. Check for Frequency (Every 10 minutes)
            # To save spreadsheet space, we only log when the minute is 0, 10, 20, 30, 40, 50.
            is_logging_time = jst_now.minute % 10 == 0

            if total_guests == 0:
                print(f"Skipping logging: Total guest count is 0.")
            elif is_off_hours:
                print(f"Skipping logging: Current time ({jst_now.strftime('%H:%M')}) is out of business hours (17:00-07:00).")
            elif not is_logging_time:
                print(f"Skipping logging: Interval optimization (Minute {jst_now.minute} is not divisible by 10).")
            else:
                # Log to Google Sheets (in background)
                threading.Thread(target=logger.log_data, args=(data,)).start()
                print("Logging data to Google Sheets...")
        else:
            print("No data retrieved.")
    except Exception as e:
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

@app.route('/api/status')
def get_status():
    global latest_data
    with data_lock:
        # Check staleness (older than 90 seconds?)
        is_stale = False
        if latest_data['last_updated']:
            last_time = datetime.datetime.strptime(latest_data['last_updated'], "%Y-%m-%d %H:%M:%S")
            # Convert current server time to JST for comparison if last_updated is in JST
            # Actually, standardizing on comparing objects is safer, but for now stick to manual offset
            # This comparison logic checks relative time so timezone offset matters less as long as consistent
            # BUT if we change storage to JST, we must compare against JST
            current_jst = datetime.datetime.now() + datetime.timedelta(hours=9)
            if (current_jst - last_time).total_seconds() > 90:
                is_stale = True
        
        # If no data or stale, fetch synchronously
        if not latest_data['full_data'] or is_stale:
            print(f"Data missing or stale (Stale: {is_stale}), fetching synchronously...")
            try:
                data = monitor.get_all_data()
                if data:
                    # Add region info (FIX: ensure region is present even on sync fetch)
                    for store in data:
                        store['region'] = detect_region(store['name'])

                    sorted_data = sorted(data, key=lambda x: (x['women'], x['men']), reverse=True)
                    latest_data['top_store'] = sorted_data[0] if sorted_data else None
                    latest_data['full_data'] = sorted_data
                    # Store as JST
                    jst_now = datetime.datetime.now() + datetime.timedelta(hours=9)
                    latest_data['last_updated'] = jst_now.strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                print(f"Sync update failed: {e}")

        return jsonify({
            'timestamp': latest_data['last_updated'],
            'ranking': latest_data['full_data'],
            'status': 'success' if latest_data['full_data'] else 'no_data'
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)

