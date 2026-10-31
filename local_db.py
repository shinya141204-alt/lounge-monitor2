import sqlite3
import datetime
import os
import sys
import time
import threading
import urllib.parse
import json
import requests
import turbo_store

DB_PATH = os.environ.get('LOCAL_DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lounge_data.db')


def _is_pg() -> bool:
    return turbo_store.is_postgres_active()


def _active_db_key() -> str:
    return f"pg:{turbo_store.get_database_url()}" if _is_pg() else DB_PATH


def _db_available() -> bool:
    return _is_pg() or os.path.exists(DB_PATH)


def _fetch_all_rows(sql_qmark: str, params: tuple = ()):
    """Execute a read query against PostgreSQL (`DATABASE_URL`) or SQLite (`DB_PATH`) seamlessly."""
    if _is_pg():
        with turbo_store.pg_connection() as conn:
            with conn.cursor() as cur:
                if params:
                    pg_sql = sql_qmark.replace("%", "%%").replace("?", "%s")
                    cur.execute(pg_sql, params)
                else:
                    cur.execute(sql_qmark)
                rows = cur.fetchall()
            try:
                conn.rollback()  # BUG-09: Close read transaction to prevent 'idle in transaction' in pool
            except Exception:
                pass
            return rows
    conn = _get_read_connection()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(sql_qmark, params)
        return cur.fetchall()
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass


_db_lock = threading.Lock()
_read_tls = threading.local()
_sync_in_progress = False
_full_sync_completed = False
_partial_recent_only = False
_sync_complete_callbacks = []
_store_7d_cache = {}
_STORE_7D_TTL = 180.0
_STORE_7D_CACHE_MAX = 128
_live_benchmarks_cache = {}
_LIVE_BENCHMARKS_TTL = 600.0
_LIVE_BENCHMARKS_CACHE_MAX = 8
_benchmark_base_cache = {}
_BENCHMARK_BASE_TTL = 1800.0
_live_benchmarks_computing = False
_live_benchmarks_lock = threading.Lock()
_db_stats_cache = {}
_DB_STATS_TTL = 120.0
_store_backfill_in_flight = set()
_store_backfill_lock = threading.Lock()
_forecast_schema_ready = set()
_forecast_acc_report_cache = {}
_FORECAST_REPORT_TTL = 180.0
_forecast_store_acc_cache = {}
_FORECAST_STORE_ACC_TTL = 180.0
_forecast_residual_bias_cache = {}
_FORECAST_RESIDUAL_TTL = 300.0
_forecast_cache_computing = False
_forecast_cache_lock = threading.Lock()
_forecast_backfill_in_flight = False
_forecast_backfill_lock = threading.Lock()


def clear_store_7d_cache(store_name: str = None, clear_benchmarks: bool = False):
    _db_stats_cache.clear()
    db_k = _active_db_key()
    if store_name:
        _store_7d_cache.pop((db_k, store_name), None)
    else:
        _store_7d_cache.clear()
        _forecast_acc_report_cache.clear()
        _forecast_store_acc_cache.clear()
        _forecast_residual_bias_cache.clear()
    if clear_benchmarks:
        _live_benchmarks_cache.clear()
        _benchmark_base_cache.clear()
        _forecast_acc_report_cache.clear()
        _forecast_store_acc_cache.clear()
        _forecast_residual_bias_cache.clear()


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


_INODE_CHECK_INTERVAL = 2.0  # seconds between os.stat inode checks to minimize system call overhead (BUG-08)


def _get_read_connection():
    """Returns a persistent per-thread SQLite read connection, reopening if DB_PATH changed or was recreated."""
    now = time.monotonic()
    conn = getattr(_read_tls, 'conn', None)
    tls_db_path = getattr(_read_tls, 'db_path', None)
    tls_inode = getattr(_read_tls, 'db_inode', None)
    last_check = getattr(_read_tls, 'last_check', 0.0)

    # Fast path: reuse connection without os.stat if checked recently (BUG-08)
    if conn is not None and tls_db_path == DB_PATH and (now - last_check) < _INODE_CHECK_INTERVAL:
        conn.row_factory = sqlite3.Row
        return conn

    exists_now = os.path.exists(DB_PATH)
    cur_inode = None
    if exists_now:
        try:
            cur_inode = os.stat(DB_PATH).st_ino
        except OSError:
            exists_now = False

    _read_tls.last_check = now

    if conn is not None and tls_db_path == DB_PATH and exists_now and tls_inode == cur_inode:
        conn.row_factory = sqlite3.Row
        return conn
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _read_tls.conn = None
        _read_tls.db_path = None
        _read_tls.db_inode = None
    conn = get_connection()
    try:
        cur_inode = os.stat(DB_PATH).st_ino if os.path.exists(DB_PATH) else None
    except OSError:
        cur_inode = None
    _read_tls.conn = conn
    _read_tls.db_path = DB_PATH
    _read_tls.db_inode = cur_inode
    _read_tls.last_check = now
    return conn



