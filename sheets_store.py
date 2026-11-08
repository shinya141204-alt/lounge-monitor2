"""
sheets_store.py - Google Sheets を「消えない保存先」として使うための共通ヘルパー

Render の無料/標準インスタンスはデプロイや再起動でローカルディスク
（SQLite・/tmp・data/）が初期化されます。投票・投稿済みフラグ・アクセス集計など
「消えると困るデータ」は、このモジュール経由で既存のスプレッドシート
（'Lounge Monitor Data'）内の専用ワークシートに追記して永続化します。

Sheets が使えない環境（認証ファイルなし・テスト時など）では全関数が
安全に何もせず False / [] を返すので、呼び出し側はフォールバックできます。
"""
import os
import sys
import queue
import threading
import logging

_ws_cache = {}
_ws_lock = threading.Lock()


def is_disabled():
    return os.environ.get('DISABLE_SHEETS', '') == '1'


def _is_test_runtime() -> bool:
    """unittest / pytest 実行中は本番 Google Sheets への実通信をハードロック遮断する。"""
    if os.environ.get('ALLOW_LIVE_SHEETS_IN_TESTS', '') == '1':
        return False
    return ('unittest' in sys.modules) or ('pytest' in sys.modules)


def get_worksheet(title, header):
    """指定タイトルのワークシートを取得（なければヘッダー付きで作成）。失敗時は None。"""
    if is_disabled() or _is_test_runtime():
        return None
    with _ws_lock:
        cached = _ws_cache.get(title)
        if cached is not None:
            return cached

    try:
        import logger
        client = logger.get_client()
        if not client:
            return None
        sh = logger.open_spreadsheet(client) if hasattr(logger, 'open_spreadsheet') else client.open(logger.SHEET_NAME)
        if sh is None:
            return None
        try:
            ws = sh.worksheet(title)
        except Exception:
            with _ws_lock:
                cached = _ws_cache.get(title)
                if cached is not None:
                    return cached
            try:
                ws = sh.add_worksheet(title=title, rows=1000, cols=max(len(header), 4))
                ws.append_row(header)
            except Exception:
                ws = sh.worksheet(title)
        with _ws_lock:
            _ws_cache[title] = ws
        return ws
    except Exception as e:
        logging.error(f"[sheets_store] worksheet '{title}' unavailable: {e}")
        return None


def _strip_sheet_headers(values, header):
    """ヘッダー行が先頭以外にズレ込んだ場合でも先頭データ行を欠落させずに除外する。"""
    if not values:
        return []
    hdr = list(header) if header else []
    if hdr and values[0] == hdr:
        return [r for r in values[1:] if r != hdr]
    if hdr:
        return [r for r in values if r != hdr]
    return values[1:]


def _append_ws_rows(title, header, rows):
    if not rows:
        return True
    ws = get_worksheet(title, header)
    if ws is None:
        return False
    try:
        ws.append_rows(rows, value_input_option='RAW')
        return True
    except Exception as e:
        logging.warning(f"[sheets_store] append to '{title}' failed; refreshing worksheet handle and retrying once: {e}")
        with _ws_lock:
            _ws_cache.pop(title, None)
    # Stale cached handle (e.g. worksheet recreated by another VM): re-fetch once and retry
    ws = get_worksheet(title, header)
    if ws is None:
        return False
    try:
        ws.append_rows(rows, value_input_option='RAW')
        return True
    except Exception as e:
        logging.error(f"[sheets_store] append to '{title}' failed after retry: {e}")
        with _ws_lock:
            _ws_cache.pop(title, None)
        return False


def _read_ws_rows_or_none(title, header):
    ws = get_worksheet(title, header)
    if ws is None:
        return None
    try:
        values = ws.get_all_values()
        return _strip_sheet_headers(values, header)
    except Exception as e:
        print(f"[sheets_store] read '{title}' failed; refreshing worksheet handle and retrying once: {e}", file=sys.stderr)
        with _ws_lock:
            _ws_cache.pop(title, None)
    ws = get_worksheet(title, header)
    if ws is None:
        return None
    try:
        values = ws.get_all_values()
        return _strip_sheet_headers(values, header)
    except Exception as e:
        print(f"[sheets_store] read '{title}' failed after retry: {e}", file=sys.stderr)
        with _ws_lock:
            _ws_cache.pop(title, None)
        return None


