"""
turbo_store.py - Plan Take (竹) High-Speed Shared Bus, Pre-Compute Cache & Async Job Queue

Provides sub-millisecond cross-process coordination between `APP_ROLE=web` (Gunicorn workers)
and `APP_ROLE=worker` (`worker.py`) in the Tokyo (`nrt`) deployment:
  1. Live 53-store headcount & scraper health snapshot (`aiseki:live_snapshot`) via Upstash Redis (`REDIS_URL`).
  2. Shared AI Pre-Compute Cache (`aiseki:ai:<namespace>:<hash>`) so drafts generated ahead of schedule
     by `worker.py` are served in <0.5ms across all web workers.
  3. Pre-rendered SNS Media Blob Cache (`aiseki:media:<key>`) for 1080x1350 Instagram carousel PNG/JPEG
     slides and OGP cards, bypassing on-request Pillow rendering.
  4. Async SNS Job Queue (`aiseki:sns_queue` + `aiseki:sns_job:<id>`) so manual/API SNS triggers return
     HTTP 202 in ~1ms while `worker.py` executes Gemini/Claude + X/Threads/IG API calls in the background.
  5. Persistent PostgreSQL (`DATABASE_URL`) connection pool & KV/Analytics tables so `job_state.py`,
     `analytics.py`, and `local_db.py` never block on Google Sheets I/O or lose history on redeploy.

Gracefully degrades when `redis` or `psycopg2` are not installed locally, and provides an in-memory
shared bus (`enable_memory_bus_for_tests`) for deterministic unit testing.
"""

import base64
import collections
import contextlib
import datetime
import hashlib
import json
import logging
import os
import secrets
import threading
import time

try:
    import redis as _redis_lib
except ImportError:
    _redis_lib = None

try:
    import psycopg2 as _psycopg2
    from psycopg2 import pool as _pg_pool
    from psycopg2 import extras as _pg_extras
except ImportError:
    _psycopg2 = None
    _pg_pool = None
    _pg_extras = None

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

KEY_PREFIX = os.environ.get("TURBO_KEY_PREFIX", "aiseki").strip() or "aiseki"
LIVE_SNAPSHOT_KEY = f"{KEY_PREFIX}:live_snapshot"
SNS_QUEUE_KEY = f"{KEY_PREFIX}:sns_queue"
SNS_RECENT_JOBS_KEY = f"{KEY_PREFIX}:sns_recent_jobs"
STATE_KEYS_SET = f"{KEY_PREFIX}:job_state:keys"
STATE_KV_HASH = f"{KEY_PREFIX}:job_state:kv"
STATE_KV_TS_HASH = f"{KEY_PREFIX}:job_state:kv_ts"

_redis_lock = threading.Lock()
_redis_client = None
_redis_url_cached = None
_redis_failed_at = 0.0

_pg_lock = threading.Lock()
_pg_conn_pool = None
_pg_url_cached = None
_pg_schema_ready = False
_pg_failed_at = 0.0

# Ultra-short L1 RAM cache for live snapshot to avoid hitting Redis multiple times within the same second
_l1_snap_lock = threading.Lock()
_l1_snap_cache = {"mono": 0.0, "data": None}
_L1_SNAP_TTL_SEC = 1.0

# Deterministic in-memory shared bus for unit tests & single-host fallback
_mem_bus_enabled = False
_mem_lock = threading.Lock()
_mem_live_snapshot = None
_mem_ai_cache = {}       # (namespace, hash_str) -> (expire_epoch, dict_copy)
_mem_media_cache = {}    # media_key -> (expire_epoch, bytes, mime_type)
_mem_sns_queue = collections.deque()
_mem_sns_cond = threading.Condition(_mem_lock)
_mem_sns_jobs = collections.OrderedDict()
_mem_state_keys = collections.OrderedDict()
_mem_state_kv = {}
_mem_state_kv_ts = {}
_mem_analytics = collections.defaultdict(int)  # (date_jst, event, label) -> count


def _now_jst_str() -> str:
    return datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def enable_memory_bus_for_tests(enabled: bool = True):
    """Enable or disable the in-memory shared bus for unit testing without an external Redis server."""
    global _mem_bus_enabled
    with _mem_lock:
        _mem_bus_enabled = bool(enabled)
    with _l1_snap_lock:
        _l1_snap_cache["mono"] = 0.0
        _l1_snap_cache["data"] = None


