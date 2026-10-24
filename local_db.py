import sqlite3
import datetime
import os
import sys
import threading
import urllib.parse
import json
import requests

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


def insert_logs_with_ts(items):
    """
    Batch-inserts records where each item has its own 'ts' field.
    Prevents N+1 SQLite transactions when backfilling from Google Sheets.
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

    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.executemany("""
                INSERT OR IGNORE INTO monitor_logs (timestamp, store_name, men, women, source)
                VALUES (?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            return cur.rowcount
        except Exception as e:
            print(f"[local_db] Batch insert error: {e}", file=sys.stderr)
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

def sync_recent_from_google_sheets(hours=2):
    """
    Fast synchronous/priority sync of the last N hours (~600 rows, <0.5s) from Google Sheets
    so that 30-min momentum (women_diff / men_diff) is immediately available on cold start / redeploy.
    """
    try:
        import logger
        access_token = logger.get_access_token()
        spreadsheet_id = logger.get_spreadsheet_id()
        if not access_token or not spreadsheet_id:
            return 0

        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        start_dt = now_jst - datetime.timedelta(hours=hours)
        start_ts_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")

        query = f"SELECT A, B, C, D, E WHERE A >= '{start_ts_str}'"
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={urllib.parse.quote(query)}"
        headers = {"Authorization": f"Bearer {access_token}"}

        response = requests.get(url, headers=headers, timeout=10)
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

        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.executemany("""
                    INSERT OR IGNORE INTO monitor_logs (timestamp, store_name, men, women, source)
                    VALUES (?, ?, ?, ?, ?)
                """, batch)
                conn.commit()
                print(f"[local_db] Fast recent sync complete! Imported {len(batch)} rows (last {hours}h).")
                return len(batch)
            finally:
                conn.close()
    except Exception as e:
        print(f"[local_db] Fast recent sync error: {e}", file=sys.stderr)
        return 0


def sync_from_google_sheets(days=14, on_recent_synced=None):
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
        # Step 1: Prioritize the last 2 hours first so 30-min momentum works within 0.5s of boot
        imported_recent = sync_recent_from_google_sheets(hours=2)
        if imported_recent and callable(on_recent_synced):
            try:
                on_recent_synced()
            except Exception as cb_err:
                print(f"[local_db] on_recent_synced callback error: {cb_err}", file=sys.stderr)

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

        response = requests.get(url, headers=headers, timeout=20)
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

def start_background_sync(days=14, on_recent_synced=None):
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


def get_store_7day_analytics(store_name: str) -> dict:
    """
    Computes the past 7 business days' daily peak log and data-driven strategy insights for a store.
    Used on individual store pages (/store/<slug>) to provide unique primary research data (E-E-A-T).
    """
    from collections import defaultdict
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    rows = get_store_history(store_name, days=10)

    daily_buckets = defaultdict(list)
    slot_ratios = defaultdict(list)
    wd_ratios = defaultdict(list)
    midnight_women = []
    peak_band_women = []

    for r in rows:
        ts_str = r.get("ts") or ""
        w = int(r.get("women") or 0)
        m = int(r.get("men") or 0)
        tot = w + m
        if not ts_str or (w == 0 and m == 0):
            continue
        try:
            dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        # Business date (00:00 - 06:59 belongs to previous night)
        biz_dt = dt - datetime.timedelta(days=1) if dt.hour <= 6 else dt
        biz_date_str = biz_dt.strftime("%Y-%m-%d")
        daily_buckets[biz_date_str].append((dt, w, m))

        if tot >= 6:
            wpct = (w / tot) * 100.0
            wd_ratios[biz_dt.weekday()].append(wpct)
            h = dt.hour
            if 18 <= h <= 20:
                slot_ratios["19:00〜21:00（早割・1軒目帯）"].append(wpct)
            elif 21 <= h <= 22:
                slot_ratios["21:00〜23:00（ピーク帯）"].append(wpct)
            elif h == 23 or h == 0:
                slot_ratios["23:00〜翌1:00（終電前後・2回転目）"].append(wpct)
            elif 1 <= h <= 4:
                slot_ratios["翌1:00〜03:00（深夜オールナイト帯）"].append(wpct)

        if 21 <= dt.hour <= 23:
            peak_band_women.append(w)
        elif 0 <= dt.hour <= 2:
            midnight_women.append(w)

    daily_logs = []
    for bdate in sorted(daily_buckets.keys(), reverse=True)[:7]:
        entries = daily_buckets[bdate]
        if not entries:
            continue
        peak_entry = max(entries, key=lambda x: (x[1], x[2]))
        peak_dt, peak_w, peak_m = peak_entry
        core_ratios = [
            (w / (w + m)) * 100.0
            for (dt, w, m) in entries
            if (w + m) >= 4 and (20 <= dt.hour <= 23 or dt.hour == 0)
        ]
        avg_wpct = round(sum(core_ratios) / len(core_ratios)) if core_ratios else (
            round((peak_w / max(1, peak_w + peak_m)) * 100)
        )
        b_dt_obj = datetime.datetime.strptime(bdate, "%Y-%m-%d")
        wd_label = weekdays_ja[b_dt_obj.weekday()]
        daily_logs.append({
            "date_raw": bdate,
            "date_label": f"{b_dt_obj.month}/{b_dt_obj.day}({wd_label})",
            "weekday": wd_label,
            "peak_women": peak_w,
            "peak_men": peak_m,
            "peak_time": peak_dt.strftime("%H:%M"),
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

    max_7d_w = max((d["peak_women"] for d in daily_logs), default=0)
    votes_24h = get_recent_votes(store_name=store_name, hours=24)

    return {
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
        "votes_24h": votes_24h,
    }
