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
import json
import datetime
import threading

import sheets_store

SHEET_TITLE = 'Job State'
HEADER = ['key', 'done_at_jst']

_LOCAL_DIR = os.environ.get('JOB_STATE_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
_LOCAL_FILE = os.path.join(_LOCAL_DIR, 'job_state.json')

_lock = threading.Lock()
_keys = None  # set[str]
_sheets_loaded = False
_last_sheet_try = 0.0


def _now_str():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')


def _load():
    """ローカル→Sheets の順で読み込む。Sheets 読み込みに失敗した場合は5分後に再試行する。"""
    global _keys, _sheets_loaded, _last_sheet_try
    import time
    if _keys is None:
        _keys = set()
        try:
            if os.path.exists(_LOCAL_FILE):
                with open(_LOCAL_FILE, 'r', encoding='utf-8') as f:
                    _keys.update(json.load(f).get('keys', []))
        except Exception:
            pass
    if not _sheets_loaded and not sheets_store.is_disabled() and time.time() - _last_sheet_try > 300:
        _last_sheet_try = time.time()
        ws = sheets_store.get_worksheet(SHEET_TITLE, HEADER)
        if ws is not None:
            for row in sheets_store.read_rows(SHEET_TITLE, HEADER):
                if row and row[0]:
                    _keys.add(row[0])
            _sheets_loaded = True
    return _keys


def _save_local():
    try:
        os.makedirs(_LOCAL_DIR, exist_ok=True)
        # 直近2000件だけ保持
        with open(_LOCAL_FILE, 'w', encoding='utf-8') as f:
            json.dump({'keys': sorted(_keys)[-2000:]}, f, ensure_ascii=False)
    except Exception:
        pass


def is_done(key):
    with _lock:
        return key in _load()


def all_keys():
    with _lock:
        return set(_load())


def mark_done(key):
    with _lock:
        keys = _load()
        if key in keys:
            return
        keys.add(key)
        _save_local()
    sheets_store.append_rows_async(SHEET_TITLE, HEADER, [[key, _now_str()]])


def get_value(prefix):
    """Retrieve the latest stored value for a prefix like 'token:threads:' from Job State."""
    full_prefix = f"{prefix}="
    with _lock:
        keys = _load()
        matched = [k for k in keys if k.startswith(full_prefix)]
    if not matched:
        return None
    # Sort by timestamp suffix if present, otherwise return last
    matched.sort()
    raw = matched[-1][len(full_prefix):]
    if "|" in raw:
        return raw.split("|", 1)[1]
    return raw


def set_value(prefix, val):
    """Persist a key-value entry in Job State across restarts and redeploys."""
    ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d%H%M%S')
    entry = f"{prefix}={ts}|{val}"
    mark_done(entry)


def reset_cache_for_tests():
    global _keys, _sheets_loaded, _last_sheet_try
    with _lock:
        _keys = None
        _sheets_loaded = False
        _last_sheet_try = 0.0
