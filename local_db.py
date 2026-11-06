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
_moat_rollup_schema_ready = set()
_moat_coverage_cache = {}
_watch_intents_lock = threading.Lock()
_memory_watch_intents = []
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
_recent_trajectory_ring = {}
_recent_trajectory_warmed_at = {}
_recent_trajectory_lock = threading.Lock()
_recent_lags_warming = set()


def clear_store_7d_cache(store_name: str = None, clear_benchmarks: bool = False):
    _db_stats_cache.clear()
    _moat_coverage_cache.clear()
    db_k = _active_db_key()
    if store_name:
        _store_7d_cache.pop((db_k, store_name), None)
    else:
        _store_7d_cache.clear()
        _forecast_acc_report_cache.clear()
        _forecast_store_acc_cache.clear()
        _forecast_residual_bias_cache.clear()
        with _recent_trajectory_lock:
            _recent_trajectory_ring.clear()
            _recent_trajectory_warmed_at.clear()
    if clear_benchmarks:
        _live_benchmarks_cache.clear()
        _benchmark_base_cache.clear()
        _forecast_acc_report_cache.clear()
        _forecast_store_acc_cache.clear()
        _forecast_residual_bias_cache.clear()
        with _recent_trajectory_lock:
            _recent_trajectory_ring.clear()
            _recent_trajectory_warmed_at.clear()


