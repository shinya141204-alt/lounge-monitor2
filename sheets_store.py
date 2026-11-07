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
        logging.error(f"[sheets_store] append to '{title}' failed: {e}")
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
        print(f"[sheets_store] read '{title}' failed: {e}", file=sys.stderr)
        with _ws_lock:
            _ws_cache.pop(title, None)
        return None


def append_rows(title, header, rows):
    """行を追記。成功で True。"""
    return _append_ws_rows(title, header, rows)


def append_rows_async(title, header, rows):
    """リクエスト処理をブロックしないようにバックグラウンドで追記（一時失敗時は1秒・3秒待機で最大3回リトライ）。"""
    if not rows or is_disabled() or _is_test_runtime():
        return

    def _worker():
        import time
        retry_delays = (1.0, 3.0)
        for attempt in range(3):
            if is_disabled():
                return
            if append_rows(title, header, rows):
                return
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
        logging.error(f"[sheets_store] append_rows_async failed after 3 attempts for '{title}'")

    threading.Thread(target=_worker, daemon=True).start()


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
