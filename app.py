from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import monitor
import atexit
import datetime
import threading

app = Flask(__name__)

# Global storage for the latest data (thread-safe)
data_lock = threading.Lock()
latest_data = {
    'top_store': None,
    'last_updated': None,
    'full_data': []
}



def update_job():
    global latest_data
    print(f"[{datetime.datetime.now()}] Updating data...")
    try:
        data = monitor.get_all_data()
        if data:
            # Sort data by women count descending, then men count descending
            sorted_data = sorted(data, key=lambda x: (x['women'], x['men']), reverse=True)
            top_store = sorted_data[0] if sorted_data else None
            
            with data_lock:
                latest_data['top_store'] = top_store
                latest_data['full_data'] = sorted_data
                latest_data['last_updated'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"Data updated. Top store: {top_store['name'] if top_store else 'None'}")
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
        # If no data yet, try to fetch immediately (fallback)
        if not latest_data['full_data']:
            print("No data in cache, fetching synchronously...")
            # Release lock briefly to allow update_job to run if needed, 
            # but here we just call the logic directly or call update_job
            # simpler to just call the monitor logic directly to avoid lock complexity
            try:
                data = monitor.get_all_data()
                if data:
                    sorted_data = sorted(data, key=lambda x: (x['women'], x['men']), reverse=True)
                    latest_data['top_store'] = sorted_data[0] if sorted_data else None
                    latest_data['full_data'] = sorted_data
                    latest_data['last_updated'] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