def reset_for_tests():
    """Clear all in-memory bus state and L1 caches between unit tests."""
    global _mem_live_snapshot, _mem_bus_enabled
    with _mem_lock:
        _mem_bus_enabled = False
        _mem_live_snapshot = None
        _mem_ai_cache.clear()
        _mem_media_cache.clear()
        _mem_sns_queue.clear()
        _mem_sns_jobs.clear()
        _mem_state_keys.clear()
        _mem_state_kv.clear()
        _mem_state_kv_ts.clear()
        _mem_analytics.clear()
    with _l1_snap_lock:
        _l1_snap_cache["mono"] = 0.0
        _l1_snap_cache["data"] = None


# =====================================================================
# Connection Management: Upstash Redis (`REDIS_URL`) & PostgreSQL (`DATABASE_URL`)
# =====================================================================
def get_redis_url() -> str:
    return (os.environ.get("REDIS_URL") or "").strip()


def get_database_url() -> str:
    raw = (os.environ.get("DATABASE_URL") or "").strip()
    if raw.startswith("postgres://"):
        return "postgresql://" + raw[len("postgres://"):]
    return raw


def get_redis_client():
    """Return a thread-safe Redis client when `REDIS_URL` is configured and reachable, else `None`."""
    global _redis_client, _redis_url_cached, _redis_failed_at
    url = get_redis_url()
    if not url or _redis_lib is None:
        return None
    now_m = time.monotonic()
    with _redis_lock:
        if _redis_client is not None and _redis_url_cached == url:
            return _redis_client
        if _redis_failed_at > 0.0 and (now_m - _redis_failed_at < 15.0) and _redis_url_cached == url:
            return None
        try:
            kwargs = {
                "socket_connect_timeout": 3.0,
                "socket_timeout": 20.0,
                "health_check_interval": 30,
                "decode_responses": False,
            }
            if url.startswith("rediss://"):
                kwargs["ssl_cert_reqs"] = None
            client = _redis_lib.Redis.from_url(url, **kwargs)
            client.ping()
            _redis_client = client
            _redis_url_cached = url
            _redis_failed_at = 0.0
            logger.info("[turbo_store] Connected to Redis shared bus.")
            return _redis_client
        except Exception as err:
            _redis_client = None
            _redis_url_cached = url
            _redis_failed_at = now_m
            logger.warning(f"[turbo_store] Redis connection unavailable ({err}); falling back safely.")
            return None


def is_shared_bus_active() -> bool:
    """True when either `REDIS_URL` is active or the test memory bus is enabled."""
    if _mem_bus_enabled:
        return True
    return get_redis_client() is not None


def is_postgres_active() -> bool:
    """True when `DATABASE_URL` points to PostgreSQL and `psycopg2` is installed."""
    url = get_database_url()
    return bool(url and url.startswith("postgresql://") and _psycopg2 is not None)


def _get_pg_pool():
    global _pg_conn_pool, _pg_url_cached, _pg_failed_at, _pg_schema_ready
    url = get_database_url()
    if not url or not url.startswith("postgresql://") or _psycopg2 is None or _pg_pool is None:
        return None
    now_m = time.monotonic()
    with _pg_lock:
        if _pg_conn_pool is not None and _pg_url_cached == url:
            return _pg_conn_pool
        if _pg_failed_at > 0.0 and (now_m - _pg_failed_at < 15.0) and _pg_url_cached == url:
            return None
        if _pg_conn_pool is not None:
            try:
                _pg_conn_pool.closeall()
            except Exception:
                pass
            _pg_conn_pool = None
            _pg_schema_ready = False
        try:
            max_conns = max(4, min(20, int(os.environ.get("PG_POOL_MAX", "10"))))
            pool_obj = _pg_pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=max_conns,
                dsn=url,
                connect_timeout=4,
            )
            _pg_conn_pool = pool_obj
            _pg_url_cached = url
            _pg_failed_at = 0.0
            logger.info("[turbo_store] PostgreSQL connection pool initialized.")
            return _pg_conn_pool
        except Exception as err:
            _pg_conn_pool = None
            _pg_url_cached = url
            _pg_failed_at = now_m
            logger.warning(f"[turbo_store] PostgreSQL pool unavailable ({err}).")
            return None


