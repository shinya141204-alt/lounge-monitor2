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


def get_worksheet(title, header):
    """指定タイトルのワークシートを取得（なければヘッダー付きで作成）。失敗時は None。"""
    if is_disabled():
        return None
    with _ws_lock:
        if title in _ws_cache:
            return _ws_cache[title]
        try:
            import logger
            client = logger.get_client()
            if not client:
                return None
            sh = client.open(logger.SHEET_NAME)
            try:
                ws = sh.worksheet(title)
            except Exception:
                ws = sh.add_worksheet(title=title, rows=1000, cols=max(len(header), 4))
                ws.append_row(header)
            _ws_cache[title] = ws
            return ws
        except Exception as e:
            logging.error(f"[sheets_store] worksheet '{title}' unavailable: {e}")
            return None


def append_rows(title, header, rows):
    """行を追記。成功で True。"""
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


def append_rows_async(title, header, rows):
    """リクエスト処理をブロックしないようにバックグラウンドで追記。"""
    if not rows or is_disabled():
        return
    threading.Thread(target=append_rows, args=(title, header, rows), daemon=True).start()


def read_rows(title, header):
    """ヘッダー行を除く全行を list[list[str]] で返す。失敗時は []。"""
    ws = get_worksheet(title, header)
    if ws is None:
        return []
    try:
        values = ws.get_all_values()
        return values[1:] if values else []
    except Exception as e:
        print(f"[sheets_store] read '{title}' failed: {e}", file=sys.stderr)
        return []