def append_rows(title, header, rows):
    """行を追記。成功で True。"""
    return _append_ws_rows(title, header, rows)


_ASYNC_QUEUE_MAX = 2000
_ASYNC_BATCH_MAX_ROWS = 500
_async_queue = queue.Queue(maxsize=_ASYNC_QUEUE_MAX)
_async_worker_lock = threading.Lock()
_async_worker_thread = None


def _append_with_retry(title, header, rows):
    import time
    retry_delays = (1.0, 3.0)
    for attempt in range(3):
        if is_disabled():
            return
        if append_rows(title, header, rows):
            return
        if attempt < len(retry_delays):
            time.sleep(retry_delays[attempt])
    logging.error(f"[sheets_store] append_rows_async failed after 3 attempts for '{title}' ({len(rows)} rows)")


def _async_append_worker():
    """単一デーモンワーカー: 同一 title の連続タスクを1回の append_rows にまとめて API 呼び出し数を削減する。"""
    carry = None
    while True:
        task = carry if carry is not None else _async_queue.get()
        carry = None
        title, header, rows = task
        batch = list(rows)
        consumed = 1
        try:
            while len(batch) < _ASYNC_BATCH_MAX_ROWS:
                try:
                    nxt = _async_queue.get_nowait()
                except queue.Empty:
                    break
                consumed += 1
                if nxt[0] == title and list(nxt[1]) == list(header):
                    batch.extend(nxt[2])
                else:
                    carry = nxt
                    break
            _append_with_retry(title, header, batch)
        except Exception as e:
            logging.error(f"[sheets_store] async append worker error for '{title}': {e}")
        finally:
            # carry was fetched via get() but is processed (and marked done) in the next iteration
            for _ in range(consumed - (1 if carry is not None else 0)):
                try:
                    _async_queue.task_done()
                except ValueError:
                    break


def _ensure_async_worker():
    global _async_worker_thread
    with _async_worker_lock:
        if _async_worker_thread is None or not _async_worker_thread.is_alive():
            _async_worker_thread = threading.Thread(
                target=_async_append_worker, daemon=True, name='sheets-append-worker'
            )
            _async_worker_thread.start()


def append_rows_async(title, header, rows):
    """リクエスト処理をブロックしないようにバックグラウンドで追記（一時失敗時は1秒・3秒待機で最大3回リトライ）。
    呼び出しごとのスレッド生成は廃止し、上限2,000件のキュー＋単一ワーカーで直列・まとめ追記する。"""
    if not rows or is_disabled() or _is_test_runtime():
        return
    _ensure_async_worker()
    try:
        _async_queue.put_nowait((title, list(header), [list(r) for r in rows]))
    except queue.Full:
        logging.error(
            f"[sheets_store] async append queue full ({_ASYNC_QUEUE_MAX}); dropping {len(rows)} rows for '{title}' "
            "(primary stores PG/Redis/local are unaffected)"
        )


def read_rows_or_none(title, header):
    """ヘッダー行を除く全行を list[list[str]] で返す。未接続・エラー時は None を返す。"""
    return _read_ws_rows_or_none(title, header)


def read_rows(title, header):
    """ヘッダー行を除く全行を list[list[str]] で返す。失敗時は []。"""
    rows = read_rows_or_none(title, header)
    return [] if rows is None else rows


def append_hourly_rows(title, header, rows):
    """時間帯別集計シート（'Analytics Hourly'）へ行を追記。成功で True。"""
    return _append_ws_rows(title, header, rows)


def read_hourly_rows_or_none(title, header):
    """時間帯別集計シートのヘッダー行を除く全行を list[list[str]] で返す。未接続・エラー時は None。"""
    return _read_ws_rows_or_none(title, header)


def read_hourly_rows(title, header):
    """時間帯別集計シートのヘッダー行を除く全行を list[list[str]] で返す。失敗時は []。"""
    rows = read_hourly_rows_or_none(title, header)
    return [] if rows is None else rows