@contextlib.contextmanager
def pg_connection():
    """Yield a healthy PostgreSQL connection from the pool, auto-closing broken sockets."""
    pool_obj = _get_pg_pool()
    if pool_obj is None:
        raise RuntimeError("PostgreSQL pool is not configured")
    conn = pool_obj.getconn()
    broken = False
    try:
        if getattr(conn, "closed", 0):
            pool_obj.putconn(conn, close=True)
            conn = pool_obj.getconn()
        yield conn
    except Exception as err:
        broken = bool(getattr(conn, "closed", 0)) or isinstance(
            err, (_psycopg2.OperationalError, _psycopg2.InterfaceError)
        )
        if not broken:
            try:
                conn.rollback()
            except Exception:
                broken = True
        raise
    finally:
        try:
            # BUG-09: Prevent 'idle in transaction' when returning connection to pool
            if not broken and not getattr(conn, "closed", 0):
                if getattr(conn, "status", 0) != 0:
                    try:
                        conn.rollback()
                    except Exception:
                        broken = True
            pool_obj.putconn(conn, close=broken)
        except Exception:
            pass


def init_postgres_aux_schema() -> bool:
    """Ensure `job_state` and `analytics_daily` tables exist in PostgreSQL."""
    global _pg_schema_ready
    if not is_postgres_active():
        return False
    if _pg_schema_ready:
        return True
    try:
        with pg_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS job_state_keys (
                        key TEXT PRIMARY KEY,
                        done_at_jst TEXT NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS job_state_kv (
                        prefix TEXT PRIMARY KEY,
                        val TEXT NOT NULL,
                        ts_jst TEXT NOT NULL,
                        updated_at_jst TEXT NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analytics_daily (
                        date_jst TEXT NOT NULL,
                        event TEXT NOT NULL,
                        label TEXT NOT NULL,
                        count BIGINT NOT NULL DEFAULT 0,
                        PRIMARY KEY (date_jst, event, label)
                    );
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_analytics_daily_date ON analytics_daily(date_jst);"
                )
                cur.execute(
                    """
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
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pos_tel_store_ts ON store_pos_telemetry_logs(store_name, timestamp);"
                )
                cur.execute(
                    """
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
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ext_ctx_city_ts ON external_context_logs(city_key, timestamp);"
                )
            conn.commit()
        _pg_schema_ready = True
        return True
    except Exception as err:
        logger.warning(f"[turbo_store] init_postgres_aux_schema warning: {err}")
        return False


def execute_pg_values_batch(sql: str, rows: list, page_size: int = 500) -> int:
    """Fast multi-row batch insert helper using `psycopg2.extras.execute_values`."""
    if not rows or not is_postgres_active():
        return 0
    with pg_connection() as conn:
        with conn.cursor() as cur:
            _pg_extras.execute_values(cur, sql, rows, page_size=page_size)
            inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return inserted


def get_backend_status() -> dict:
    """Diagnostic summary of active Plan Take backends (safe for `/api/health/deep` admin detail)."""
    return {
        "app_role": (os.environ.get("APP_ROLE") or "all").strip().lower(),
        "redis_configured": bool(get_redis_url()),
        "redis_connected": get_redis_client() is not None,
        "postgres_configured": bool(get_database_url()),
        "postgres_active": is_postgres_active(),
        "memory_bus_test_mode": bool(_mem_bus_enabled),
    }


# =====================================================================
# 1. Live 53-Store Snapshot Bus (`worker` -> `web`)
# =====================================================================
def publish_live_snapshot(
    full_data: list,
    last_updated: str,
    is_seed: bool = False,
    source_health: dict = None,
    ttl_sec: int = 3600,
) -> bool:
    """Publish the latest 53-store headcounts & scraper health so all `web` workers read in <0.5ms."""
    payload = {
        "full_data": list(full_data or []),
        "last_updated": last_updated or _now_jst_str(),
        "is_seed": bool(is_seed),
        "source_health": source_health or {},
        "published_at_epoch": time.time(),
    }
    with _l1_snap_lock:
        _l1_snap_cache["mono"] = time.monotonic()
        _l1_snap_cache["data"] = payload

    if _mem_bus_enabled:
        with _mem_lock:
            global _mem_live_snapshot
            _mem_live_snapshot = json.loads(json.dumps(payload, ensure_ascii=False))
        return True

    r = get_redis_client()
    if r is None:
        return False
    try:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r.setex(LIVE_SNAPSHOT_KEY, max(60, int(ttl_sec)), raw)
        return True
    except Exception as err:
        logger.warning(f"[turbo_store] publish_live_snapshot failed: {err}")
        return False


def get_live_snapshot(bypass_l1: bool = False) -> dict:
    """Read the latest 53-store snapshot published by `worker.py` (with 1.0s L1 memoization)."""
    now_m = time.monotonic()
    if not bypass_l1:
        with _l1_snap_lock:
            if _l1_snap_cache["data"] is not None and (now_m - _l1_snap_cache["mono"] < _L1_SNAP_TTL_SEC):
                return _l1_snap_cache["data"]

    if _mem_bus_enabled:
        with _mem_lock:
            snap = json.loads(json.dumps(_mem_live_snapshot)) if _mem_live_snapshot is not None else None
        if snap is not None:
            with _l1_snap_lock:
                _l1_snap_cache["mono"] = now_m
                _l1_snap_cache["data"] = snap
        return snap

    r = get_redis_client()
    if r is None:
        return None
    try:
        raw = r.get(LIVE_SNAPSHOT_KEY)
        if not raw:
            return None
        snap = json.loads(raw.decode("utf-8"))
        if isinstance(snap, dict):
            with _l1_snap_lock:
                _l1_snap_cache["mono"] = now_m
                _l1_snap_cache["data"] = snap
            return snap
    except Exception as err:
        logger.warning(f"[turbo_store] get_live_snapshot failed: {err}")
    return None


def update_live_snapshot_store_votes(store_name: str, votes_dict: dict) -> bool:
    """Update a single store's crowd votes inside the shared snapshot immediately after a user vote."""
    if not store_name or not isinstance(votes_dict, dict):
        return False
    snap = get_live_snapshot(bypass_l1=True)
    if not snap or not isinstance(snap.get("full_data"), list):
        return False
    changed = False
    for s in snap["full_data"]:
        if s.get("name") == store_name:
            s["votes"] = dict(votes_dict)
            changed = True
            break
    if not changed:
        return False
    return publish_live_snapshot(
        full_data=snap["full_data"],
        last_updated=snap.get("last_updated"),
        is_seed=bool(snap.get("is_seed")),
        source_health=snap.get("source_health"),
    )


# =====================================================================
# 2. Shared AI Pre-Compute Cache (`sns_editorial`, `column_editorial`, `pdca`)
# =====================================================================
def make_cache_hash(raw_key) -> str:
    """Convert any nested tuple/list/dict cache key into a deterministic SHA-256 hex digest."""
    canonical = json.dumps(raw_key, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def set_ai_cache(namespace: str, raw_key, value_dict: dict, ttl_sec: int = 2400) -> bool:
    if raw_key is None or not isinstance(value_dict, dict):
        return False
    ns = (namespace or "default").strip()
    h = make_cache_hash(raw_key)
    ttl = max(30, int(ttl_sec))

    if _mem_bus_enabled:
        with _mem_lock:
            _mem_ai_cache[(ns, h)] = (time.time() + ttl, json.loads(json.dumps(value_dict, ensure_ascii=False)))
        return True

    r = get_redis_client()
    if r is None:
        return False
    try:
        rkey = f"{KEY_PREFIX}:ai:{ns}:{h}"
        r.setex(rkey, ttl, json.dumps(value_dict, ensure_ascii=False).encode("utf-8"))
        return True
    except Exception as err:
        logger.debug(f"[turbo_store] set_ai_cache skipped: {err}")
        return False


def get_ai_cache(namespace: str, raw_key) -> dict:
    if raw_key is None:
        return None
    ns = (namespace or "default").strip()
    h = make_cache_hash(raw_key)

    if _mem_bus_enabled:
        now_t = time.time()
        with _mem_lock:
            hit = _mem_ai_cache.get((ns, h))
            if not hit:
                return None
            exp_t, val = hit
            if now_t >= exp_t:
                _mem_ai_cache.pop((ns, h), None)
                return None
            return json.loads(json.dumps(val, ensure_ascii=False))

    r = get_redis_client()
    if r is None:
        return None
    try:
        rkey = f"{KEY_PREFIX}:ai:{ns}:{h}"
        raw = r.get(rkey)
        if not raw:
            return None
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception as err:
        logger.debug(f"[turbo_store] get_ai_cache skipped: {err}")
        return None


def clear_ai_cache(namespace: str = None) -> int:
    ns = (namespace or "").strip()
    removed = 0
    if _mem_bus_enabled:
        with _mem_lock:
            target_keys = [k for k in _mem_ai_cache.keys() if (not ns or k[0] == ns)]
            for k in target_keys:
                _mem_ai_cache.pop(k, None)
                removed += 1
        return removed

    r = get_redis_client()
    if r is None:
        return 0
    try:
        pattern = f"{KEY_PREFIX}:ai:{ns}:*" if ns else f"{KEY_PREFIX}:ai:*"
        batch = []
        for k in r.scan_iter(match=pattern, count=100):
            batch.append(k)
            if len(batch) >= 100:
                removed += int(r.delete(*batch) or 0)
                batch.clear()
        if batch:
            removed += int(r.delete(*batch) or 0)
    except Exception as err:
        logger.debug(f"[turbo_store] clear_ai_cache skipped: {err}")
    return removed


# =====================================================================
# 3. Pre-Rendered SNS Media Blob Cache (IG Carousel PNG/JPEG & ZIP)
# =====================================================================
def set_media_blob(media_key: str, raw_bytes: bytes, mime_type: str = "image/png", ttl_sec: int = 3600) -> bool:
    if not media_key or not isinstance(raw_bytes, (bytes, bytearray)) or not raw_bytes:
        return False
    ttl = max(60, int(ttl_sec))
    b_data = bytes(raw_bytes)

    if _mem_bus_enabled:
        with _mem_lock:
            _mem_media_cache[media_key] = (time.time() + ttl, b_data, mime_type or "application/octet-stream")
        return True

    r = get_redis_client()
    if r is None:
        return False
    try:
        rkey = f"{KEY_PREFIX}:media:{media_key}"
        envelope = (mime_type or "application/octet-stream").encode("ascii", errors="ignore") + b"\n" + b_data
        r.setex(rkey, ttl, envelope)
        return True
    except Exception as err:
        logger.debug(f"[turbo_store] set_media_blob skipped: {err}")
        return False


def get_media_blob(media_key: str) -> tuple:
    """Return `(bytes, mime_type)` if cached and fresh, else `None`."""
    if not media_key:
        return None

    if _mem_bus_enabled:
        now_t = time.time()
        with _mem_lock:
            hit = _mem_media_cache.get(media_key)
            if not hit:
                return None
            exp_t, b_data, mime = hit
            if now_t >= exp_t:
                _mem_media_cache.pop(media_key, None)
                return None
            return (b_data, mime)

    r = get_redis_client()
    if r is None:
        return None
    try:
        rkey = f"{KEY_PREFIX}:media:{media_key}"
        raw = r.get(rkey)
        if not raw or b"\n" not in raw:
            return None
        mime_b, body = raw.split(b"\n", 1)
        return (body, mime_b.decode("ascii", errors="replace"))
    except Exception as err:
        logger.debug(f"[turbo_store] get_media_blob skipped: {err}")
        return None


# =====================================================================
# 4. Async SNS Job Queue (`/api/sns-jobs` & `/api/trigger-*?async=1`)
# =====================================================================
def _sns_job_redis_key(job_id: str) -> str:
    return f"{KEY_PREFIX}:sns_job:{job_id}"


def enqueue_sns_job(job_type: str, params: dict = None, requested_by: str = "master") -> dict:
    """Push an SNS posting task onto the async queue and return its tracking record in <1ms."""
    job_id = datetime.datetime.now(JST).strftime("%Y%m%d_%H%M%S_") + secrets.token_hex(4)
    job = {
        "job_id": job_id,
        "job_type": str(job_type or "").strip(),
        "params": dict(params or {}),
        "requested_by": requested_by,
        "status": "queued",
        "created_at": _now_jst_str(),
        "started_at": "",
        "finished_at": "",
        "result": None,
        "error": "",
    }

    if _mem_bus_enabled:
        with _mem_sns_cond:
            _mem_sns_jobs[job_id] = dict(job)
            while len(_mem_sns_jobs) > 100:
                _mem_sns_jobs.popitem(last=False)
            _mem_sns_queue.append(job_id)
            _mem_sns_cond.notify()
        return dict(job)

    r = get_redis_client()
    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.setex(_sns_job_redis_key(job_id), 86400, json.dumps(job, ensure_ascii=False).encode("utf-8"))
            pipe.rpush(SNS_QUEUE_KEY, job_id.encode("utf-8"))
            pipe.lpush(SNS_RECENT_JOBS_KEY, job_id.encode("utf-8"))
            pipe.ltrim(SNS_RECENT_JOBS_KEY, 0, 49)
            pipe.expire(SNS_RECENT_JOBS_KEY, 86400)
            pipe.execute()
            return dict(job)
        except Exception as err:
            logger.warning(f"[turbo_store] Redis enqueue_sns_job fallback to local memory: {err}")

    # Fallback to process-local memory queue when Redis is not configured
    with _mem_sns_cond:
        _mem_sns_jobs[job_id] = dict(job)
        while len(_mem_sns_jobs) > 100:
            _mem_sns_jobs.popitem(last=False)
        _mem_sns_queue.append(job_id)
        _mem_sns_cond.notify()
    return dict(job)


def get_sns_job(job_id: str) -> dict:
    if not job_id:
        return None
    if not _mem_bus_enabled:
        r = get_redis_client()
        if r is not None:
            try:
                raw = r.get(_sns_job_redis_key(job_id))
                if raw:
                    return json.loads(raw.decode("utf-8"))
            except Exception:
                pass
    with _mem_lock:
        hit = _mem_sns_jobs.get(job_id)
        return json.loads(json.dumps(hit, ensure_ascii=False)) if hit else None


def list_recent_sns_jobs(limit: int = 12) -> list:
    lim = max(1, min(50, int(limit or 12)))
    if not _mem_bus_enabled:
        r = get_redis_client()
        if r is not None:
            try:
                ids = r.lrange(SNS_RECENT_JOBS_KEY, 0, lim - 1) or []
                out = []
                for raw_id in ids:
                    jid = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                    j = get_sns_job(jid)
                    if j:
                        out.append(j)
                return out
            except Exception:
                pass
    with _mem_lock:
        vals = list(_mem_sns_jobs.values())[-lim:]
        vals.reverse()
        return json.loads(json.dumps(vals, ensure_ascii=False))


def _save_sns_job_record(job: dict):
    job_id = job.get("job_id")
    if not job_id:
        return
    if not _mem_bus_enabled:
        r = get_redis_client()
        if r is not None:
            try:
                r.setex(_sns_job_redis_key(job_id), 86400, json.dumps(job, ensure_ascii=False).encode("utf-8"))
                return
            except Exception as err:
                logger.warning(f"[turbo_store] _save_sns_job_record fallback: {err}")
    with _mem_lock:
        _mem_sns_jobs[job_id] = json.loads(json.dumps(job, ensure_ascii=False))


def claim_next_sns_job(timeout_sec: float = 2.0) -> dict:
    """Pop the next queued SNS job and mark it `running` (`worker.py` consumer loop)."""
    if not _mem_bus_enabled:
        r = get_redis_client()
        if r is not None:
            try:
                popped = r.blpop(SNS_QUEUE_KEY, timeout=max(1, int(round(timeout_sec))))
                if not popped:
                    return None
                _, raw_id = popped
                job_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                job = get_sns_job(job_id)
                if not job:
                    return None
                job["status"] = "running"
                job["started_at"] = _now_jst_str()
                _save_sns_job_record(job)
                return job
            except Exception as err:
                logger.warning(f"[turbo_store] claim_next_sns_job error: {err}")
                return None

    with _mem_sns_cond:
        if not _mem_sns_queue and timeout_sec > 0:
            _mem_sns_cond.wait(timeout=float(timeout_sec))
        if not _mem_sns_queue:
            return None
        job_id = _mem_sns_queue.popleft()
        job = _mem_sns_jobs.get(job_id)
        if not job:
            return None
        job["status"] = "running"
        job["started_at"] = _now_jst_str()
        return json.loads(json.dumps(job, ensure_ascii=False))


def complete_sns_job(job_id: str, result: dict = None, error: str = "") -> dict:
    """Finalize an SNS job with `succeeded` or `failed` status and persist its payload."""
    job = get_sns_job(job_id)
    if not job:
        return None
    err_str = str(error or "").strip()
    if not err_str and isinstance(result, dict) and result.get("status") == "error":
        err_str = str(result.get("error") or result.get("reason") or "SNS API error")
    job["status"] = "failed" if err_str else "succeeded"
    job["finished_at"] = _now_jst_str()
    job["result"] = result
    job["error"] = err_str
    _save_sns_job_record(job)
    return job


# =====================================================================
# 5. Fast Persistent `Job State` & `Analytics` Store (Postgres / Redis)
# =====================================================================
def state_mark_done(key: str, done_at_jst: str = None) -> bool:
    if not key:
        return False
    ts = done_at_jst or _now_jst_str()
    wrote = False

    if _mem_bus_enabled:
        with _mem_lock:
            _mem_state_keys[key] = ts
        wrote = True

    if is_postgres_active() and init_postgres_aux_schema():
        try:
            with pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO job_state_keys (key, done_at_jst)
                        VALUES (%s, %s)
                        ON CONFLICT (key) DO NOTHING;
                        """,
                        (key, ts),
                    )
                conn.commit()
            wrote = True
        except Exception as err:
            logger.warning(f"[turbo_store] state_mark_done pg error: {err}")

    r = get_redis_client()
    if r is not None:
        try:
            r.sadd(STATE_KEYS_SET, key.encode("utf-8"))
            wrote = True
        except Exception as err:
            logger.debug(f"[turbo_store] state_mark_done redis skipped: {err}")

    return wrote


def state_set_kv(prefix: str, obfuscated_or_plain_val: str, ts_14: str, updated_at_jst: str = None) -> bool:
    if not prefix:
        return False
    val_s = "" if obfuscated_or_plain_val is None else str(obfuscated_or_plain_val)
    ts_s = str(ts_14 or "")
    upd = updated_at_jst or _now_jst_str()
    wrote = False

    if _mem_bus_enabled:
        with _mem_lock:
            if ts_s >= _mem_state_kv_ts.get(prefix, ""):
                _mem_state_kv[prefix] = val_s
                _mem_state_kv_ts[prefix] = ts_s
        wrote = True

    if is_postgres_active() and init_postgres_aux_schema():
        try:
            with pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO job_state_kv (prefix, val, ts_jst, updated_at_jst)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (prefix) DO UPDATE
                        SET val = EXCLUDED.val,
                            ts_jst = EXCLUDED.ts_jst,
                            updated_at_jst = EXCLUDED.updated_at_jst
                        WHERE EXCLUDED.ts_jst >= job_state_kv.ts_jst;
                        """,
                        (prefix, val_s, ts_s, upd),
                    )
                conn.commit()
            wrote = True
        except Exception as err:
            logger.warning(f"[turbo_store] state_set_kv pg error: {err}")

    r = get_redis_client()
    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.hset(STATE_KV_HASH, prefix.encode("utf-8"), val_s.encode("utf-8"))
            pipe.hset(STATE_KV_TS_HASH, prefix.encode("utf-8"), ts_s.encode("utf-8"))
            pipe.execute()
            wrote = True
        except Exception as err:
            logger.debug(f"[turbo_store] state_set_kv redis skipped: {err}")

    return wrote


def state_load_snapshot() -> dict:
    """
    Load `(keys_list, kv_dict, kv_ts_dict)` from shared fast storage (Redis / PostgreSQL / test bus)
    so `job_state.py` syncs cross-process updates (tokens, tone settings, posted flags) in <1ms.
    Returns `None` if neither Redis, Postgres, nor test bus is active.
    """
    has_any_backend = _mem_bus_enabled or is_postgres_active() or (get_redis_client() is not None)
    if not has_any_backend:
        return None

    keys_out = []
    kv_out = {}
    kv_ts_out = {}

    if _mem_bus_enabled:
        with _mem_lock:
            keys_out.extend(_mem_state_keys.keys())
            kv_out.update(_mem_state_kv)
            kv_ts_out.update(_mem_state_kv_ts)
        return {"keys": keys_out, "kv": kv_out, "kv_ts": kv_ts_out}

    if is_postgres_active() and init_postgres_aux_schema():
        try:
            with pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT key FROM job_state_keys ORDER BY done_at_jst ASC;")
                    for (k,) in cur.fetchall():
                        if k:
                            keys_out.append(str(k))
                    cur.execute("SELECT prefix, val, ts_jst FROM job_state_kv;")
                    for pfx, val, ts_j in cur.fetchall():
                        if pfx:
                            p_s = str(pfx)
                            kv_out[p_s] = str(val or "")
                            kv_ts_out[p_s] = str(ts_j or "")
                try:
                    conn.rollback()
                except Exception:
                    pass
        except Exception as err:
            logger.warning(f"[turbo_store] state_load_snapshot pg error: {err}")

    r = get_redis_client()
    if r is not None:
        try:
            raw_set = r.smembers(STATE_KEYS_SET) or set()
            seen = set(keys_out)
            for rk in raw_set:
                ks = rk.decode("utf-8") if isinstance(rk, bytes) else str(rk)
                if ks and ks not in seen:
                    seen.add(ks)
                    keys_out.append(ks)
            raw_kv = r.hgetall(STATE_KV_HASH) or {}
            raw_ts = r.hgetall(STATE_KV_TS_HASH) or {}
            for rk, rv in raw_kv.items():
                p_s = rk.decode("utf-8") if isinstance(rk, bytes) else str(rk)
                v_s = rv.decode("utf-8") if isinstance(rv, bytes) else str(rv)
                t_raw = raw_ts.get(rk, b"")
                t_s = t_raw.decode("utf-8") if isinstance(t_raw, bytes) else str(t_raw)
                if t_s >= kv_ts_out.get(p_s, ""):
                    kv_out[p_s] = v_s
                    if t_s:
                        kv_ts_out[p_s] = t_s
        except Exception as err:
            logger.debug(f"[turbo_store] state_load_snapshot redis skipped: {err}")

    return {"keys": keys_out, "kv": kv_out, "kv_ts": kv_ts_out}


def analytics_add_batch(items: list) -> bool:
    """
    Atomically increment `analytics_daily` counters in PostgreSQL / test bus.
    `items`: list of `((date_jst, event, label), count)`
    """
    if not items:
        return True
    wrote = False

    if _mem_bus_enabled:
        with _mem_lock:
            for (d, e, l), c in items:
                _mem_analytics[(str(d), str(e), str(l))] += int(c)
        wrote = True

    if is_postgres_active() and init_postgres_aux_schema():
        try:
            rows = [(str(d), str(e), str(l), int(c)) for (d, e, l), c in items if int(c) > 0]
            if rows:
                execute_pg_values_batch(
                    """
                    INSERT INTO analytics_daily (date_jst, event, label, count)
                    VALUES %s
                    ON CONFLICT (date_jst, event, label)
                    DO UPDATE SET count = analytics_daily.count + EXCLUDED.count;
                    """,
                    rows,
                )
            wrote = True
        except Exception as err:
            logger.warning(f"[turbo_store] analytics_add_batch pg error: {err}")

    return wrote


def analytics_replace_from_sheets(rows: list) -> int:
    """Seed `analytics_daily` in PostgreSQL from historical Google Sheets rows (`[date, event, label, count]`)."""
    if not rows or not (is_postgres_active() and init_postgres_aux_schema()):
        return 0
    agg = collections.defaultdict(int)
    for r in rows:
        if len(r) >= 4 and r[0] and r[1]:
            try:
                agg[(str(r[0]), str(r[1]), str(r[2] or ""))] += int(r[3])
            except ValueError:
                continue
    if not agg:
        return 0
    batch = [(d, e, l, cnt) for (d, e, l), cnt in agg.items() if cnt > 0]
    with pg_connection() as conn:
        with conn.cursor() as cur:
            _pg_extras.execute_values(
                cur,
                """
                INSERT INTO analytics_daily (date_jst, event, label, count)
                VALUES %s
                ON CONFLICT (date_jst, event, label)
                DO UPDATE SET count = GREATEST(analytics_daily.count, EXCLUDED.count);
                """,
                batch,
                page_size=500,
            )
        conn.commit()
    return len(batch)


def analytics_query_since(start_date_str: str) -> list:
    """
    Return `[[date_jst, event, label, count], ...]` from PostgreSQL or test bus in <2ms,
    or `None` if PostgreSQL/test bus is not active.
    """
    if _mem_bus_enabled:
        with _mem_lock:
            return [
                [d, e, l, cnt]
                for (d, e, l), cnt in _mem_analytics.items()
                if d >= start_date_str
            ]

    if is_postgres_active() and init_postgres_aux_schema():
        try:
            with pg_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT date_jst, event, label, count
                        FROM analytics_daily
                        WHERE date_jst >= %s;
                        """,
                        (start_date_str,),
                    )
                    rows = cur.fetchall()
                try:
                    conn.rollback()
                except Exception:
                    pass
                return [[r[0], r[1], r[2], int(r[3])] for r in rows]
        except Exception as err:
            logger.warning(f"[turbo_store] analytics_query_since pg error: {err}")
    return None