def reset_caches_for_tests():
    """Reset in-memory caches, schema-ready memo sets, and thread-local SQLite read handles for isolated unit tests."""
    clear_store_7d_cache(clear_benchmarks=True)
    _forecast_schema_ready.clear()
    _moat_rollup_schema_ready.clear()
    with _watch_intents_lock:
        _memory_watch_intents.clear()
    conn = getattr(_read_tls, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _read_tls.conn = None
        _read_tls.db_path = None
        _read_tls.db_inode = None


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



def _migrate_forecast_columns(conn, cur):
    """Idempotently add unadjusted baseline & online-bias columns without taking exclusive locks when already present."""
    try:
        cur.execute("SELECT raw_pred_wpct, online_bias_adj FROM forecast_accuracy_logs LIMIT 0")
        return
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    for col_ddl in (
        "ALTER TABLE forecast_accuracy_logs ADD COLUMN raw_pred_wpct INTEGER",
        "ALTER TABLE forecast_accuracy_logs ADD COLUMN online_bias_adj REAL DEFAULT 0.0",
    ):
        try:
            if _is_pg():
                try:
                    cur.execute("SET LOCAL lock_timeout = '2s'")
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            cur.execute(col_ddl)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def _migrate_crowd_votes_columns(conn, cur):
    """Idempotently add optional `wait_minutes` column to `crowd_votes`."""
    try:
        cur.execute("SELECT wait_minutes FROM crowd_votes LIMIT 0")
        return
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    try:
        if _is_pg():
            try:
                cur.execute("SET LOCAL lock_timeout = '2s'")
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
        cur.execute("ALTER TABLE crowd_votes ADD COLUMN wait_minutes INTEGER")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _migrate_external_context_columns(conn, cur):
    """Idempotently add JR transit disruption columns to `external_context_logs`."""
    try:
        cur.execute("SELECT transit_disrupted, transit_lines_json FROM external_context_logs LIMIT 0")
        return
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    for col_ddl in (
        "ALTER TABLE external_context_logs ADD COLUMN transit_disrupted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE external_context_logs ADD COLUMN transit_lines_json TEXT",
    ):
        try:
            if _is_pg():
                try:
                    cur.execute("SET LOCAL lock_timeout = '2s'")
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            cur.execute(col_ddl)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def _create_moat_rollup_tables_on_cursor(cur, is_pg: bool):
    """Create permanent hourly rollups, day context, event history, official arena schedule, and watch intent tables."""
    real_t = "DOUBLE PRECISION" if is_pg else "REAL"
    watch_pk = "id BIGSERIAL PRIMARY KEY" if is_pg else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS store_hourly_rollups (
            date_jst TEXT NOT NULL,
            hour_jst INTEGER NOT NULL,
            business_date TEXT NOT NULL,
            store_name TEXT NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            avg_women {real_t} NOT NULL DEFAULT 0.0,
            max_women INTEGER NOT NULL DEFAULT 0,
            avg_men {real_t} NOT NULL DEFAULT 0.0,
            max_men INTEGER NOT NULL DEFAULT 0,
            avg_wpct {real_t} NOT NULL DEFAULT 0.0,
            avg_men_waiting_groups {real_t},
            max_men_waiting_groups INTEGER,
            avg_men_waiting_guests {real_t},
            avg_matched_groups {real_t},
            avg_vip_guests {real_t},
            precip_mm_max {real_t},
            temp_c_avg {real_t},
            is_rainy INTEGER NOT NULL DEFAULT 0,
            transit_disrupted INTEGER NOT NULL DEFAULT 0,
            arena_event_active INTEGER NOT NULL DEFAULT 0,
            campaign_count INTEGER NOT NULL DEFAULT 0,
            fc_resolved_count INTEGER NOT NULL DEFAULT 0,
            fc_mae_wpct {real_t},
            votes_smooth INTEGER NOT NULL DEFAULT 0,
            votes_waiting INTEGER NOT NULL DEFAULT 0,
            votes_lively INTEGER NOT NULL DEFAULT 0,
            avg_reported_wait_min {real_t},
            PRIMARY KEY (date_jst, hour_jst, store_name)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rollup_store_bdate ON store_hourly_rollups(store_name, business_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rollup_bdate_hour ON store_hourly_rollups(business_date, hour_jst);")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS day_context_logs (
            date_jst TEXT PRIMARY KEY,
            dow INTEGER NOT NULL,
            is_holiday INTEGER NOT NULL DEFAULT 0,
            is_pre_holiday INTEGER NOT NULL DEFAULT 0,
            holiday_streak_pos TEXT NOT NULL DEFAULT 'weekday',
            is_payday_window INTEGER NOT NULL DEFAULT 0,
            is_bonus_window INTEGER NOT NULL DEFAULT 0,
            season_tag TEXT NOT NULL DEFAULT 'regular',
            tags_json TEXT
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_day_ctx_season ON day_context_logs(season_tag, date_jst);")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS store_event_history (
            event_key TEXT PRIMARY KEY,
            brand TEXT NOT NULL,
            store_scope TEXT NOT NULL,
            target_label TEXT,
            event_category TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT,
            first_seen_date TEXT NOT NULL,
            last_seen_date TEXT NOT NULL,
            days_seen INTEGER NOT NULL DEFAULT 1,
            raw_json TEXT
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ev_hist_dates ON store_event_history(last_seen_date, first_seen_date);")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS arena_schedule_events (
            event_date TEXT NOT NULL,
            venue_id TEXT NOT NULL,
            venue_name TEXT NOT NULL,
            city_key TEXT NOT NULL,
            event_title TEXT NOT NULL,
            open_time TEXT,
            start_time TEXT,
            est_end_hour INTEGER NOT NULL DEFAULT 21,
            source_url TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (event_date, venue_id, event_title)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_arena_sched_date ON arena_schedule_events(event_date, city_key);")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS store_watch_intents (
            {watch_pk},
            timestamp TEXT NOT NULL,
            store_name TEXT NOT NULL,
            target_cond TEXT NOT NULL,
            ip_hash TEXT
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_watch_intents_store_ts ON store_watch_intents(store_name, timestamp);")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS web_push_subscriptions (
            endpoint_hash TEXT PRIMARY KEY,
            endpoint TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            store_name TEXT NOT NULL,
            target_cond TEXT NOT NULL,
            ip_hash TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_notified_at TEXT
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_web_push_store_exp ON web_push_subscriptions(store_name, expires_at);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_web_push_endpoint ON web_push_subscriptions(endpoint);")


def _ensure_moat_rollup_schema() -> bool:
    """Ensure permanent moat rollup & first-party tables/columns exist on the active DB (memoized per DB key)."""
    db_k = _active_db_key()
    if db_k in _moat_rollup_schema_ready:
        if _is_pg() or os.path.exists(DB_PATH):
            return True
        _moat_rollup_schema_ready.discard(db_k)

    if _is_pg():
        try:
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    _create_moat_rollup_tables_on_cursor(cur, is_pg=True)
                    conn.commit()
                    _migrate_crowd_votes_columns(conn, cur)
                    _migrate_external_context_columns(conn, cur)
            _moat_rollup_schema_ready.add(db_k)
            return True
        except Exception as err:
            print(f"[local_db] _ensure_moat_rollup_schema PG warning: {err}", file=sys.stderr)
            return False

    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            _create_moat_rollup_tables_on_cursor(cur, is_pg=False)
            conn.commit()
            _migrate_crowd_votes_columns(conn, cur)
            _migrate_external_context_columns(conn, cur)
            _moat_rollup_schema_ready.add(db_k)
            return True
        except Exception as err:
            print(f"[local_db] _ensure_moat_rollup_schema SQLite warning: {err}", file=sys.stderr)
            return False
        finally:
            conn.close()


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
                            ip_hash TEXT,
                            wait_minutes INTEGER
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
                            transit_disrupted INTEGER NOT NULL DEFAULT 0,
                            transit_lines_json TEXT,
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
                            model_version TEXT NOT NULL DEFAULT 'v3.0-statespace',
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
                            raw_pred_wpct INTEGER,
                            online_bias_adj REAL DEFAULT 0.0,
                            UNIQUE(predicted_at, store_name)
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_pending ON forecast_accuracy_logs(resolved_at, target_at);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_store_pred ON forecast_accuracy_logs(store_name, predicted_at);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_resolved_pred ON forecast_accuracy_logs(predicted_at, resolved_at);")
                    _create_moat_rollup_tables_on_cursor(cur, is_pg=True)
                    conn.commit()
                    _migrate_forecast_columns(conn, cur)
                    _migrate_crowd_votes_columns(conn, cur)
                    _migrate_external_context_columns(conn, cur)
            turbo_store.init_postgres_aux_schema()
            _forecast_schema_ready.add(_active_db_key())
            _moat_rollup_schema_ready.add(_active_db_key())
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
                    ip_hash TEXT,
                    wait_minutes INTEGER
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
                    transit_disrupted INTEGER NOT NULL DEFAULT 0,
                    transit_lines_json TEXT,
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
                    model_version TEXT NOT NULL DEFAULT 'v3.0-statespace',
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
                    raw_pred_wpct INTEGER,
                    online_bias_adj REAL DEFAULT 0.0,
                    UNIQUE(predicted_at, store_name)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_pending ON forecast_accuracy_logs(resolved_at, target_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_store_pred ON forecast_accuracy_logs(store_name, predicted_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_resolved_pred ON forecast_accuracy_logs(predicted_at, resolved_at);")
            _create_moat_rollup_tables_on_cursor(cur, is_pg=False)
            conn.commit()
            _migrate_forecast_columns(conn, cur)
            _migrate_crowd_votes_columns(conn, cur)
            _migrate_external_context_columns(conn, cur)
            _forecast_schema_ready.add(_active_db_key())
            _moat_rollup_schema_ready.add(_active_db_key())
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
            _record_snapshot_to_trajectory_ring(items, timestamp_str)
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
    _record_snapshot_to_trajectory_ring(items, timestamp_str)
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
                int(c.get('transit_disrupted', 0) or 0),
                str(c.get('transit_lines_json', '[]')),
            ))
    except Exception:
        ext_rows = []

    if not pos_rows and not ext_rows:
        return

    _ensure_moat_rollup_schema()

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
                        weather_code, wind_kmh, is_rainy, active_arenas_json,
                        transit_disrupted, transit_lines_json
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
                        weather_code, wind_kmh, is_rainy, active_arenas_json,
                        transit_disrupted, transit_lines_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    try:
        response = session.get(url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            return 0
    except Exception as req_err:
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
    inflight_key = (_active_db_key(), store_name)
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

def get_snapshot_around(minutes_ago_min=10, minutes_ago_max=25, now_jst=None):
    """
    Returns {store_name: {'men': m, 'women': w}} from ~15 minutes ago (10-25 min window)
    to calculate real-time 15-min crowd momentum.
    Automatically widens window to 5-45 mins if the primary window has no snapshot.
    Optimized via SQL window function (ROW_NUMBER OVER PARTITION) to fetch only latest row per store directly from DB (BUG-03 / F5.1).
    """
    if now_jst is None:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    elif getattr(now_jst, "tzinfo", None) is not None:
        now_jst = now_jst.astimezone(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    windows = [(minutes_ago_min, minutes_ago_max)]
    if minutes_ago_min == 10 and minutes_ago_max == 25:
        windows.append((5, 45))
    elif minutes_ago_min == 45 and minutes_ago_max == 80:
        windows.append((35, 95))
    elif minutes_ago_min == 95 and minutes_ago_max == 150:
        windows.append((85, 175))
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


def get_multi_horizon_snapshots(now_jst=None) -> dict:
    """
    Return 15m, 60m, and 120m historical snapshots so editorial & column researchers can detect
    60-120 minute crowd waveform dramas (male rush reversals, post-last-train second waves, steady holds).
    """
    return {
        "snap_15m": get_snapshot_around(10, 25, now_jst=now_jst),
        "snap_60m": get_snapshot_around(45, 80, now_jst=now_jst),
        "snap_120m": get_snapshot_around(95, 150, now_jst=now_jst),
    }

VOTE_SHEET_TITLE = 'Crowd Votes'
VOTE_SHEET_HEADER = ['timestamp_jst', 'store_name', 'vote_type', 'voter_hash']
# 荒らし対策のレート制限（10分あたり）
VOTE_LIMIT_PER_VOTER = int(os.environ.get('VOTE_LIMIT_PER_VOTER', '8'))
VOTE_LIMIT_PER_IP = int(os.environ.get('VOTE_LIMIT_PER_IP', '40'))  # 携帯回線のIP共有を考慮して緩め


def _normalize_wait_minutes(wait_minutes):
    if wait_minutes is None or wait_minutes == "":
        return None
    try:
        val = int(wait_minutes)
        if 0 <= val <= 120:
            return val
    except (TypeError, ValueError):
        pass
    return None


def add_crowd_vote(store_name, vote_type, ip_hash="", voter_id="", wait_minutes=None):
    """
    Adds a live user crowd report vote (with optional `wait_minutes` first-party wait report).
    - 重複判定: 端末ID（voter_id）があればそれを優先、なければIPハッシュで「同じ店舗・同じ種類は30分に1回」
      （ただし投票直後に任意の待ち分数サブピルをタップした場合は、票数を増やさず `wait_minutes` のみ更新）
    - レート制限: 端末ごと10分8票、IPごと10分40票（同一回線を複数人が使う携帯回線を考慮）
    - 永続化: PostgreSQL/SQLite（14日経過後はIPハッシュを匿名化して永久保持）＋ Google Sheets 'Crowd Votes'
    """
    if vote_type not in ('smooth', 'waiting', 'lively'):
        return False, "invalid_type"

    wait_val = _normalize_wait_minutes(wait_minutes)
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    cutoff_30m = (now_jst - datetime.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_10m = (now_jst - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    identity = voter_id or ip_hash

    updated_wait_only = False
    with _votes_lock:
        recent_by_voter = 0
        recent_by_ip = 0
        matched_vote = None
        for v in _memory_votes:
            v_identity = v.get('voter_id') or v['ip_hash']
            if v['timestamp'] >= cutoff_10m:
                if v_identity == identity:
                    recent_by_voter += 1
                if ip_hash and v['ip_hash'] == ip_hash:
                    recent_by_ip += 1
            if (
                matched_vote is None
                and v['store_name'] == store_name
                and v['vote_type'] == vote_type
                and v_identity == identity
                and v['timestamp'] >= cutoff_30m
            ):
                matched_vote = v

        if matched_vote is not None:
            if wait_val is not None and matched_vote.get('wait_minutes') != wait_val:
                wait_updates = int(matched_vote.get('wait_updates') or 0)
                if wait_updates >= 3 or recent_by_voter > VOTE_LIMIT_PER_VOTER or recent_by_ip >= VOTE_LIMIT_PER_IP:
                    return False, "rate_limited"
                matched_vote['wait_minutes'] = wait_val
                matched_vote['wait_updates'] = wait_updates + 1
                updated_wait_only = True
            else:
                return False, "already_voted"
        else:
            if recent_by_voter >= VOTE_LIMIT_PER_VOTER or recent_by_ip >= VOTE_LIMIT_PER_IP:
                return False, "rate_limited"
            _memory_votes.append({
                'timestamp': now_str,
                'store_name': store_name,
                'vote_type': vote_type,
                'ip_hash': ip_hash,
                'voter_id': voter_id,
                'wait_minutes': wait_val,
                'wait_updates': 0,
            })
            # Prune memory votes older than 6 hours
            cutoff_6h = (now_jst - datetime.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
            _memory_votes[:] = [v for v in _memory_votes if v['timestamp'] >= cutoff_6h]

    if updated_wait_only:
        _update_recent_vote_wait_minutes(store_name, vote_type, identity, cutoff_30m, wait_val)
        clear_store_7d_cache(store_name)
        return True, "updated_wait"

    _insert_vote_row(now_str, store_name, vote_type, identity, wait_val)
    clear_store_7d_cache(store_name)

    try:
        import sheets_store
        sheets_store.append_rows_async(VOTE_SHEET_TITLE, VOTE_SHEET_HEADER, [[now_str, store_name, vote_type, identity]])
    except Exception:
        pass

    return True, "ok"


def _update_recent_vote_wait_minutes(store_name, vote_type, identity, cutoff_30m, wait_val):
    _ensure_moat_rollup_schema()
    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE crowd_votes
                        SET wait_minutes = %s
                        WHERE id = (
                            SELECT id FROM crowd_votes
                            WHERE store_name = %s AND vote_type = %s AND ip_hash = %s AND timestamp >= %s
                            ORDER BY timestamp DESC LIMIT 1
                        )
                        """,
                        (wait_val, store_name, vote_type, identity, cutoff_30m),
                    )
                conn.commit()
            return
        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE crowd_votes
                    SET wait_minutes = ?
                    WHERE id = (
                        SELECT id FROM crowd_votes
                        WHERE store_name = ? AND vote_type = ? AND ip_hash = ? AND timestamp >= ?
                        ORDER BY timestamp DESC LIMIT 1
                    )
                    """,
                    (wait_val, store_name, vote_type, identity, cutoff_30m),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _insert_vote_row(ts, store_name, vote_type, identity, wait_minutes=None):
    _ensure_moat_rollup_schema()
    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash, wait_minutes)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (ts, store_name, vote_type, identity, wait_minutes),
                    )
                conn.commit()
            return
        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash, wait_minutes)
                    VALUES (?, ?, ?, ?, ?)
                """, (ts, store_name, vote_type, identity, wait_minutes))
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


def _business_date_for_ymd_hour(date_str: str, hour_jst: int) -> str:
    if hour_jst <= 6:
        try:
            d_obj = datetime.date.fromisoformat(date_str[:10]) - datetime.timedelta(days=1)
            return d_obj.strftime("%Y-%m-%d")
        except Exception:
            return date_str[:10]
    return date_str[:10]


def _contiguous_date_ranges(sorted_dates: list) -> list:
    """Split sorted YYYY-MM-DD strings into contiguous `(start_ymd, end_ymd)` intervals so sparse rollups never scan intervening months."""
    ranges = []
    start = None
    prev = None
    for ds in sorted_dates:
        try:
            d = datetime.date.fromisoformat(str(ds)[:10])
        except ValueError:
            continue
        if prev is not None and (d - prev).days == 1:
            prev = d
            continue
        if start is not None:
            ranges.append((start.isoformat(), prev.isoformat()))
        start = prev = d
    if start is not None:
        ranges.append((start.isoformat(), prev.isoformat()))
    return ranges


def _fetch_rows_by_date_ranges(sql_qmark: str, sorted_dates: list, date_only: bool = False) -> list:
    """Execute `sql_qmark` per contiguous date interval instead of a single wide `min_ts..max_ts` scan."""
    out = []
    for d0, d1 in _contiguous_date_ranges(sorted_dates):
        params = (d0, d1) if date_only else (f"{d0} 00:00:00", f"{d1} 23:59:59")
        out.extend(_fetch_all_rows(sql_qmark, params))
    return out


def rollup_store_hourly_stats(target_dates=None, days_back: int = 3) -> dict:
    """
    Permanently roll up 10-minute `monitor_logs`, `store_pos_telemetry_logs`, `external_context_logs`,
    `forecast_accuracy_logs`, `crowd_votes`, `arena_schedule_events`, and `store_event_history`
    into 1-hour `store_hourly_rollups` + `day_context_logs` so raw log pruning NEVER loses long-term moat history.
    """
    if not _db_available() or not _ensure_moat_rollup_schema():
        return {"ok": False, "upserted_rollups": 0, "upserted_days": 0}

    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    date_set = set()
    if target_dates:
        for d in target_dates:
            ds = str(d or "").strip()[:10]
            if len(ds) == 10 and ds[4] == "-" and ds[7] == "-":
                date_set.add(ds)
    else:
        for off in range(max(1, int(days_back))):
            date_set.add((now_jst.date() - datetime.timedelta(days=off)).strftime("%Y-%m-%d"))

    if not date_set:
        return {"ok": True, "upserted_rollups": 0, "upserted_days": 0}

    sorted_dates = sorted(date_set)
    target_date_lookup = set(sorted_dates)

    # Also include business dates (previous day for 00:00-06:59) in day_context_logs
    ctx_dates = set(sorted_dates)
    for ds in sorted_dates:
        try:
            prev_d = (datetime.date.fromisoformat(ds) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            ctx_dates.add(prev_d)
        except Exception:
            pass

    try:
        import arena_calendar
        import moat_intelligence
        day_ctx_rows = []
        for ds in sorted(ctx_dates):
            c = arena_calendar.classify_day_context(ds)
            day_ctx_rows.append((
                c["date_jst"],
                int(c["dow"]),
                int(c["is_holiday"]),
                int(c["is_pre_holiday"]),
                str(c["holiday_streak_pos"]),
                int(c["is_payday_window"]),
                int(c["is_bonus_window"]),
                str(c["season_tag"]),
                str(c["tags_json"]),
            ))

        mon_rows = _fetch_rows_by_date_ranges(
            """
            SELECT timestamp, store_name, women, men
            FROM monitor_logs
            WHERE timestamp >= ? AND timestamp <= ?
            """,
            sorted_dates,
        )
        pos_rows = _fetch_rows_by_date_ranges(
            """
            SELECT timestamp, store_name, men_waiting_groups, men_waiting_guests, matched_groups, vip_guests
            FROM store_pos_telemetry_logs
            WHERE timestamp >= ? AND timestamp <= ?
            """,
            sorted_dates,
        )
        ext_rows = _fetch_rows_by_date_ranges(
            """
            SELECT timestamp, city_key, precip_mm, temp_c, is_rainy, active_arenas_json,
                   COALESCE(transit_disrupted, 0)
            FROM external_context_logs
            WHERE timestamp >= ? AND timestamp <= ?
            """,
            sorted_dates,
        )
        fc_rows = _fetch_rows_by_date_ranges(
            """
            SELECT predicted_at, store_name, abs_error_wpct_pt
            FROM forecast_accuracy_logs
            WHERE resolved_at IS NOT NULL AND abs_error_wpct_pt IS NOT NULL
              AND predicted_at >= ? AND predicted_at <= ?
            """,
            sorted_dates,
        )
        vote_rows = _fetch_rows_by_date_ranges(
            """
            SELECT timestamp, store_name, vote_type, wait_minutes
            FROM crowd_votes
            WHERE timestamp >= ? AND timestamp <= ?
            """,
            sorted_dates,
        )
        arena_sched_rows = _fetch_rows_by_date_ranges(
            """
            SELECT event_date, venue_id, est_end_hour
            FROM arena_schedule_events
            WHERE event_date >= ? AND event_date <= ?
            """,
            sorted_dates,
            date_only=True,
        )
        ev_hist_rows = []
        for d0, d1 in _contiguous_date_ranges(sorted_dates):
            ev_hist_rows.extend(_fetch_all_rows(
                """
                SELECT brand, store_scope, first_seen_date, last_seen_date
                FROM store_event_history
                WHERE last_seen_date >= ? AND first_seen_date <= ?
                """,
                (d0, d1),
            ))

        from collections import defaultdict
        buckets = defaultdict(lambda: [0, 0.0, 0, 0.0, 0, 0.0, 0])  # [n, sum_w, max_w, sum_m, max_m, sum_wp, wp_n]
        for ts_s, sname, w, m in mon_rows:
            ts_s = str(ts_s or "")
            sname = str(sname or "").strip()
            if len(ts_s) < 13 or not sname or _is_korea_store_name(sname) or _is_synthetic_seed_alias(sname):
                continue
            ds = ts_s[:10]
            if ds not in target_date_lookup:
                continue
            try:
                hr = int(ts_s[11:13])
            except Exception:
                continue
            wv = max(0, int(w or 0))
            mv = max(0, int(m or 0))
            b = buckets[(ds, hr, sname)]
            b[0] += 1
            b[1] += wv
            if wv > b[2]:
                b[2] = wv
            b[3] += mv
            if mv > b[4]:
                b[4] = mv
            if (wv + mv) >= 4:
                b[5] += (wv / float(wv + mv)) * 100.0
                b[6] += 1

        pos_agg = defaultdict(lambda: [0, 0.0, 0, 0.0, 0.0, 0.0])  # [n, sum_mwg, max_mwg, sum_mwp, sum_mg, sum_vip]
        for ts_s, sname, mwg, mwp, mg, vip in pos_rows:
            ts_s = str(ts_s or "")
            sname = str(sname or "").strip()
            if len(ts_s) < 13 or not sname or mwg is None:
                continue
            ds = ts_s[:10]
            if ds not in target_date_lookup:
                continue
            try:
                hr = int(ts_s[11:13])
            except Exception:
                continue
            p = pos_agg[(ds, hr, sname)]
            mwg_i = int(mwg)
            p[0] += 1
            p[1] += mwg_i
            if mwg_i > p[2]:
                p[2] = mwg_i
            p[3] += int(mwp or 0)
            p[4] += int(mg or 0)
            p[5] += int(vip or 0)

        ext_agg = defaultdict(lambda: [0, 0.0, 0.0, 0, 0, set(), 0, 0])  # (ds, hr, ck) -> [n, max_prec, sum_temp, rainy, transit, arenas_set, temp_n, prec_n]
        for ts_s, ck, prec, tc, rainy, ar_json, tr_dis in ext_rows:
            ts_s = str(ts_s or "")
            ck = str(ck or "").strip()
            if len(ts_s) < 13 or not ck:
                continue
            ds = ts_s[:10]
            if ds not in target_date_lookup:
                continue
            try:
                hr = int(ts_s[11:13])
            except Exception:
                continue
            e = ext_agg[(ds, hr, ck)]
            e[0] += 1
            if prec is not None:
                pr_f = float(prec)
                if pr_f > e[1]:
                    e[1] = pr_f
                e[7] += 1
            if tc is not None:
                e[2] += float(tc)
                e[6] += 1
            if int(rainy or 0) > 0:
                e[3] = 1
            if int(tr_dis or 0) > 0:
                e[4] = 1
            if ar_json and ar_json not in ("[]", ""):
                try:
                    for vid in json.loads(ar_json):
                        if vid:
                            e[5].add(str(vid))
                except Exception:
                    pass

        sched_arena_slots = defaultdict(set)  # (ds, vid) -> set of active end hours
        for ev_d, vid, est_h in arena_sched_rows:
            eh = int(est_h or 21)
            for h_cand in (eh - 1, eh, eh + 1):
                if 0 <= h_cand <= 23:
                    sched_arena_slots[(str(ev_d), str(vid))].add(h_cand)

        fc_agg = defaultdict(lambda: [0, 0.0])
        for p_at, sname, abs_e in fc_rows:
            p_at = str(p_at or "")
            if len(p_at) < 13:
                continue
            ds = p_at[:10]
            if ds not in target_date_lookup:
                continue
            try:
                hr = int(p_at[11:13])
            except Exception:
                continue
            fa = fc_agg[(ds, hr, str(sname or "").strip())]
            fa[0] += 1
            fa[1] += float(abs_e or 0.0)

        vt_agg = defaultdict(lambda: [0, 0, 0, 0.0, 0])  # [sm, wt, lv, sum_wait, n_wait]
        for ts_s, sname, vtype, wmin in vote_rows:
            ts_s = str(ts_s or "")
            if len(ts_s) < 13:
                continue
            ds = ts_s[:10]
            if ds not in target_date_lookup:
                continue
            try:
                hr = int(ts_s[11:13])
            except Exception:
                continue
            va = vt_agg[(ds, hr, str(sname or "").strip())]
            if vtype == "smooth":
                va[0] += 1
            elif vtype == "waiting":
                va[1] += 1
            elif vtype == "lively":
                va[2] += 1
            if wmin is not None:
                va[3] += float(wmin)
                va[4] += 1

        def _store_brand_family(sname: str) -> str:
            if sname.startswith("OLG ag"):
                return "ag"
            if sname.startswith("OLG "):
                return "olg"
            if sname.startswith("JIS "):
                return "jis"
            if sname.startswith("ASOBIBAR"):
                return "asobibar"
            if sname.startswith("YATAKOI"):
                return "yatakoi"
            return "other"

        def _campaign_count_for(sname: str, bdate: str) -> int:
            if not ev_hist_rows:
                return 0
            cnt = 0
            fam = _store_brand_family(sname)
            seen_ev = set()
            for br, scope, f_d, l_d in ev_hist_rows:
                if not (str(f_d) <= bdate <= str(l_d)):
                    continue
                ev_sig = (str(br), str(scope), str(f_d))
                if ev_sig in seen_ev:
                    continue
                sc = str(scope or "")
                br_s = str(br or "")
                matched = False
                if sc == sname:
                    matched = True
                elif sc == "ALL":
                    if fam == "jis" and "JIS" in br_s.upper():
                        matched = True
                    elif fam == "ag" and br_s.lower() in ("ag", "ag（アグ）"):
                        matched = True
                    elif fam == "olg" and br_s in ("オリエンタルラウンジ", "OLG"):
                        matched = True
                    elif fam == "asobibar" and "ASOBIBAR" in br_s.upper():
                        matched = True
                    elif fam == "yatakoi" and ("ヤタコイ" in br_s or "YATAKOI" in br_s.upper()):
                        matched = True
                elif sc and sname in sc.split(","):
                    matched = True
                if matched:
                    seen_ev.add(ev_sig)
                    cnt += 1
            return cnt

        rollup_rows = []
        for (ds, hr, sname), b in buckets.items():
            n_s = b[0]
            if n_s <= 0:
                continue
            bdate = _business_date_for_ymd_hour(ds, hr)
            avg_w = round(b[1] / n_s, 2)
            avg_m = round(b[3] / n_s, 2)
            avg_wp = round(b[5] / b[6], 2) if b[6] > 0 else 0.0

            p = pos_agg.get((ds, hr, sname))
            avg_mwg = round(p[1] / p[0], 2) if p and p[0] > 0 else None
            max_mwg = int(p[2]) if p and p[0] > 0 else None
            avg_mwp = round(p[3] / p[0], 2) if p and p[0] > 0 else None
            avg_mg = round(p[4] / p[0], 2) if p and p[0] > 0 else None
            avg_vip = round(p[5] / p[0], 2) if p and p[0] > 0 else None

            ck = moat_intelligence.city_key_for_store(sname)
            e = ext_agg.get((ds, hr, ck)) if ck else None
            prec_max = round(e[1], 2) if e and e[7] > 0 else None
            temp_avg = round(e[2] / e[6], 2) if e and e[6] > 0 else None
            is_rain = int(e[3]) if e else 0
            tr_dis = int(e[4]) if e else 0

            hub = moat_intelligence._STORE_TO_ARENA.get(sname)
            arena_act = 0
            if hub:
                vid = hub["venue_id"]
                if (e and vid in e[5]) or (hr in sched_arena_slots.get((ds, vid), set())):
                    arena_act = 1

            camp_cnt = _campaign_count_for(sname, bdate)
            fa = fc_agg.get((ds, hr, sname))
            fc_n = int(fa[0]) if fa else 0
            fc_mae = round(fa[1] / fa[0], 2) if fa and fa[0] > 0 else None

            va = vt_agg.get((ds, hr, sname))
            v_sm = int(va[0]) if va else 0
            v_wt = int(va[1]) if va else 0
            v_lv = int(va[2]) if va else 0
            avg_rep_w = round(va[3] / va[4], 1) if va and va[4] > 0 else None

            rollup_rows.append((
                ds, hr, bdate, sname, n_s,
                avg_w, int(b[2]), avg_m, int(b[4]), avg_wp,
                avg_mwg, max_mwg, avg_mwp, avg_mg, avg_vip,
                prec_max, temp_avg, is_rain, tr_dis, arena_act,
                camp_cnt, fc_n, fc_mae, v_sm, v_wt, v_lv, avg_rep_w,
            ))

        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    for dr in day_ctx_rows:
                        cur.execute(
                            """
                            INSERT INTO day_context_logs (
                                date_jst, dow, is_holiday, is_pre_holiday, holiday_streak_pos,
                                is_payday_window, is_bonus_window, season_tag, tags_json
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (date_jst) DO UPDATE SET
                                dow = EXCLUDED.dow,
                                is_holiday = EXCLUDED.is_holiday,
                                is_pre_holiday = EXCLUDED.is_pre_holiday,
                                holiday_streak_pos = EXCLUDED.holiday_streak_pos,
                                is_payday_window = EXCLUDED.is_payday_window,
                                is_bonus_window = EXCLUDED.is_bonus_window,
                                season_tag = EXCLUDED.season_tag,
                                tags_json = EXCLUDED.tags_json
                            """,
                            dr,
                        )
                conn.commit()
            if rollup_rows:
                turbo_store.execute_pg_values_batch(
                    """
                    INSERT INTO store_hourly_rollups (
                        date_jst, hour_jst, business_date, store_name, sample_count,
                        avg_women, max_women, avg_men, max_men, avg_wpct,
                        avg_men_waiting_groups, max_men_waiting_groups, avg_men_waiting_guests,
                        avg_matched_groups, avg_vip_guests,
                        precip_mm_max, temp_c_avg, is_rainy, transit_disrupted, arena_event_active,
                        campaign_count, fc_resolved_count, fc_mae_wpct,
                        votes_smooth, votes_waiting, votes_lively, avg_reported_wait_min
                    ) VALUES %s
                    ON CONFLICT (date_jst, hour_jst, store_name) DO UPDATE SET
                        business_date = EXCLUDED.business_date,
                        sample_count = EXCLUDED.sample_count,
                        avg_women = EXCLUDED.avg_women,
                        max_women = EXCLUDED.max_women,
                        avg_men = EXCLUDED.avg_men,
                        max_men = EXCLUDED.max_men,
                        avg_wpct = EXCLUDED.avg_wpct,
                        avg_men_waiting_groups = COALESCE(EXCLUDED.avg_men_waiting_groups, store_hourly_rollups.avg_men_waiting_groups),
                        max_men_waiting_groups = COALESCE(EXCLUDED.max_men_waiting_groups, store_hourly_rollups.max_men_waiting_groups),
                        avg_men_waiting_guests = COALESCE(EXCLUDED.avg_men_waiting_guests, store_hourly_rollups.avg_men_waiting_guests),
                        avg_matched_groups = COALESCE(EXCLUDED.avg_matched_groups, store_hourly_rollups.avg_matched_groups),
                        avg_vip_guests = COALESCE(EXCLUDED.avg_vip_guests, store_hourly_rollups.avg_vip_guests),
                        precip_mm_max = COALESCE(EXCLUDED.precip_mm_max, store_hourly_rollups.precip_mm_max),
                        temp_c_avg = COALESCE(EXCLUDED.temp_c_avg, store_hourly_rollups.temp_c_avg),
                        is_rainy = GREATEST(EXCLUDED.is_rainy, store_hourly_rollups.is_rainy),
                        transit_disrupted = GREATEST(EXCLUDED.transit_disrupted, store_hourly_rollups.transit_disrupted),
                        arena_event_active = GREATEST(EXCLUDED.arena_event_active, store_hourly_rollups.arena_event_active),
                        campaign_count = GREATEST(EXCLUDED.campaign_count, store_hourly_rollups.campaign_count),
                        fc_resolved_count = GREATEST(EXCLUDED.fc_resolved_count, store_hourly_rollups.fc_resolved_count),
                        fc_mae_wpct = COALESCE(EXCLUDED.fc_mae_wpct, store_hourly_rollups.fc_mae_wpct),
                        votes_smooth = GREATEST(EXCLUDED.votes_smooth, store_hourly_rollups.votes_smooth),
                        votes_waiting = GREATEST(EXCLUDED.votes_waiting, store_hourly_rollups.votes_waiting),
                        votes_lively = GREATEST(EXCLUDED.votes_lively, store_hourly_rollups.votes_lively),
                        avg_reported_wait_min = COALESCE(EXCLUDED.avg_reported_wait_min, store_hourly_rollups.avg_reported_wait_min)
                    """,
                    rollup_rows,
                    page_size=400,
                )
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    cur.executemany(
                        """
                        INSERT INTO day_context_logs (
                            date_jst, dow, is_holiday, is_pre_holiday, holiday_streak_pos,
                            is_payday_window, is_bonus_window, season_tag, tags_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (date_jst) DO UPDATE SET
                            dow = excluded.dow,
                            is_holiday = excluded.is_holiday,
                            is_pre_holiday = excluded.is_pre_holiday,
                            holiday_streak_pos = excluded.holiday_streak_pos,
                            is_payday_window = excluded.is_payday_window,
                            is_bonus_window = excluded.is_bonus_window,
                            season_tag = excluded.season_tag,
                            tags_json = excluded.tags_json
                        """,
                        day_ctx_rows,
                    )
                    if rollup_rows:
                        cur.executemany(
                            """
                            INSERT INTO store_hourly_rollups (
                                date_jst, hour_jst, business_date, store_name, sample_count,
                                avg_women, max_women, avg_men, max_men, avg_wpct,
                                avg_men_waiting_groups, max_men_waiting_groups, avg_men_waiting_guests,
                                avg_matched_groups, avg_vip_guests,
                                precip_mm_max, temp_c_avg, is_rainy, transit_disrupted, arena_event_active,
                                campaign_count, fc_resolved_count, fc_mae_wpct,
                                votes_smooth, votes_waiting, votes_lively, avg_reported_wait_min
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (date_jst, hour_jst, store_name) DO UPDATE SET
                                business_date = excluded.business_date,
                                sample_count = excluded.sample_count,
                                avg_women = excluded.avg_women,
                                max_women = excluded.max_women,
                                avg_men = excluded.avg_men,
                                max_men = excluded.max_men,
                                avg_wpct = excluded.avg_wpct,
                                avg_men_waiting_groups = COALESCE(excluded.avg_men_waiting_groups, store_hourly_rollups.avg_men_waiting_groups),
                                max_men_waiting_groups = COALESCE(excluded.max_men_waiting_groups, store_hourly_rollups.max_men_waiting_groups),
                                avg_men_waiting_guests = COALESCE(excluded.avg_men_waiting_guests, store_hourly_rollups.avg_men_waiting_guests),
                                avg_matched_groups = COALESCE(excluded.avg_matched_groups, store_hourly_rollups.avg_matched_groups),
                                avg_vip_guests = COALESCE(excluded.avg_vip_guests, store_hourly_rollups.avg_vip_guests),
                                precip_mm_max = COALESCE(excluded.precip_mm_max, store_hourly_rollups.precip_mm_max),
                                temp_c_avg = COALESCE(excluded.temp_c_avg, store_hourly_rollups.temp_c_avg),
                                is_rainy = MAX(excluded.is_rainy, store_hourly_rollups.is_rainy),
                                transit_disrupted = MAX(excluded.transit_disrupted, store_hourly_rollups.transit_disrupted),
                                arena_event_active = MAX(excluded.arena_event_active, store_hourly_rollups.arena_event_active),
                                campaign_count = MAX(excluded.campaign_count, store_hourly_rollups.campaign_count),
                                fc_resolved_count = MAX(excluded.fc_resolved_count, store_hourly_rollups.fc_resolved_count),
                                fc_mae_wpct = COALESCE(excluded.fc_mae_wpct, store_hourly_rollups.fc_mae_wpct),
                                votes_smooth = MAX(excluded.votes_smooth, store_hourly_rollups.votes_smooth),
                                votes_waiting = MAX(excluded.votes_waiting, store_hourly_rollups.votes_waiting),
                                votes_lively = MAX(excluded.votes_lively, store_hourly_rollups.votes_lively),
                                avg_reported_wait_min = COALESCE(excluded.avg_reported_wait_min, store_hourly_rollups.avg_reported_wait_min)
                            """,
                            rollup_rows,
                        )
                    conn.commit()
                finally:
                    conn.close()
        _moat_coverage_cache.clear()
        return {"ok": True, "upserted_rollups": len(rollup_rows), "upserted_days": len(day_ctx_rows)}
    except Exception as err:
        print(f"[local_db] rollup_store_hourly_stats error: {err}", file=sys.stderr)
        return {"ok": False, "upserted_rollups": 0, "upserted_days": 0, "error": str(err)}


def prune_old_logs(monitor_days: int = 90, vote_days: int = 14) -> dict:
    """
    Safely maintain raw 10-min tables while guaranteeing ZERO permanent data loss:
    1. Raw `monitor_logs`, POS, weather, and forecast logs are retained for `monitor_days` (default 90 days).
    2. Before deleting ANY raw rows older than `monitor_days`, all expiring dates (plus recent 3 days)
       are permanently rolled up into `store_hourly_rollups` and `day_context_logs`. If rollup fails, deletion is aborted.
    3. `crowd_votes` and `store_watch_intents` are NEVER physically deleted — rows older than `vote_days`
       simply have `ip_hash` cleared (`''`) for privacy while preserving 100% of first-party vote & wait statistics.
    """
    _ensure_moat_rollup_schema()
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    cutoff_date = now_jst.date() - datetime.timedelta(days=max(31, int(monitor_days)))
    mon_cutoff = cutoff_date.strftime("%Y-%m-%d") + " 00:00:00"
    vote_cutoff = (now_jst - datetime.timedelta(days=max(2, int(vote_days)))).strftime("%Y-%m-%d %H:%M:%S")

    # Step 1: Identify expiring raw dates (bounded to 7 days/run to guarantee low memory) + recent 3 days and roll up BEFORE pruning
    rollup_ok = True
    rolled_up_count = 0
    delete_before = mon_cutoff
    try:
        expiring_date_rows = _fetch_all_rows(
            "SELECT DISTINCT SUBSTR(timestamp, 1, 10) FROM monitor_logs WHERE timestamp < ?",
            (mon_cutoff,),
        )
        expiring_dates = sorted({str(r[0]) for r in expiring_date_rows if r and r[0]})[:7]
        if expiring_dates:
            last_rolled_next = datetime.date.fromisoformat(expiring_dates[-1]) + datetime.timedelta(days=1)
            delete_before = min(mon_cutoff, last_rolled_next.strftime("%Y-%m-%d") + " 00:00:00")
        dates_to_rollup = set(expiring_dates)
        for off in range(3):
            dates_to_rollup.add((now_jst.date() - datetime.timedelta(days=off)).strftime("%Y-%m-%d"))
        if dates_to_rollup:
            r_res = rollup_store_hourly_stats(target_dates=sorted(dates_to_rollup))
            rollup_ok = bool(r_res.get("ok", False))
            rolled_up_count = int(r_res.get("upserted_rollups") or 0)
    except Exception as err:
        print(f"[local_db] pre-prune rollup failed, aborting raw log deletion: {err}", file=sys.stderr)
        rollup_ok = False

    deleted_mon = 0
    anonymized_votes = 0
    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    if rollup_ok:
                        cur.execute("DELETE FROM monitor_logs WHERE timestamp < %s", (delete_before,))
                        deleted_mon = max(0, cur.rowcount or 0)
                        try:
                            cur.execute("DELETE FROM store_pos_telemetry_logs WHERE timestamp < %s", (delete_before,))
                            cur.execute("DELETE FROM external_context_logs WHERE timestamp < %s", (delete_before,))
                            cur.execute("DELETE FROM forecast_accuracy_logs WHERE predicted_at < %s", (delete_before,))
                        except Exception:
                            pass
                    cur.execute(
                        "UPDATE crowd_votes SET ip_hash = '' WHERE timestamp < %s AND ip_hash IS NOT NULL AND ip_hash != ''",
                        (vote_cutoff,),
                    )
                    anonymized_votes = max(0, cur.rowcount or 0)
                    try:
                        cur.execute(
                            "UPDATE store_watch_intents SET ip_hash = '' WHERE timestamp < %s AND ip_hash IS NOT NULL AND ip_hash != ''",
                            (vote_cutoff,),
                        )
                        cur.execute(
                            "DELETE FROM web_push_subscriptions WHERE expires_at < %s",
                            (now_jst.strftime("%Y-%m-%d %H:%M:%S"),),
                        )
                        cur.execute(
                            "UPDATE web_push_subscriptions SET ip_hash = '' WHERE created_at < %s AND ip_hash IS NOT NULL AND ip_hash != ''",
                            (vote_cutoff,),
                        )
                    except Exception:
                        pass
                conn.commit()
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    if rollup_ok:
                        cur.execute("DELETE FROM monitor_logs WHERE timestamp < ?", (delete_before,))
                        deleted_mon = max(0, cur.rowcount or 0)
                        try:
                            cur.execute("DELETE FROM store_pos_telemetry_logs WHERE timestamp < ?", (delete_before,))
                            cur.execute("DELETE FROM external_context_logs WHERE timestamp < ?", (delete_before,))
                            cur.execute("DELETE FROM forecast_accuracy_logs WHERE predicted_at < ?", (delete_before,))
                        except Exception:
                            pass
                    cur.execute(
                        "UPDATE crowd_votes SET ip_hash = '' WHERE timestamp < ? AND ip_hash IS NOT NULL AND ip_hash != ''",
                        (vote_cutoff,),
                    )
                    anonymized_votes = max(0, cur.rowcount or 0)
                    try:
                        cur.execute(
                            "UPDATE store_watch_intents SET ip_hash = '' WHERE timestamp < ? AND ip_hash IS NOT NULL AND ip_hash != ''",
                            (vote_cutoff,),
                        )
                        cur.execute(
                            "DELETE FROM web_push_subscriptions WHERE expires_at < ?",
                            (now_jst.strftime("%Y-%m-%d %H:%M:%S"),),
                        )
                        cur.execute(
                            "UPDATE web_push_subscriptions SET ip_hash = '' WHERE created_at < ? AND ip_hash IS NOT NULL AND ip_hash != ''",
                            (vote_cutoff,),
                        )
                    except Exception:
                        pass
                    conn.commit()
                finally:
                    conn.close()
        if deleted_mon > 0:
            clear_store_7d_cache(clear_benchmarks=True)
    except Exception as err:
        print(f"[local_db] prune_old_logs error: {err}", file=sys.stderr)
    return {
        'deleted_monitor_logs': deleted_mon,
        'deleted_crowd_votes': 0,
        'anonymized_crowd_votes': anonymized_votes,
        'rolled_up_hours': rolled_up_count,
        'rollup_ok': rollup_ok,
    }


def get_recent_votes(store_name=None, hours=4):
    """Returns aggregated vote counts and optional first-party wait-time average in the last `hours` hours."""
    _ensure_moat_rollup_schema()
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    cutoff_str = (now_jst - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    counts_by_store = {}
    wait_acc = {}  # sname -> [sum_wait, cnt_wait]

    def _default_vote_dict():
        return {
            'smooth': 0,
            'waiting': 0,
            'lively': 0,
            'reported_wait_avg': None,
            'reported_wait_samples': 0,
        }

    def _ensure(sname):
        if sname not in counts_by_store:
            counts_by_store[sname] = _default_vote_dict()

    used_db = False
    try:
        if store_name:
            rows = _fetch_all_rows("""
                SELECT store_name, vote_type, COUNT(*) as cnt,
                       SUM(CASE WHEN wait_minutes IS NOT NULL THEN wait_minutes ELSE 0 END) as w_sum,
                       COUNT(wait_minutes) as w_cnt
                FROM crowd_votes
                WHERE store_name = ? AND timestamp >= ?
                GROUP BY store_name, vote_type
            """, (store_name, cutoff_str))
        else:
            rows = _fetch_all_rows("""
                SELECT store_name, vote_type, COUNT(*) as cnt,
                       SUM(CASE WHEN wait_minutes IS NOT NULL THEN wait_minutes ELSE 0 END) as w_sum,
                       COUNT(wait_minutes) as w_cnt
                FROM crowd_votes
                WHERE timestamp >= ?
                GROUP BY store_name, vote_type
            """, (cutoff_str,))
        for r in rows:
            sname = r[0]
            vtype = r[1]
            _ensure(sname)
            if vtype in ('smooth', 'waiting', 'lively'):
                counts_by_store[sname][vtype] = int(r[2] or 0)
            w_cnt = int(r[4] or 0) if len(r) > 4 else 0
            if w_cnt > 0:
                wa = wait_acc.setdefault(sname, [0.0, 0])
                wa[0] += float(r[3] or 0.0)
                wa[1] += w_cnt
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
                    if vtype in ('smooth', 'waiting', 'lively'):
                        counts_by_store[sname][vtype] += 1
                    if v.get('wait_minutes') is not None:
                        wa = wait_acc.setdefault(sname, [0.0, 0])
                        wa[0] += float(v['wait_minutes'])
                        wa[1] += 1

    for sname, (w_sum, w_cnt) in wait_acc.items():
        if w_cnt > 0 and sname in counts_by_store:
            counts_by_store[sname]['reported_wait_avg'] = round(w_sum / w_cnt, 1)
            counts_by_store[sname]['reported_wait_samples'] = int(w_cnt)

    if store_name:
        return counts_by_store.get(store_name, _default_vote_dict())
    return counts_by_store


def _classify_event_moat_category(title: str, gender: str = "") -> str:
    t = str(title or "")
    if any(k in t for k in ("クリスマス", "Xmas", "X'mas", "忘年会", "年末", "カウントダウン", "新年", "ハロウィン", "バレンタイン")):
        return "seasonal_party"
    if gender == "men" or any(k in t for k in ("男性", "半額", "早割", "定額", "プラン", "飲み放題")):
        return "mens_discount"
    if gender == "women" or any(k in t for k in ("女性", "女子", "ギフト", "プレゼント", "コスメ", "スイーツ", "美容", "マイル", "キャッシュバック")):
        return "ladies_perk"
    if any(k in t for k in ("フェア", "フード", "グルメ", "北海道", "韓国", "肉", "寿司", "いちご")):
        return "food_fair"
    return "other"


def upsert_store_event_history(events_list: list, date_jst: str = None) -> int:
    """
    Persist daily event/campaign observations into `store_event_history` so campaign start/end dates
    and seasonal elasticity are permanently retained instead of overwritten in `events_cache.json`.
    """
    if not events_list or not _db_available() or not _ensure_moat_rollup_schema():
        return 0
    import hashlib
    import re
    if not date_jst:
        date_jst = datetime.datetime.now(_JST_TZ).strftime("%Y-%m-%d")
    date_jst = str(date_jst)[:10]

    rows = []
    seen_keys = set()
    for ev in events_list:
        if not isinstance(ev, dict):
            continue
        title = str(ev.get("title") or "").strip()
        if not title:
            continue
        brand_raw = str(ev.get("brand") or "").strip()
        if brand_raw == "OLG":
            brand = "オリエンタルラウンジ"
        elif brand_raw == "YATAKOI":
            brand = "ヤタコイ"
        else:
            brand = brand_raw or "その他"

        sname = str(ev.get("store_name") or "").strip()
        t_stores = ev.get("target_stores") or []
        if sname:
            canon = resolve_canonical_store_name(sname) or sname
            scope = canon
        elif t_stores:
            resolved_ts = [resolve_canonical_store_name(x) or str(x) for x in t_stores if x]
            scope = ",".join(sorted(set(resolved_ts)))[:240]
        else:
            scope = "ALL"

        norm_title = re.sub(r"\s+", "", title)
        ev_key = hashlib.sha1(f"{brand}|{scope}|{norm_title}".encode("utf-8")).hexdigest()[:24]
        if ev_key in seen_keys:
            continue
        seen_keys.add(ev_key)
        cat = _classify_event_moat_category(title, str(ev.get("gender") or ""))
        rows.append((
            ev_key,
            brand,
            scope,
            str(ev.get("target_label") or "")[:80],
            cat,
            title[:160],
            str(ev.get("url") or "")[:300],
            date_jst,
            date_jst,
            1,
            json.dumps(ev, ensure_ascii=False),
        ))

    if not rows:
        return 0

    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    for r in rows:
                        cur.execute(
                            """
                            INSERT INTO store_event_history (
                                event_key, brand, store_scope, target_label, event_category,
                                title, source_url, first_seen_date, last_seen_date, days_seen, raw_json
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (event_key) DO UPDATE SET
                                first_seen_date = LEAST(store_event_history.first_seen_date, EXCLUDED.first_seen_date),
                                days_seen = CASE
                                    WHEN store_event_history.last_seen_date < EXCLUDED.last_seen_date
                                    THEN store_event_history.days_seen + 1
                                    ELSE store_event_history.days_seen
                                END,
                                last_seen_date = GREATEST(store_event_history.last_seen_date, EXCLUDED.last_seen_date),
                                source_url = COALESCE(NULLIF(EXCLUDED.source_url, ''), store_event_history.source_url),
                                raw_json = EXCLUDED.raw_json
                            """,
                            r,
                        )
                conn.commit()
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    cur.executemany(
                        """
                        INSERT INTO store_event_history (
                            event_key, brand, store_scope, target_label, event_category,
                            title, source_url, first_seen_date, last_seen_date, days_seen, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (event_key) DO UPDATE SET
                            first_seen_date = MIN(store_event_history.first_seen_date, excluded.first_seen_date),
                            days_seen = CASE
                                WHEN store_event_history.last_seen_date < excluded.last_seen_date
                                THEN store_event_history.days_seen + 1
                                ELSE store_event_history.days_seen
                            END,
                            last_seen_date = MAX(store_event_history.last_seen_date, excluded.last_seen_date),
                            source_url = COALESCE(NULLIF(excluded.source_url, ''), store_event_history.source_url),
                            raw_json = excluded.raw_json
                        """,
                        rows,
                    )
                    conn.commit()
                finally:
                    conn.close()
        _moat_coverage_cache.clear()
        return len(rows)
    except Exception as err:
        print(f"[local_db] upsert_store_event_history error: {err}", file=sys.stderr)
        return 0


def get_event_history_summary(days: int = 90) -> dict:
    """Return archived campaign/event breakdown and recent observations from `store_event_history`."""
    if not _db_available() or not _ensure_moat_rollup_schema():
        return {"total_archived": 0, "by_category": {}, "recent_events": [], "seasonal_events": []}
    cutoff_d = (datetime.datetime.now(_JST_TZ).date() - datetime.timedelta(days=max(7, int(days)))).strftime("%Y-%m-%d")
    try:
        rows = _fetch_all_rows(
            """
            SELECT brand, store_scope, target_label, event_category, title, source_url,
                   first_seen_date, last_seen_date, days_seen
            FROM store_event_history
            WHERE last_seen_date >= ?
            ORDER BY last_seen_date DESC, days_seen DESC
            LIMIT 200
            """,
            (cutoff_d,),
        )
    except Exception:
        rows = []

    import store_master
    by_cat = {"ladies_perk": 0, "mens_discount": 0, "seasonal_party": 0, "food_fair": 0, "other": 0}
    recent_list = []
    seasonal_list = []
    for r in rows:
        br = str(r[0] or "")
        if br == "OLG":
            br = "オリエンタルラウンジ"
        elif br == "JIS":
            br = "JIS（ジス）"
        cat = str(r[3] or "other")
        by_cat[cat] = by_cat.get(cat, 0) + 1
        scope_raw = str(r[1] or "")
        if scope_raw == "ALL":
            scope_disp = str(r[2] or "ブランド共通")
        elif "," in scope_raw:
            parts = [store_master.seo_name(p) for p in scope_raw.split(",") if p]
            scope_disp = f"{parts[0]} ほか計{len(parts)}店" if len(parts) > 1 else (parts[0] if parts else "")
        else:
            scope_disp = store_master.seo_name(scope_raw)
        item = {
            "brand": br,
            "scope_display": scope_disp,
            "category": cat,
            "title": str(r[4] or ""),
            "url": str(r[5] or ""),
            "first_seen_date": str(r[6] or ""),
            "last_seen_date": str(r[7] or ""),
            "days_seen": int(r[8] or 1),
        }
        if len(recent_list) < 25:
            recent_list.append(item)
        if cat == "seasonal_party" and len(seasonal_list) < 20:
            seasonal_list.append(item)

    return {
        "total_archived": len(rows),
        "by_category": by_cat,
        "recent_events": recent_list,
        "seasonal_events": seasonal_list,
    }


def upsert_arena_schedule_events(events_list: list) -> int:
    """Persist official public dome/arena event schedule rows into `arena_schedule_events`."""
    if not events_list or not _db_available() or not _ensure_moat_rollup_schema():
        return 0
    now_str = datetime.datetime.now(_JST_TZ).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    seen = set()
    for ev in events_list:
        if not isinstance(ev, dict):
            continue
        ed = str(ev.get("event_date") or "").strip()[:10]
        vid = str(ev.get("venue_id") or "").strip()
        title = str(ev.get("event_title") or "").strip()[:120]
        if not ed or not vid or not title or (ed, vid, title) in seen:
            continue
        seen.add((ed, vid, title))
        rows.append((
            ed,
            vid,
            str(ev.get("venue_name") or "")[:80],
            str(ev.get("city_key") or "")[:32],
            title,
            str(ev.get("open_time") or "")[:16],
            str(ev.get("start_time") or "")[:16],
            int(ev.get("est_end_hour") or 21),
            str(ev.get("source_url") or "")[:300],
            now_str,
        ))
    if not rows:
        return 0
    try:
        if _is_pg():
            turbo_store.execute_pg_values_batch(
                """
                INSERT INTO arena_schedule_events (
                    event_date, venue_id, venue_name, city_key, event_title,
                    open_time, start_time, est_end_hour, source_url, updated_at
                ) VALUES %s
                ON CONFLICT (event_date, venue_id, event_title) DO UPDATE SET
                    open_time = EXCLUDED.open_time,
                    start_time = EXCLUDED.start_time,
                    est_end_hour = EXCLUDED.est_end_hour,
                    updated_at = EXCLUDED.updated_at
                """,
                rows,
                page_size=300,
            )
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    cur.executemany(
                        """
                        INSERT INTO arena_schedule_events (
                            event_date, venue_id, venue_name, city_key, event_title,
                            open_time, start_time, est_end_hour, source_url, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (event_date, venue_id, event_title) DO UPDATE SET
                            open_time = excluded.open_time,
                            start_time = excluded.start_time,
                            est_end_hour = excluded.est_end_hour,
                            updated_at = excluded.updated_at
                        """,
                        rows,
                    )
                    conn.commit()
                finally:
                    conn.close()
        _moat_coverage_cache.clear()
        return len(rows)
    except Exception as err:
        print(f"[local_db] upsert_arena_schedule_events error: {err}", file=sys.stderr)
        return 0


def get_upcoming_arena_events(days_ahead: int = 60, from_date: str = None, limit: int = 80) -> list:
    """Return upcoming official dome/arena events from DB (falling back to cached `arena_calendar` if DB is empty)."""
    if not from_date:
        from_date = datetime.datetime.now(_JST_TZ).strftime("%Y-%m-%d")
    from_date = str(from_date)[:10]
    try:
        end_date = (datetime.date.fromisoformat(from_date) + datetime.timedelta(days=max(1, int(days_ahead)))).strftime("%Y-%m-%d")
    except Exception:
        end_date = "2027-01-31"

    out = []
    if _db_available() and _ensure_moat_rollup_schema():
        try:
            rows = _fetch_all_rows(
                """
                SELECT event_date, venue_id, venue_name, city_key, event_title,
                       open_time, start_time, est_end_hour, source_url
                FROM arena_schedule_events
                WHERE event_date >= ? AND event_date <= ?
                ORDER BY event_date ASC, venue_id ASC
                LIMIT ?
                """,
                (from_date, end_date, int(limit)),
            )
            for r in rows:
                out.append({
                    "event_date": str(r[0]),
                    "venue_id": str(r[1]),
                    "venue_name": str(r[2]),
                    "city_key": str(r[3]),
                    "event_title": str(r[4]),
                    "open_time": str(r[5] or ""),
                    "start_time": str(r[6] or ""),
                    "est_end_hour": int(r[7] or 21),
                    "source_url": str(r[8] or ""),
                })
        except Exception:
            out = []

    if not out:
        try:
            import arena_calendar
            for ev in arena_calendar.get_arena_schedule_events(allow_blocking=False):
                ed = str(ev.get("event_date") or "")
                if from_date <= ed <= end_date:
                    out.append(dict(ev))
                    if len(out) >= limit:
                        break
        except Exception:
            pass
    return out


def add_watch_intent(store_name: str, target_cond: str = "female_55", ip_hash: str = "", voter_id: str = "") -> tuple:
    """
    Record an anonymous first-party latent entry-intent signal when a user arms an in-page condition watch
    (e.g. `female_55`: 女性比率55%以上になったら通知, `zero_wait`: 男性待ち解消で通知, `evening_peak`: 今夜の狙い目監視).
    Rate-limiting (`20/10m`) is strictly enforced per pure `ip_hash` so rotating `voter_id` cannot bypass it.
    """
    if target_cond not in ("female_55", "zero_wait", "evening_peak"):
        return False, "invalid_cond"
    canon = resolve_canonical_store_name(store_name) or str(store_name or "").strip()
    if not canon:
        return False, "invalid_store"

    _ensure_moat_rollup_schema()
    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    cutoff_15m = (now_jst - datetime.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_10m = (now_jst - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_6h = (now_jst - datetime.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    identity = voter_id or ip_hash

    with _watch_intents_lock:
        ip_cnt_10m = 0
        already = False
        for item in _memory_watch_intents:
            if ip_hash and item.get("ip_hash") == ip_hash and item["timestamp"] >= cutoff_10m:
                ip_cnt_10m += 1
            if (
                identity
                and item.get("identity") == identity
                and item["store_name"] == canon
                and item["target_cond"] == target_cond
                and item["timestamp"] >= cutoff_15m
            ):
                already = True
        if ip_cnt_10m >= 20:
            return False, "rate_limited"
        if already:
            return False, "already_watching"
        _memory_watch_intents.append({
            "timestamp": now_str,
            "store_name": canon,
            "target_cond": target_cond,
            "ip_hash": ip_hash,
            "identity": identity,
        })
        _memory_watch_intents[:] = [w for w in _memory_watch_intents if w["timestamp"] >= cutoff_6h]

    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO store_watch_intents (timestamp, store_name, target_cond, ip_hash) VALUES (%s, %s, %s, %s)",
                        (now_str, canon, target_cond, identity),
                    )
                conn.commit()
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO store_watch_intents (timestamp, store_name, target_cond, ip_hash) VALUES (?, ?, ?, ?)",
                        (now_str, canon, target_cond, identity),
                    )
                    conn.commit()
                finally:
                    conn.close()
        _moat_coverage_cache.clear()
    except Exception:
        pass
    return True, "ok"


def get_watch_intent_counts(store_name: str = None, hours: int = 6) -> dict:
    """Return unique active latent watcher counts (`COUNT(DISTINCT ...)`) in the last `hours` hours."""
    _ensure_moat_rollup_schema()
    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    cutoff_str = (now_jst - datetime.timedelta(hours=max(1, int(hours)))).strftime("%Y-%m-%d %H:%M:%S")
    canon = resolve_canonical_store_name(store_name) if store_name else None
    try:
        if canon:
            rows = _fetch_all_rows(
                "SELECT COUNT(DISTINCT CASE WHEN ip_hash != '' THEN ip_hash ELSE CAST(id AS TEXT) END) "
                "FROM store_watch_intents WHERE store_name = ? AND timestamp >= ?",
                (canon, cutoff_str),
            )
            return {"store_name": canon, "watching_count": int(rows[0][0] or 0) if rows else 0}
        rows = _fetch_all_rows(
            "SELECT store_name, COUNT(DISTINCT CASE WHEN ip_hash != '' THEN ip_hash ELSE CAST(id AS TEXT) END) "
            "FROM store_watch_intents WHERE timestamp >= ? GROUP BY store_name",
            (cutoff_str,),
        )
        return {str(r[0]): int(r[1] or 0) for r in rows}
    except Exception:
        return {"store_name": canon, "watching_count": 0} if canon else {}


def _push_sub_hash(endpoint: str, store_name: str) -> str:
    import hashlib
    return hashlib.sha256(f"{endpoint}|{store_name}".encode("utf-8")).hexdigest()


def upsert_web_push_subscription(
    endpoint: str,
    p256dh: str,
    auth: str,
    store_name: str,
    target_cond: str = "female_55",
    ip_hash: str = "",
    ttl_days: int = 30,
    max_per_endpoint: int = 5,
    record_watch_intent: bool = False,
) -> tuple[bool, str]:
    """Persist or update a browser Web Push subscription for a store condition watch."""
    _ensure_moat_rollup_schema()
    canon = resolve_canonical_store_name(store_name) if store_name else ""
    if not endpoint or not p256dh or not auth or not canon:
        return False, "invalid_params"
    if target_cond not in ("female_55", "zero_wait", "evening_peak"):
        return False, "invalid_cond"

    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    now_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    exp_str = (now_jst + datetime.timedelta(days=max(1, min(90, int(ttl_days))))).strftime("%Y-%m-%d %H:%M:%S")
    ep_hash = _push_sub_hash(endpoint, canon)
    clean_ip = str(ip_hash or "")[:64]

    try:
        existing_rows = _fetch_all_rows(
            "SELECT store_name, endpoint_hash FROM web_push_subscriptions WHERE endpoint = ? AND expires_at >= ?",
            (endpoint, now_str),
        )
        existing_stores = {str(r[0]): str(r[1]) for r in existing_rows}
        is_new_store_sub = canon not in existing_stores
        if is_new_store_sub and len(existing_stores) >= max_per_endpoint:
            # Evict oldest active store subscription for this endpoint so user never hits an unexpected wall
            oldest_rows = _fetch_all_rows(
                "SELECT endpoint_hash FROM web_push_subscriptions WHERE endpoint = ? AND expires_at >= ? ORDER BY created_at ASC LIMIT 1",
                (endpoint, now_str),
            )
            if oldest_rows:
                old_h = str(oldest_rows[0][0])
                if _is_pg():
                    with turbo_store.pg_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM web_push_subscriptions WHERE endpoint_hash = %s", (old_h,))
                        conn.commit()
                else:
                    with _db_lock:
                        conn = get_connection()
                        try:
                            conn.execute("DELETE FROM web_push_subscriptions WHERE endpoint_hash = ?", (old_h,))
                            conn.commit()
                        finally:
                            conn.close()

        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO web_push_subscriptions
                            (endpoint_hash, endpoint, p256dh, auth, store_name, target_cond, ip_hash, created_at, expires_at, last_notified_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                        ON CONFLICT (endpoint_hash) DO UPDATE SET
                            p256dh = EXCLUDED.p256dh,
                            auth = EXCLUDED.auth,
                            target_cond = EXCLUDED.target_cond,
                            expires_at = EXCLUDED.expires_at
                        """,
                        (ep_hash, endpoint, p256dh, auth, canon, target_cond, clean_ip, now_str, exp_str),
                    )
                conn.commit()
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    conn.execute(
                        """
                        INSERT INTO web_push_subscriptions
                            (endpoint_hash, endpoint, p256dh, auth, store_name, target_cond, ip_hash, created_at, expires_at, last_notified_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        ON CONFLICT(endpoint_hash) DO UPDATE SET
                            p256dh = excluded.p256dh,
                            auth = excluded.auth,
                            target_cond = excluded.target_cond,
                            expires_at = excluded.expires_at
                        """,
                        (ep_hash, endpoint, p256dh, auth, canon, target_cond, clean_ip, now_str, exp_str),
                    )
                    conn.commit()
                finally:
                    conn.close()
        if record_watch_intent and is_new_store_sub:
            try:
                add_watch_intent(canon, target_cond=target_cond, ip_hash=clean_ip)
            except Exception:
                pass
        return True, ep_hash
    except Exception as err:
        print(f"[local_db] upsert_web_push_subscription error: {err}", file=sys.stderr)
        return False, "db_error"


def delete_web_push_subscription(endpoint: str, store_name: str = None) -> int:
    """Remove a browser Web Push subscription (single store or all stores for `endpoint`)."""
    _ensure_moat_rollup_schema()
    if not endpoint:
        return 0
    canon = resolve_canonical_store_name(store_name) if store_name else None
    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    if canon:
                        cur.execute(
                            "DELETE FROM web_push_subscriptions WHERE endpoint_hash = %s",
                            (_push_sub_hash(endpoint, canon),),
                        )
                    else:
                        cur.execute("DELETE FROM web_push_subscriptions WHERE endpoint = %s", (endpoint,))
                    removed = max(0, cur.rowcount or 0)
                conn.commit()
                return removed
        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                if canon:
                    cur.execute(
                        "DELETE FROM web_push_subscriptions WHERE endpoint_hash = ?",
                        (_push_sub_hash(endpoint, canon),),
                    )
                else:
                    cur.execute("DELETE FROM web_push_subscriptions WHERE endpoint = ?", (endpoint,))
                removed = max(0, cur.rowcount or 0)
                conn.commit()
                return removed
            finally:
                conn.close()
    except Exception:
        return 0


def get_active_web_push_subscriptions(
    limit: int = 300,
    cooldown_cutoff_str: str = None,
    now_str: str = None,
) -> list[dict]:
    """Return bounded active Web Push subscriptions (`expires_at >= now`), oldest-unnotified first."""
    _ensure_moat_rollup_schema()
    active_now = now_str or datetime.datetime.now(_JST_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    lim = max(1, min(1000, int(limit or 300)))
    try:
        if cooldown_cutoff_str:
            rows = _fetch_all_rows(
                "SELECT endpoint_hash, endpoint, p256dh, auth, store_name, target_cond, last_notified_at "
                "FROM web_push_subscriptions "
                "WHERE expires_at >= ? AND (last_notified_at IS NULL OR last_notified_at = '' OR last_notified_at < ?) "
                "ORDER BY COALESCE(last_notified_at, '') ASC, created_at ASC LIMIT ?",
                (active_now, str(cooldown_cutoff_str), lim),
            )
        else:
            rows = _fetch_all_rows(
                "SELECT endpoint_hash, endpoint, p256dh, auth, store_name, target_cond, last_notified_at "
                "FROM web_push_subscriptions "
                "WHERE expires_at >= ? "
                "ORDER BY COALESCE(last_notified_at, '') ASC, created_at ASC LIMIT ?",
                (active_now, lim),
            )
        return [
            {
                "endpoint_hash": str(r[0]),
                "endpoint": str(r[1]),
                "p256dh": str(r[2]),
                "auth": str(r[3]),
                "store_name": str(r[4]),
                "target_cond": str(r[5]),
                "last_notified_at": str(r[6]) if r[6] else "",
            }
            for r in rows
        ]
    except Exception:
        return []


def mark_web_push_notified(endpoint_hashes: list[str], notified_at_str: str = None) -> int:
    """Update `last_notified_at` for dispatched subscriptions in safe chunks to enforce cooldown."""
    if not endpoint_hashes:
        return 0
    _ensure_moat_rollup_schema()
    ts_str = notified_at_str or datetime.datetime.now(_JST_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    hashes = list(dict.fromkeys(str(h) for h in endpoint_hashes if h))[:1000]
    if not hashes:
        return 0
    total_cnt = 0
    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    for idx in range(0, len(hashes), 200):
                        chunk = hashes[idx : idx + 200]
                        placeholders = ",".join(["%s"] * len(chunk))
                        cur.execute(
                            f"UPDATE web_push_subscriptions SET last_notified_at = %s WHERE endpoint_hash IN ({placeholders})",
                            tuple([ts_str] + chunk),
                        )
                        total_cnt += max(0, cur.rowcount or 0)
                conn.commit()
                return total_cnt
        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                for idx in range(0, len(hashes), 200):
                    chunk = hashes[idx : idx + 200]
                    placeholders = ",".join(["?"] * len(chunk))
                    cur.execute(
                        f"UPDATE web_push_subscriptions SET last_notified_at = ? WHERE endpoint_hash IN ({placeholders})",
                        tuple([ts_str] + chunk),
                    )
                    total_cnt += max(0, cur.rowcount or 0)
                conn.commit()
                return total_cnt
            finally:
                conn.close()
    except Exception:
        return total_cnt


def purge_dead_web_push_endpoints(endpoints: list[str]) -> int:
    """Delete dead/unsubscribed endpoints (HTTP 404 / 410 Gone from Push service) in safe chunks."""
    if not endpoints:
        return 0
    _ensure_moat_rollup_schema()
    eps = list(dict.fromkeys(str(e) for e in endpoints if e))[:1000]
    if not eps:
        return 0
    total_cnt = 0
    try:
        if _is_pg():
            with turbo_store.pg_connection() as conn:
                with conn.cursor() as cur:
                    for idx in range(0, len(eps), 200):
                        chunk = eps[idx : idx + 200]
                        placeholders = ",".join(["%s"] * len(chunk))
                        cur.execute(
                            f"DELETE FROM web_push_subscriptions WHERE endpoint IN ({placeholders})",
                            tuple(chunk),
                        )
                        total_cnt += max(0, cur.rowcount or 0)
                conn.commit()
                return total_cnt
        with _db_lock:
            conn = get_connection()
            try:
                cur = conn.cursor()
                for idx in range(0, len(eps), 200):
                    chunk = eps[idx : idx + 200]
                    placeholders = ",".join(["?"] * len(chunk))
                    cur.execute(
                        f"DELETE FROM web_push_subscriptions WHERE endpoint IN ({placeholders})",
                        tuple(chunk),
                    )
                    total_cnt += max(0, cur.rowcount or 0)
                conn.commit()
                return total_cnt
            finally:
                conn.close()
    except Exception:
        return total_cnt


def get_moat_coverage_summary() -> dict:
    """Return cached high-level counts of Aiseki Navi's proprietary multi-layer dataset."""
    db_k = _active_db_key()
    now_m = time.monotonic()
    cached = _moat_coverage_cache.get(db_k)
    if cached and (now_m - cached[0]) < 90.0:
        return dict(cached[1])

    out = {
        "raw_snapshots": 0,
        "min_timestamp": None,
        "max_timestamp": None,
        "rolled_up_hours": 0,
        "pos_telemetry_rows": 0,
        "weather_context_rows": 0,
        "event_history_count": 0,
        "arena_events_count": 0,
        "crowd_votes_total": 0,
        "watch_intents_total": 0,
        "raw_retention_days": 90,
    }
    if not _db_available():
        return out

    _ensure_moat_rollup_schema()
    db_st = get_db_stats()
    out["raw_snapshots"] = int(db_st.get("count") or 0)
    out["min_timestamp"] = db_st.get("min_timestamp")
    out["max_timestamp"] = db_st.get("max_timestamp")
    try:
        rows = _fetch_all_rows(
            "SELECT "
            "(SELECT COUNT(*) FROM store_hourly_rollups), "
            "(SELECT COUNT(*) FROM store_pos_telemetry_logs), "
            "(SELECT COUNT(*) FROM external_context_logs), "
            "(SELECT COUNT(*) FROM store_event_history), "
            "(SELECT COUNT(*) FROM arena_schedule_events), "
            "(SELECT COUNT(*) FROM crowd_votes), "
            "(SELECT COUNT(*) FROM store_watch_intents)"
        )
        if rows and rows[0]:
            r = rows[0]
            out["rolled_up_hours"] = int(r[0] or 0)
            out["pos_telemetry_rows"] = int(r[1] or 0)
            out["weather_context_rows"] = int(r[2] or 0)
            out["event_history_count"] = int(r[3] or 0)
            out["arena_events_count"] = int(r[4] or 0)
            out["crowd_votes_total"] = int(r[5] or 0)
            out["watch_intents_total"] = int(r[6] or 0)
    except Exception:
        pass

    _moat_coverage_cache[db_k] = (now_m, dict(out))
    return out


def get_monthly_store_stats_rows(days: int = 30) -> list:
    """
    Build clean CC BY 4.0 primary statistical rows per domestic store over the past `days` days
    for `/stats/monthly.csv` and LLM/researcher citation. Strictly uses `store_master.seo_name`
    so standalone 'オリエンタル' or 'OLG' never appears.
    """
    import store_master
    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    cutoff_ts = (now_jst - datetime.timedelta(days=max(7, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
    master = store_master.load() or {}
    valid_domestic = _get_valid_domestic_canonical_stores()

    agg = {}
    if _db_available():
        try:
            rows = _fetch_all_rows(
                """
                SELECT store_name,
                       COUNT(*) AS samples,
                       AVG(women) AS avg_w,
                       MAX(women) AS max_w,
                       AVG(men) AS avg_m,
                       MAX(men) AS max_m,
                       AVG(CASE WHEN (women + men) >= 4 THEN (women * 100.0) / (women + men) ELSE NULL END) AS avg_wpct
                FROM monitor_logs
                WHERE timestamp >= ? AND (women > 0 OR men > 0)
                GROUP BY store_name
                """,
                (cutoff_ts,),
            )
            for r in rows:
                sn = str(r[0] or "").strip()
                if not sn or _is_korea_store_name(sn) or _is_synthetic_seed_alias(sn):
                    continue
                if valid_domestic and sn not in valid_domestic:
                    continue
                agg[sn] = {
                    "samples": int(r[1] or 0),
                    "avg_women": round(float(r[2] or 0.0), 1),
                    "max_women": int(r[3] or 0),
                    "avg_men": round(float(r[4] or 0.0), 1),
                    "max_men": int(r[5] or 0),
                    "avg_wpct": round(float(r[6]), 1) if r[6] is not None else None,
                }
        except Exception:
            agg = {}

    brand_ja_map = {
        "olg": "オリエンタルラウンジ",
        "oriental": "オリエンタルラウンジ",
        "ag": "ag（アグ）",
        "jis": "JIS（ジス）",
        "asobibar": "ASOBIBAR（アソビバー）",
        "yatakoi": "ヤタコイ",
        "alfa": "ALFA（アルファ）",
        "clovers": "CLOVERS（クローバーズ）",
        "xix": "XIX（キックス）",
    }

    result = []
    for sname in sorted(agg.keys()):
        st = agg[sname]
        if st["samples"] < 3:
            continue
        info = master.get(sname) or {}
        br_code = str(info.get("brand") or "").lower()
        br_label = brand_ja_map.get(br_code)
        if not br_label:
            if sname.startswith("OLG ag"):
                br_label = "ag（アグ）"
            elif sname.startswith("OLG "):
                br_label = "オリエンタルラウンジ"
            elif sname.startswith("JIS "):
                br_label = "JIS（ジス）"
            elif sname.startswith("ASOBIBAR"):
                br_label = "ASOBIBAR（アソビバー）"
            elif sname.startswith("YATAKOI"):
                br_label = "ヤタコイ"
            else:
                br_label = "相席ラウンジ"
        slug = _linkable_store_slug(sname)
        seo_nm = store_master.seo_name(sname)
        result.append({
            "store_slug": slug,
            "store_name": seo_nm,
            "brand_name": br_label,
            "area_label": str(info.get("area_label") or ""),
            "window_days": int(days),
            "sample_count": st["samples"],
            "avg_women": st["avg_women"],
            "peak_women": st["max_women"],
            "avg_men": st["avg_men"],
            "peak_men": st["max_men"],
            "avg_female_ratio_pct": st["avg_wpct"] if st["avg_wpct"] is not None else "",
            "store_url": f"https://aiseki-navi.com/store/{slug}" if slug else "https://aiseki-navi.com/stats",
        })
    result.sort(key=lambda x: (-x["peak_women"], -x["sample_count"]))
    return result


def get_year_end_whitepaper_data() -> dict:
    """
    Assemble strictly factual, zero-fabrication primary statistics for `/stats/year-end-2026`:
    - Actual observed weekday vs. Friday/Saturday pre-holiday crowd & female-ratio comparison from `monitor_logs`
    - Upcoming Nov-Dec 2026 official dome/arena concerts that trigger terminal lounge post-show inflows
    - Archived seasonal/discount campaigns from `store_event_history`
    - Deterministic 2026 year-end calendar phases (`classify_day_context`) with honest 'observed vs. upcoming' status
    """
    import arena_calendar
    import moat_intelligence
    import store_master

    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    today_str = now_jst.strftime("%Y-%m-%d")
    cutoff_30d = (now_jst - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    # 1) Observed pre-holiday (Fri/Sat/pre-holiday) vs regular weekday evening metrics (20:00-23:59)
    wd_stats = {
        "pre_holiday": {"samples": 0, "sum_w": 0.0, "sum_m": 0.0, "sum_wp": 0.0, "wp_n": 0},
        "weekday": {"samples": 0, "sum_w": 0.0, "sum_m": 0.0, "sum_wp": 0.0, "wp_n": 0},
    }
    store_peaks = {}
    if _db_available():
        try:
            rows = _fetch_all_rows(
                """
                SELECT SUBSTR(timestamp, 1, 10) AS d_str,
                       CAST(SUBSTR(timestamp, 12, 2) AS INTEGER) AS hr,
                       store_name, women, men
                FROM monitor_logs
                WHERE timestamp >= ? AND (women + men) >= 6
                """,
                (cutoff_30d,),
            )
            day_pre_memo = {}
            for r in rows:
                ds = str(r[0] or "")
                hr = int(r[1] or 0)
                sn = str(r[2] or "").strip()
                if not ds or not sn or _is_korea_store_name(sn) or _is_synthetic_seed_alias(sn):
                    continue
                wv = max(0, int(r[3] or 0))
                mv = max(0, int(r[4] or 0))
                if 20 <= hr <= 23:
                    memo = day_pre_memo.get(ds)
                    if memo is None:
                        ctx = arena_calendar.classify_day_context(ds)
                        memo = (
                            bool(ctx.get("is_pre_holiday")),
                            int(ctx.get("dow", 0)) == 6 or bool(ctx.get("is_holiday")),
                        )
                        day_pre_memo[ds] = memo
                    is_pre, is_rest_day = memo
                    # Exclude non-pre-holiday Sundays/holidays so 'weekday' strictly represents Mon-Thu worknights
                    if is_pre or not is_rest_day:
                        bucket = wd_stats["pre_holiday" if is_pre else "weekday"]
                        bucket["samples"] += 1
                        bucket["sum_w"] += wv
                        bucket["sum_m"] += mv
                        bucket["sum_wp"] += (wv / float(wv + mv)) * 100.0
                        bucket["wp_n"] += 1
                prev_pk = store_peaks.get(sn)
                if prev_pk is None or (wv + mv) > prev_pk["total"]:
                    store_peaks[sn] = {"women": wv, "men": mv, "total": wv + mv, "date": ds}
        except Exception:
            pass

    def _fmt_bucket(b):
        if b["samples"] < 5:
            return {"samples": b["samples"], "avg_women": None, "avg_men": None, "avg_wpct": None}
        return {
            "samples": b["samples"],
            "avg_women": round(b["sum_w"] / b["samples"], 1),
            "avg_men": round(b["sum_m"] / b["samples"], 1),
            "avg_wpct": round(b["sum_wp"] / b["wp_n"], 1) if b["wp_n"] > 0 else None,
        }

    top_peak_stores = []
    for sn, pk in sorted(store_peaks.items(), key=lambda kv: kv[1]["total"], reverse=True)[:10]:
        slug = _linkable_store_slug(sn)
        top_peak_stores.append({
            "store_name": store_master.seo_name(sn),
            "store_slug": slug,
            "women": pk["women"],
            "men": pk["men"],
            "total": pk["total"],
            "date": pk["date"],
        })

    # 2) Upcoming year-end dome/arena concerts (Nov-Dec 2026) mapped to affected terminal lounge hubs
    hub_by_vid = {h["venue_id"]: h for h in moat_intelligence.MAJOR_ARENA_HUBS}
    upcoming_arenas = get_upcoming_arena_events(days_ahead=60, from_date=today_str, limit=60)
    enriched_arenas = []
    for ev in upcoming_arenas:
        hub = hub_by_vid.get(ev.get("venue_id"))
        hub_stores = [store_master.seo_name(s) for s in (hub["stores"][:3] if hub else [])]
        enriched_arenas.append({
            **ev,
            "nearby_stores": hub_stores,
        })

    # 3) 2026 Year-End key calendar milestones (with honest observed vs upcoming status)
    milestones = [
        ("2026-11-20", "11月下旬・忘年会シーズン序盤（金曜＋給料日前後）", "bounenkai_early"),
        ("2026-12-04", "12月第1金曜・忘年会ピーク突入", "bounenkai_peak"),
        ("2026-12-11", "12月第2金曜・冬季賞与支給窓×二次会最繁忙帯", "bounenkai_peak"),
        ("2026-12-18", "12月第3金曜・年間最大級の二次会流入ピーク", "bounenkai_peak"),
        ("2026-12-24", "クリスマスイブ（木曜・1対1志向とペア回遊の二極化）", "christmas_eve"),
        ("2026-12-25", "クリスマス当日（金曜・給料日窓重なり）", "christmas"),
        ("2026-12-28", "仕事納めピーク〜年末連休突入", "work_year_end"),
        ("2026-12-31", "大晦日・カウントダウン深夜営業帯", "new_year_eve"),
    ]
    milestone_rows = []
    for m_date, label_ja, s_tag in milestones:
        c = arena_calendar.classify_day_context(m_date)
        milestone_rows.append({
            "date_jst": m_date,
            "label_ja": label_ja,
            "season_tag": s_tag,
            "is_payday_window": bool(c.get("is_payday_window")),
            "is_bonus_window": bool(c.get("is_bonus_window")),
            "status": "実測集計済み" if m_date < today_str else ("本日リアルタイム計測中" if m_date == today_str else "到来後に実測値を自動追記"),
        })

    return {
        "generated_date": today_str,
        "pre_holiday_20_23": _fmt_bucket(wd_stats["pre_holiday"]),
        "weekday_20_23": _fmt_bucket(wd_stats["weekday"]),
        "top_peak_stores": top_peak_stores,
        "upcoming_arena_events": enriched_arenas[:24],
        "milestones": milestone_rows,
        "moat_coverage": get_moat_coverage_summary(),
    }


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


def _is_synthetic_seed_alias(store_name: str) -> bool:
    """Return True for SEO/display test-seed store aliases never emitted by live scrapers."""
    s = (store_name or "").strip()
    if not s:
        return False
    if (
        s.startswith("オリエンタルラウンジ")
        or "（アグ）" in s
        or "（アソビバー）" in s
        or "（ジス）" in s
        or s.startswith("ag ")
        or s.startswith("ヤタコイ")
    ):
        return True
    if s.startswith("ag") and len(s) > 2 and not s[2].isascii():
        return True
    return False


def _is_ghost_multistore_batch(dt: datetime.datetime, smap: dict) -> bool:
    """Detect timezone-shifted (PDT -16h) ghost batches in multi-store (>=15 stores) snapshots."""
    if len(smap) < 15 or dt is None:
        return False
    h = dt.hour
    tot_all = 0
    n_open = 0
    n_w_only = 0
    for w, m in smap.values():
        t = w + m
        tot_all += t
        if t > 0:
            n_open += 1
            if w >= 2 and m == 0:
                n_w_only += 1

    if 8 <= h <= 16 and tot_all > 60:
        return True
    if h == 7 and tot_all > 120:
        return True
    if h == 17 and n_open == 1 and tot_all >= 25:
        return True
    if h == 18 and dt.minute >= 8 and (n_open <= 3 or tot_all < 20):
        return True
    if h == 18 and dt.minute < 8 and n_open == 1 and tot_all >= 25:
        return True
    if (19 <= h <= 23 or 0 <= h <= 2) and (tot_all < 100 or n_open <= 6):
        return True
    if 2 <= h <= 7:
        if n_open >= 10 and n_w_only >= max(6, int(n_open * 0.45)):
            return True
        try:
            import moat_intelligence
            severe_viol = 0
            mild_viol = 0
            for s, (w, m) in smap.items():
                t = w + m
                if t >= 2:
                    mtc = moat_intelligence._mins_until_close(s, dt)
                    if mtc <= -90 and t >= 6:
                        severe_viol += 1
                    if mtc <= -45:
                        mild_viol += 1
            if severe_viol >= 2 or mild_viol >= max(5, int(len(smap) * 0.20)):
                return True
        except Exception:
            pass
    return False


def _bray_curtis_multistore(map_a: dict, map_b: dict):
    common = [s for s in map_a if s in map_b]
    if len(common) < 15:
        return None
    denom = 0
    num = 0
    for s in common:
        wa, ma = map_a[s]
        wb, mb = map_b[s]
        denom += (wa + ma) + (wb + mb)
        num += abs(wa - wb) + abs(ma - mb)
    if denom < 120:
        return None
    return num / float(denom)


def _extract_clean_snapshot_batches(
    raw_rows: list,
    valid_domestic: set = None,
    check_taint: bool = False,
    exclude_korea: bool = True,
) -> list:
    """
    Group ascending `(timestamp, store_name, women, men)` rows into strict <=2.0s scrape batches
    and surgically remove synthetic seed taints and PDT (-16h) interleaved ghost batches.
    """
    if not raw_rows:
        return []

    batches = []
    cur_dt = None
    cur_ts = None
    cur_map = {}
    cur_tainted = False
    last_ts_key = None
    last_parsed_dt = None

    for r in raw_rows:
        ts_raw = str(r[0] or "")[:19]
        if len(ts_raw) < 19:
            continue
        if ts_raw == last_ts_key:
            dt = last_parsed_dt
        else:
            dt = _parse_jst_ts(ts_raw)
            last_ts_key = ts_raw
            last_parsed_dt = dt
        if dt is None:
            continue

        if cur_dt is None or (dt - cur_dt).total_seconds() > 2.0:
            if cur_map and not cur_tainted:
                batches.append((cur_dt, cur_ts, cur_map))
            cur_dt = dt
            cur_ts = ts_raw
            cur_map = {}
            cur_tainted = False

        sname_r = str(r[1] or "").strip()
        if not sname_r:
            continue
        if _is_synthetic_seed_alias(sname_r):
            cur_tainted = True
            continue
        if exclude_korea and _is_korea_store_name(sname_r):
            continue
        if check_taint and valid_domestic and sname_r not in valid_domestic:
            cur_tainted = True
            continue
        cur_map[sname_r] = (max(0, int(r[2] or 0)), max(0, int(r[3] or 0)))

    if cur_map and not cur_tainted:
        batches.append((cur_dt, cur_ts, cur_map))

    stage1 = [b for b in batches if not _is_ghost_multistore_batch(b[0], b[2])]
    if len(stage1) <= 1:
        return stage1

    clean_batches = []
    n_s1 = len(stage1)
    for idx, (dt, ts_s, smap) in enumerate(stage1):
        drop = False
        if len(smap) >= 15 and clean_batches:
            prev_multi = None
            for back_i in range(len(clean_batches) - 1, -1, -1):
                if len(clean_batches[back_i][2]) >= 15:
                    prev_multi = clean_batches[back_i]
                    break
            if prev_multi is not None:
                gap_prev = (dt - prev_multi[0]).total_seconds()
                if 0.0 < gap_prev <= 2400.0:
                    bc_prev = _bray_curtis_multistore(smap, prev_multi[2])
                    if bc_prev is not None and bc_prev > 0.30:
                        checked_fwd = 0
                        for fwd_i in range(idx + 1, n_s1):
                            if len(stage1[fwd_i][2]) < 15:
                                continue
                            fwd_multi = stage1[fwd_i]
                            if (fwd_multi[0] - prev_multi[0]).total_seconds() > 3000.0:
                                break
                            checked_fwd += 1
                            bc_pn = _bray_curtis_multistore(fwd_multi[2], prev_multi[2])
                            bc_nf = _bray_curtis_multistore(smap, fwd_multi[2])
                            if bc_pn is not None and bc_pn <= 0.26 and (bc_nf is None or bc_nf > 0.28):
                                drop = True
                                break
                            if checked_fwd >= 6:
                                break
        if not drop:
            clean_batches.append((dt, ts_s, smap))
    return clean_batches


def _compute_benchmark_base_from_sqlite(cutoff_str: str) -> dict:
    """Scans the last 30 days of monitor_logs once (with ghost-batch filtering) and returns hour/weekday base accumulators."""
    from collections import defaultdict
    by_wd_hour = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    by_hour = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    by_wd_peak = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    peak_21_22 = defaultdict(lambda: [0.0, 0])
    mid_00_01 = defaultdict(lambda: [0.0, 0])
    band_stats = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    stores_seen = set()
    _date_memo = {}

    raw_rows = []
    if _db_available():
        try:
            raw_rows = _fetch_all_rows(
                """
                SELECT timestamp, store_name, women, men
                FROM monitor_logs
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (cutoff_str,),
            )
        except Exception as e:
            print(f"[local_db] get_all_stores_live_benchmarks error: {e}", file=sys.stderr)
            raw_rows = []

    clean_batches = _extract_clean_snapshot_batches(raw_rows, check_taint=False, exclude_korea=False)
    grouped_acc = defaultdict(lambda: [0.0, 0.0, 0])
    for dt, ts_s, smap in clean_batches:
        d_str = ts_s[:10]
        h = dt.hour
        for sname, (w, m) in smap.items():
            if w > 0 or m > 0:
                acc = grouped_acc[(sname, d_str, h)]
                acc[0] += float(w)
                acc[1] += float(m)
                acc[2] += 1

    for (sname, d_str, h), (sum_w, sum_m, cnt) in grouped_acc.items():
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


def get_all_stores_hourly_waveforms(now_jst: datetime.datetime = None, allow_blocking: bool = True) -> dict:
    """
    Derive per-store 20:00 -> 22:00 -> 00:00 -> 01:00 crowd waveform & gender-asymmetry crossover profiles
    directly from the 30-day `_benchmark_base_cache` in O(1) without extra SQL scans.
    """
    if now_jst is None:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    elif getattr(now_jst, "tzinfo", None) is not None:
        now_jst = now_jst.astimezone(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)

    db_k = _active_db_key()
    base_entry = _benchmark_base_cache.get(db_k)
    if base_entry is None:
        get_all_stores_live_benchmarks(now_jst=now_jst, allow_blocking=allow_blocking)
        base_entry = _benchmark_base_cache.get(db_k)
    if not base_entry or not base_entry[2]:
        return {}

    base_data = base_entry[2]
    by_hour = base_data.get("by_hour") or {}
    static_per_store = base_data.get("static_per_store") or {}
    profiles = {}

    def _slot_point(h_map: dict, h: int):
        st = h_map.get(h)
        if not st or st[2] < 2:
            return None
        aw = round(st[0] / st[2], 1)
        am = round(st[1] / st[2], 1)
        tot = aw + am
        wp = round((aw / tot) * 100, 1) if tot >= 2.0 else 50.0
        return {"w": aw, "m": am, "total": round(tot, 1), "wpct": wp, "n": int(st[2])}

    for sname in base_data.get("stores_seen") or ():
        h_map = by_hour.get(sname) or {}
        p20 = _slot_point(h_map, 20)
        p22 = _slot_point(h_map, 22)
        p00 = _slot_point(h_map, 0)
        p01 = _slot_point(h_map, 1)
        p23 = _slot_point(h_map, 23)

        reversal_hour = None
        if p20 and p20["wpct"] >= 58.0:
            for cand_h in (22, 23, 0, 1, 2):
                pt = _slot_point(h_map, cand_h)
                if pt and pt["total"] >= 8.0 and pt["wpct"] < 50.0:
                    reversal_hour = cand_h
                    break

        rebound_w = 0.0
        if p23 and p01 and p01["w"] > p23["w"]:
            rebound_w = round(p01["w"] - p23["w"], 1)
        elif p00 and p01 and p01["w"] > p00["w"]:
            rebound_w = round(p01["w"] - p00["w"], 1)

        if reversal_hour is not None:
            archetype = "early_female_late_male_flip"
            archetype_ja = f"序盤女性先行→{reversal_hour}時台から男性先行へ反転（女子待ちゼロ化）"
        elif p22 and p01 and p01["total"] >= 70.0 and p01["w"] >= p22["w"] * 0.85:
            archetype = "midnight_monster_surge"
            archetype_ja = "終電後も100名規模の熱量が続く深夜2波・大箱拮抗型"
        elif p20 and p00 and p20["wpct"] >= 62.0 and p00["wpct"] >= 56.0:
            archetype = "steady_female_lead"
            archetype_ja = "20時〜終電後まで女性優位が崩れにくい安定ラウンジ型"
        else:
            archetype = "first_train_early_peak"
            archetype_ja = "20〜22時が中心で終電前に落ち着く1軒目ピーク型"

        st_meta = static_per_store.get(sname) or {}
        profiles[sname] = {
            "h20": p20,
            "h22": p22,
            "h23": p23,
            "h00": p00,
            "h01": p01,
            "reversal_hour": reversal_hour,
            "second_wave_rebound_w": rebound_w,
            "waveform_archetype": archetype,
            "waveform_archetype_ja": archetype_ja,
            "midnight_retention_pct": st_meta.get("midnight_retention_pct"),
        }
    return profiles


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
    if s.startswith("YATAKOI "):
        return "ヤタコイ " + s[8:].strip()
    if s == "YATAKOI":
        return "ヤタコイ"
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
    """Return `/store/<slug>` slug only when it resolves to a visible active store or 301 legacy redirect (never 404/410)."""
    if not store_name:
        return ""
    try:
        import ops_governance
        import store_master
        if ops_governance.is_store_hidden(store_name):
            return ""
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
                            model_version TEXT NOT NULL DEFAULT 'v3.0-statespace',
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
                            raw_pred_wpct INTEGER,
                            online_bias_adj REAL DEFAULT 0.0,
                            UNIQUE(predicted_at, store_name)
                        );
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_pending ON forecast_accuracy_logs(resolved_at, target_at);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_store_pred ON forecast_accuracy_logs(store_name, predicted_at);")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_resolved_pred ON forecast_accuracy_logs(predicted_at, resolved_at);")
                    conn.commit()
                    _migrate_forecast_columns(conn, cur)
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
                    model_version TEXT NOT NULL DEFAULT 'v3.0-statespace',
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
                    raw_pred_wpct INTEGER,
                    online_bias_adj REAL DEFAULT 0.0,
                    UNIQUE(predicted_at, store_name)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_pending ON forecast_accuracy_logs(resolved_at, target_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_store_pred ON forecast_accuracy_logs(store_name, predicted_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fc_acc_resolved_pred ON forecast_accuracy_logs(predicted_at, resolved_at);")
            conn.commit()
            _migrate_forecast_columns(conn, cur)
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


def _to_naive_jst(dt: datetime.datetime = None) -> datetime.datetime:
    if dt is None:
        return datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    if getattr(dt, "tzinfo", None) is not None:
        return dt.astimezone(_JST_TZ).replace(tzinfo=None)
    return dt


def _record_snapshot_to_trajectory_ring(items: list, timestamp_str: str):
    """Append a fresh live snapshot into the bounded in-memory trajectory ring buffer for O(1) lag lookups."""
    if not items or not timestamp_str:
        return
    ts_dt = _parse_jst_ts(timestamp_str)
    if ts_dt is None:
        return
    dt_naive = _to_naive_jst(ts_dt)

    f_times = [
        float(it.get("fetched_at") or 0.0)
        for it in items
        if isinstance(it, dict) and "fetched_at" in it and float(it.get("fetched_at") or 0.0) > 0.0
    ]
    max_fetched = max(f_times) if f_times else 0.0

    smap = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        sname_check = str(it.get("name") or "").strip()
        if _is_synthetic_seed_alias(sname_check):
            return
        if "fetched_at" in it:
            fat = float(it.get("fetched_at") or 0.0)
            if fat <= 0.0 or (max_fetched > 0.0 and (max_fetched - fat) > 180.0):
                continue
        sname = sname_check
        if not sname or _is_korea_store_name(sname):
            continue
        smap[sname] = (max(0, int(it.get("women") or 0)), max(0, int(it.get("men") or 0)))

    if not smap or _is_ghost_multistore_batch(ts_dt, smap):
        return

    db_k = _active_db_key()
    with _recent_trajectory_lock:
        ring = list(_recent_trajectory_ring.get(db_k) or ())
        if ring and len(smap) >= 15:
            # Retroactively evict any interleaved off-trajectory spike when `smap` reconnects to an earlier multi-store anchor
            anchor_idx = None
            for a_i in range(len(ring) - 1, -1, -1):
                a_dt, a_map = ring[a_i]
                if len(a_map) < 15:
                    continue
                gap_ac = (dt_naive - a_dt).total_seconds()
                if gap_ac <= 2.0:
                    continue
                if gap_ac > 3000.0:
                    break
                bc_ac = _bray_curtis_multistore(smap, a_map)
                if bc_ac is not None and bc_ac <= 0.26:
                    anchor_idx = a_i
                    break
            if anchor_idx is not None and anchor_idx < len(ring) - 1:
                a_dt, a_map = ring[anchor_idx]
                kept = ring[:anchor_idx + 1]
                for b_dt, b_map in ring[anchor_idx + 1:]:
                    if len(b_map) >= 15 and b_dt < dt_naive:
                        bc_ab = _bray_curtis_multistore(b_map, a_map)
                        bc_bc = _bray_curtis_multistore(b_map, smap)
                        if bc_ab is not None and bc_ab > 0.30 and (bc_bc is None or bc_bc > 0.28):
                            continue
                    kept.append((b_dt, b_map))
                ring = kept
        merged = False
        for idx, (b_dt, b_map) in enumerate(ring):
            if abs((dt_naive - b_dt).total_seconds()) <= 2.0:
                updated_map = dict(b_map)
                updated_map.update(smap)
                ring[idx] = (b_dt, updated_map)
                merged = True
                break
        if not merged:
            ring.append((dt_naive, smap))
            ring.sort(key=lambda x: x[0])
        if len(ring) > 28:
            ring = ring[-28:]
        _recent_trajectory_ring[db_k] = ring


def _warm_trajectory_ring_from_db(target_dt_naive: datetime.datetime):
    """Populate `_recent_trajectory_ring` from the last 85 minutes of `monitor_logs` (cleaned of ghost batches)."""
    if not _db_available():
        return
    db_k = _active_db_key()
    db_cnt = int(get_db_stats().get("count") or 0)
    if db_cnt <= 0:
        with _recent_trajectory_lock:
            _recent_trajectory_warmed_at[db_k] = (time.monotonic(), 0, target_dt_naive)
        return

    w_lo = (target_dt_naive - datetime.timedelta(minutes=85)).strftime("%Y-%m-%d %H:%M:%S")
    w_hi = target_dt_naive.strftime("%Y-%m-%d %H:%M:%S")
    try:
        raw_rows = _fetch_all_rows(
            """
            SELECT timestamp, store_name, women, men
            FROM monitor_logs
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (w_lo, w_hi),
        )
    except Exception:
        raw_rows = []

    clean_batches = _extract_clean_snapshot_batches(raw_rows, check_taint=False, exclude_korea=True)
    db_entries = [(_to_naive_jst(b[0]), b[2]) for b in clean_batches]

    with _recent_trajectory_lock:
        ring = list(_recent_trajectory_ring.get(db_k) or ())
        for e_dt, e_map in db_entries:
            merged = False
            for idx, (b_dt, b_map) in enumerate(ring):
                if abs((e_dt - b_dt).total_seconds()) <= 2.0:
                    comb = dict(e_map)
                    comb.update(b_map)
                    ring[idx] = (b_dt, comb)
                    merged = True
                    break
            if not merged:
                ring.append((e_dt, e_map))
        ring.sort(key=lambda x: x[0])
        if len(ring) > 28:
            ring = ring[-28:]
        _recent_trajectory_ring[db_k] = ring
        _recent_trajectory_warmed_at[db_k] = (time.monotonic(), db_cnt, target_dt_naive)


def _extract_lags_from_ring_entries(ring_entries: list, target_dt_naive: datetime.datetime) -> dict:
    if not ring_entries:
        return {}
    b15, err15 = None, 999.0
    b30, err30 = None, 999.0
    b60, err60 = None, 999.0
    for b_dt, b_map in ring_entries:
        dm = (target_dt_naive - b_dt).total_seconds() / 60.0
        if 8.0 <= dm <= 23.0:
            e = abs(dm - 15.0)
            if e < err15:
                err15, b15 = e, b_map
        elif 23.0 < dm <= 42.0:
            e = abs(dm - 30.0)
            if e < err30:
                err30, b30 = e, b_map
        elif 48.0 <= dm <= 75.0:
            e = abs(dm - 60.0)
            if e < err60:
                err60, b60 = e, b_map

    if b15 is None and b30 is None and b60 is None:
        return {}

    stores = set()
    if b15:
        stores.update(b15.keys())
    if b30:
        stores.update(b30.keys())
    if b60:
        stores.update(b60.keys())

    out = {}
    for s in stores:
        d = {}
        if b15 and s in b15:
            d["lag_15m"] = b15[s]
        if b30 and s in b30:
            d["lag_30m"] = b30[s]
        if b60 and s in b60:
            d["lag_60m"] = b60[s]
        if d:
            out[s] = d
    return out


def get_cached_recent_lags_map(now_jst: datetime.datetime = None, allow_db_warm: bool = False) -> dict:
    """Return `{store_name: {'lag_15m': (w,m), 'lag_30m': (w,m), 'lag_60m': (w,m)}}` from RAM ring buffer."""
    target_dt = _to_naive_jst(now_jst)
    db_k = _active_db_key()
    with _recent_trajectory_lock:
        snap = list(_recent_trajectory_ring.get(db_k) or ())
        warm_meta = _recent_trajectory_warmed_at.get(db_k)
        is_warming = db_k in _recent_lags_warming

    has_window = any(8.0 <= (target_dt - b_dt).total_seconds() / 60.0 <= 75.0 for b_dt, _ in snap)
    if not has_window and not is_warming:
        now_mono = time.monotonic()
        if (
            not allow_db_warm
            and warm_meta is not None
            and warm_meta[1] >= 2500
            and abs((target_dt - warm_meta[2]).total_seconds()) < 300.0
            and (now_mono - warm_meta[0]) < 120.0
        ):
            return _extract_lags_from_ring_entries(snap, target_dt)
        if not _db_available():
            return {}
        db_cnt = int(get_db_stats().get("count") or 0)
        already_checked = (
            warm_meta is not None
            and warm_meta[1] == db_cnt
            and abs((target_dt - warm_meta[2]).total_seconds()) < 300.0
            and (now_mono - warm_meta[0]) < 120.0
        )
        if db_cnt > 0 and not already_checked:
            if allow_db_warm or (db_cnt < 2500 and not _sync_in_progress):
                _warm_trajectory_ring_from_db(target_dt)
                with _recent_trajectory_lock:
                    snap = list(_recent_trajectory_ring.get(db_k) or ())
            else:
                warm_key = db_k
                spawn = False
                with _recent_trajectory_lock:
                    if warm_key not in _recent_lags_warming:
                        _recent_lags_warming.add(warm_key)
                        spawn = True
                if spawn:
                    def _bg_warm():
                        try:
                            _warm_trajectory_ring_from_db(target_dt)
                        except Exception:
                            pass
                        finally:
                            with _recent_trajectory_lock:
                                _recent_lags_warming.discard(warm_key)
                    threading.Thread(target=_bg_warm, daemon=True).start()

    return _extract_lags_from_ring_entries(snap, target_dt)


def get_cached_store_recent_lags(store_name: str, now_jst: datetime.datetime = None):
    """Non-blocking single-store lookup into `get_cached_recent_lags_map`."""
    if not store_name:
        return None
    lags_map = get_cached_recent_lags_map(now_jst=now_jst, allow_db_warm=False)
    if store_name in lags_map:
        return lags_map[store_name]
    try:
        import moat_intelligence
        canon = moat_intelligence._resolve_internal_store_key(store_name)
        if canon and canon in lags_map:
            return lags_map[canon]
    except Exception:
        pass
    return None


def _interp_usual_from_maps(by_wh: dict, by_h: dict, dt_naive: datetime.datetime):
    """Continuously interpolate `(usual_w, usual_m, eff_cnt)` around hour midpoints (`HH:30`)."""
    h_a = dt_naive.hour
    wd_a = ((dt_naive - datetime.timedelta(days=1)).date() if h_a <= 6 else dt_naive.date()).weekday()
    s_a = by_wh.get((wd_a, h_a))
    if not s_a or s_a[2] < 3:
        s_a = by_h.get(h_a)
    if not s_a or s_a[2] <= 0:
        return None
    uw_a = s_a[0] / s_a[2]
    um_a = s_a[1] / s_a[2]
    c_a = float(s_a[2])

    mins = dt_naive.minute + (dt_naive.second / 60.0)
    if mins < 30.0:
        dt_b = dt_naive - datetime.timedelta(hours=1)
        alpha = (30.0 - mins) / 60.0
    else:
        dt_b = dt_naive + datetime.timedelta(hours=1)
        alpha = (mins - 30.0) / 60.0

    h_b = dt_b.hour
    wd_b = ((dt_b - datetime.timedelta(days=1)).date() if h_b <= 6 else dt_b.date()).weekday()
    s_b = by_wh.get((wd_b, h_b))
    if not s_b or s_b[2] < 3:
        s_b = by_h.get(h_b)
    if s_b and s_b[2] >= 3:
        uw_b = s_b[0] / s_b[2]
        um_b = s_b[1] / s_b[2]
        c_b = float(s_b[2])
        return (
            (1.0 - alpha) * uw_a + alpha * uw_b,
            (1.0 - alpha) * um_a + alpha * um_b,
            (1.0 - alpha) * c_a + alpha * c_b,
        )
    return (uw_a, um_a, c_a)


def get_cached_store_usual_pair(store_name: str, now_jst: datetime.datetime):
    """Non-blocking O(1) RAM lookup of continuously-interpolated 30-day usual `(w_t, m_t, w_t60, m_t60, cnt_t60)`."""
    if not store_name or now_jst is None:
        return None
    db_k = _active_db_key()
    base_entry = _benchmark_base_cache.get(db_k)
    now_naive = _to_naive_jst(now_jst)
    if base_entry is None or (time.monotonic() - base_entry[0]) >= _BENCHMARK_BASE_TTL:
        if not _live_benchmarks_computing and _db_available():
            try:
                get_all_stores_live_benchmarks(now_jst=now_naive, allow_blocking=False)
            except Exception:
                pass
            base_entry = _benchmark_base_cache.get(db_k)
    if not base_entry:
        return None
    base_data = base_entry[2]
    if not base_data:
        return None
    stores_seen = base_data.get("stores_seen", ())
    lookup_name = store_name
    if lookup_name not in stores_seen:
        try:
            import moat_intelligence
            lookup_name = moat_intelligence._resolve_internal_store_key(store_name)
        except Exception:
            pass
    if not lookup_name or lookup_name not in stores_seen:
        return None

    by_wh = base_data["by_wd_hour"].get(lookup_name, {})
    by_h = base_data["by_hour"].get(lookup_name, {})

    p0 = _interp_usual_from_maps(by_wh, by_h, now_naive)
    p1 = _interp_usual_from_maps(by_wh, by_h, now_naive + datetime.timedelta(minutes=60))
    if not p1 or p1[2] < 3.0:
        return None
    uw0, um0 = (p0[0], p0[1]) if p0 else (0.0, 0.0)
    return (uw0, um0, p1[0], p1[1], int(round(p1[2])))


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
    live_smap = {}
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        sname_raw = (it.get("name") or "").strip()
        if _is_synthetic_seed_alias(sname_raw):
            return {"resolved": 0, "recorded": 0}
        if "fetched_at" in it:
            fat = float(it.get("fetched_at") or 0.0)
            if fat <= 0.0 or (max_fetched > 0.0 and (max_fetched - fat) > 180.0):
                continue
        if sname_raw:
            w_it = max(0, int(it.get("women") or 0))
            m_it = max(0, int(it.get("men") or 0))
            live_map[sname_raw] = (w_it, m_it, it)
            if not _is_korea_store_name(sname_raw):
                live_smap[sname_raw] = (w_it, m_it)
            fresh_items.append(it)

    if len(live_smap) >= 15 and _is_ghost_multistore_batch(ts_dt, live_smap):
        live_map = {}
        fresh_items = []

    valid_domestic = _get_valid_domestic_canonical_stores()

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
    overdue_pending = []
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
            overdue_pending.append((row_id, tgt_dt, sname, pred_w, pred_m, pred_wp, v1_wp, diff_min))

    if overdue_pending:
        clusters = []
        cur_cluster = []
        for ov in overdue_pending:
            if not cur_cluster or (ov[1] - cur_cluster[-1][1]).total_seconds() <= 5400.0:
                cur_cluster.append(ov)
            else:
                clusters.append(cur_cluster)
                cur_cluster = [ov]
        if cur_cluster:
            clusters.append(cur_cluster)

        for cl in clusters:
            w_lo = (cl[0][1] - datetime.timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
            w_hi = (cl[-1][1] + datetime.timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
            try:
                raw_ov_rows = _fetch_all_rows(
                    """
                    SELECT timestamp, store_name, women, men
                    FROM monitor_logs
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                    """,
                    (w_lo, w_hi),
                )
            except Exception:
                raw_ov_rows = []
            clean_ov_batches = _extract_clean_snapshot_batches(
                raw_ov_rows,
                valid_domestic=valid_domestic,
                check_taint=True,
                exclude_korea=True,
            )
            for row_id, tgt_dt, sname, pred_w, pred_m, pred_wp, v1_wp, diff_min in cl:
                best_match = None
                best_gap = 900.001
                for b_dt, b_ts, b_map in clean_ov_batches:
                    if sname not in b_map:
                        continue
                    g = abs((b_dt - tgt_dt).total_seconds())
                    if g <= 900.0 and g < best_gap:
                        best_gap = g
                        best_match = (b_ts, b_map[sname][0], b_map[sname][1])
                if best_match is not None:
                    updates.append(_compute_resolution_tuple(
                        row_id, pred_w, pred_m, pred_wp, v1_wp, best_match[1], best_match[2], best_match[0]
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

    lags_map = get_cached_recent_lags_map(ts_dt, allow_db_warm=True)
    peer_map = moat_intelligence.build_city_peer_context(fresh_items)

    # Fallback 15-min DB lookup only if `fresh_items` lacked `women_diff` AND `lags_map` had no `lag_15m`
    need_db_diff = any(
        (
            "women_diff" not in it
            and (int(it.get("women") or 0) + int(it.get("men") or 0)) >= 6
            and "lag_15m" not in (lags_map.get((it.get("name") or "").strip()) or {})
        )
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
    is_full_batch = len(fresh_items) >= 15

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
        if is_full_batch and 8 <= ts_dt.hour <= 17:
            bh_sched = store_hours.get_business_hours_status(sname, now_jst=ts_dt, total_guests=0)
            if not bh_sched.get("scheduled_open", True):
                continue
        bh = store_hours.get_business_hours_status(sname, now_jst=ts_dt, total_guests=tot)
        if not bh.get("is_open_now", True):
            continue

        canon_s = moat_intelligence._resolve_internal_store_key(sname)
        st_lags = lags_map.get(canon_s) or lags_map.get(sname) or {}
        if "women_diff" in item:
            wd = int(item.get("women_diff") or 0)
            md = int(item.get("men_diff") or 0)
        elif "lag_15m" in st_lags and (int(st_lags["lag_15m"][0]) + int(st_lags["lag_15m"][1])) >= 2:
            wd = w - int(st_lags["lag_15m"][0])
            md = m - int(st_lags["lag_15m"][1])
        elif sname in prev15_map and (prev15_map[sname][0] + prev15_map[sname][1]) >= 2:
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
            arena_signal=arena_map.get(sname) or arena_map.get(canon_s),
            apply_online_bias=True,
            lag_context=st_lags,
            peer_context=peer_map.get(canon_s) or peer_map.get(sname),
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
            arena_signal=arena_map.get(sname) or arena_map.get(canon_s),
        )
        start_wp = int(round((w / float(tot)) * 100.0))
        pred_wp_val = int(fc["pred_60m_wpct"])
        raw_wp_val = int(fc["raw_pred_60m_wpct"]) if fc.get("raw_pred_60m_wpct") is not None else pred_wp_val
        bias_adj_val = float(fc.get("online_bias_adj_pt") or 0.0)
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
            pred_wp_val,
            int(v1_fc["pred_60m_wpct"]) if v1_fc.get("pred_60m_wpct") is not None else None,
            int(fc.get("flip_risk_pct") or 0),
            str(fc.get("alert_code") or "steady"),
            raw_wp_val,
            bias_adj_val,
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
                        flip_risk_pct, alert_code,
                        raw_pred_wpct, online_bias_adj
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
                            flip_risk_pct, alert_code,
                            raw_pred_wpct, online_bias_adj
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    Causally backfill resolved 60-minute predictions from historical `monitor_logs` (cleaned of PDT ghost batches)
    so accuracy reports and online store calibration reflect the active model immediately without future leakage.
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
        import moat_intelligence
        import store_hours
        from collections import defaultdict

        cur_ver = moat_intelligence.FORECAST_MODEL_VERSION
        try:
            cnt_rows = _fetch_all_rows(
                "SELECT COUNT(*) FROM forecast_accuracy_logs WHERE resolved_at IS NOT NULL AND model_version = ?",
                (cur_ver,),
            )
            existing_resolved = int(cnt_rows[0][0] or 0) if cnt_rows else 0
        except Exception:
            existing_resolved = 0

        if not force and existing_resolved >= min_existing:
            return {"inserted": 0, "skipped": True, "existing_resolved": existing_resolved}

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

        clean_batches = _extract_clean_snapshot_batches(
            raw_rows,
            valid_domestic=valid_domestic,
            check_taint=True,
            exclude_korea=True,
        )
        n_b = len(clean_batches)
        if n_b < 2:
            return {"inserted": 0, "skipped": True, "existing_resolved": existing_resolved}

        # Select newest-first (~25m spacing) batch pairs up to `max_rows`, then evaluate causally forward
        selected_pairs = []
        planned_rows = 0
        last_sampled_dt = None
        for i in range(n_b - 2, -1, -1):
            dt, _, smap = clean_batches[i]
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
            smap60 = clean_batches[best_k][2]
            valid_cnt = sum(1 for s, (w, m) in smap.items() if (w + m) >= 6 and s in smap60)
            if valid_cnt <= 0:
                continue
            selected_pairs.append((i, best_k))
            last_sampled_dt = dt
            planned_rows += valid_cnt
            if planned_rows >= max_rows:
                break

        if not selected_pairs:
            return {"inserted": 0, "skipped": False, "existing_resolved": existing_resolved}

        selected_pairs.reverse()
        cum_wh = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
        cum_h = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
        hist_ptr = 0
        backfill_rows = []

        for i, best_k in selected_pairs:
            dt, ts_s, smap = clean_batches[i]
            # Strictly causal usual curve: accumulate only past shifts (>=12h prior to `dt`)
            while hist_ptr < i and (dt - clean_batches[hist_ptr][0]).total_seconds() >= 43200.0:
                h_dt, _, h_map = clean_batches[hist_ptr]
                h_naive = _to_naive_jst(h_dt)
                hh = h_naive.hour
                h_wd = ((h_naive - datetime.timedelta(days=1)).date() if hh <= 6 else h_naive.date()).weekday()
                for hs, (hw, hm) in h_map.items():
                    if hw > 0 or hm > 0:
                        b_wh = cum_wh[hs][(h_wd, hh)]
                        b_wh[0] += float(hw)
                        b_wh[1] += float(hm)
                        b_wh[2] += 1
                        b_h = cum_h[hs][hh]
                        b_h[0] += float(hw)
                        b_h[1] += float(hm)
                        b_h[2] += 1
                hist_ptr += 1

            p15, e15 = None, 999.0
            p30, e30 = None, 999.0
            p60, e60 = None, 999.0
            for k in range(i - 1, -1, -1):
                dm = (dt - clean_batches[k][0]).total_seconds() / 60.0
                if dm > 76.0:
                    break
                if 8.0 <= dm <= 23.0 and abs(dm - 15.0) < e15:
                    e15, p15 = abs(dm - 15.0), clean_batches[k][2]
                elif 23.0 < dm <= 42.0 and abs(dm - 30.0) < e30:
                    e30, p30 = abs(dm - 30.0), clean_batches[k][2]
                elif 48.0 <= dm <= 75.0 and abs(dm - 60.0) < e60:
                    e60, p60 = abs(dm - 60.0), clean_batches[k][2]

            peer_map = moat_intelligence.build_city_peer_context(smap)
            dt_naive = _to_naive_jst(dt)
            dt60_naive = dt_naive + datetime.timedelta(minutes=60)
            _, res_ts_s, smap60 = clean_batches[best_k]
            tgt_s = dt60_naive.strftime("%Y-%m-%d %H:%M:%S")
            is_full_smap = len(smap) >= 15

            for sname, (w, m) in smap.items():
                tot = w + m
                if tot < 6 or sname not in smap60:
                    continue
                if is_full_smap and 8 <= dt_naive.hour <= 17:
                    bh_sched = store_hours.get_business_hours_status(
                        sname, total_guests=0, now_jst=dt_naive.replace(tzinfo=store_hours.JST)
                    )
                    if not bh_sched.get("scheduled_open", True):
                        continue
                w60, m60 = smap60[sname]
                wd15 = (w - p15[sname][0]) if (p15 and sname in p15) else 0
                md15 = (m - p15[sname][1]) if (p15 and sname in p15) else 0

                lag_ctx = {}
                if p15 and sname in p15:
                    lag_ctx["lag_15m"] = p15[sname]
                if p30 and sname in p30:
                    lag_ctx["lag_30m"] = p30[sname]
                if p60 and sname in p60:
                    lag_ctx["lag_60m"] = p60[sname]

                causal_usual = ()
                if sname in cum_h:
                    u0 = _interp_usual_from_maps(cum_wh[sname], cum_h[sname], dt_naive)
                    u1 = _interp_usual_from_maps(cum_wh[sname], cum_h[sname], dt60_naive)
                    if u1 and u1[2] >= 3.0:
                        uw0, um0 = (u0[0], u0[1]) if u0 else (0.0, 0.0)
                        causal_usual = (uw0, um0, u1[0], u1[1], int(round(u1[2])))

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
                    lag_context=lag_ctx,
                    peer_context=peer_map.get(sname),
                    usual_pair_override=causal_usual,
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
                raw_wp = int(fc["raw_pred_60m_wpct"]) if fc.get("raw_pred_60m_wpct") is not None else pred_wp
                v1_wp = int(v1_fc["pred_60m_wpct"]) if v1_fc.get("pred_60m_wpct") is not None else None
                res_tup = _compute_resolution_tuple(0, pred_w, pred_m, pred_wp, v1_wp, w60, m60, res_ts_s)

                backfill_rows.append((
                    ts_s,
                    tgt_s,
                    sname,
                    cur_ver,
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
                    raw_wp,
                    0.0,
                ))

        if len(backfill_rows) > max_rows:
            backfill_rows = backfill_rows[-max_rows:]

        if not backfill_rows:
            return {"inserted": 0, "skipped": False, "existing_resolved": existing_resolved}

        inserted_total = 0
        if _is_pg():
            try:
                with turbo_store.pg_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM forecast_accuracy_logs WHERE resolved_at IS NOT NULL AND model_version != %s",
                            (cur_ver,),
                        )
                    conn.commit()
                inserted_total = turbo_store.execute_pg_values_batch(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, v1_pred_wpct,
                        flip_risk_pct, alert_code,
                        resolved_at, actual_women, actual_men, actual_wpct,
                        error_wpct_pt, abs_error_wpct_pt, v1_abs_error_wpct_pt,
                        error_women, error_men,
                        raw_pred_wpct, online_bias_adj
                    ) VALUES %s
                    ON CONFLICT (predicted_at, store_name) DO NOTHING
                    """,
                    backfill_rows,
                    page_size=400,
                )
            except Exception as err:
                print(f"[local_db] PG backfill_forecast_accuracy error: {err}", file=sys.stderr)
        else:
            with _db_lock:
                conn = get_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "DELETE FROM forecast_accuracy_logs WHERE resolved_at IS NOT NULL AND model_version != ?",
                        (cur_ver,),
                    )
                    conn.commit()
                except Exception as err:
                    print(f"[local_db] SQLite legacy forecast cleanup warning: {err}", file=sys.stderr)
                finally:
                    conn.close()

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
                                error_women, error_men,
                                raw_pred_wpct, online_bias_adj
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    """Build per-store accuracy badge map and damped Empirical-Bayes online residual adjustment cache."""
    if not _db_available() or not _ensure_forecast_schema():
        return {}, {}

    import moat_intelligence
    cur_ver = moat_intelligence.FORECAST_MODEL_VERSION
    db_k = _active_db_key()
    now_jst = datetime.datetime.now(_JST_TZ).replace(tzinfo=None)
    cutoff_str = (now_jst - datetime.timedelta(days=max(3, int(days)))).strftime("%Y-%m-%d %H:%M:%S")

    try:
        rows = _fetch_all_rows(
            """
            SELECT store_name, predicted_at, pred_wpct, actual_wpct, error_wpct_pt, abs_error_wpct_pt,
                   actual_women, actual_men, model_version, raw_pred_wpct, online_bias_adj
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
    store_acc = defaultdict(lambda: [0.0, 0.0, 0, 0, None])  # sname -> [sum_abs, sum_signed, within10, n, latest_dict]
    bias_store_err = defaultdict(lambda: [0.0, 0.0])         # sname -> [weighted_sum_base_signed, eff_n]
    bias_slot_err = defaultdict(lambda: [0.0, 0.0])          # (sname, hr) -> [weighted_sum_base_signed, eff_n]
    dh_burst_counts = defaultdict(int)                       # (sname, date_str, hr) -> intra-hour burst index k

    for r in rows:
        sname = str(r[0] or "")
        pred_at = str(r[1] or "")
        pred_wp = int(r[2] or 0)
        act_wp = int(r[3] or 0)
        err_pt = float(r[4] or 0.0)
        abs_pt = float(r[5] or 0.0)
        aw = int(r[6] or 0)
        am = int(r[7] or 0)
        row_ver = str(r[8] or "") if len(r) > 8 else ""
        if not sname or (aw + am) < 4 or row_ver != cur_ver:
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

        # Unadjusted base model residual eliminates feedback double-counting & oscillation:
        # base_err = raw_pred_wpct - actual_wpct (pre-migration NULL rows are damped by 0.65 to prevent transient over-correction)
        raw_wp_col = r[9] if len(r) > 9 else None
        applied_adj_col = float(r[10] or 0.0) if (len(r) > 10 and r[10] is not None) else 0.0
        if raw_wp_col is not None:
            base_err_pt = float(int(raw_wp_col) - act_wp)
        else:
            base_err_pt = (err_pt - applied_adj_col) * 0.65

        # Geometric intra-hour burst discount (latest row in each date-hour gets k=0 -> 1.0, older intra-hour ticks get 0.5**k; sum < 2.0)
        dh_key = (sname, pred_at[:10], hr)
        k_burst = dh_burst_counts[dh_key]
        dh_burst_counts[dh_key] = k_burst + 1
        w_burst = 0.5 ** min(8, k_burst)

        age_days = 0.0
        if len(pred_at) >= 16:
            try:
                p_dt = datetime.datetime(
                    int(pred_at[0:4]), int(pred_at[5:7]), int(pred_at[8:10]),
                    int(pred_at[11:13]), int(pred_at[14:16]),
                )
                age_days = max(0.0, (now_jst - p_dt).total_seconds() / 86400.0)
            except Exception:
                age_days = 0.0
        w_decay = 0.5 ** (age_days / 7.0)
        w_obs = w_burst * w_decay

        bs = bias_store_err[sname]
        bs[0] += w_obs * base_err_pt
        bs[1] += w_obs
        if hr >= 0:
            sl = bias_slot_err[(sname, hr)]
            sl[0] += w_obs * base_err_pt
            sl[1] += w_obs

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

    # Require eff_n_st >= 2.6 so any single-hour burst (< 2.0) can NEVER trigger store bias alone
    for sname, (w_sum_sig, eff_n_st) in bias_store_err.items():
        if eff_n_st >= 2.6:
            store_mean_err = w_sum_sig / eff_n_st
            store_shrunk = store_mean_err * (eff_n_st / (eff_n_st + 8.0))
            bias_map[(sname, None)] = round(max(-5.0, min(5.0, -0.85 * store_shrunk)), 2)

    for (sname, hr), (w_sum_sl, eff_n_sl) in bias_slot_err.items():
        st_b = bias_store_err.get(sname)
        if not st_b or st_b[1] < 2.6 or eff_n_sl <= 0.0:
            continue
        eff_n_st = st_b[1]
        store_mean_err = st_b[0] / eff_n_st
        store_shrunk = store_mean_err * (eff_n_st / (eff_n_st + 8.0))
        slot_mean_err = w_sum_sl / eff_n_sl
        slot_shrunk = slot_mean_err * (eff_n_sl / (eff_n_sl + 4.5))
        comb_err = 0.60 * slot_shrunk + 0.40 * store_shrunk
        bias_map[(sname, hr)] = round(max(-5.0, min(5.0, -0.85 * comb_err)), 2)

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
    if not store_name:
        return 0.0
    db_k = _active_db_key()
    cached = _forecast_residual_bias_cache.get(db_k)
    if cached is not None:
        if time.monotonic() - cached[0] >= _FORECAST_RESIDUAL_TTL and not _forecast_cache_computing:
            refresh_forecast_caches_async()
        bmap = cached[1]
        return float(bmap.get((store_name, int(hour)), bmap.get((store_name, None), 0.0)))

    if _forecast_cache_computing or not _db_available():
        return 0.0
    db_cnt = int(get_db_stats().get("count") or 0)
    if db_cnt < 2500 and not _sync_in_progress:
        bmap, _ = _refresh_forecast_caches_from_db()
        return float(bmap.get((store_name, int(hour)), bmap.get((store_name, None), 0.0)))
    refresh_forecast_caches_async()
    return 0.0


def get_cached_store_accuracy_map() -> dict:
    """Return cached per-store 60m forecast accuracy track records without blocking HTTP requests."""
    db_k = _active_db_key()
    cached = _forecast_store_acc_cache.get(db_k)
    if cached is not None:
        if time.monotonic() - cached[0] >= _FORECAST_STORE_ACC_TTL and not _forecast_cache_computing:
            refresh_forecast_caches_async()
        return cached[1]

    if _forecast_cache_computing or not _db_available():
        return {}
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
    v1 vs. v3.0 live error reduction, recent resolved timeline, and pending live forecast queue.
    """
    import moat_intelligence
    import store_master

    days = max(1, min(120, int(days or 21)))
    limit_recent = max(5, min(100, int(limit_recent or 30)))
    raw_store = (store_name or "").strip()
    store_filter = resolve_canonical_store_name(raw_store) if raw_store else None
    db_k = _active_db_key()
    gov_sig = None
    try:
        import ops_governance
        gov_sig = (tuple(sorted(ops_governance.get_hidden_brands())), tuple(sorted(ops_governance.get_hidden_stores())))
    except Exception:
        pass
    cache_key = (db_k, days, store_filter, limit_recent, gov_sig)
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
            "total_resolved_rows": 0,
            "closing_censored_count": 0,
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
            "flip_brier_score": None,
            "flip_log_loss": None,
            "flip_alert_precision_pct": None,
            "flip_alert_recall_pct": None,
            "actual_male_glut_rate_pct": None,
        },
        "flip_calibration_bins": [],
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
    cur_ver = moat_intelligence.FORECAST_MODEL_VERSION

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
                       error_women, error_men, model_version
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
                       error_women, error_men, model_version
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
        if res_rows and any(str(r[21] or "") == cur_ver for r in res_rows):
            res_rows = [r for r in res_rows if str(r[21] or "") == cur_ver]
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
    closing_censored_count = 0

    flip_brier_sum = 0.0
    flip_logloss_sum = 0.0
    flip_tp = 0
    flip_fp = 0
    flip_fn = 0
    flip_pos_total = 0
    calib_bins_raw = [[0, 0.0, 0.0] for _ in range(5)]  # 0-19, 20-39, 40-59, 60-79, 80-100: [n, sum_pred_pct, sum_y]

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

            # Male-glut flip (<50% wpct) proper scoring rules & reliability calibration
            y_flip = 1.0 if awp < 50 else 0.0
            p_flip = max(0.01, min(0.99, float(flip_r) / 100.0))
            flip_brier_sum += (p_flip - y_flip) ** 2
            flip_logloss_sum += -(y_flip * math.log(p_flip) + (1.0 - y_flip) * math.log(1.0 - p_flip))
            if y_flip > 0.5:
                flip_pos_total += 1
            if flip_r >= 50:
                if y_flip > 0.5:
                    flip_tp += 1
                else:
                    flip_fp += 1
            elif y_flip > 0.5:
                flip_fn += 1
            b_idx = min(4, max(0, int(flip_r) // 20))
            cb = calib_bins_raw[b_idx]
            cb[0] += 1
            cb[1] += float(flip_r)
            cb[2] += y_flip

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
                    "store_name": store_master.seo_name(sname),
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
        else:
            closing_censored_count += 1

    n_valid = len(wp_abs_list)
    if n_valid > 0:
        mae_wp = round(sum(wp_abs_list) / n_valid, 2)
        rmse_wp = round(math.sqrt(sum(wp_sq_list) / n_valid), 2)
        bias_wp = round(sum(wp_sig_list) / n_valid, 2)
        w5_pct = round(sum(1 for e in wp_abs_list if e <= 5.0) / n_valid * 100.0, 1)
        w10_pct = round(sum(1 for e in wp_abs_list if e <= 10.0) / n_valid * 100.0, 1)
        flip_brier = round(flip_brier_sum / n_valid, 4)
        flip_ll = round(flip_logloss_sum / n_valid, 4)
        flip_prec = round((flip_tp / float(flip_tp + flip_fp)) * 100.0, 1) if (flip_tp + flip_fp) > 0 else None
        flip_rec = round((flip_tp / float(flip_tp + flip_fn)) * 100.0, 1) if (flip_tp + flip_fn) > 0 else None
        act_mglut_pct = round((flip_pos_total / float(n_valid)) * 100.0, 1)
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
        flip_brier = flip_ll = flip_prec = flip_rec = act_mglut_pct = None

    bin_labels = ("0〜19%", "20〜39%", "40〜59%", "60〜79%", "80〜100%")
    flip_calibration_bins = []
    for idx, lbl in enumerate(bin_labels):
        bn, b_sum_p, b_sum_y = calib_bins_raw[idx]
        if bn > 0:
            flip_calibration_bins.append({
                "range_label": lbl,
                "count": bn,
                "avg_pred_risk_pct": round(b_sum_p / bn, 1),
                "actual_flip_rate_pct": round((b_sum_y / bn) * 100.0, 1),
            })

    summary = {
        "resolved_count": n_valid,
        "total_resolved_rows": len(res_rows),
        "closing_censored_count": closing_censored_count,
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
        "flip_brier_score": flip_brier,
        "flip_log_loss": flip_ll,
        "flip_alert_precision_pct": flip_prec,
        "flip_alert_recall_pct": flip_rec,
        "actual_male_glut_rate_pct": act_mglut_pct,
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
            "store_name": store_master.seo_name(sname),
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
            "store_name": store_master.seo_name(sn),
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
                "store_name": store_master.seo_name(sn),
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
        "flip_calibration_bins": flip_calibration_bins,
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

