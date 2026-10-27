import sqlite3
import datetime
import os
import sys
import time
import threading
import urllib.parse
import json
import requests

DB_PATH = os.environ.get('LOCAL_DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lounge_data.db')
_db_lock = threading.Lock()
_sync_in_progress = False
_full_sync_completed = False
_partial_recent_only = False
_sync_complete_callbacks = []
_store_7d_cache = {}
_STORE_7D_TTL = 180.0
_live_benchmarks_cache = {}
_LIVE_BENCHMARKS_TTL = 600.0
_live_benchmarks_computing = False
_live_benchmarks_lock = threading.Lock()


def clear_store_7d_cache(store_name: str = None, clear_benchmarks: bool = False):
    if store_name:
        _store_7d_cache.pop((DB_PATH, store_name), None)
    else:
        _store_7d_cache.clear()
    if clear_benchmarks:
        _live_benchmarks_cache.clear()


def refresh_live_benchmarks_async():
    """Recompute 30-day live benchmarks in a background daemon thread so HTTP requests never block."""
    global _live_benchmarks_computing
    with _live_benchmarks_lock:
        if _live_benchmarks_computing:
            return
        _live_benchmarks_computing = True

    def _worker():
        global _live_benchmarks_computing
        try:
            get_all_stores_live_benchmarks(allow_blocking=True, force_refresh=True)
        except Exception as err:
            print(f"[local_db] refresh_live_benchmarks_async error: {err}", file=sys.stderr)
        finally:
            with _live_benchmarks_lock:
                _live_benchmarks_computing = False

    threading.Thread(target=_worker, daemon=True).start()


def is_full_sync_completed() -> bool:
    return _full_sync_completed


def is_partial_recent_only() -> bool:
    """True when only the 2-hour fast boot sync has populated SQLite and multi-day sync hasn't finished yet."""
    return _partial_recent_only


def register_sync_complete_callback(cb):
    if callable(cb) and cb not in _sync_complete_callbacks:
        _sync_complete_callbacks.append(cb)


def has_multi_day_span(rows: list, min_hours: float = 20.0) -> bool:
    """Returns True if `rows` (list of dicts with 'ts') spans at least `min_hours` hours."""
    if not rows or len(rows) < 3:
        return False
    try:
        first_ts = rows[0].get('ts') or rows[0].get('timestamp') or ''
        last_ts = rows[-1].get('ts') or rows[-1].get('timestamp') or ''
        dt1 = datetime.datetime.strptime(str(first_ts), "%Y-%m-%d %H:%M:%S")
        dt2 = datetime.datetime.strptime(str(last_ts), "%Y-%m-%d %H:%M:%S")
        return abs((dt2 - dt1).total_seconds()) >= min_hours * 3600.0
    except Exception:
        return len(rows) >= 120