def init_db():
    """Initializes tables and indexes on PostgreSQL (`DATABASE_URL`) or SQLite WAL (`DB_PATH`)."""
    if _is_pg():
        try:
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS monitor_logs (
                            id BIGSERIAL PRIMARY KEY,
                            timestamp TEXT NOT NULL,
                            store_name TEXT NOT NULL,
                            men INTEGER NOT NULL,
                            women INTEGER NOT NULL,
                            source TEXT,
                            UNIQUE(timestamp, store_name)
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_store_ts ON monitor_logs(store_name, timestamp);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_ts_store_wm ON monitor_logs(timestamp, store_name, women, men);")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS crowd_votes (
                            id BIGSERIAL PRIMARY KEY,
                            timestamp TEXT NOT NULL,
                            store_name TEXT NOT NULL,
                            vote_type TEXT NOT NULL,
                            ip_hash TEXT
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_store_ts ON crowd_votes(store_name, timestamp);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_ts ON crowd_votes(timestamp, store_name, vote_type);")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS store_pos_telemetry_logs (
                            id BIGSERIAL PRIMARY KEY,
                            timestamp TEXT NOT NULL,
                            store_name TEXT NOT NULL,
                            telemetry_type TEXT NOT NULL,
                            men_groups INTEGER NOT NULL DEFAULT 0,
                            women_groups INTEGER NOT NULL DEFAULT 0,
                            matched_groups INTEGER NOT NULL DEFAULT 0,
                            men_waiting_groups INTEGER NOT NULL DEFAULT 0,
                            men_waiting_guests INTEGER NOT NULL DEFAULT 0,
                            women_waiting_groups INTEGER NOT NULL DEFAULT 0,
                            vip_guests INTEGER NOT NULL DEFAULT 0,
                            raw_json TEXT,
                            UNIQUE(timestamp, store_name)
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_pos_tel_store_ts ON store_pos_telemetry_logs(store_name, timestamp);")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS external_context_logs (
                            id BIGSERIAL PRIMARY KEY,
                            timestamp TEXT NOT NULL,
                            city_key TEXT NOT NULL,
                            temp_c DOUBLE PRECISION,
                            precip_mm DOUBLE PRECISION,
                            weather_code INTEGER,
                            wind_kmh DOUBLE PRECISION,
                            is_rainy INTEGER NOT NULL DEFAULT 0,
                            active_arenas_json TEXT,
                            UNIQUE(timestamp, city_key)
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_ext_ctx_city_ts ON external_context_logs(city_key, timestamp);")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS forecast_accuracy_logs (
                            id BIGSERIAL PRIMARY KEY,
                            predicted_at TEXT NOT NULL,
                            target_at TEXT NOT NULL,
                            store_name TEXT NOT NULL,
                            model_version TEXT NOT NULL DEFAULT 'v2.1-statespace',
                            start_women INTEGER NOT NULL,
                            start_men INTEGER NOT NULL,
                            start_wpct INTEGER NOT NULL,
                            pred_women INTEGER NOT NULL,
                            pred_men INTEGER NOT NULL,
                            pred_wpct INTEGER NOT NULL,
                            v1_pred_wpct INTEGER,
                            flip_risk_pct INTEGER NOT NULL DEFAULT 0,
                            alert_code TEXT NOT NULL DEFAULT 'steady',
                            resolved_at TEXT,
                            actual_women INTEGER,
                            actual_men INTEGER,
                            actual_wpct INTEGER,
                            error_wpct_pt INTEGER,
                            abs_error_wpct_pt INTEGER,
                            v1_abs_error_wpct_pt INTEGER,
                            error_women INTEGER,
                            error_men INTEGER,
                            UNIQUE(predicted_at, store_name)
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_pending ON forecast_accuracy_logs(resolved_at, target_at);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_store_pred ON forecast_accuracy_logs(store_name, predicted_at);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_resolved_pred ON forecast_accuracy_logs(predicted_at, resolved_at);")
                conn.commit()
            turbo_store.init_postgres_aux_schema()
            _forecast_schema_ready.add(_active_db_key())
            _db_stats_cache.clear()
            print("[local_db] PostgreSQL database initialized successfully.")
            return
        except Exception as e:
            print(f"[local_db] Error initializing PostgreSQL: {e}", file=sys.stderr)
            return

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
            cur.execute("DROP INDEX IF EXISTS idx_ts;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ts_store_wm ON monitor_logs(timestamp, store_name, women, men);")
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_ts ON crowd_votes(timestamp, store_name, vote_type);")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS store_pos_telemetry_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    telemetry_type TEXT NOT NULL,
                    men_groups INTEGER NOT NULL DEFAULT 0,
                    women_groups INTEGER NOT NULL DEFAULT 0,
                    matched_groups INTEGER NOT NULL DEFAULT 0,
                    men_waiting_groups INTEGER NOT NULL DEFAULT 0,
                    men_waiting_guests INTEGER NOT NULL DEFAULT 0,
                    women_waiting_groups INTEGER NOT NULL DEFAULT 0,
                    vip_guests INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT,
                    UNIQUE(timestamp, store_name)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pos_tel_store_ts ON store_pos_telemetry_logs(store_name, timestamp);")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS external_context_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    city_key TEXT NOT NULL,
                    temp_c REAL,
                    precip_mm REAL,
                    weather_code INTEGER,
                    wind_kmh REAL,
                    is_rainy INTEGER NOT NULL DEFAULT 0,
                    active_arenas_json TEXT,
                    UNIQUE(timestamp, city_key)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ext_ctx_city_ts ON external_context_logs(city_key, timestamp);")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS forecast_accuracy_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    predicted_at TEXT NOT NULL,
                    target_at TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    model_version TEXT NOT NULL DEFAULT 'v2.1-statespace',
                    start_women INTEGER NOT NULL,
                    start_men INTEGER NOT NULL,
                    start_wpct INTEGER NOT NULL,
                    pred_women INTEGER NOT NULL,
                    pred_men INTEGER NOT NULL,
                    pred_wpct INTEGER NOT NULL,
                    v1_pred_wpct INTEGER,
                    flip_risk_pct INTEGER NOT NULL DEFAULT 0,
                    alert_code TEXT NOT NULL DEFAULT 'steady',
                    resolved_at TEXT,
                    actual_women INTEGER,
                    actual_men INTEGER,
                    actual_wpct INTEGER,
                    error_wpct_pt INTEGER,
                    abs_error_wpct_pt INTEGER,
                    v1_abs_error_wpct_pt INTEGER,
                    error_women INTEGER,
                    error_men INTEGER,
                    UNIQUE(predicted_at, store_name)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_pending ON forecast_accuracy_logs(resolved_at, target_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_store_pred ON forecast_accuracy_logs(store_name, predicted_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_resolved_pred ON forecast_accuracy_logs(predicted_at, resolved_at);")
            conn.commit()
            _forecast_schema_ready.add(_active_db_key())
            _db_stats_cache.clear()
            print("[local_db] SQLite database initialized successfully (WAL mode enabled).")
        except Exception as e:
            print(f"[local_db] Error initializing SQLite: {e}", file=sys.stderr)
        finally:
            conn.close()

def insert_logs(items, timestamp_str):
    """
    Inserts a batch of store records into PostgreSQL or SQLite.
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

    db_k = _active_db_key()
    if _is_pg():
        try:
            inserted = turbo_store.execute_pg_values_batch(
                """
                INSERT INTO monitor_logs (timestamp, store_name, men, women, source)
                VALUES %s
                ON CONFLICT (timestamp, store_name) DO NOTHING
                """,
                rows,
                page_size=500,
            )
            cached_stat = _db_stats_cache.get(db_k)
            if cached_stat is not None and int((cached_stat[1] or {}).get('count') or 0) >= 500:
                prev_info = dict(cached_stat[1])
                prev_info['count'] = int(prev_info.get('count') or 0) + inserted
                if timestamp_str and (not prev_info.get('max_timestamp') or timestamp_str > prev_info['max_timestamp']):
                    prev_info['max_timestamp'] = timestamp_str
                _db_stats_cache[db_k] = (cached_stat[0], prev_info)
                _store_7d_cache.clear()
            else:
                clear_store_7d_cache()
            _save_moat_aux_logs(items, timestamp_str)
            try:
                record_and_resolve_forecasts(items, timestamp_str)
            except Exception as fc_err:
                print(f"[local_db] Forecast record/resolve warning (PG): {fc_err}", file=sys.stderr)
            return inserted
        except Exception as e:
            print(f"[local_db] PostgreSQL insert error: {e}", file=sys.stderr)
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
            inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cached_stat = _db_stats_cache.get(db_k)
            if cached_stat is not None and int((cached_stat[1] or {}).get('count') or 0) >= 500:
                prev_info = dict(cached_stat[1])
                prev_info['count'] = int(prev_info.get('count') or 0) + inserted
                if timestamp_str and (not prev_info.get('max_timestamp') or timestamp_str > prev_info['max_timestamp']):
                    prev_info['max_timestamp'] = timestamp_str
                _db_stats_cache[db_k] = (cached_stat[0], prev_info)
                _store_7d_cache.clear()
            else:
                clear_store_7d_cache()
        except Exception as e:
            print(f"[local_db] Insert error: {e}", file=sys.stderr)
            return 0
        finally:
            conn.close()
    _save_moat_aux_logs(items, timestamp_str)
    try:
        record_and_resolve_forecasts(items, timestamp_str)
    except Exception as fc_err:
        print(f"[local_db] Forecast record/resolve warning (SQLite): {fc_err}", file=sys.stderr)
    return inserted


def _save_moat_aux_logs(items, timestamp_str):
    """Persist Plan A (`pos_telemetry`) and Plan B (`external_context_logs`) alongside 10-min snapshots.
    Best-effort and fault-isolated so `monitor_logs` ingestion is never disrupted.
    """
    if not items or not timestamp_str:
        return

    pos_rows = []
    for item in items:
        pos = item.get('pos_telemetry')
        if not isinstance(pos, dict) or not pos.get('telemetry_type'):
            continue
        pos_rows.append((
            str(timestamp_str),
            str(item.get('name', '')),
            str(pos.get('telemetry_type', '')),
            int(pos.get('men_total_groups', 0) or 0),
            int(pos.get('women_total_groups', 0) or 0),
            int(pos.get('matched_groups', 0) or 0),
            int(pos.get('men_waiting_groups', 0) or 0),
            int(pos.get('men_waiting_guests', 0) or 0),
            int(pos.get('women_waiting_groups', 0) or 0),
            int(pos.get('vip_guests', 0) or 0),
            json.dumps(pos, ensure_ascii=False),
        ))

    ext_rows = []
    try:
        import moat_intelligence
        # BUG-02: Never block DB insert transaction with synchronous external weather API call.
        # Use allow_blocking=False so weather snapshot is read from RAM cache or refreshed in background.
        ctx_list = moat_intelligence.capture_external_context_snapshot(
            live_stores=items,
            allow_blocking=False,
        )
        for c in (ctx_list or []):
            ext_rows.append((
                str(timestamp_str),
                str(c.get('city_key', '')),
                float(c.get('temp_c', 0.0) or 0.0),
                float(c.get('precip_mm', 0.0) or 0.0),
                int(c.get('weather_code', 0) or 0),
                float(c.get('wind_kmh', 0.0) or 0.0),
                int(c.get('is_rainy', 0) or 0),
                str(c.get('active_arenas_json', '[]')),
            ))
    except Exception:
        ext_rows = []

    if not pos_rows and not ext_rows:
        return

    if _is_pg():
        try:
            if pos_rows:
                turbo_store.execute_pg_values_batch(
                    """
                    INSERT INTO store_pos_telemetry_logs (
                        timestamp, store_name, telemetry_type,
                        men_groups, women_groups, matched_groups,
                        men_waiting_groups, men_waiting_guests, women_waiting_groups,
                        vip_guests, raw_json
                    ) VALUES %s
                    ON CONFLICT (timestamp, store_name) DO NOTHING
                    """,
                    pos_rows,
                    page_size=200,
                )
            if ext_rows:
                turbo_store.execute_pg_values_batch(
                    """
                    INSERT INTO external_context_logs (
                        timestamp, city_key, temp_c, precip_mm,
                        weather_code, wind_kmh, is_rainy, active_arenas_json
                    ) VALUES %s
                    ON CONFLICT (timestamp, city_key) DO NOTHING
                    """,
                    ext_rows,
                    page_size=100,
                )
        except Exception as err:
            print(f"[local_db] PostgreSQL moat aux insert skipped: {err}", file=sys.stderr)
        return

    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if pos_rows:
                cur.executemany(
                    """
                    INSERT OR IGNORE INTO store_pos_telemetry_logs (
                        timestamp, store_name, telemetry_type,
                        men_groups, women_groups, matched_groups,
                        men_waiting_groups, men_waiting_guests, women_waiting_groups,
                        vip_guests, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    pos_rows,
                )
            if ext_rows:
                cur.executemany(
                    """
                    INSERT OR IGNORE INTO external_context_logs (
                        timestamp, city_key, temp_c, precip_mm,
                        weather_code, wind_kmh, is_rainy, active_arenas_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ext_rows,
                )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()


def insert_logs_with_ts(items):
    """
    Batch-inserts records where each item has its own 'ts' field.
    Splits large backfills into micro-batches to avoid holding _db_lock / GIL for long.
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

    if _is_pg():
        try:
            total_inserted = turbo_store.execute_pg_values_batch(
                """
                INSERT INTO monitor_logs (timestamp, store_name, men, women, source)
                VALUES %s
                ON CONFLICT (timestamp, store_name) DO NOTHING
                """,
                rows,
                page_size=800,
            )
            clear_store_7d_cache()
            return total_inserted
        except Exception as e:
            print(f"[local_db] PostgreSQL batch insert error: {e}", file=sys.stderr)
            return 0

    total_inserted = 0
    chunk_size = 800
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
            time.sleep(0.05)

    clear_store_7d_cache()
    return total_inserted

def get_store_history(store_name, days=30):
    """
    Retrieves history logs for a specific store from PostgreSQL/SQLite in milliseconds.
    Returns: list of dicts [{'ts': '...', 'name': '...', 'men': ..., 'women': ...}]
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    start_dt = now_jst - datetime.timedelta(days=days)
    start_ts = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        rows = _fetch_all_rows("""
            SELECT timestamp, store_name, men, women
            FROM monitor_logs
            WHERE store_name = ? AND timestamp >= ?
            ORDER BY timestamp ASC
        """, (store_name, start_ts))
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

def get_weekly_history(start_ts_str):
    """
    Retrieves all store records from start_ts_str to current from PostgreSQL/SQLite.
    Returns: list of dicts [{'ts': ..., 'name': ..., 'men': ..., 'women': ..., 'source': ...}]
    """
    try:
        rows = _fetch_all_rows("""
            SELECT timestamp, store_name, men, women, source
            FROM monitor_logs
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
        """, (start_ts_str,))
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

def get_db_stats():
    """Returns row count and time range from PostgreSQL/SQLite with O(1) index min/max lookup and memory cache."""
    now_mono = time.monotonic()
    db_k = _active_db_key()
    cached = _db_stats_cache.get(db_k)
    if cached is not None and _db_available() and (now_mono - cached[0] < _DB_STATS_TTL):
        return dict(cached[1])

    try:
        rows = _fetch_all_rows(
            "SELECT (SELECT COUNT(*) FROM monitor_logs), "
            "(SELECT MIN(timestamp) FROM monitor_logs), "
            "(SELECT MAX(timestamp) FROM monitor_logs)"
        )
        row = rows[0] if rows else None
        count, min_ts, max_ts = (row[0], row[1], row[2]) if row else (0, None, None)
        res = {
            'count': int(count or 0),
            'min_timestamp': min_ts,
            'max_timestamp': max_ts
        }
        if len(_db_stats_cache) >= 8:
            _db_stats_cache.clear()
        _db_stats_cache[db_k] = (now_mono, res)
        return dict(res)
    except Exception as e:
        return {'count': 0, 'error': str(e)}

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

    if _is_pg():
        turbo_store.execute_pg_values_batch(
            """
            INSERT INTO monitor_logs (timestamp, store_name, men, women, source)
            VALUES %s
            ON CONFLICT (timestamp, store_name) DO NOTHING
            """,
            batch,
            page_size=800,
        )
        clear_store_7d_cache()
        return len(batch)

    chunk_size = 800
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
            time.sleep(0.05)

    clear_store_7d_cache()
    return len(batch)


def sync_recent_from_google_sheets(hours=2):
    """
    Fast synchronous/priority sync of the last N hours (~600 rows, <0.5s) from Google Sheets
    so that 30-min momentum (women_diff / men_diff) is immediately available on cold start / redeploy.
    During daytime off-hours (07:00-17:00 JST), automatically widens to 14h if 2h returns 0 rows.
    """
    global _partial_recent_only
    try:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        start_dt = now_jst - datetime.timedelta(hours=hours)
        start_ts_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        query = f"SELECT A, B, C, D, E WHERE A >= '{start_ts_str}'"
        count = _fetch_gviz_rows_and_insert(query, timeout=12)
        if not count and hours <= 3:
            fallback_hours = 14
            fb_start_str = (now_jst - datetime.timedelta(hours=fallback_hours)).strftime("%Y-%m-%d %H:%M:%S")
            fb_query = f"SELECT A, B, C, D, E WHERE A >= '{fb_start_str}'"
            count = _fetch_gviz_rows_and_insert(fb_query, timeout=15)
            if count:
                hours = fallback_hours
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
        safe_store = store_name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"SELECT A, B, C, D, E WHERE B = '{safe_store}' AND A >= '{start_ts_str}'"
        count = _fetch_gviz_rows_and_insert(query, timeout=15)
        if count:
            print(f"[local_db] Store backfill complete for {store_name}: {count} rows.")
        return count
    except Exception as e:
        print(f"[local_db] Store backfill error for {store_name}: {e}", file=sys.stderr)
        return 0


def _trigger_store_backfill_async(store_name: str, days: int = 10):
    """Runs `sync_store_from_google_sheets` in a deduplicated background thread so HTTP requests never block."""
    if not store_name:
        return
    inflight_key = (DB_PATH, store_name)
    with _store_backfill_lock:
        if inflight_key in _store_backfill_in_flight:
            return
        _store_backfill_in_flight.add(inflight_key)

    def _worker():
        try:
            sync_store_from_google_sheets(store_name, days=days)
            clear_store_7d_cache(store_name)
        except Exception as err:
            print(f"[local_db] async store backfill error for {store_name}: {err}", file=sys.stderr)
        finally:
            with _store_backfill_lock:
                _store_backfill_in_flight.discard(inflight_key)

    threading.Thread(target=_worker, daemon=True).start()


def _notify_sync_complete():
    try:
        refresh_live_benchmarks_async()
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

        # Shortcut: if PostgreSQL/SQLite already covers the requested history window, only sync delta since max_ts
        try:
            target_min_str = (now_jst - datetime.timedelta(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d 00:00:00")
            cnt_rows = _fetch_all_rows(
                "SELECT (SELECT COUNT(*) FROM monitor_logs), "
                "(SELECT MIN(timestamp) FROM monitor_logs), "
                "(SELECT MAX(timestamp) FROM monitor_logs)"
            )
            cnt_row = cnt_rows[0] if cnt_rows else None
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
                time.sleep(0.8)

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
    Optimized via SQL window function (ROW_NUMBER OVER PARTITION) to fetch only latest row per store directly from DB (BUG-03 / F5.1).
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    windows = [(minutes_ago_min, minutes_ago_max)]
    if minutes_ago_min == 10 and minutes_ago_max == 25:
        windows.append((5, 45))
    try:
        for w_min, w_max in windows:
            start_ts = (now_jst - datetime.timedelta(minutes=w_max)).strftime("%Y-%m-%d %H:%M:%S")
            end_ts = (now_jst - datetime.timedelta(minutes=w_min)).strftime("%Y-%m-%d %H:%M:%S")
            rows = _fetch_all_rows("""
                WITH ranked AS (
                    SELECT store_name, men, women, timestamp,
                           ROW_NUMBER() OVER (PARTITION BY store_name ORDER BY timestamp DESC) as rn
                    FROM monitor_logs
                    WHERE timestamp >= ? AND timestamp <= ?
                )
                SELECT store_name, men, women, timestamp
                FROM ranked
                WHERE rn = 1
            """, (start_ts, end_ts))
            if rows:
                return {r[0]: {'men': r[1], 'women': r[2], 'ts': r[3]} for r in rows}
        return {}
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
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (ts, store_name, vote_type, identity),
                    )
                conn.commit()
            return
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
    """起動時に Sheets から直近の投票を PostgreSQL/SQLite とメモリへ重複除外マージで復元する。
    起動直後に1票入った場合でも過去の投票履歴を取りこぼさない。"""
    global _votes_restored
    if _votes_restored:
        return 0
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    cutoff = (now_jst - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    existing_keys = set()
    try:
        for row in _fetch_all_rows(
            "SELECT timestamp, store_name, vote_type, COALESCE(ip_hash, '') FROM crowd_votes WHERE timestamp >= ?",
            (cutoff,),
        ):
            existing_keys.add((str(row[0]), str(row[1]), str(row[2]), str(row[3])))
        import sheets_store
        rows = sheets_store.read_rows(VOTE_SHEET_TITLE, VOTE_SHEET_HEADER)
    except Exception as e:
        print(f"[local_db] vote restore skipped: {e}", file=sys.stderr)
        return 0

    _votes_restored = True
    rows_to_insert = []
    mem_to_append = []
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
        rows_to_insert.append((r[0], r[1], r[2], identity))
        mem_to_append.append({
            'timestamp': r[0],
            'store_name': r[1],
            'vote_type': r[2],
            'ip_hash': identity,
            'voter_id': identity,
        })

    if rows_to_insert:
        try:
            if _is_pg():
                turbo_store.execute_pg_values_batch(
                    "INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash) VALUES %s",
                    rows_to_insert,
                    page_size=500,
                )
            else:
                with _db_lock:
                    conn = get_connection()
                    try:
                        conn.executemany(
                            "INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash) VALUES (?, ?, ?, ?)",
                            rows_to_insert,
                        )
                        conn.commit()
                    finally:
                        conn.close()
        except Exception as err:
            print(f"[local_db] batch vote restore insert failed: {err}", file=sys.stderr)
        with _votes_lock:
            _memory_votes.extend(mem_to_append)
        clear_store_7d_cache()

    restored = len(rows_to_insert)
    print(f"[local_db] Restored {restored} crowd votes from Google Sheets.")
    return restored


def prune_old_logs(monitor_days: int = 60, vote_days: int = 14) -> dict:
    """Prune expired PostgreSQL/SQLite monitor_logs and crowd_votes rows during daily off-hours maintenance."""
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    mon_cutoff = (now_jst - datetime.timedelta(days=max(31, int(monitor_days)))).strftime("%Y-%m-%d %H:%M:%S")
    vote_cutoff = (now_jst - datetime.timedelta(days=max(2, int(vote_days)))).strftime("%Y-%m-%d %H:%M:%S")
    deleted_mon = 0
    deleted_votes = 0
    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM monitor_logs WHERE timestamp < %s", (mon_cutoff,))
                    deleted_mon = max(0, cur.rowcount or 0)
                    cur.execute("DELETE FROM crowd_votes WHERE timestamp < %s", (vote_cutoff,))
                    deleted_votes = max(0, cur.rowcount or 0)
                    try:
                        cur.execute("DELETE FROM store_pos_telemetry_logs WHERE timestamp < %s", (mon_cutoff,))
                        cur.execute("DELETE FROM external_context_logs WHERE timestamp < %s", (mon_cutoff,))
                        cur.execute("DELETE FROM forecast_accuracy_logs WHERE predicted_at < %s", (mon_cutoff,))
                    except Exception:
                        pass
                conn.commit()
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM monitor_logs WHERE timestamp < ?", (mon_cutoff,))
                    deleted_mon = max(0, cur.rowcount or 0)
                    cur.execute("DELETE FROM crowd_votes WHERE timestamp < ?", (vote_cutoff,))
                    deleted_votes = max(0, cur.rowcount or 0)
                    try:
                        cur.execute("DELETE FROM store_pos_telemetry_logs WHERE timestamp < ?", (mon_cutoff,))
                        cur.execute("DELETE FROM external_context_logs WHERE timestamp < ?", (mon_cutoff,))
                        cur.execute("DELETE FROM forecast_accuracy_logs WHERE predicted_at < ?", (mon_cutoff,))
                    except Exception:
                        pass
                    conn.commit()
                finally:
                    conn.close()
        if deleted_mon > 0 or deleted_votes > 0:
            clear_store_7d_cache(clear_benchmarks=True)
    except Exception as err:
        print(f"[local_db] prune_old_logs error: {err}", file=sys.stderr)
    return {'deleted_monitor_logs': deleted_mon, 'deleted_crowd_votes': deleted_votes}

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
        if store_name:
            rows = _fetch_all_rows("""
                SELECT store_name, vote_type, COUNT(*) as cnt
                FROM crowd_votes
                WHERE store_name = ? AND timestamp >= ?
                GROUP BY store_name, vote_type
            """, (store_name, cutoff_str))
        else:
            rows = _fetch_all_rows("""
                SELECT store_name, vote_type, COUNT(*) as cnt
                FROM crowd_votes
                WHERE timestamp >= ?
                GROUP BY store_name, vote_type
            """, (cutoff_str,))
        for r in rows:
            sname = r[0]
            vtype = r[1]
            _ensure(sname)
            if vtype in counts_by_store[sname]:
                counts_by_store[sname][vtype] = int(r[2])
        used_db = True
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
    cache_key = (_active_db_key(), store_name)
    now_mono = time.monotonic()
    cached = _store_7d_cache.get(cache_key)
    if cached is not None and (now_mono - cached[0] < _STORE_7D_TTL):
        if cached[1].get("has_history") or not is_partial_recent_only():
            return cached[1]

    from collections import defaultdict
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    rows = get_store_history(store_name, days=10)
    if is_partial_recent_only() and not has_multi_day_span(rows):
        _trigger_store_backfill_async(store_name, days=10)

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

    club_keywords = ("梅田", "茶屋町", "心斎橋", "難波", "渋谷", "新宿", "栄", "名古屋 錦", "名古屋錦", "福岡", "札幌", "UMEDA", "CHAYAMACHI", "NAMBA", "SHIBUYA", "SHINJUKU", "FUKUOKA", "SAPPORO")
    is_club_area = any(k in (store_name or "").upper() for k in club_keywords)
    avg_mid_w = (sum(midnight_women) / len(midnight_women)) if midnight_women else 0
    avg_peak_w = (sum(peak_band_women) / len(peak_band_women)) if peak_band_women else 0
    strong_midnight = is_club_area or (avg_peak_w > 0 and avg_mid_w >= avg_peak_w * 0.55)

    # Pull 30-day benchmark metrics (core time, midnight retention %, usual women/men)
    benchmarks = get_all_stores_live_benchmarks(allow_blocking=False).get(store_name, {})
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
    if len(_store_7d_cache) > _STORE_7D_CACHE_MAX:
        items_snap = list(_store_7d_cache.items())
        items_snap.sort(key=lambda kv: kv[1][0])
        for k, _ in items_snap[:max(1, len(items_snap) - _STORE_7D_CACHE_MAX)]:
            _store_7d_cache.pop(k, None)
    return result


def _compute_benchmark_base_from_sqlite(cutoff_str: str) -> dict:
    """Scans the last 30 days of monitor_logs once and returns hour/weekday base accumulators for all stores."""
    from collections import defaultdict
    by_wd_hour = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    by_hour = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    by_wd_peak = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    peak_21_22 = defaultdict(lambda: [0.0, 0])
    mid_00_01 = defaultdict(lambda: [0.0, 0])
    band_stats = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    stores_seen = set()
    _date_memo = {}

    if _db_available():
        try:
            grouped_rows = _fetch_all_rows(
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
        except Exception as e:
            print(f"[local_db] get_all_stores_live_benchmarks error: {e}", file=sys.stderr)
            grouped_rows = []
    else:
        grouped_rows = []

    for r in grouped_rows:
        sname = r[0]
        d_str = r[1]
        h = r[2]
        sum_w = float(r[3] or 0.0)
        sum_m = float(r[4] or 0.0)
        cnt = int(r[5] or 0)
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

    # Precompute hour-independent static store metrics (core_time & midnight_retention) once!
    club_keywords = ("梅田", "茶屋町", "心斎橋", "難波", "渋谷", "新宿", "栄", "名古屋 錦", "名古屋錦", "福岡", "札幌", "熊本", "UMEDA", "CHAYAMACHI", "NAMBA", "SHIBUYA", "SHINJUKU", "FUKUOKA", "SAPPORO")
    static_per_store = {}
    for sname in stores_seen:
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

        static_per_store[sname] = {
            "core_time_short": core_short,
            "core_time_wpct": core_wpct,
            "midnight_retention_pct": ret_pct,
            "midnight_label_short": mid_label,
            "peak_band_avg_w": round(p_avg_w, 1),
            "midnight_avg_w": round(m_avg_w, 1),
        }

    return {
        "stores_seen": stores_seen,
        "by_wd_hour": by_wd_hour,
        "by_hour": by_hour,
        "by_wd_peak": by_wd_peak,
        "static_per_store": static_per_store,
    }


def _derive_benchmarks_for_slot(base_data: dict, cur_wd: int, cur_h: int) -> dict:
    """Derives per-store benchmarks for (cur_wd, cur_h) in <0.05ms from pre-aggregated base_data."""
    if not base_data or not base_data.get("stores_seen"):
        return {}
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_ja = weekdays_ja[cur_wd]
    is_active_hour = (18 <= cur_h <= 23) or (0 <= cur_h <= 5)
    by_wd_hour = base_data["by_wd_hour"]
    by_hour = base_data["by_hour"]
    by_wd_peak = base_data["by_wd_peak"]
    static_per_store = base_data["static_per_store"]

    out = {}
    for sname in base_data["stores_seen"]:
        wh_stat = by_wd_hour[sname].get((cur_wd, cur_h))
        slot_label = f"{wd_ja}曜{cur_h}時台"
        is_fallback_peak = False
        if not wh_stat or wh_stat[2] < 2:
            h_stat = by_hour[sname].get(cur_h)
            if h_stat and h_stat[2] >= 2:
                wh_stat = h_stat
                slot_label = f"普段の{cur_h}時台"
            elif not is_active_hour:
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

        st = static_per_store[sname]
        out[sname] = {
            "usual_women": usual_w,
            "usual_men": usual_m,
            "usual_slot_label": slot_label,
            "is_active_hour": is_active_hour,
            "is_fallback_peak": is_fallback_peak,
            "core_time_short": st["core_time_short"],
            "core_time_wpct": st["core_time_wpct"],
            "midnight_retention_pct": st["midnight_retention_pct"],
            "midnight_label_short": st["midnight_label_short"],
            "peak_band_avg_w": st["peak_band_avg_w"],
            "midnight_avg_w": st["midnight_avg_w"],
        }
    return out


def get_all_stores_live_benchmarks(now_jst: datetime.datetime = None, allow_blocking: bool = True, force_refresh: bool = False) -> dict:
    """
    Computes per-store live benchmarks from the last 30 days of SQLite monitor_logs:
      - Base 30-day store accumulators are cached per DB_PATH for 30 minutes, so top-of-the-hour
        transitions (e.g. 21:59 -> 22:00) derive new slot benchmarks in <0.05ms with ZERO SQLite queries.
      - When allow_blocking=False on a large/syncing DB without a warm cache, returns immediately ({})
        and triggers an async background computation so HTTP worker threads never block.
    """
    if now_jst is None:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    cur_h = now_jst.hour
    biz_date = (now_jst.date() - datetime.timedelta(days=1)) if cur_h <= 6 else now_jst.date()
    cur_wd = biz_date.weekday()

    db_k = _active_db_key()
    cache_key = (db_k, cur_wd, cur_h)
    now_mono = time.monotonic()
    db_row_count = int(get_db_stats().get("count") or 0) if _db_available() else 0

    if not force_refresh:
        cached = _live_benchmarks_cache.get(cache_key)
        if cached is not None:
            cached_ts, cached_out = cached[0], cached[1]
            cached_cnt = cached[2] if len(cached) > 2 else db_row_count
            small_db_mutated = (db_row_count < 2500 and db_row_count != cached_cnt)
            if not small_db_mutated:
                if now_mono - cached_ts < _LIVE_BENCHMARKS_TTL:
                    return cached_out
                if not allow_blocking:
                    refresh_live_benchmarks_async()
                    return cached_out

        # Fast O(1) derivation from 30-min base cache (eliminates top-of-hour DB stampede)
        base_entry = _benchmark_base_cache.get(db_k)
        if base_entry is not None:
            base_ts, base_cnt, base_data = base_entry
            small_db_mutated = (db_row_count < 2500 and db_row_count != base_cnt)
            if not small_db_mutated:
                if now_mono - base_ts < _BENCHMARK_BASE_TTL:
                    derived = _derive_benchmarks_for_slot(base_data, cur_wd, cur_h)
                    _live_benchmarks_cache[cache_key] = (now_mono, derived, db_row_count)
                    return derived
                if not allow_blocking:
                    refresh_live_benchmarks_async()
                    return _derive_benchmarks_for_slot(base_data, cur_wd, cur_h)

        # True non-blocking guard when cold & DB is syncing or large (>=5,000 rows)
        if not allow_blocking and (_sync_in_progress or db_row_count >= 5000):
            refresh_live_benchmarks_async()
            for k, v in list(_live_benchmarks_cache.items()):
                if k[0] == db_k and v and v[1]:
                    return v[1]
            return {}

    cutoff_str = (now_jst - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    base_data = _compute_benchmark_base_from_sqlite(cutoff_str)
    now_done = time.monotonic()
    _benchmark_base_cache[db_k] = (now_done, db_row_count, base_data)
    if len(_benchmark_base_cache) > 4:
        oldest_db = min(_benchmark_base_cache.items(), key=lambda kv: kv[1][0])[0]
        _benchmark_base_cache.pop(oldest_db, None)

    out = _derive_benchmarks_for_slot(base_data, cur_wd, cur_h)
    _live_benchmarks_cache[cache_key] = (now_done, out, db_row_count)
    if len(_live_benchmarks_cache) > _LIVE_BENCHMARKS_CACHE_MAX:
        items_snap = list(_live_benchmarks_cache.items())
        items_snap.sort(key=lambda kv: kv[1][0])
        for k, _ in items_snap[:max(1, len(items_snap) - _LIVE_BENCHMARKS_CACHE_MAX)]:
            _live_benchmarks_cache.pop(k, None)
    return out


# ==============================================================================
# 60-Minute Forecast Accuracy Recording, Auto-Resolution & Feedback Loop
# ==============================================================================
_JST_TZ = datetime.timezone(datetime.timedelta(hours=9))

_ALERT_LABELS_JA = {
    "crowd_rush_warn": "⚡ 群衆殺到注意（逆転警戒）",
    "jackpot_hold": "🛡️ 70%確変持続（安全圏）",
    "male_glut_lock": "⚠️ 男余り滞留警戒",
    "last_train_cliff": "🚃 終電前女性減少",
    "rain_shelter_boost": "☔ 雨天・駅直結ブースト",
    "first_train_rebound": "🌅 始発待ち女性優位",
    "steady": "🔮 通常推移予測",
}


def _format_brand_safe_store_name(raw_name: str) -> str:
    """Format store name for public UI/API without isolated 'OLG' or standalone 'オリエンタル'."""
    s = str(raw_name or "").strip()
    if not s:
        return ""
    try:
        import x_poster
        formatted = x_poster.format_ja_store_name(s)
        if formatted:
            return formatted
    except Exception:
        pass
    if s.startswith("OLG ag"):
        return "ag " + s[len("OLG ag"):].strip()
    if s.startswith("OLG "):
        return "オリエンタルラウンジ " + s[4:].strip()
    if s == "OLG":
        return "オリエンタルラウンジ"
    return s


def _is_korea_store_name(store_name: str) -> bool:
    s = str(store_name or "").upper()
    if any(k in s for k in ("ソウル", "カンナム", "ホンデ", "SEOUL", "GANGNAM", "HONGDAE")):
        return True
    try:
        import store_master
        info = store_master.get(store_name)
        if info and info.get("region") == "Korea":
            return True
    except Exception:
        pass
    return False


def _get_valid_domestic_canonical_stores() -> set:
    """Return canonical domestic store keys (active master + legacy canonical slugs), excluding Korea and synthetic seed rows."""
    valid = set()
    try:
        import store_master
        for k, info in (store_master.load() or {}).items():
            if k and (info or {}).get("region") != "Korea" and not _is_korea_store_name(k):
                valid.add(k)
        for k in getattr(store_master, "STORE_SLUGS", {}).keys():
            if k and not _is_korea_store_name(k):
                valid.add(k)
    except Exception:
        pass
    return valid


def _linkable_store_slug(store_name: str) -> str:
    """Return `/store/<slug>` slug only when it resolves to an active store or 301 legacy redirect (never 404)."""
    if not store_name:
        return ""
    try:
        import store_master
        slug = store_master.slug_of(store_name)
        if not slug:
            return ""
        if store_master.store_by_slug(slug) or store_master.legacy_redirect_for_slug(slug):
            return slug
    except Exception:
        pass
    return ""


def resolve_canonical_store_name(raw_query: str) -> str:
    """Resolve canonical name, SEO name, brand-safe display name, or URL slug to canonical internal `store_name`."""
    q = str(raw_query or "").strip()
    if not q:
        return ""
    try:
        import store_master
        master = store_master.load() or {}
        if q in master or q in getattr(store_master, "STORE_SLUGS", {}):
            return q
        by_slug = store_master.store_by_slug(q)
        if by_slug:
            return by_slug
        q_norm = q.replace("　", " ").strip()
        q_nospace = q_norm.replace(" ", "").lower()
        all_candidates = list(master.keys()) + [k for k in getattr(store_master, "STORE_SLUGS", {}).keys() if k not in master]
        for cand in all_candidates:
            disp_brand = _format_brand_safe_store_name(cand)
            seo_disp = store_master.seo_name(cand)
            for alias in (disp_brand, seo_disp):
                if not alias:
                    continue
                if alias == q_norm or alias.replace(" ", "").lower() == q_nospace:
                    return cand
    except Exception:
        pass
    return q


def _parse_jst_ts(ts_str: str):
    if not ts_str:
        return None
    try:
        return datetime.datetime.strptime(str(ts_str)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_JST_TZ)
    except Exception:
        return None


def _ensure_forecast_schema() -> bool:
    """Ensure `forecast_accuracy_logs` exists on the active DB (idempotent & memoized per DB key)."""
    db_k = _active_db_key()
    if db_k in _forecast_schema_ready:
        if _is_pg() or os.path.exists(DB_PATH):
            return True
        _forecast_schema_ready.discard(db_k)

    if _is_pg():
        try:
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS forecast_accuracy_logs (
                            id BIGSERIAL PRIMARY KEY,
                            predicted_at TEXT NOT NULL,
                            target_at TEXT NOT NULL,
                            store_name TEXT NOT NULL,
                            model_version TEXT NOT NULL DEFAULT 'v2.2-statespace',
                            start_women INTEGER NOT NULL,
                            start_men INTEGER NOT NULL,
                            start_wpct INTEGER NOT NULL,
                            pred_women INTEGER NOT NULL,
                            pred_men INTEGER NOT NULL,
                            pred_wpct INTEGER NOT NULL,
                            v1_pred_wpct INTEGER,
                            flip_risk_pct INTEGER NOT NULL DEFAULT 0,
                            alert_code TEXT NOT NULL DEFAULT 'steady',
                            resolved_at TEXT,
                            actual_women INTEGER,
                            actual_men INTEGER,
                            actual_wpct INTEGER,
                            error_wpct_pt INTEGER,
                            abs_error_wpct_pt INTEGER,
                            v1_abs_error_wpct_pt INTEGER,
                            error_women INTEGER,
                            error_men INTEGER,
                            UNIQUE(predicted_at, store_name)
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_pending ON forecast_accuracy_logs(resolved_at, target_at);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_store_pred ON forecast_accuracy_logs(store_name, predicted_at);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_resolved_pred ON forecast_accuracy_logs(predicted_at, resolved_at);")
                conn.commit()
            _forecast_schema_ready.add(db_k)
            return True
        except Exception as err:
            print(f"[local_db] _ensure_forecast_schema PG warning: {err}", file=sys.stderr)
            return False

    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS forecast_accuracy_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    predicted_at TEXT NOT NULL,
                    target_at TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    model_version TEXT NOT NULL DEFAULT 'v2.2-statespace',
                    start_women INTEGER NOT NULL,
                    start_men INTEGER NOT NULL,
                    start_wpct INTEGER NOT NULL,
                    pred_women INTEGER NOT NULL,
                    pred_men INTEGER NOT NULL,
                    pred_wpct INTEGER NOT NULL,
                    v1_pred_wpct INTEGER,
                    flip_risk_pct INTEGER NOT NULL DEFAULT 0,
                    alert_code TEXT NOT NULL DEFAULT 'steady',
                    resolved_at TEXT,
                    actual_women INTEGER,
                    actual_men INTEGER,
                    actual_wpct INTEGER,
                    error_wpct_pt INTEGER,
                    abs_error_wpct_pt INTEGER,
                    v1_abs_error_wpct_pt INTEGER,
                    error_women INTEGER,
                    error_men INTEGER,
                    UNIQUE(predicted_at, store_name)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_pending ON forecast_accuracy_logs(resolved_at, target_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_store_pred ON forecast_accuracy_logs(store_name, predicted_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_resolved_pred ON forecast_accuracy_logs(predicted_at, resolved_at);")
            conn.commit()
            _forecast_schema_ready.add(db_k)
            return True
        except Exception as err:
            print(f"[local_db] _ensure_forecast_schema SQLite warning: {err}", file=sys.stderr)
            return False
        finally:
            conn.close()


def _invalidate_forecast_caches():
    db_k = _active_db_key()
    for k in list(_forecast_acc_report_cache.keys()):
        if isinstance(k, tuple) and k and k[0] == db_k:
            _forecast_acc_report_cache.pop(k, None)
    _forecast_store_acc_cache.pop(db_k, None)
    _forecast_residual_bias_cache.pop(db_k, None)


def get_cached_store_usual_pair(store_name: str, now_jst: datetime.datetime):
    """Non-blocking O(1) RAM lookup of a store's 30-day usual (w_t, m_t, w_t60, m_t60, cnt_t60) from `_benchmark_base_cache`."""
    if not store_name or now_jst is None:
        return None
    base_entry = _benchmark_base_cache.get(_active_db_key())
    if not base_entry:
        return None
    base_data = base_entry[2]
    if not base_data or store_name not in base_data.get("stores_seen", ()):
        return None

    t60 = now_jst + datetime.timedelta(minutes=60)
    h0 = now_jst.hour
    wd0 = ((now_jst - datetime.timedelta(days=1)).date() if h0 <= 6 else now_jst.date()).weekday()
    h1 = t60.hour
    wd1 = ((t60 - datetime.timedelta(days=1)).date() if h1 <= 6 else t60.date()).weekday()

    by_wh = base_data["by_wd_hour"].get(store_name, {})
    by_h = base_data["by_hour"].get(store_name, {})

    s0 = by_wh.get((wd0, h0))
    if not s0 or s0[2] < 3:
        s0 = by_h.get(h0)
    s1 = by_wh.get((wd1, h1))
    if not s1 or s1[2] < 3:
        s1 = by_h.get(h1)

    if not s1 or s1[2] < 3:
        return None
    uw0 = (s0[0] / s0[2]) if (s0 and s0[2] > 0) else 0.0
    um0 = (s0[1] / s0[2]) if (s0 and s0[2] > 0) else 0.0
    uw1 = s1[0] / s1[2]
    um1 = s1[1] / s1[2]
    return (uw0, um0, uw1, um1, int(s1[2]))


def _compute_resolution_tuple(row_id: int, pred_w: int, pred_m: int, pred_wp: int, v1_pred_wp, act_w: int, act_m: int, resolved_ts: str):
    act_w = max(0, int(act_w or 0))
    act_m = max(0, int(act_m or 0))
    act_tot = act_w + act_m
    err_w = int(pred_w) - act_w
    err_m = int(pred_m) - act_m
    if act_tot >= 2:
        act_wp = int(round((act_w / float(act_tot)) * 100.0))
        err_wp = int(pred_wp) - act_wp
        abs_err_wp = abs(err_wp)
        v1_abs_err = abs(int(v1_pred_wp) - act_wp) if v1_pred_wp is not None else None
    else:
        act_wp = None
        err_wp = None
        abs_err_wp = None
        v1_abs_err = None
    return (
        resolved_ts,
        act_w,
        act_m,
        act_wp,
        err_wp,
        abs_err_wp,
        v1_abs_err,
        err_w,
        err_m,
        int(row_id),
    )


def record_and_resolve_forecasts(items: list, timestamp_str: str, resolve_only: bool = False) -> dict:
    """
    1) Resolve due pending 60m forecasts (`resolved_at IS NULL`) using fresh live `items` (when within [-1.5m, +25m] of `target_at`)
       or historical `monitor_logs` (when overdue >25m), expiring unresolvable rows >3h old.
    2) Record new 60m forecasts for active canonical domestic stores (`women + men >= 6`) with zero blocking I/O.
    """
    if not timestamp_str or not _ensure_forecast_schema():
        return {"resolved": 0, "recorded": 0}

    ts_dt = _parse_jst_ts(timestamp_str)
    if ts_dt is None:
        return {"resolved": 0, "recorded": 0}
    ts_norm = ts_dt.strftime("%Y-%m-%d %H:%M:%S")

    f_times = [
        float(it.get("fetched_at") or 0.0)
        for it in (items or [])
        if isinstance(it, dict) and "fetched_at" in it and float(it.get("fetched_at") or 0.0) > 0.0
    ]
    max_fetched = max(f_times) if f_times else 0.0

    fresh_items = []
    live_map = {}
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        if "fetched_at" in it:
            fat = float(it.get("fetched_at") or 0.0)
            if fat <= 0.0 or (max_fetched > 0.0 and (max_fetched - fat) > 180.0):
                continue
        sname = (it.get("name") or "").strip()
        if sname:
            live_map[sname] = (max(0, int(it.get("women") or 0)), max(0, int(it.get("men") or 0)), it)
            fresh_items.append(it)

    # --- Step A: Resolve due pending forecasts (allow at most 90s early clock jitter so 5m ticks never resolve at 54m) ---
    upper_due_str = (ts_dt + datetime.timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        pending_due = _fetch_all_rows(
            """
            SELECT id, target_at, store_name, pred_women, pred_men, pred_wpct, v1_pred_wpct
            FROM forecast_accuracy_logs
            WHERE resolved_at IS NULL AND target_at <= ?
            ORDER BY target_at ASC
            LIMIT 400
            """,
            (upper_due_str,),
        )
    except Exception:
        pending_due = []

    updates = []
    stale_delete_ids = []
    for r in pending_due:
        row_id = int(r[0])
        tgt_str = str(r[1])
        sname = str(r[2])
        pred_w = int(r[3] or 0)
        pred_m = int(r[4] or 0)
        pred_wp = int(r[5] or 50)
        v1_wp = int(r[6]) if r[6] is not None else None
        tgt_dt = _parse_jst_ts(tgt_str)
        if tgt_dt is None:
            stale_delete_ids.append(row_id)
            continue

        diff_min = (ts_dt - tgt_dt).total_seconds() / 60.0
        if -1.5 <= diff_min <= 25.0 and sname in live_map:
            aw, am, _ = live_map[sname]
            updates.append(_compute_resolution_tuple(row_id, pred_w, pred_m, pred_wp, v1_wp, aw, am, ts_norm))
        elif diff_min > 25.0:
            w_lo = (tgt_dt - datetime.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
            w_hi = (tgt_dt + datetime.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
            try:
                cand_rows = _fetch_all_rows(
                    """
                    SELECT timestamp, women, men
                    FROM monitor_logs
                    WHERE store_name = ? AND timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                    """,
                    (sname, w_lo, w_hi),
                )
            except Exception:
                cand_rows = []
            best_c = None
            best_gap = 999999.0
            for cr in cand_rows:
                cdt = _parse_jst_ts(cr[0])
                if cdt is not None:
                    g = abs((cdt - tgt_dt).total_seconds())
                    if g < best_gap:
                        best_gap = g
                        best_c = cr
            if best_c is not None:
                updates.append(_compute_resolution_tuple(
                    row_id, pred_w, pred_m, pred_wp, v1_wp, int(best_c[1] or 0), int(best_c[2] or 0), str(best_c[0])[:19]
                ))
            elif diff_min > 180.0:
                stale_delete_ids.append(row_id)

    resolved_cnt = 0
    if updates or stale_delete_ids:
        if _is_pg():
            try:
                with turbo_store.pg_connection() as conn:
                    with conn.cursor() as cur:
                        for u in updates:
                            cur.execute(
                                """
                                UPDATE forecast_accuracy_logs
                                SET resolved_at = %s,
                                    actual_women = %s,
                                    actual_men = %s,
                                    actual_wpct = %s,
                                    error_wpct_pt = %s,
                                    abs_error_wpct_pt = %s,
                                    v1_abs_error_wpct_pt = %s,
                                    error_women = %s,
                                    error_men = %s
                                WHERE id = %s AND resolved_at IS NULL
                                """,
                                u,
                            )
                            resolved_cnt += max(0, cur.rowcount or 0)
                        if stale_delete_ids:
                            cur.execute(
                                "DELETE FROM forecast_accuracy_logs WHERE id = ANY(%s) AND resolved_at IS NULL",
                                (stale_delete_ids,),
                            )
                    conn.commit()
            except Exception as err:
                print(f"[local_db] PG forecast resolution error: {err}", file=sys.stderr)
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    if updates:
                        cur.executemany(
                            """
                            UPDATE forecast_accuracy_logs
                            SET resolved_at = ?,
                                actual_women = ?,
                                actual_men = ?,
                                actual_wpct = ?,
                                error_wpct_pt = ?,
                                abs_error_wpct_pt = ?,
                                v1_abs_error_wpct_pt = ?,
                                error_women = ?,
                                error_men = ?
                            WHERE id = ? AND resolved_at IS NULL
                            """,
                            updates,
                        )
                        resolved_cnt = len(updates)
                    if stale_delete_ids:
                        qmarks = ",".join("?" for _ in stale_delete_ids)
                        cur.execute(
                            f"DELETE FROM forecast_accuracy_logs WHERE id IN ({qmarks}) AND resolved_at IS NULL",
                            tuple(stale_delete_ids),
                        )
                    conn.commit()
                except Exception as err:
                    print(f"[local_db] SQLite forecast resolution error: {err}", file=sys.stderr)
                finally:
                    conn.close()

    if resolve_only or not fresh_items:
        if resolved_cnt > 0 or stale_delete_ids:
            _invalidate_forecast_caches()
        return {"resolved": resolved_cnt, "recorded": 0}

    # --- Step B: Record new 60-minute forecasts ---
    import moat_intelligence
    import store_hours

    valid_domestic = _get_valid_domestic_canonical_stores()

    dup_lo = (ts_dt - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    dup_hi = (ts_dt + datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        recent_existing = {
            str(r[0])
            for r in _fetch_all_rows(
                "SELECT store_name FROM forecast_accuracy_logs WHERE predicted_at >= ? AND predicted_at <= ?",
                (dup_lo, dup_hi),
            )
        }
    except Exception:
        recent_existing = set()

    # Check if we need 15-min diffs when `fresh_items` didn't carry `women_diff`/`men_diff`
    need_db_diff = any(
        ("women_diff" not in it and (int(it.get("women") or 0) + int(it.get("men") or 0)) >= 6)
        for it in fresh_items
    )
    prev15_map = {}
    if need_db_diff:
        p_lo = (ts_dt - datetime.timedelta(minutes=22)).strftime("%Y-%m-%d %H:%M:%S")
        p_hi = (ts_dt - datetime.timedelta(minutes=8)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            for pr in _fetch_all_rows(
                "SELECT store_name, women, men FROM monitor_logs WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC",
                (p_lo, p_hi),
            ):
                if pr[0] not in prev15_map:
                    prev15_map[pr[0]] = (int(pr[1] or 0), int(pr[2] or 0))
        except Exception:
            prev15_map = {}

    weather_map = moat_intelligence.get_city_weather_map(allow_blocking=False)
    arena_map = moat_intelligence.detect_arena_event_surges(fresh_items, now_jst=ts_dt)
    target_str = (ts_dt + datetime.timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")

    to_insert = []
    for item in fresh_items:
        sname = (item.get("name") or "").strip()
        if not sname or _is_korea_store_name(sname) or sname in recent_existing:
            continue
        if valid_domestic and sname not in valid_domestic:
            continue
        w = max(0, int(item.get("women") or 0))
        m = max(0, int(item.get("men") or 0))
        tot = w + m
        if tot < 6:
            continue
        bh = store_hours.get_business_hours_status(sname, now_jst=ts_dt, total_guests=tot)
        if not bh.get("is_open_now", True):
            continue

        if "women_diff" in item:
            wd = int(item.get("women_diff") or 0)
            md = int(item.get("men_diff") or 0)
        elif sname in prev15_map:
            wd = w - prev15_map[sname][0]
            md = m - prev15_map[sname][1]
        else:
            wd, md = 0, 0

        fc = moat_intelligence.predict_store_60m_dynamics(
            store_name=sname,
            women=w,
            men=m,
            is_open_now=True,
            women_diff=wd,
            men_diff=md,
            usual_w_diff=item.get("usual_w_diff"),
            now_jst=ts_dt,
            weather_map=weather_map,
            arena_signal=arena_map.get(sname),
            apply_online_bias=True,
        )
        if fc.get("pred_60m_wpct") is None:
            continue
        v1_fc = moat_intelligence._predict_store_60m_v1_legacy(
            store_name=sname,
            women=w,
            men=m,
            is_open_now=True,
            women_diff=wd,
            men_diff=md,
            now_jst=ts_dt,
            weather_map=weather_map,
            arena_signal=arena_map.get(sname),
        )
        start_wp = int(round((w / float(tot)) * 100.0))
        to_insert.append((
            ts_norm,
            target_str,
            sname,
            str(fc.get("model_version") or moat_intelligence.FORECAST_MODEL_VERSION),
            w,
            m,
            start_wp,
            int(fc["pred_60m_women"]),
            int(fc["pred_60m_men"]),
            int(fc["pred_60m_wpct"]),
            int(v1_fc["pred_60m_wpct"]) if v1_fc.get("pred_60m_wpct") is not None else None,
            int(fc.get("flip_risk_pct") or 0),
            str(fc.get("alert_code") or "steady"),
        ))

    recorded_cnt = 0
    if to_insert:
        if _is_pg():
            try:
                recorded_cnt = turbo_store.execute_pg_values_batch(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, v1_pred_wpct,
                        flip_risk_pct, alert_code
                    ) VALUES %s
                    ON CONFLICT (predicted_at, store_name) DO NOTHING
                    """,
                    to_insert,
                    page_size=200,
                )
            except Exception as err:
                print(f"[local_db] PG forecast insert error: {err}", file=sys.stderr)
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    cur.executemany(
                        """
                        INSERT OR IGNORE INTO forecast_accuracy_logs (
                            predicted_at, target_at, store_name, model_version,
                            start_women, start_men, start_wpct,
                            pred_women, pred_men, pred_wpct, v1_pred_wpct,
                            flip_risk_pct, alert_code
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        to_insert,
                    )
                    conn.commit()
                    recorded_cnt = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(to_insert)
                except Exception as err:
                    print(f"[local_db] SQLite forecast insert error: {err}", file=sys.stderr)
                finally:
                    conn.close()

    if resolved_cnt > 0 or recorded_cnt > 0 or stale_delete_ids:
        _invalidate_forecast_caches()
    return {"resolved": resolved_cnt, "recorded": recorded_cnt}


def resolve_due_forecasts_only(items: list, timestamp_str: str) -> dict:
    """Resolve due pending 60m forecasts without inserting new forecast rows (used during off-hour transitions like 07:00-08:30)."""
    return record_and_resolve_forecasts(items, timestamp_str, resolve_only=True)


def backfill_forecast_accuracy_from_history(
    days: int = 21,
    min_existing: int = 80,
    max_rows: int = 4500,
    force: bool = False,
    _already_claimed: bool = False,
) -> dict:
    """
    Causally backfill resolved 60-minute predictions from historical `monitor_logs` so accuracy reports
    and online store calibration are populated immediately on existing databases without waiting 60 minutes.
    """
    global _forecast_backfill_in_flight

    if not _db_available() or not _ensure_forecast_schema():
        return {"inserted": 0, "skipped": True}

    acquired_flight = False
    if not force and not _already_claimed:
        with _forecast_backfill_lock:
            if _forecast_backfill_in_flight:
                return {"inserted": 0, "skipped": True, "in_flight": True}
            _forecast_backfill_in_flight = True
            acquired_flight = True

    try:
        try:
            cnt_rows = _fetch_all_rows("SELECT COUNT(*) FROM forecast_accuracy_logs WHERE resolved_at IS NOT NULL")
            existing_resolved = int(cnt_rows[0][0] or 0) if cnt_rows else 0
        except Exception:
            existing_resolved = 0

        if not force and existing_resolved >= min_existing:
            return {"inserted": 0, "skipped": True, "existing_resolved": existing_resolved}

        import moat_intelligence

        valid_domestic = _get_valid_domestic_canonical_stores()
        now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
        cutoff_str = (now_jst - datetime.timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
        try:
            raw_rows = _fetch_all_rows(
                """
                SELECT timestamp, store_name, women, men
                FROM monitor_logs
                WHERE timestamp >= ? AND store_name NOT LIKE ?
                ORDER BY timestamp ASC
                """,
                (cutoff_str, "%ソウル%"),
            )
        except Exception as err:
            print(f"[local_db] backfill_forecast_accuracy query error: {err}", file=sys.stderr)
            return {"inserted": 0, "skipped": False, "error": str(err)}

        if len(raw_rows) < 10:
            return {"inserted": 0, "skipped": True, "existing_resolved": existing_resolved}

        # Group into snapshot batches (within 90s), discarding synthetic batches tainted by non-canonical seed store names
        batches = []
        cur_dt = None
        cur_ts = None
        cur_map = {}
        cur_tainted = False
        for r in raw_rows:
            dt = _parse_jst_ts(r[0])
            if dt is None:
                continue
            if cur_dt is None or (dt - cur_dt).total_seconds() > 90.0:
                if cur_map and not cur_tainted:
                    batches.append((cur_dt, cur_ts, cur_map))
                cur_dt = dt
                cur_ts = dt.strftime("%Y-%m-%d %H:%M:%S")
                cur_map = {}
                cur_tainted = False
            sname_r = str(r[1] or "").strip()
            if not sname_r or _is_korea_store_name(sname_r):
                continue
            if valid_domestic and sname_r not in valid_domestic:
                cur_tainted = True
                continue
            cur_map[sname_r] = (max(0, int(r[2] or 0)), max(0, int(r[3] or 0)))
        if cur_map and not cur_tainted:
            batches.append((cur_dt, cur_ts, cur_map))

        # Filter out rogue interleaved legacy batches when multi-store snapshots are present
        stage1 = []
        for dt, ts_s, smap in batches:
            if len(smap) >= 20:
                tot_all = sum(w + m for w, m in smap.values())
                if 8 <= dt.hour <= 16 and tot_all > 60:
                    continue
                if (20 <= dt.hour <= 23 or 0 <= dt.hour <= 2) and tot_all < 80:
                    continue
            stage1.append((dt, ts_s, smap))

        clean_batches = []
        for idx, (dt, ts_s, smap) in enumerate(stage1):
            if len(smap) >= 20 and 0 < idx < len(stage1) - 1:
                p_dt, _, p_map = stage1[idx - 1]
                n_dt, _, n_map = stage1[idx + 1]
                if (n_dt - p_dt).total_seconds() <= 2100.0:
                    common = [s for s in smap if s in p_map and s in n_map]
                    if len(common) >= 20:
                        d_prev = sum(abs((smap[s][0] + smap[s][1]) - (p_map[s][0] + p_map[s][1])) for s in common) / len(common)
                        d_next = sum(abs((smap[s][0] + smap[s][1]) - (n_map[s][0] + n_map[s][1])) for s in common) / len(common)
                        d_pn = sum(abs((n_map[s][0] + n_map[s][1]) - (p_map[s][0] + p_map[s][1])) for s in common) / len(common)
                        if d_prev > 16.0 and d_next > 16.0 and d_pn < 12.0:
                            continue
            clean_batches.append((dt, ts_s, smap))

        n_b = len(clean_batches)
        if n_b < 2:
            return {"inserted": 0, "skipped": True, "existing_resolved": existing_resolved}

        # Walk backwards from newest batch (~25m spacing) up to `max_rows` resolved records
        backfill_rows = []
        last_sampled_dt = None
        for i in range(n_b - 2, -1, -1):
            dt, ts_s, smap = clean_batches[i]
            if last_sampled_dt is not None and (last_sampled_dt - dt).total_seconds() < 1400.0 and n_b > 30:
                continue
            best_k = None
            best_err = 999.0
            for k in range(i + 1, n_b):
                dm = (clean_batches[k][0] - dt).total_seconds() / 60.0
                if dm < 50.0:
                    continue
                if dm > 70.0:
                    break
                err = abs(dm - 60.0)
                if err < best_err:
                    best_err = err
                    best_k = k
            if best_k is None:
                continue

            p15 = None
            for k in range(i - 1, -1, -1):
                dm = (dt - clean_batches[k][0]).total_seconds() / 60.0
                if 8.0 <= dm <= 23.0:
                    p15 = clean_batches[k][2]
                    break
                if dm > 25.0:
                    break

            res_dt, res_ts_s, smap60 = clean_batches[best_k]
            tgt_s = (dt + datetime.timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")
            added_in_batch = 0

            for sname, (w, m) in smap.items():
                tot = w + m
                if tot < 6 or sname not in smap60:
                    continue
                w60, m60 = smap60[sname]
                wd15 = (w - p15[sname][0]) if (p15 and sname in p15) else 0
                md15 = (m - p15[sname][1]) if (p15 and sname in p15) else 0

                fc = moat_intelligence.predict_store_60m_dynamics(
                    store_name=sname,
                    women=w,
                    men=m,
                    is_open_now=True,
                    women_diff=wd15,
                    men_diff=md15,
                    now_jst=dt,
                    weather_map={},
                    arena_signal=None,
                    apply_online_bias=False,
                )
                if fc.get("pred_60m_wpct") is None:
                    continue
                v1_fc = moat_intelligence._predict_store_60m_v1_legacy(
                    store_name=sname,
                    women=w,
                    men=m,
                    is_open_now=True,
                    women_diff=wd15,
                    men_diff=md15,
                    now_jst=dt,
                    weather_map={},
                    arena_signal=None,
                )
                start_wp = int(round((w / float(tot)) * 100.0))
                pred_w = int(fc["pred_60m_women"])
                pred_m = int(fc["pred_60m_men"])
                pred_wp = int(fc["pred_60m_wpct"])
                v1_wp = int(v1_fc["pred_60m_wpct"]) if v1_fc.get("pred_60m_wpct") is not None else None
                res_tup = _compute_resolution_tuple(0, pred_w, pred_m, pred_wp, v1_wp, w60, m60, res_ts_s)

                backfill_rows.append((
                    ts_s,
                    tgt_s,
                    sname,
                    moat_intelligence.FORECAST_MODEL_VERSION,
                    w,
                    m,
                    start_wp,
                    pred_w,
                    pred_m,
                    pred_wp,
                    v1_wp,
                    int(fc.get("flip_risk_pct") or 0),
                    str(fc.get("alert_code") or "steady"),
                    res_tup[0],
                    res_tup[1],
                    res_tup[2],
                    res_tup[3],
                    res_tup[4],
                    res_tup[5],
                    res_tup[6],
                    res_tup[7],
                    res_tup[8],
                ))
                added_in_batch += 1
                if len(backfill_rows) >= max_rows:
                    break

            if added_in_batch > 0:
                last_sampled_dt = dt
            if len(backfill_rows) >= max_rows:
                break

        if not backfill_rows:
            return {"inserted": 0, "skipped": False, "existing_resolved": existing_resolved}

        backfill_rows.reverse()
        inserted_total = 0
        if _is_pg():
            try:
                inserted_total = turbo_store.execute_pg_values_batch(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, v1_pred_wpct,
                        flip_risk_pct, alert_code,
                        resolved_at, actual_women, actual_men, actual_wpct,
                        error_wpct_pt, abs_error_wpct_pt, v1_abs_error_wpct_pt,
                        error_women, error_men
                    ) VALUES %s
                    ON CONFLICT (predicted_at, store_name) DO NOTHING
                    """,
                    backfill_rows,
                    page_size=400,
                )
            except Exception as err:
                print(f"[local_db] PG backfill_forecast_accuracy error: {err}", file=sys.stderr)
        else:
            chunk_sz = 500
            for off in range(0, len(backfill_rows), chunk_sz):
                sub = backfill_rows[off:off + chunk_sz]
                with _db_lock:
                    conn = get_connection()
                    try:
                        cur = conn.cursor()
                        cur.executemany(
                            """
                            INSERT OR IGNORE INTO forecast_accuracy_logs (
                                predicted_at, target_at, store_name, model_version,
                                start_women, start_men, start_wpct,
                                pred_women, pred_men, pred_wpct, v1_pred_wpct,
                                flip_risk_pct, alert_code,
                                resolved_at, actual_women, actual_men, actual_wpct,
                                error_wpct_pt, abs_error_wpct_pt, v1_abs_error_wpct_pt,
                                error_women, error_men
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            sub,
                        )
                        conn.commit()
                        inserted_total += max(0, cur.rowcount or 0)
                    except Exception as err:
                        print(f"[local_db] SQLite backfill_forecast_accuracy error: {err}", file=sys.stderr)
                        break
                    finally:
                        conn.close()

        _invalidate_forecast_caches()
        _refresh_forecast_caches_from_db(days=days)
        return {"inserted": inserted_total, "skipped": False, "existing_resolved": existing_resolved + inserted_total}
    finally:
        if acquired_flight:
            with _forecast_backfill_lock:
                _forecast_backfill_in_flight = False


def trigger_forecast_backfill_async(days: int = 21, min_existing: int = 80, max_rows: int = 4500) -> bool:
    """Atomically claim single-flight backfill lock and spawn at most one background daemon thread."""
    global _forecast_backfill_in_flight
    if not _db_available():
        return False
    with _forecast_backfill_lock:
        if _forecast_backfill_in_flight:
            return False
        _forecast_backfill_in_flight = True

    def _bg():
        global _forecast_backfill_in_flight
        try:
            backfill_forecast_accuracy_from_history(
                days=days, min_existing=min_existing, max_rows=max_rows, _already_claimed=True
            )
        except Exception as err:
            print(f"[local_db] trigger_forecast_backfill_async error: {err}", file=sys.stderr)
        finally:
            with _forecast_backfill_lock:
                _forecast_backfill_in_flight = False

    threading.Thread(target=_bg, daemon=True).start()
    return True


def _refresh_forecast_caches_from_db(days: int = 21):
    """Build per-store accuracy badge map and Empirical-Bayes online residual adjustment cache from `forecast_accuracy_logs`."""
    if not _db_available() or not _ensure_forecast_schema():
        return {}, {}

    db_k = _active_db_key()
    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    cutoff_str = (now_jst - datetime.timedelta(days=max(3, int(days)))).strftime("%Y-%m-%d %H:%M:%S")

    try:
        rows = _fetch_all_rows(
            """
            SELECT store_name, predicted_at, pred_wpct, actual_wpct, error_wpct_pt, abs_error_wpct_pt, actual_women, actual_men
            FROM forecast_accuracy_logs
            WHERE resolved_at IS NOT NULL
              AND actual_wpct IS NOT NULL
              AND predicted_at >= ?
            ORDER BY predicted_at DESC
            """,
            (cutoff_str,),
        )
    except Exception:
        rows = []

    from collections import defaultdict
    slot_err = defaultdict(lambda: [0.0, 0])   # (sname, hr) -> [sum_signed_err, n]
    store_acc = defaultdict(lambda: [0.0, 0.0, 0, 0, None])  # sname -> [sum_abs, sum_signed, within10, n, latest_dict]

    for r in rows:
        sname = str(r[0] or "")
        pred_at = str(r[1] or "")
        pred_wp = int(r[2] or 0)
        act_wp = int(r[3] or 0)
        err_pt = float(r[4] or 0.0)
        abs_pt = float(r[5] or 0.0)
        aw = int(r[6] or 0)
        am = int(r[7] or 0)
        if not sname or (aw + am) < 4:
            continue

        try:
            hr = int(pred_at[11:13])
        except Exception:
            hr = -1

        st = store_acc[sname]
        st[0] += abs_pt
        st[1] += err_pt
        if abs_pt <= 10.0:
            st[2] += 1
        st[3] += 1
        if st[4] is None:
            st[4] = {
                "predicted_hm": pred_at[11:16] if len(pred_at) >= 16 else pred_at,
                "pred_wpct": pred_wp,
                "actual_wpct": act_wp,
                "error_pt": int(round(err_pt)),
            }

        if hr >= 0:
            sl = slot_err[(sname, hr)]
            sl[0] += err_pt
            sl[1] += 1

    bias_map = {}
    acc_map = {}
    for sname, (sum_abs, sum_sig, w10, n_st, latest_d) in store_acc.items():
        if n_st >= 3:
            acc_map[sname] = {
                "resolved_count": n_st,
                "mae_wpct": round(sum_abs / n_st, 1),
                "within_10pt_pct": round((w10 / float(n_st)) * 100.0, 1),
                "bias_wpct": round(sum_sig / n_st, 1),
                "latest": latest_d,
            }
        if n_st >= 5:
            store_mean_err = sum_sig / float(n_st)
            store_shrunk = store_mean_err * (n_st / (n_st + 25.0))
            bias_map[(sname, None)] = round(max(-4.5, min(4.5, -0.55 * store_shrunk)), 2)

    for (sname, hr), (sum_sl, n_sl) in slot_err.items():
        st = store_acc.get(sname)
        n_st = st[3] if st else 0
        if n_st < 5:
            continue
        store_mean_err = st[1] / float(n_st)
        store_shrunk = store_mean_err * (n_st / (n_st + 25.0))
        slot_mean_err = sum_sl / float(n_sl)
        slot_shrunk = slot_mean_err * (n_sl / (n_sl + 14.0))
        comb_err = 0.65 * slot_shrunk + 0.35 * store_shrunk
        bias_map[(sname, hr)] = round(max(-5.0, min(5.0, -0.55 * comb_err)), 2)

    now_mono = time.monotonic()
    _forecast_residual_bias_cache[db_k] = (now_mono, bias_map)
    _forecast_store_acc_cache[db_k] = (now_mono, acc_map)
    return bias_map, acc_map


def refresh_forecast_caches_async(days: int = 21):
    global _forecast_cache_computing
    with _forecast_cache_lock:
        if _forecast_cache_computing:
            return
        _forecast_cache_computing = True

    def _bg():
        global _forecast_cache_computing
        try:
            _refresh_forecast_caches_from_db(days=days)
        except Exception as err:
            print(f"[local_db] refresh_forecast_caches_async error: {err}", file=sys.stderr)
        finally:
            with _forecast_cache_lock:
                _forecast_cache_computing = False

    threading.Thread(target=_bg, daemon=True).start()


def get_cached_forecast_residual_adjustment(store_name: str, hour: int) -> float:
    """Return damped Empirical-Bayes residual adjustment (in wpct points) for `(store_name, hour)` without blocking."""
    if not store_name or not _db_available():
        return 0.0
    db_k = _active_db_key()
    now_mono = time.monotonic()
    cached = _forecast_residual_bias_cache.get(db_k)
    if cached is not None:
        if now_mono - cached[0] >= _FORECAST_RESIDUAL_TTL:
            refresh_forecast_caches_async()
        bmap = cached[1]
        return float(bmap.get((store_name, int(hour)), bmap.get((store_name, None), 0.0)))

    db_cnt = int(get_db_stats().get("count") or 0)
    if db_cnt < 2500 and not _sync_in_progress:
        bmap, _ = _refresh_forecast_caches_from_db()
        return float(bmap.get((store_name, int(hour)), bmap.get((store_name, None), 0.0)))
    refresh_forecast_caches_async()
    return 0.0


def get_cached_store_accuracy_map() -> dict:
    """Return cached per-store 60m forecast accuracy track records without blocking HTTP requests."""
    if not _db_available():
        return {}
    db_k = _active_db_key()
    now_mono = time.monotonic()
    cached = _forecast_store_acc_cache.get(db_k)
    if cached is not None:
        if now_mono - cached[0] >= _FORECAST_STORE_ACC_TTL:
            refresh_forecast_caches_async()
        return cached[1]

    db_cnt = int(get_db_stats().get("count") or 0)
    if db_cnt < 2500 and not _sync_in_progress:
        _, amap = _refresh_forecast_caches_from_db()
        return amap
    refresh_forecast_caches_async()
    return {}


def get_forecast_accuracy_report(
    days: int = 21,
    store_name: str = None,
    limit_recent: int = 30,
    allow_blocking: bool = True,
    auto_backfill: bool = False,
) -> dict:
    """
    Aggregate 60-minute forecast vs. actual verification metrics, hourly/alert/store breakdowns,
    v1 vs. v2.2 live error reduction, recent resolved timeline, and pending live forecast queue.
    """
    import moat_intelligence

    days = max(1, min(60, int(days or 21)))
    limit_recent = max(5, min(100, int(limit_recent or 30)))
    raw_store = (store_name or "").strip()
    store_filter = resolve_canonical_store_name(raw_store) if raw_store else None
    db_k = _active_db_key()
    cache_key = (db_k, days, store_filter, limit_recent)
    now_mono = time.monotonic()

    cached = _forecast_acc_report_cache.get(cache_key)
    if cached is not None and (now_mono - cached[0] < _FORECAST_REPORT_TTL):
        return cached[1]
    if not allow_blocking and cached is not None:
        return cached[1]

    empty_report = {
        "model_version": moat_intelligence.FORECAST_MODEL_VERSION,
        "window_days": days,
        "store_filter": store_filter,
        "store_filter_display": _format_brand_safe_store_name(store_filter) if store_filter else None,
        "holdout_backtest": dict(moat_intelligence.HOLDOUT_BACKTEST_SUMMARY),
        "summary": {
            "resolved_count": 0,
            "pending_count": 0,
            "mae_wpct": None,
            "rmse_wpct": None,
            "bias_wpct": None,
            "within_5pt_pct": None,
            "within_10pt_pct": None,
            "mae_women": None,
            "mae_men": None,
            "direction_hit_pct": None,
            "v1_mae_wpct": None,
            "mae_improvement_pt": None,
            "mae_reduction_pct": None,
        },
        "by_alert": [],
        "by_hour": [],
        "by_store": [],
        "recent_resolved": [],
        "pending_forecasts": [],
        "bias_adjustments": [],
    }

    if not _db_available() or not _ensure_forecast_schema():
        return empty_report

    if auto_backfill and allow_blocking and store_filter is None:
        try:
            backfill_forecast_accuracy_from_history(days=days, min_existing=40, max_rows=4500)
        except Exception:
            pass

    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    cutoff_str = (now_jst - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        if store_filter:
            res_rows = _fetch_all_rows(
                """
                SELECT predicted_at, target_at, resolved_at, store_name,
                       start_women, start_men, start_wpct,
                       pred_women, pred_men, pred_wpct, v1_pred_wpct,
                       flip_risk_pct, alert_code,
                       actual_women, actual_men, actual_wpct,
                       error_wpct_pt, abs_error_wpct_pt, v1_abs_error_wpct_pt,
                       error_women, error_men
                FROM forecast_accuracy_logs
                WHERE resolved_at IS NOT NULL AND predicted_at >= ? AND store_name = ?
                ORDER BY predicted_at DESC
                """,
                (cutoff_str, store_filter),
            )
            pend_cnt_rows = _fetch_all_rows(
                """
                SELECT COUNT(*)
                FROM forecast_accuracy_logs
                WHERE resolved_at IS NULL AND predicted_at >= ? AND store_name = ?
                """,
                (cutoff_str, store_filter),
            )
            pend_rows = _fetch_all_rows(
                """
                SELECT predicted_at, target_at, store_name,
                       start_women, start_men, start_wpct,
                       pred_women, pred_men, pred_wpct, flip_risk_pct, alert_code
                FROM forecast_accuracy_logs
                WHERE resolved_at IS NULL AND predicted_at >= ? AND store_name = ?
                ORDER BY target_at ASC
                LIMIT 40
                """,
                (cutoff_str, store_filter),
            )
        else:
            res_rows = _fetch_all_rows(
                """
                SELECT predicted_at, target_at, resolved_at, store_name,
                       start_women, start_men, start_wpct,
                       pred_women, pred_men, pred_wpct, v1_pred_wpct,
                       flip_risk_pct, alert_code,
                       actual_women, actual_men, actual_wpct,
                       error_wpct_pt, abs_error_wpct_pt, v1_abs_error_wpct_pt,
                       error_women, error_men
                FROM forecast_accuracy_logs
                WHERE resolved_at IS NOT NULL AND predicted_at >= ?
                ORDER BY predicted_at DESC
                """,
                (cutoff_str,),
            )
            pend_cnt_rows = _fetch_all_rows(
                """
                SELECT COUNT(*)
                FROM forecast_accuracy_logs
                WHERE resolved_at IS NULL AND predicted_at >= ?
                """,
                (cutoff_str,),
            )
            pend_rows = _fetch_all_rows(
                """
                SELECT predicted_at, target_at, store_name,
                       start_women, start_men, start_wpct,
                       pred_women, pred_men, pred_wpct, flip_risk_pct, alert_code
                FROM forecast_accuracy_logs
                WHERE resolved_at IS NULL AND predicted_at >= ?
                ORDER BY target_at ASC
                LIMIT 60
                """,
                (cutoff_str,),
            )
        exact_pending_count = int(pend_cnt_rows[0][0] or 0) if pend_cnt_rows else len(pend_rows)
    except Exception as err:
        print(f"[local_db] get_forecast_accuracy_report query error: {err}", file=sys.stderr)
        return empty_report

    import math
    from collections import defaultdict

    wp_abs_list = []
    wp_sq_list = []
    wp_sig_list = []
    v1_abs_list = []
    paired_v2_abs_list = []
    w_abs_list = []
    m_abs_list = []
    dir_hits = 0
    dir_tot = 0

    alert_acc = defaultdict(lambda: [0, 0.0, 0, 0.0, 0.0, 0.0])  # [n, sum_abs, w10, sum_pred, sum_act, sum_sig]
    hour_acc = defaultdict(lambda: [0, 0.0, 0.0, 0, 0, 0.0])      # [n, sum_abs, sum_v1_abs, n_v1, w10, sum_sig]
    store_grp = defaultdict(lambda: [0, 0.0, 0, 0, 0.0, 0.0, 0.0])  # [n, sum_abs, w5, w10, sum_sig, sum_w_abs, sum_m_abs]

    recent_resolved = []

    for r in res_rows:
        pred_at = str(r[0] or "")
        tgt_at = str(r[1] or "")
        res_at = str(r[2] or "")
        sname = str(r[3] or "")
        sw, sm, swp = int(r[4] or 0), int(r[5] or 0), int(r[6] or 0)
        pw, pm, pwp = int(r[7] or 0), int(r[8] or 0), int(r[9] or 0)
        v1_pwp = int(r[10]) if r[10] is not None else None
        flip_r = int(r[11] or 0)
        acode = str(r[12] or "steady")
        aw = int(r[13] or 0)
        am = int(r[14] or 0)
        awp = int(r[15]) if r[15] is not None else None
        err_wp = int(r[16]) if r[16] is not None else None
        abs_wp = int(r[17]) if r[17] is not None else None
        v1_abs = int(r[18]) if r[18] is not None else None
        err_w = int(r[19] or 0)
        err_m = int(r[20] or 0)

        # KPIs require meaningful headcount at t+60m (>=4 guests) to avoid 1-person quantization & closed 0:0 artifacts
        if awp is not None and abs_wp is not None and (aw + am) >= 4:
            wp_abs_list.append(float(abs_wp))
            wp_sq_list.append(float(err_wp * err_wp))
            wp_sig_list.append(float(err_wp))
            w_abs_list.append(abs(err_w))
            m_abs_list.append(abs(err_m))
            if v1_abs is not None:
                v1_abs_list.append(float(v1_abs))
                paired_v2_abs_list.append(float(abs_wp))

            pred_chg = pwp - swp
            act_chg = awp - swp
            if abs(act_chg) >= 3:
                dir_tot += 1
                if (pred_chg >= 0 and act_chg >= 0) or (pred_chg <= 0 and act_chg <= 0) or abs_wp <= 4:
                    dir_hits += 1

            aa = alert_acc[acode]
            aa[0] += 1
            aa[1] += abs_wp
            if abs_wp <= 10:
                aa[2] += 1
            aa[3] += pwp
            aa[4] += awp
            aa[5] += err_wp

            try:
                hr = int(pred_at[11:13])
            except Exception:
                hr = -1
            if hr >= 0:
                ha = hour_acc[hr]
                ha[0] += 1
                ha[1] += abs_wp
                if v1_abs is not None:
                    ha[2] += v1_abs
                    ha[3] += 1
                if abs_wp <= 10:
                    ha[4] += 1
                ha[5] += err_wp

            sg = store_grp[sname]
            sg[0] += 1
            sg[1] += abs_wp
            if abs_wp <= 5:
                sg[2] += 1
            if abs_wp <= 10:
                sg[3] += 1
            sg[4] += err_wp
            sg[5] += abs(err_w)
            sg[6] += abs(err_m)

            if len(recent_resolved) < limit_recent:
                if abs_wp <= 3:
                    grade = "🎯 ピタリ一致"
                elif abs_wp <= 7:
                    grade = "✅ 高精度"
                elif abs_wp <= 12:
                    grade = "🟡 概ね合致"
                else:
                    grade = "⚡ 急変発生"
                recent_resolved.append({
                    "predicted_at": pred_at,
                    "predicted_mdhm": pred_at[5:16].replace("-", "/") if len(pred_at) >= 16 else pred_at,
                    "target_hm": tgt_at[11:16] if len(tgt_at) >= 16 else tgt_at,
                    "resolved_at": res_at,
                    "store_name": sname,
                    "store_slug": _linkable_store_slug(sname),
                    "display_name": _format_brand_safe_store_name(sname),
                    "start_women": sw,
                    "start_men": sm,
                    "start_wpct": swp,
                    "pred_women": pw,
                    "pred_men": pm,
                    "pred_wpct": pwp,
                    "v1_pred_wpct": v1_pwp,
                    "flip_risk_pct": flip_r,
                    "actual_women": aw,
                    "actual_men": am,
                    "actual_wpct": awp,
                    "error_wpct_pt": err_wp,
                    "abs_error_wpct_pt": abs_wp,
                    "alert_code": acode,
                    "alert_label": _ALERT_LABELS_JA.get(acode, acode),
                    "grade": grade,
                })

    n_valid = len(wp_abs_list)
    if n_valid > 0:
        mae_wp = round(sum(wp_abs_list) / n_valid, 2)
        rmse_wp = round(math.sqrt(sum(wp_sq_list) / n_valid), 2)
        bias_wp = round(sum(wp_sig_list) / n_valid, 2)
        w5_pct = round(sum(1 for e in wp_abs_list if e <= 5.0) / n_valid * 100.0, 1)
        w10_pct = round(sum(1 for e in wp_abs_list if e <= 10.0) / n_valid * 100.0, 1)
        if v1_abs_list:
            v1_raw = sum(v1_abs_list) / len(v1_abs_list)
            v2_paired_raw = sum(paired_v2_abs_list) / len(paired_v2_abs_list)
            v1_mae = round(v1_raw, 2)
            imp_pt = round(v1_raw - v2_paired_raw, 2)
            red_pct = round(((v1_raw - v2_paired_raw) / v1_raw) * 100.0, 1) if v1_raw > 0 else None
        else:
            v1_mae = imp_pt = red_pct = None
    else:
        mae_wp = rmse_wp = bias_wp = w5_pct = w10_pct = v1_mae = imp_pt = red_pct = None

    summary = {
        "resolved_count": n_valid,
        "total_resolved_rows": len(res_rows),
        "pending_count": exact_pending_count,
        "mae_wpct": mae_wp,
        "rmse_wpct": rmse_wp,
        "bias_wpct": bias_wp,
        "within_5pt_pct": w5_pct,
        "within_10pt_pct": w10_pct,
        "mae_women": round(sum(w_abs_list) / len(w_abs_list), 2) if w_abs_list else None,
        "mae_men": round(sum(m_abs_list) / len(m_abs_list), 2) if m_abs_list else None,
        "direction_hit_pct": round((dir_hits / float(dir_tot)) * 100.0, 1) if dir_tot > 0 else None,
        "v1_mae_wpct": v1_mae,
        "mae_improvement_pt": imp_pt,
        "mae_reduction_pct": red_pct,
    }

    by_alert = []
    for acode, (cnt, s_abs, w10, s_pred, s_act, s_sig) in sorted(alert_acc.items(), key=lambda kv: kv[1][0], reverse=True):
        by_alert.append({
            "alert_code": acode,
            "label": _ALERT_LABELS_JA.get(acode, acode),
            "count": cnt,
            "mae_wpct": round(s_abs / cnt, 2),
            "within_10pt_pct": round((w10 / float(cnt)) * 100.0, 1),
            "avg_pred_wpct": round(s_pred / cnt, 1),
            "avg_actual_wpct": round(s_act / cnt, 1),
            "bias_wpct": round(s_sig / cnt, 2),
        })

    night_order = [18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5]
    by_hour = []
    for hr in night_order:
        if hr not in hour_acc:
            continue
        cnt, s_abs, s_v1, n_v1, w10, s_sig = hour_acc[hr]
        lbl = f"翌{hr:02d}時台発" if hr < 12 else f"{hr:02d}時台発"
        by_hour.append({
            "hour": hr,
            "slot_label": lbl,
            "count": cnt,
            "mae_wpct": round(s_abs / cnt, 2),
            "v1_mae_wpct": round(s_v1 / n_v1, 2) if n_v1 > 0 else None,
            "within_10pt_pct": round((w10 / float(cnt)) * 100.0, 1),
            "bias_wpct": round(s_sig / cnt, 2),
        })

    bias_cache_entry = _forecast_residual_bias_cache.get(db_k)
    if bias_cache_entry is None and n_valid > 0:
        bias_map_now, _ = _refresh_forecast_caches_from_db(days=days)
    else:
        bias_map_now = bias_cache_entry[1] if bias_cache_entry else {}

    by_store = []
    for sname, (cnt, s_abs, w5, w10, s_sig, sw_a, sm_a) in store_grp.items():
        if cnt < 2 and not store_filter:
            continue
        by_store.append({
            "store_name": sname,
            "store_slug": _linkable_store_slug(sname),
            "display_name": _format_brand_safe_store_name(sname),
            "count": cnt,
            "mae_wpct": round(s_abs / cnt, 2),
            "within_5pt_pct": round((w5 / float(cnt)) * 100.0, 1),
            "within_10pt_pct": round((w10 / float(cnt)) * 100.0, 1),
            "bias_wpct": round(s_sig / cnt, 2),
            "mae_women": round(sw_a / cnt, 1),
            "mae_men": round(sm_a / cnt, 1),
            "online_adj_pt": float(bias_map_now.get((sname, None), 0.0)),
        })
    by_store.sort(key=lambda d: (d["mae_wpct"], -d["count"]))

    pending_list = []
    for pr in pend_rows[:30]:
        p_at = str(pr[0] or "")
        t_at = str(pr[1] or "")
        sn = str(pr[2] or "")
        ac = str(pr[10] or "steady")
        pending_list.append({
            "predicted_at": p_at,
            "predicted_hm": p_at[11:16] if len(p_at) >= 16 else p_at,
            "target_at": t_at,
            "target_hm": t_at[11:16] if len(t_at) >= 16 else t_at,
            "store_name": sn,
            "store_slug": _linkable_store_slug(sn),
            "display_name": _format_brand_safe_store_name(sn),
            "start_women": int(pr[3] or 0),
            "start_men": int(pr[4] or 0),
            "start_wpct": int(pr[5] or 0),
            "pred_women": int(pr[6] or 0),
            "pred_men": int(pr[7] or 0),
            "pred_wpct": int(pr[8] or 0),
            "flip_risk_pct": int(pr[9] or 0),
            "alert_code": ac,
            "alert_label": _ALERT_LABELS_JA.get(ac, ac),
        })

    bias_adjustments = []
    for (sn, hr_k), adj_v in bias_map_now.items():
        if hr_k is None and abs(adj_v) >= 0.4:
            bias_adjustments.append({
                "store_name": sn,
                "store_slug": _linkable_store_slug(sn),
                "display_name": _format_brand_safe_store_name(sn),
                "online_adj_pt": adj_v,
            })
    bias_adjustments.sort(key=lambda x: abs(x["online_adj_pt"]), reverse=True)

    result = {
        "model_version": moat_intelligence.FORECAST_MODEL_VERSION,
        "window_days": days,
        "store_filter": store_filter,
        "store_filter_display": _format_brand_safe_store_name(store_filter) if store_filter else None,
        "holdout_backtest": dict(moat_intelligence.HOLDOUT_BACKTEST_SUMMARY),
        "summary": summary,
        "by_alert": by_alert,
        "by_hour": by_hour,
        "by_store": by_store,
        "recent_resolved": recent_resolved,
        "pending_forecasts": pending_list,
        "bias_adjustments": bias_adjustments[:15],
    }
    if len(_forecast_acc_report_cache) >= 64:
        oldest_k = min(_forecast_acc_report_cache.items(), key=lambda kv: kv[1][0])[0]
        _forecast_acc_report_cache.pop(oldest_k, None)
    _forecast_acc_report_cache[cache_key] = (time.monotonic(), result)
    return result

