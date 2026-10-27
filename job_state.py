"""
job_state.py - 自動投稿の「投稿済み」記録を再起動・再デプロイ後も保持する

以前は /tmp や data/ のファイルに保存していたため、Render の再起動で消え、
・note の最新記事が再デプロイのたびに再投稿される
・再起動直後に同じ時間帯の投稿が二重に走る
といった問題が起こり得ました。

ここでは Google Sheets の「Job State」シートを正とし、ローカルファイルを
キャッシュ兼フォールバックとして使います。
"""
import os
import re
import json
import base64
import secrets
import hashlib
import datetime
import threading

import sheets_store

SHEET_TITLE = 'Job State'
HEADER = ['key', 'done_at_jst']
MAX_HISTORY_KEYS = 3000
_OBF_PREFIX = 'obf1:'
_KV_ENTRY_RE = re.compile(r'^([^=]+)=(\d{14})\|(.*)$', re.S)

_LOCAL_DIR = os.environ.get('JOB_STATE_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
_LOCAL_FILE = os.path.join(_LOCAL_DIR, 'job_state.json')

_lock = threading.Lock()
_sheets_sync_lock = threading.Lock()
_keys = None       # dict[str, None] preserving insertion order
_kv = None         # dict[str, str] exempt from history truncation
_kv_ts = None      # dict[str, str] latest timestamp per kv prefix
_local_loaded = False
_sheets_loaded = False
_last_sheet_try = 0.0


def _now_str():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')


def _derive_stream(nonce: bytes, length: int) -> bytes:
    secret = (
        os.environ.get('JOB_STATE_SECRET')
        or os.environ.get('FLASK_SECRET_KEY')
        or 'aiseki-navi-job-state-v1'
    ).encode('utf-8')
    out = bytearray()
    counter = 0
    while len(out) < length:
        digest = hashlib.sha256(secret + nonce + counter.to_bytes(4, 'big')).digest()
        out.extend(digest)
        counter += 1
    return bytes(out[:length])


def _obfuscate_secret(plain: str) -> str:
    if not plain:
        return ''
    raw = plain.encode('utf-8')
    nonce = secrets.token_bytes(8)
    mask = _derive_stream(nonce, len(raw))
    xored = bytes(a ^ b for a, b in zip(raw, mask))
    encoded = base64.urlsafe_b64encode(nonce + xored).decode('ascii')
    return f"{_OBF_PREFIX}{encoded}"


def _deobfuscate_secret(stored: str) -> str:
    if not stored or not stored.startswith(_OBF_PREFIX):
        return stored
    try:
        blob = base64.urlsafe_b64decode(stored[len(_OBF_PREFIX):].encode('ascii'))
        if len(blob) < 8:
            return stored
        nonce, xored = blob[:8], blob[8:]
        mask = _derive_stream(nonce, len(xored))
        return bytes(a ^ b for a, b in zip(xored, mask)).decode('utf-8')
    except Exception:
        return stored


def _ingest_entry(entry: str):
    """Register a raw key or KV entry into ordered `_keys` / `_kv`."""
    if not entry:
        return
    m = _KV_ENTRY_RE.match(entry)
    if m:
        pfx, ts, raw_val = m.group(1), m.group(2), m.group(3)
        prev_ts = _kv_ts.get(pfx, '')
        if ts >= prev_ts:
            _kv_ts[pfx] = ts
            val = _deobfuscate_secret(raw_val) if pfx.startswith('token:') else raw_val
            _kv[pfx] = val
        return
    if entry in _keys:
        del _keys[entry]
    _keys[entry] = None


def _trimmed_history_list(keys_iter) -> list:
    all_hist = list(keys_iter)
    if len(all_hist) <= MAX_HISTORY_KEYS:
        return all_hist
    pinned = [k for k in all_hist if k.startswith(('note:', 'note_title:', 'col_release:'))]
    rest = [k for k in all_hist if not k.startswith(('note:', 'note_title:', 'col_release:'))]
    keep_rest = max(0, MAX_HISTORY_KEYS - len(pinned))
    return pinned + (rest[-keep_rest:] if keep_rest > 0 else [])


def _ensure_local_loaded_locked():
    """`_lock` 保持中にローカルキャッシュを初期化する（ネットワーク通信なし・0.1ms未満）。"""
    global _keys, _kv, _kv_ts, _local_loaded
    if _keys is None:
        _keys = {}
        _kv = {}
        _kv_ts = {}
        _local_loaded = False
        try:
            if os.path.exists(_LOCAL_FILE):
                with open(_LOCAL_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
                for k in data.get('keys', []):
                    _ingest_entry(k)
                kv_data = data.get('kv')
                if isinstance(kv_data, dict):
                    for k, v in kv_data.items():
                        if k is not None and v is not None:
                            ks = str(k)
                            vs = str(v)
                            _kv[ks] = _deobfuscate_secret(vs) if ks.startswith('token:') else vs
                _local_loaded = True
        except Exception:
            pass
    return _keys


def _sync_sheets_worker():
    """`_lock` を保持せずに Google Sheets から読み込み、完了時のみ短時間ロックしてマージする。"""
    global _keys, _sheets_loaded, _last_sheet_try
    import time
    if _sheets_loaded or sheets_store.is_disabled():
        return
    with _sheets_sync_lock:
        if _sheets_loaded or sheets_store.is_disabled():
            return
        now_t = time.time()
        if now_t - _last_sheet_try <= 300:
            return
        _last_sheet_try = now_t
        rows = sheets_store.read_rows_or_none(SHEET_TITLE, HEADER)
        if rows is not None:
            with _lock:
                _ensure_local_loaded_locked()
                for row in rows:
                    if row and row[0]:
                        _ingest_entry(row[0])
                _keys = {k: None for k in _trimmed_history_list(_keys.keys())}
                _sheets_loaded = True


def _ensure_loaded(allow_blocking: bool = True):
    """ローカル初期化後、必要なら Google Sheets 同期を行う（`allow_blocking=False` 時は非同期起動して即返却）。"""
    import time
    with _lock:
        _ensure_local_loaded_locked()
        already_synced = _sheets_loaded or sheets_store.is_disabled()
        should_try = (not already_synced) and (time.time() - _last_sheet_try > 300)
    if should_try:
        if allow_blocking:
            _sync_sheets_worker()
        else:
            threading.Thread(target=_sync_sheets_worker, daemon=True).start()


def is_storage_healthy() -> bool:
    """
    Return False when running with Sheets enabled and the initial Sheets read has not succeeded yet,
    so callers like note_poster never rely solely on stale local state before Sheets sync completes.
    """
    _ensure_loaded(allow_blocking=True)
    with _lock:
        if sheets_store.is_disabled():
            return True
        return bool(_sheets_loaded)


def _save_local():
    global _keys
    try:
        os.makedirs(_LOCAL_DIR, exist_ok=True)
        trimmed = _trimmed_history_list(_keys.keys())
        _keys = {k: None for k in trimmed}
        local_kv = {}
        for k, v in (_kv or {}).items():
            ks = str(k)
            vs = "" if v is None else str(v)
            local_kv[ks] = _obfuscate_secret(vs) if ks.startswith('token:') else vs
        payload = {
            'keys': trimmed,
            'kv': local_kv,
        }
        with open(_LOCAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def is_done(key):
    _ensure_loaded(allow_blocking=True)
    with _lock:
        return key in _ensure_local_loaded_locked()


def all_keys():
    _ensure_loaded(allow_blocking=True)
    with _lock:
        return set(_ensure_local_loaded_locked().keys())


def mark_done(key):
    _ensure_loaded(allow_blocking=True)
    with _lock:
        keys = _ensure_local_loaded_locked()
        if key in keys:
            return
        _ingest_entry(key)
        _save_local()
    sheets_store.append_rows_async(SHEET_TITLE, HEADER, [[key, _now_str()]])


def get_value(prefix, allow_blocking: bool = True):
    """Retrieve the latest stored value for a prefix like 'token:threads' from Job State."""
    _ensure_loaded(allow_blocking=allow_blocking)
    full_prefix = f"{prefix}="
    with _lock:
        keys = _ensure_local_loaded_locked()
        if _kv is not None and prefix in _kv:
            return _kv[prefix]
        matched = [k for k in keys if k.startswith(full_prefix)]
    if not matched:
        return None
    matched.sort()
    raw = matched[-1][len(full_prefix):]
    if "|" in raw:
        raw = raw.split("|", 1)[1]
    return _deobfuscate_secret(raw) if str(prefix).startswith('token:') else raw


def set_value(prefix, val):
    """Persist a key-value entry in Job State across restarts and redeploys (exempt from key cap)."""
    val_str = "" if val is None else str(val)
    ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d%H%M%S')
    _ensure_loaded(allow_blocking=False)
    with _lock:
        _ensure_local_loaded_locked()
        _kv[str(prefix)] = val_str
        _kv_ts[str(prefix)] = ts
        _save_local()
    sheet_val = _obfuscate_secret(val_str) if str(prefix).startswith('token:') else val_str
    entry = f"{prefix}={ts}|{sheet_val}"
    sheets_store.append_rows_async(SHEET_TITLE, HEADER, [[entry, _now_str()]])


def reset_cache_for_tests():
    global _keys, _kv, _kv_ts, _local_loaded, _sheets_loaded, _last_sheet_try
    with _lock:
        _keys = None
        _kv = None
        _kv_ts = None
        _local_loaded = False
        _sheets_loaded = False
        _last_sheet_try = 0.0
