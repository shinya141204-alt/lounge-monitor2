import sqlite3
import datetime
import os
import sys
import threading
import urllib.parse
import json

DB_PATH = os.environ.get('LOCAL_DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lounge_data.db')
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crowd_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    vote_type TEXT NOT NULL,
                    ip_hash TEXT
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_store_ts ON crowd_votes(store_name, timestamp);")
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
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
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

        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
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

# In-memory fallback for votes
_memory_votes = []
_votes_lock = threading.Lock()

def get_snapshot_around(minutes_ago_min=15, minutes_ago_max=50):
    """
    Returns {store_name: {'men': m, 'women': w}} from ~20-45 minutes ago
    to calculate real-time crowd momentum.
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    start_ts = (now_jst - datetime.timedelta(minutes=minutes_ago_max)).strftime("%Y-%m-%d %H:%M:%S")
    end_ts = (now_jst - datetime.timedelta(minutes=minutes_ago_min)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT store_name, men, women, timestamp
                FROM monitor_logs
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp DESC
            """, (start_ts, end_ts))
            rows = cur.fetchall()
            result = {}
            for r in rows:
                sname = r['store_name']
                if sname not in result:
                    result[sname] = {'men': r['men'], 'women': r['women'], 'ts': r['timestamp']}
            return result
        finally:
            conn.close()
    except Exception:
        return {}

VOTE_SHEET_TITLE = 'Crowd Votes'
VOTE_SHEET_HEADER = ['timestamp_jst', 'store_name', 'vote_type', 'voter_hash']
# 荒らし対策のレート制限（10分あたり）
VOTE_LIMIT_PER_VOTER = int(os.environ.get('VOTE_LIMIT_PER_VOTER', '8'))
VOTE_LIMIT_PER_IP = int(os.environ.get('VOTE_LIMIT_PER_IP', '40'))  # 携帯回線のIP共有を考慮して緩め


def add_crowd_vote(store_name, vote_type, ip_hash="", voter_id=""):
    """
    Adds a live user crowd report vote.
    - 重複判定: 端末ID（voter_id）があればそれを優先、なければIPハッシュで「同じ店舗・同じ種類は30分に1回」
    - レート制限: 端末ごと10分8票、IPごと10分40票（同一回線を複数人が使う携帯回線を考慮）
    - 永続化: SQLite に加え Google Sheets 'Crowd Votes' に追記（再デプロイで消えないように）
    """
    if vote_type not in ('smooth', 'waiting', 'lively'):
        return False, "invalid_type"

    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    cutoff_30m = (now_jst - datetime.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_10m = (now_jst - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    identity = voter_id or ip_hash

    with _votes_lock:
        recent_by_voter = 0
        recent_by_ip = 0
        for v in _memory_votes:
            v_identity = v.get('voter_id') or v['ip_hash']
            if (
                v['store_name'] == store_name
                and v['vote_type'] == vote_type
                and v_identity == identity
                and v['timestamp'] >= cutoff_30m
            ):
                return False, "already_voted"
            if v['timestamp'] >= cutoff_10m:
                if v_identity == identity:
                    recent_by_voter += 1
                if ip_hash and v['ip_hash'] == ip_hash:
                    recent_by_ip += 1
        if recent_by_voter >= VOTE_LIMIT_PER_VOTER or recent_by_ip >= VOTE_LIMIT_PER_IP:
            return False, "rate_limited"
        _memory_votes.append({
            'timestamp': now_str,
            'store_name': store_name,
            'vote_type': vote_type,
            'ip_hash': ip_hash,
            'voter_id': voter_id,
        })
        # Prune memory votes older than 6 hours
        cutoff_6h = (now_jst - datetime.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        _memory_votes[:] = [v for v in _memory_votes if v['timestamp'] >= cutoff_6h]

    _insert_vote_row(now_str, store_name, vote_type, identity)

    try:
        import sheets_store
        sheets_store.append_rows_async(VOTE_SHEET_TITLE, VOTE_SHEET_HEADER, [[now_str, store_name, vote_type, identity]])
    except Exception:
        pass

    return True, "ok"


def _insert_vote_row(ts, store_name, vote_type, identity):
    try:
        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash)
                    VALUES (?, ?, ?, ?)
                """, (ts, store_name, vote_type, identity))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def restore_votes_from_sheets(hours=24):
    """起動時に Sheets から直近の投票を SQLite とメモリへ復元する（SQLite が空のときのみ）。"""
    try:
        conn = get_connection()
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM crowd_votes").fetchone()[0]
        finally:
            conn.close()
        if cnt > 0:
            return 0
        import sheets_store
        rows = sheets_store.read_rows(VOTE_SHEET_TITLE, VOTE_SHEET_HEADER)
    except Exception as e:
        print(f"[local_db] vote restore skipped: {e}", file=sys.stderr)
        return 0

    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    cutoff = (now_jst - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    restored = 0
    for r in rows:
        if len(r) < 3 or not r[0] or r[0] < cutoff:
            continue
        if r[2] not in ('smooth', 'waiting', 'lively'):
            continue
        identity = r[3] if len(r) > 3 else ''
        _insert_vote_row(r[0], r[1], r[2], identity)
        with _votes_lock:
            _memory_votes.append({'timestamp': r[0], 'store_name': r[1], 'vote_type': r[2],
                                  'ip_hash': identity, 'voter_id': identity})
        restored += 1
    print(f"[local_db] Restored {restored} crowd votes from Google Sheets.")
    return restored

def get_recent_votes(store_name=None, hours=4):
    """Returns aggregated vote counts in the last `hours` hours."""
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    cutoff_str = (now_jst - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    counts_by_store = {}

    def _ensure(sname):
        if sname not in counts_by_store:
            counts_by_store[sname] = {'smooth': 0, 'waiting': 0, 'lively': 0}

    used_db = False
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if store_name:
                cur.execute("""
                    SELECT store_name, vote_type, COUNT(*) as cnt
                    FROM crowd_votes
                    WHERE store_name = ? AND timestamp >= ?
                    GROUP BY store_name, vote_type
                """, (store_name, cutoff_str))
            else:
                cur.execute("""
                    SELECT store_name, vote_type, COUNT(*) as cnt
                    FROM crowd_votes
                    WHERE timestamp >= ?
                    GROUP BY store_name, vote_type
                """, (cutoff_str,))
            rows = cur.fetchall()
            for r in rows:
                sname = r['store_name']
                vtype = r['vote_type']
                _ensure(sname)
                if vtype in counts_by_store[sname]:
                    counts_by_store[sname][vtype] = int(r['cnt'])
            used_db = True
        finally:
            conn.close()
    except Exception:
        pass

    if not used_db:
        with _votes_lock:
            for v in _memory_votes:
                if v['timestamp'] >= cutoff_str and (not store_name or v['store_name'] == store_name):
                    sname = v['store_name']
                    _ensure(sname)
                    vtype = v['vote_type']
                    if vtype in counts_by_store[sname]:
                        counts_by_store[sname][vtype] += 1

    if store_name:
        return counts_by_store.get(store_name, {'smooth': 0, 'waiting': 0, 'lively': 0})
    return counts_by_store