def get_connection():
    """Returns a SQLite connection configured for high-concurrency read/write."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-16000;")
        conn.execute("PRAGMA mmap_size=67108864;")
    except Exception:
        pass
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
            clear_store_7d_cache()
            return inserted
        except Exception as e:
            print(f"[local_db] Insert error: {e}", file=sys.stderr)
            return 0
        finally:
            conn.close()


def insert_logs_with_ts(items):
    """
    Batch-inserts records where each item has its own 'ts' field.
    Splits large backfills into 1,500-row micro-batches to avoid holding _db_lock / GIL for long.
    """
    if not items:
        return 0

    rows = []
    for item in items:
        ts = item.get('ts') or item.get('timestamp')
        name = item.get('name') or item.get('store_name')
        if not ts or not name:
            continue
        rows.append((
            str(ts),
            str(name),
            int(item.get('men', 0) or 0),
            int(item.get('women', 0) or 0),
            str(item.get('source', '') or '')
        ))

    if not rows:
        return 0

    total_inserted = 0
    chunk_size = 1500
    for offset in range(0, len(rows), chunk_size):
        sub_rows = rows[offset:offset + chunk_size]
        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.executemany("""
                    INSERT OR IGNORE INTO monitor_logs (timestamp, store_name, men, women, source)
                    VALUES (?, ?, ?, ?, ?)
                """, sub_rows)
                conn.commit()
                if cur.rowcount and cur.rowcount > 0:
                    total_inserted += cur.rowcount
            except Exception as e:
                print(f"[local_db] Batch insert error: {e}", file=sys.stderr)
                return total_inserted
            finally:
                conn.close()
        if offset + chunk_size < len(rows):
            time.sleep(0.015)

    clear_store_7d_cache()
    return total_inserted

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
        conn.row_factory = None
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
                'ts': r[0],
                'name': r[1],
                'men': r[2],
                'women': r[3]
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
        conn.row_factory = None
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
                'ts': r[0],
                'name': r[1],
                'men': r[2],
                'women': r[3],
                'source': r[4] or ''
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

def _fetch_gviz_rows_and_insert(query: str, timeout: int = 35) -> int:
    """Executes a Google Sheets gviz query and inserts returned rows into SQLite."""
    import logger
    access_token = logger.get_access_token()
    spreadsheet_id = logger.get_spreadsheet_id()
    if not access_token or not spreadsheet_id:
        return 0

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={urllib.parse.quote(query)}"
    headers = {"Authorization": f"Bearer {access_token}"}
    session = getattr(logger, 'http_session', requests)
    response = session.get(url, headers=headers, timeout=timeout)
    if response.status_code != 200:
        return 0

    text = response.text
    if "google.visualization.Query.setResponse(" not in text:
        return 0

    json_str = text[text.find("{"):text.rfind("}") + 1]
    data_json = json.loads(json_str)
    rows = data_json.get('table', {}).get('rows', [])
    if not rows:
        return 0

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

    if not batch:
        return 0

    chunk_size = 1500
    for idx in range(0, len(batch), chunk_size):
        sub_batch = batch[idx:idx + chunk_size]
        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.executemany("""
                    INSERT OR IGNORE INTO monitor_logs (timestamp, store_name, men, women, source)
                    VALUES (?, ?, ?, ?, ?)
                """, sub_batch)
                conn.commit()
            finally:
                conn.close()
        if idx + chunk_size < len(batch):
            time.sleep(0.015)

    clear_store_7d_cache()
    return len(batch)


def sync_recent_from_google_sheets(hours=2):
    """
    Fast synchronous/priority sync of the last N hours (~600 rows, <0.5s) from Google Sheets
    so that 30-min momentum (women_diff / men_diff) is immediately available on cold start / redeploy.
    """
    global _partial_recent_only
    try:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        start_dt = now_jst - datetime.timedelta(hours=hours)
        start_ts_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        query = f"SELECT A, B, C, D, E WHERE A >= '{start_ts_str}'"
        count = _fetch_gviz_rows_and_insert(query, timeout=12)
        if count:
            if not _full_sync_completed:
                _partial_recent_only = True
            print(f"[local_db] Fast recent sync complete! Imported {count} rows (last {hours}h).")
        return count
    except Exception as e:
        print(f"[local_db] Fast recent sync error: {e}", file=sys.stderr)
        return 0


def sync_store_from_google_sheets(store_name: str, days: int = 14) -> int:
    """
    On-demand fast backfill for a single store (~1,000 rows, <0.6s) when a store detail page
    or modal is opened before the 14-day background sync has completed.
    """
    if not store_name:
        return 0
    try:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        start_dt = (now_jst - datetime.timedelta(days=days)).replace(hour=0, minute=0, second=0)
        start_ts_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        safe_store = store_name.replace("'", "\\'")
        query = f"SELECT A, B, C, D, E WHERE B = '{safe_store}' AND A >= '{start_ts_str}'"
        count = _fetch_gviz_rows_and_insert(query, timeout=15)
        if count:
            print(f"[local_db] Store backfill complete for {store_name}: {count} rows.")
        return count
    except Exception as e:
        print(f"[local_db] Store backfill error for {store_name}: {e}", file=sys.stderr)
        return 0


def _notify_sync_complete():
    try:
        get_all_stores_live_benchmarks(allow_blocking=True, force_refresh=True)
    except Exception:
        pass
    for cb in list(_sync_complete_callbacks):
        try:
            cb()
        except Exception as err:
            print(f"[local_db] sync complete callback error: {err}", file=sys.stderr)


def sync_from_google_sheets(days=30, on_recent_synced=None):
    """
    Background worker that syncs the last N days of data from Google Sheets into SQLite in chunks
    so that large multi-week datasets (~100k rows) never hit single-request HTTP timeouts.
    """
    global _sync_in_progress, _full_sync_completed, _partial_recent_only
    if _sync_in_progress:
        print("[local_db] Sync already in progress, skipping.")
        return

    _sync_in_progress = True
    try:
        # Step 1: Prioritize the last 2 hours first so 15-min momentum works within 0.5s of boot
        imported_recent = sync_recent_from_google_sheets(hours=2)
        if imported_recent and callable(on_recent_synced):
            try:
                on_recent_synced()
            except Exception as cb_err:
                print(f"[local_db] on_recent_synced callback error: {cb_err}", file=sys.stderr)

        print(f"[local_db] Starting chunked background sync from Google Sheets (last {days} days)...")
        import logger
        if not logger.get_access_token() or not logger.get_spreadsheet_id():
            print("[local_db] Sync failed: cannot authenticate with Google Sheets.")
            return

        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        total_imported = 0

        # Shortcut: if local SQLite already covers the requested history window, only sync delta since max_ts
        try:
            target_min_str = (now_jst - datetime.timedelta(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d 00:00:00")
            conn = get_connection()
            try:
                cur = conn.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM monitor_logs")
                cnt_row = cur.fetchone()
            finally:
                conn.close()
            if cnt_row and int(cnt_row[0] or 0) >= 8000 and cnt_row[1] and cnt_row[2] and str(cnt_row[1]) <= target_min_str:
                delta_query = f"SELECT A, B, C, D, E WHERE A >= '{cnt_row[2]}'"
                delta_imported = _fetch_gviz_rows_and_insert(delta_query, timeout=25)
                _partial_recent_only = False
                _full_sync_completed = True
                if delta_imported > 0:
                    _notify_sync_complete()
                print(f"[local_db] Fast delta sync complete! Local DB already had {cnt_row[0]} rows; imported {delta_imported} delta rows.")
                return
        except Exception:
            pass

        # Step 2: Sync in 3-to-4 day chunks from newest to oldest so multi-day logs are available within seconds
        chunk_days = 4
        cursor_end = now_jst
        remaining_days = max(1, int(days))
        first_chunk = True

        while remaining_days > 0:
            step = min(3 if first_chunk else chunk_days, remaining_days)
            cursor_start = (cursor_end - datetime.timedelta(days=step)).replace(hour=0, minute=0, second=0)
            start_str = cursor_start.strftime("%Y-%m-%d %H:%M:%S")
            end_str = cursor_end.strftime("%Y-%m-%d %H:%M:%S")

            if first_chunk:
                query = f"SELECT A, B, C, D, E WHERE A >= '{start_str}'"
            else:
                query = f"SELECT A, B, C, D, E WHERE A >= '{start_str}' AND A <= '{end_str}'"

            imported = _fetch_gviz_rows_and_insert(query, timeout=40)
            total_imported += imported
            if first_chunk and imported > 0:
                _partial_recent_only = False
                _notify_sync_complete()

            cursor_end = cursor_start
            remaining_days -= step
            first_chunk = False
            if remaining_days > 0:
                time.sleep(0.35)

        if total_imported > 0:
            _partial_recent_only = False
            _full_sync_completed = True
            _notify_sync_complete()
            print(f"[local_db] Sync complete! Imported {total_imported} rows into local SQLite.")
        else:
            print("[local_db] Sync: no rows returned from Google Sheets.")

    except Exception as e:
        print(f"[local_db] Sync exception: {e}", file=sys.stderr)
    finally:
        _sync_in_progress = False

def start_background_sync(days=30, on_recent_synced=None):
    """Spawns a background thread to sync SQLite with Google Sheets without blocking startup."""
    thread = threading.Thread(target=sync_from_google_sheets, args=(days, on_recent_synced), daemon=True)
    thread.start()
    return thread

# In-memory fallback for votes
_memory_votes = []
_votes_lock = threading.Lock()

def get_snapshot_around(minutes_ago_min=10, minutes_ago_max=25):
    """
    Returns {store_name: {'men': m, 'women': w}} from ~15 minutes ago (10-25 min window)
    to calculate real-time 15-min crowd momentum.
    Automatically widens window to 5-45 mins if the primary window has no snapshot.
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    windows = [(minutes_ago_min, minutes_ago_max)]
    if minutes_ago_min == 10 and minutes_ago_max == 25:
        windows.append((5, 45))
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            for w_min, w_max in windows:
                start_ts = (now_jst - datetime.timedelta(minutes=w_max)).strftime("%Y-%m-%d %H:%M:%S")
                end_ts = (now_jst - datetime.timedelta(minutes=w_min)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute("""
                    SELECT store_name, men, women, timestamp
                    FROM monitor_logs
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp DESC
                """, (start_ts, end_ts))
                rows = cur.fetchall()
                if rows:
                    result = {}
                    for r in rows:
                        sname = r['store_name']
                        if sname not in result:
                            result[sname] = {'men': r['men'], 'women': r['women'], 'ts': r['timestamp']}
                    return result
            return {}
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
    clear_store_7d_cache(store_name)

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


_votes_restored = False


def restore_votes_from_sheets(hours=24):
    """起動時に Sheets から直近の投票を SQLite とメモリへ重複除外マージで復元する。
    起動直後に1票入った場合でも過去の投票履歴を取りこぼさない。"""
    global _votes_restored
    if _votes_restored:
        return 0
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    cutoff = (now_jst - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    existing_keys = set()
    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT timestamp, store_name, vote_type, COALESCE(ip_hash, '') FROM crowd_votes WHERE timestamp >= ?",
                (cutoff,),
            )
            for row in cur.fetchall():
                existing_keys.add((str(row[0]), str(row[1]), str(row[2]), str(row[3])))
        finally:
            conn.close()
        import sheets_store
        rows = sheets_store.read_rows(VOTE_SHEET_TITLE, VOTE_SHEET_HEADER)
    except Exception as e:
        print(f"[local_db] vote restore skipped: {e}", file=sys.stderr)
        return 0

    _votes_restored = True
    restored = 0
    for r in rows:
        if len(r) < 3 or not r[0] or r[0] < cutoff:
            continue
        if r[2] not in ('smooth', 'waiting', 'lively'):
            continue
        identity = r[3] if len(r) > 3 else ''
        dedup_key = (str(r[0]), str(r[1]), str(r[2]), str(identity))
        if dedup_key in existing_keys:
            continue
        existing_keys.add(dedup_key)
        _insert_vote_row(r[0], r[1], r[2], identity)
        with _votes_lock:
            _memory_votes.append({'timestamp': r[0], 'store_name': r[1], 'vote_type': r[2],
                                  'ip_hash': identity, 'voter_id': identity})
        restored += 1
    if restored > 0:
        clear_store_7d_cache()
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


def get_store_7day_analytics(store_name: str) -> dict:
    """
    Computes the past 7 business days' daily peak log and data-driven strategy insights for a store.
    Used on individual store pages (/store/<slug>) to provide unique primary research data (E-E-A-T).
    """
    cache_key = (DB_PATH, store_name)
    now_mono = time.monotonic()
    cached = _store_7d_cache.get(cache_key)
    if cached is not None and (now_mono - cached[0] < _STORE_7D_TTL):
        if cached[1].get("has_history") or not is_partial_recent_only():
            return cached[1]

    from collections import defaultdict
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    rows = get_store_history(store_name, days=10)
    if is_partial_recent_only() and not has_multi_day_span(rows):
        if sync_store_from_google_sheets(store_name, days=10) > 0:
            rows = get_store_history(store_name, days=10)

    daily_buckets = defaultdict(list)
    slot_ratios = defaultdict(list)
    wd_ratios = defaultdict(list)
    midnight_women = []
    peak_band_women = []
    _date_memo = {}

    for r in rows:
        ts_str = r.get("ts") or ""
        w = int(r.get("women") or 0)
        m = int(r.get("men") or 0)
        tot = w + m
        if len(ts_str) < 16 or (w == 0 and m == 0):
            continue
        try:
            d_str = ts_str[:10]
            h = int(ts_str[11:13])
            hm_str = ts_str[11:16]
            d_info = _date_memo.get(d_str)
            if d_info is None:
                d_obj = datetime.date(int(d_str[0:4]), int(d_str[5:7]), int(d_str[8:10]))
                p_obj = d_obj - datetime.timedelta(days=1)
                d_info = (d_str, d_obj.weekday(), p_obj.isoformat(), p_obj.weekday())
                _date_memo[d_str] = d_info
        except Exception:
            continue

        # Business date (00:00 - 06:59 belongs to previous night)
        if h <= 6:
            biz_date_str = d_info[2]
            biz_wd = d_info[3]
        else:
            biz_date_str = d_info[0]
            biz_wd = d_info[1]
        daily_buckets[biz_date_str].append((h, hm_str, w, m))

        if tot >= 6:
            wpct = (w / tot) * 100.0
            wd_ratios[biz_wd].append(wpct)
            if 18 <= h <= 20:
                slot_ratios["19:00〜21:00（早割・1軒目帯）"].append(wpct)
            elif 21 <= h <= 22:
                slot_ratios["21:00〜23:00（ピーク帯）"].append(wpct)
            elif h == 23 or h == 0:
                slot_ratios["23:00〜翌1:00（終電前後・2回転目）"].append(wpct)
            elif 1 <= h <= 4:
                slot_ratios["翌1:00〜03:00（深夜オールナイト帯）"].append(wpct)

        if 21 <= h <= 23:
            peak_band_women.append(w)
        elif 0 <= h <= 2:
            midnight_women.append(w)

    daily_logs = []
    for bdate in sorted(daily_buckets.keys(), reverse=True)[:7]:
        entries = daily_buckets[bdate]
        if not entries:
            continue
        peak_entry = max(entries, key=lambda x: (x[2], x[3]))
        _, peak_hm, peak_w, peak_m = peak_entry
        core_ratios = [
            (w / (w + m)) * 100.0
            for (eh, _, w, m) in entries
            if (w + m) >= 4 and (20 <= eh <= 23 or eh == 0)
        ]
        avg_wpct = round(sum(core_ratios) / len(core_ratios)) if core_ratios else (
            round((peak_w / max(1, peak_w + peak_m)) * 100)
        )
        b_dt_obj = datetime.date(int(bdate[0:4]), int(bdate[5:7]), int(bdate[8:10]))
        wd_label = weekdays_ja[b_dt_obj.weekday()]
        daily_logs.append({
            "date_raw": bdate,
            "date_label": f"{b_dt_obj.month}/{b_dt_obj.day}({wd_label})",
            "weekday": wd_label,
            "peak_women": peak_w,
            "peak_men": peak_m,
            "peak_time": peak_hm,
            "avg_wpct": avg_wpct,
        })

    # Determine best time slot & weekday from actual data
    best_slot_name = "20:00〜21:30（1軒目・早めの時間帯）"
    best_slot_pct = 55
    if slot_ratios:
        best_s = max(slot_ratios.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        best_slot_name = best_s[0]
        best_slot_pct = round(sum(best_s[1]) / len(best_s[1]))

    best_wd_name = "水曜・木曜"
    best_wd_pct = 56
    if wd_ratios:
        best_w = max(wd_ratios.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        best_wd_name = f"{weekdays_ja[best_w[0]]}曜日"
        best_wd_pct = round(sum(best_w[1]) / len(best_w[1]))

    club_keywords = ("梅田", "茶屋町", "心斎橋", "難波", "渋谷", "新宿", "栄", "錦", "福岡", "札幌", "UMEDA", "CHAYAMACHI", "NAMBA", "SHIBUYA", "SHINJUKU", "FUKUOKA", "SAPPORO")
    is_club_area = any(k in (store_name or "").upper() for k in club_keywords)
    avg_mid_w = (sum(midnight_women) / len(midnight_women)) if midnight_women else 0
    avg_peak_w = (sum(peak_band_women) / len(peak_band_women)) if peak_band_women else 0
    strong_midnight = is_club_area or (avg_peak_w > 0 and avg_mid_w >= avg_peak_w * 0.55)

    # Pull 30-day benchmark metrics (core time, midnight retention %, usual women/men)
    benchmarks = get_all_stores_live_benchmarks().get(store_name, {})
    midnight_retention_pct = benchmarks.get("midnight_retention_pct")
    if midnight_retention_pct is None:
        if avg_peak_w >= 2.0 and avg_mid_w > 0:
            midnight_retention_pct = max(15, min(180, round((avg_mid_w / avg_peak_w) * 100)))
        else:
            midnight_retention_pct = 82 if is_club_area else 58
    midnight_label_short = benchmarks.get("midnight_label_short") or (
        "深夜増加型" if midnight_retention_pct >= 95 else ("深夜安定" if midnight_retention_pct >= 70 else "終電前型")
    )
    core_time_short = benchmarks.get("core_time_short") or "20-22時"

    max_7d_w = max((d["peak_women"] for d in daily_logs), default=0)
    votes_24h = get_recent_votes(store_name=store_name, hours=24)

    result = {
        "has_history": len(daily_logs) > 0,
        "sample_count": len(rows),
        "daily_logs": daily_logs,
        "max_7d_women": max_7d_w,
        "best_slot_name": best_slot_name,
        "best_slot_pct": best_slot_pct,
        "best_wd_name": best_wd_name,
        "best_wd_pct": best_wd_pct,
        "is_club_area": is_club_area,
        "strong_midnight": strong_midnight,
        "midnight_retention_pct": midnight_retention_pct,
        "midnight_label_short": midnight_label_short,
        "peak_band_avg_w": benchmarks.get("peak_band_avg_w") or round(avg_peak_w, 1),
        "midnight_avg_w": benchmarks.get("midnight_avg_w") or round(avg_mid_w, 1),
        "core_time_short": core_time_short,
        "usual_women": benchmarks.get("usual_women"),
        "usual_men": benchmarks.get("usual_men"),
        "usual_slot_label": benchmarks.get("usual_slot_label"),
        "votes_24h": votes_24h,
    }
    _store_7d_cache[cache_key] = (time.monotonic(), result)
    return result


def get_all_stores_live_benchmarks(now_jst: datetime.datetime = None, allow_blocking: bool = True, force_refresh: bool = False) -> dict:
    """
    Computes per-store live benchmarks from the last 30 days of SQLite monitor_logs in a single grouped query:
      - usual_women / usual_men: average women & men for the current business weekday & hour
      - usual_slot_label: e.g. '火曜21時台' (or '火曜ピーク帯' during daytime fallback)
      - core_time_short: e.g. '20-22時' (best 2-hour window balancing female ratio & volume)
      - core_time_wpct: average female ratio (%) during core_time_short
      - midnight_retention_pct: ratio (%) of 00:00-01:59 avg women vs 21:00-22:59 avg women
      - midnight_label_short: '深夜増加型' (>=95%), '深夜安定' (70-94%), or '終電前型' (<70%)
    Memoized in memory for 10 minutes per (DB_PATH, biz_wd, cur_hour).
    When allow_blocking=False, returns the cached dict immediately and refreshes asynchronously if expired.
    """
    if now_jst is None:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    cur_h = now_jst.hour
    biz_date = (now_jst.date() - datetime.timedelta(days=1)) if cur_h <= 6 else now_jst.date()
    cur_wd = biz_date.weekday()

    cache_key = (DB_PATH, cur_wd, cur_h)
    now_mono = time.monotonic()
    cached = _live_benchmarks_cache.get(cache_key)
    if not force_refresh and cached is not None:
        if now_mono - cached[0] < _LIVE_BENCHMARKS_TTL:
            return cached[1]
        if not allow_blocking:
            refresh_live_benchmarks_async()
            return cached[1]
    if not force_refresh and not allow_blocking and _sync_in_progress:
        refresh_live_benchmarks_async()
        for k, v in list(_live_benchmarks_cache.items()):
            if k[0] == DB_PATH and v and v[1]:
                return v[1]
        return {}

    from collections import defaultdict
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_ja = weekdays_ja[cur_wd]
    cutoff_str = (now_jst - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    # Store-level accumulators
    # by_wd_hour[store][(wd, h)] = [sum_w, sum_m, cnt]
    by_wd_hour = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    by_hour = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    by_wd_peak = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    peak_21_22 = defaultdict(lambda: [0.0, 0])
    mid_00_01 = defaultdict(lambda: [0.0, 0])
    band_stats = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    stores_seen = set()
    _date_memo = {}

    if os.path.exists(DB_PATH):
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    store_name,
                    substr(timestamp, 1, 10) AS d_str,
                    CAST(substr(timestamp, 12, 2) AS INTEGER) AS h,
                    SUM(women) AS sum_w,
                    SUM(men) AS sum_m,
                    COUNT(*) AS cnt
                FROM monitor_logs
                WHERE timestamp >= ? AND (women > 0 OR men > 0)
                GROUP BY store_name, d_str, h
                """,
                (cutoff_str,),
            )
            grouped_rows = cur.fetchall()
        except Exception as e:
            print(f"[local_db] get_all_stores_live_benchmarks error: {e}", file=sys.stderr)
            grouped_rows = []
        finally:
            conn.close()
    else:
        grouped_rows = []

    for r in grouped_rows:
        sname = r["store_name"]
        d_str = r["d_str"]
        h = r["h"]
        sum_w = float(r["sum_w"] or 0.0)
        sum_m = float(r["sum_m"] or 0.0)
        cnt = int(r["cnt"] or 0)
        if not sname or not d_str or len(d_str) < 10 or cnt <= 0:
            continue

        d_info = _date_memo.get(d_str)
        if d_info is None:
            try:
                d_obj = datetime.date(int(d_str[0:4]), int(d_str[5:7]), int(d_str[8:10]))
                p_obj = d_obj - datetime.timedelta(days=1)
                d_info = (d_obj.weekday(), p_obj.weekday())
                _date_memo[d_str] = d_info
            except Exception:
                continue

        biz_wd = d_info[1] if h <= 6 else d_info[0]
        stores_seen.add(sname)

        b_wh = by_wd_hour[sname][(biz_wd, h)]
        b_wh[0] += sum_w
        b_wh[1] += sum_m
        b_wh[2] += cnt

        b_h = by_hour[sname][h]
        b_h[0] += sum_w
        b_h[1] += sum_m
        b_h[2] += cnt

        if 20 <= h <= 23:
            b_wp = by_wd_peak[sname][biz_wd]
            b_wp[0] += sum_w
            b_wp[1] += sum_m
            b_wp[2] += cnt

        if h in (21, 22):
            p21 = peak_21_22[sname]
            p21[0] += sum_w
            p21[1] += cnt
        elif h in (0, 1):
            m01 = mid_00_01[sname]
            m01[0] += sum_w
            m01[1] += cnt

        # 2-hour core bands during evening/night
        if h in (19, 20):
            bs = band_stats[sname]["19-21時"]
            bs[0] += sum_w
            bs[1] += sum_m
            bs[2] += cnt
        if h in (20, 21):
            bs = band_stats[sname]["20-22時"]
            bs[0] += sum_w
            bs[1] += sum_m
            bs[2] += cnt
        if h in (21, 22):
            bs = band_stats[sname]["21-23時"]
            bs[0] += sum_w
            bs[1] += sum_m
            bs[2] += cnt
        if h in (22, 23):
            bs = band_stats[sname]["22-24時"]
            bs[0] += sum_w
            bs[1] += sum_m
            bs[2] += cnt

    club_keywords = ("梅田", "茶屋町", "心斎橋", "難波", "渋谷", "新宿", "栄", "錦", "福岡", "札幌", "熊本", "UMEDA", "CHAYAMACHI", "NAMBA", "SHIBUYA", "SHINJUKU", "FUKUOKA", "SAPPORO")
    is_active_hour = (18 <= cur_h <= 23) or (0 <= cur_h <= 5)

    out = {}
    for sname in stores_seen:
        # 1. Usual women & men for (cur_wd, cur_h)
        wh_stat = by_wd_hour[sname].get((cur_wd, cur_h))
        slot_label = f"{wd_ja}曜{cur_h}時台"
        is_fallback_peak = False
        if not wh_stat or wh_stat[2] < 2:
            h_stat = by_hour[sname].get(cur_h)
            if h_stat and h_stat[2] >= 2:
                wh_stat = h_stat
                slot_label = f"普段の{cur_h}時台"
            elif not is_active_hour:
                # Daytime fallback: use this weekday's 20-23h peak average so non-zero test/preview snapshots still work
                wp_stat = by_wd_peak[sname].get(cur_wd)
                if wp_stat and wp_stat[2] >= 2:
                    wh_stat = wp_stat
                    slot_label = f"{wd_ja}曜夜平均"
                    is_fallback_peak = True

        if wh_stat and wh_stat[2] > 0:
            usual_w = round(wh_stat[0] / wh_stat[2], 1)
            usual_m = round(wh_stat[1] / wh_stat[2], 1)
        else:
            usual_w = None
            usual_m = None

        # 2. Core time band (best female ratio among bands with solid attendance)
        b_map = band_stats[sname]
        core_short = "20-22時"
        core_wpct = 58
        if b_map:
            max_band_w = max((v[0] / v[2] for v in b_map.values() if v[2] > 0), default=0.0)
            candidates = []
            for b_label, (sw, sm, sc) in b_map.items():
                if sc <= 0 or (sw + sm) <= 0:
                    continue
                avg_w_band = sw / sc
                wpct_band = (sw / (sw + sm)) * 100.0
                # Prefer bands that have at least 55% of the store's peak female volume (or >= 8 women)
                if avg_w_band >= max(4.0, max_band_w * 0.55):
                    candidates.append((wpct_band, avg_w_band, b_label))
            if not candidates:
                for b_label, (sw, sm, sc) in b_map.items():
                    if sc > 0 and (sw + sm) > 0:
                        candidates.append(((sw / (sw + sm)) * 100.0, sw / sc, b_label))
            if candidates:
                best_cand = max(candidates, key=lambda x: (round(x[0], 1), x[1]))
                core_wpct = round(best_cand[0])
                core_short = best_cand[2]

        # 3. Midnight retention % (00:00-01:59 avg women vs 21:00-22:59 avg women)
        p_sum_w, p_cnt = peak_21_22[sname]
        m_sum_w, m_cnt = mid_00_01[sname]
        p_avg_w = (p_sum_w / p_cnt) if p_cnt >= 3 else 0.0
        m_avg_w = (m_sum_w / m_cnt) if m_cnt >= 3 else 0.0
        is_club = any(k in sname.upper() for k in club_keywords)

        if p_avg_w >= 2.0 and m_cnt >= 3:
            ret_pct = max(15, min(180, round((m_avg_w / p_avg_w) * 100)))
        else:
            ret_pct = 82 if is_club else 58

        if ret_pct >= 95:
            mid_label = "深夜増加型"
        elif ret_pct >= 70:
            mid_label = "深夜安定"
        else:
            mid_label = "終電前型"

        out[sname] = {
            "usual_women": usual_w,
            "usual_men": usual_m,
            "usual_slot_label": slot_label,
            "is_active_hour": is_active_hour,
            "is_fallback_peak": is_fallback_peak,
            "core_time_short": core_short,
            "core_time_wpct": core_wpct,
            "midnight_retention_pct": ret_pct,
            "midnight_label_short": mid_label,
            "peak_band_avg_w": round(p_avg_w, 1),
            "midnight_avg_w": round(m_avg_w, 1),
        }

    _live_benchmarks_cache[cache_key] = (time.monotonic(), out)
    return out
