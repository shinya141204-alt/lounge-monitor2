import sqlite3
import datetime
import os
import sys
import threading
import urllib.parse
import json

DB_PATH = os.path.join(os.path.dirname(__file__), 'lounge_data.db')
_db_lock = threading.Lock()
_sync_in_progress = False

def get_connection():
    """Returns a SQLite connection configured for high-concurrency read/write."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes tables, indexes, and WAL mode for high performance."""
    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monitor_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    men INTEGER NOT NULL,
                    women INTEGER NOT NULL,
                    source TEXT,
                    UNIQUE(timestamp, store_name)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_store_ts ON monitor_logs(store_name, timestamp);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ts ON monitor_logs(timestamp);")
            conn.commit()
            print("[local_db] SQLite database initialized successfully (WAL mode enabled).")
        except Exception as e:
            print(f"[local_db] Error initializing SQLite: {e}", file=sys.stderr)
        finally:
            conn.close()

def insert_logs(items, timestamp_str):
    """
    Inserts a batch of store records into SQLite.
    items: list of dicts [{'name': '...', 'men': 10, 'women': 15, 'source': '...'}, ...]
    """
    if not items:
        return 0

    rows = []
    for item in items:
        rows.append((
            timestamp_str,
            item.get('name', ''),
            int(item.get('men', 0)),
            int(item.get('women', 0)),
            item.get('source', '')
        ))

    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.executemany("""
                INSERT OR IGNORE INTO monitor_logs (timestamp, store_name, men, women, source)
                VALUES (?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            inserted = cur.rowcount
            return inserted
        except Exception as e:
            print(f"[local_db] Insert error: {e}", file=sys.stderr)
            return 0
        finally:
            conn.close()

def get_store_history(store_name, days=30):
    """
    Retrieves history logs for a specific store from SQLite in milliseconds.
    Returns: list of dicts [{'ts': '...', 'name': '...', 'men': ..., 'women': ...}]
    """
    now_jst = datetime.datetime.now() + datetime.timedelta(hours=9)
    start_dt = now_jst - datetime.timedelta(days=days)
    start_ts = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp, store_name, men, women
            FROM monitor_logs
            WHERE store_name = ? AND timestamp >= ?
            ORDER BY timestamp ASC
        """, (store_name, start_ts))
        rows = cur.fetchall()
        return [
            {
                'ts': r['timestamp'],
                'name': r['store_name'],
                'men': r['men'],
                'women': r['women']
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[local_db] Query store history error: {e}", file=sys.stderr)
        return []
    finally:
        conn.close()

def get_weekly_history(start_ts_str):
    """
    Retrieves all store records from start_ts_str to current from SQLite.
    Returns: list of dicts [{'ts': ..., 'name': ..., 'men': ..., 'women': ..., 'source': ...}]
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp, store_name, men, women, source
            FROM monitor_logs
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (start_ts_str,))
        rows = cur.fetchall()
        return [
            {
                'ts': r['timestamp'],
                'name': r['store_name'],
                'men': r['men'],
                'women': r['women'],
                'source': r['source'] or ''
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[local_db] Query weekly history error: {e}", file=sys.stderr)
        return []
    finally:
        conn.close()

def get_db_stats():
    """Returns row count and time range from SQLite."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM monitor_logs")
        count, min_ts, max_ts = cur.fetchone()
        return {
            'count': count or 0,
            'min_timestamp': min_ts,
            'max_timestamp': max_ts
        }
    except Exception as e:
        return {'count': 0, 'error': str(e)}
    finally:
        conn.close()

def sync_from_google_sheets(days=14):
    """
    Background worker that syncs the last N days of data from Google Sheets into SQLite.
    Runs once on startup or when SQLite needs backfilling.
    """
    global _sync_in_progress
    if _sync_in_progress:
        print("[local_db] Sync already in progress, skipping.")
        return

    _sync_in_progress = True
    try:
        print(f"[local_db] Starting background sync from Google Sheets (last {days} days)...")
        import logger
        access_token = logger.get_access_token()
        spreadsheet_id = logger.get_spreadsheet_id()
        if not access_token or not spreadsheet_id:
            print("[local_db] Sync failed: cannot authenticate with Google Sheets.")
            return

        now_jst = datetime.datetime.now() + datetime.timedelta(hours=9)
        start_date = (now_jst - datetime.timedelta(days=days)).replace(hour=0, minute=0, second=0)
        start_ts_str = start_date.strftime("%Y-%m-%d %H:%M:%S")

        query = f"SELECT A, B, C, D, E WHERE A >= '{start_ts_str}'"
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={urllib.parse.quote(query)}"
        headers = {"Authorization": f"Bearer {access_token}"}

        response = logger.http_session.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"[local_db] Sync HTTP {response.status_code} error from Google Sheets.")
            return

        text = response.text
        if "google.visualization.Query.setResponse(" not in text:
            print("[local_db] Sync error: invalid response format from Sheets.")
            return

        json_str = text[text.find("{"):text.rfind("}") + 1]
        data_json = json.loads(json_str)
        rows = data_json.get('table', {}).get('rows', [])

        if not rows:
            print("[local_db] Sync: no rows returned from Google Sheets.")
            return

        batch = []
        for row in rows:
            cols = row.get('c', [])
            if len(cols) >= 4:
                ts_val = (cols[0].get('v') or cols[0].get('f')) if cols[0] else None
                name_val = cols[1].get('v') if cols[1] else None
                men_val = cols[2].get('v') if cols[2] else 0
                women_val = cols[3].get('v') if cols[3] else 0
                src_val = cols[4].get('v') if len(cols) > 4 and cols[4] else ''

                if ts_val and name_val:
                    batch.append((
                        str(ts_val),
                        str(name_val),
                        int(men_val) if men_val is not None else 0,
                        int(women_val) if women_val is not None else 0,
                        str(src_val)
                    ))

        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.executemany("""
                    INSERT OR IGNORE INTO monitor_logs (timestamp, store_name, men, women, source)
                    VALUES (?, ?, ?, ?, ?)
                """, batch)
                conn.commit()
                print(f"[local_db] Sync complete! Imported {len(batch)} rows into local SQLite.")
            finally:
                conn.close()

    except Exception as e:
        print(f"[local_db] Sync exception: {e}", file=sys.stderr)
    finally:
        _sync_in_progress = False

def start_background_sync(days=14):
    """Spawns a background thread to sync SQLite with Google Sheets without blocking startup."""
    thread = threading.Thread(target=sync_from_google_sheets, args=(days,), daemon=True)
    thread.start()
    return thread
