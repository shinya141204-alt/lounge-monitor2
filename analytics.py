"""
analytics.py - 自前の軽量アクセス集計（個人情報なし）

「どの記事が読まれ、どのボタン・アフィリエイトがクリックされているか」を
日別・イベント別・ラベル別の件数だけで集計します（IP・端末IDなどは保存しません）。

・メモリ上で加算 → 10分ごとに Google Sheets 'Analytics Daily' へまとめて追記
・集計画面（/staff/analytics）は Sheets の値＋未送信分を合算して表示
"""
import datetime
import threading
import logging
from collections import defaultdict

import sheets_store

SHEET_TITLE = 'Analytics Daily'
HEADER = ['date_jst', 'event', 'label', 'count']

ALLOWED_EVENTS = {
    'page_view',        # 記事・ポータルページの閲覧（サーバー側で記録）
    'affiliate_click',  # /link/<id> 経由のアフィリエイトクリック（サーバー側で記録）
    'store_open',       # 店舗詳細モーダルを開いた
    'map_cafe',         # 2軒目: 夜カフェのマップ検索
    'map_bar',          # 2軒目: バー・バルのマップ検索
    'official_click',   # 公式サイトへのリンク
    'sns_click',        # X/Threads/note への導線
    'share',            # LINE/X 共有・コピー
    'vote',             # 現場投票
}

_lock = threading.Lock()
_pending = defaultdict(int)   # (date, event, label) -> count（Sheets未送信）
_flushed = defaultdict(int)   # 起動後に送信済みの分（Sheets読込失敗時の表示用）


def _today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d')


def track(event, label=''):
    if event not in ALLOWED_EVENTS:
        return False
    label = str(label or '')[:80]
    with _lock:
        _pending[(_today(), event, label)] += 1
    return True


def flush():
    """未送信分を Sheets に追記。失敗したら戻して次回再送。"""
    with _lock:
        if not _pending:
            return 0
        items = list(_pending.items())
        _pending.clear()
    rows = [[d, e, l, c] for (d, e, l), c in items]
    ok = sheets_store.append_rows(SHEET_TITLE, HEADER, rows)
    with _lock:
        for key, c in items:
            if ok:
                _flushed[key] += c
            else:
                _pending[key] += c
    if not ok:
        logging.warning('[analytics] flush failed; will retry')
    return len(rows) if ok else 0


def summary(days=7):
    """直近 days 日の集計: {'by_event': {...}, 'by_label': {event: [(label, count), ...]}, 'by_day': {...}}"""
    start = (datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))) -
             datetime.timedelta(days=days - 1)).strftime('%Y-%m-%d')
    totals = defaultdict(int)
    sheet_rows = sheets_store.read_rows(SHEET_TITLE, HEADER)
    if sheet_rows:
        for r in sheet_rows:
            if len(r) >= 4 and r[0] >= start:
                try:
                    totals[(r[0], r[1], r[2])] += int(r[3])
                except ValueError:
                    continue
    else:
        with _lock:
            for k, c in _flushed.items():
                totals[k] += c
    with _lock:
        for k, c in _pending.items():
            totals[k] += c

    by_event = defaultdict(int)
    by_label = defaultdict(lambda: defaultdict(int))
    by_day = defaultdict(lambda: defaultdict(int))
    for (d, e, l), c in totals.items():
        if d < start:
            continue
        by_event[e] += c
        by_label[e][l] += c
        by_day[d][e] += c
    return {
        'days': days,
        'since': start,
        'by_event': dict(sorted(by_event.items(), key=lambda x: -x[1])),
        'by_label': {e: sorted(v.items(), key=lambda x: -x[1])[:15] for e, v in by_label.items()},
        'by_day': {d: dict(v) for d, v in sorted(by_day.items())},
    }


def reset_for_tests():
    with _lock:
        _pending.clear()
        _flushed.clear()
