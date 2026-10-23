"""
health.py - データ取得の異常検知と通知

・系列（オリエンタル/JIS/XIX/ALFA/ヤタコイ/CLOVERS）ごとに取得の成否を記録
・連続失敗が一定回数を超えたら「停止」、復旧したら「復旧」を通知
・オリエンタル系の店舗数が急に減った（HTML構造変更の兆候）ときも通知
・店舗マスタにない店舗名が出てきたら（新店舗・表記変更）通知

通知先は環境変数 ALERT_WEBHOOK_URL（Discord / Slack の Incoming Webhook）。
未設定ならログ出力のみ。同じ内容の通知は ALERT_COOLDOWN_MIN 分（既定60分）抑止します。
"""
import os
import time
import json
import logging
import threading
import datetime

import requests

FAIL_THRESHOLD = int(os.environ.get('ALERT_FAIL_THRESHOLD', '5'))
COOLDOWN_SEC = int(os.environ.get('ALERT_COOLDOWN_MIN', '60')) * 60

# 正常時のおおよその店舗数。これを大きく下回ったら構造変更を疑う
EXPECTED_MIN_STORES = {
    'get_oriental_data': 30,
    'get_jis_data': 8,
}

SOURCE_LABELS = {
    'get_oriental_data': 'オリエンタルラウンジ/ag',
    'get_jis_data': 'JIS',
    'get_xix_data': 'XIX岡山',
    'get_alfa_data': 'ALFA広島',
    'get_yatakoi_data': 'ヤタコイ',
    'get_clovers_data': 'CLOVERS広島',
}

_lock = threading.Lock()
_state = {}          # source -> {'consecutive_fail': int, 'down': bool, 'last_ok': str, 'last_error': str, 'last_count': int}
_last_sent = {}      # alert_key -> epoch
_sent_log = []       # 直近の通知（テスト・確認用）
_known_unknowns = set()


def _now_str():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')


def send_alert(key, message):
    """クールダウン付きで通知。送信（またはログ記録）したら True。"""
    now = time.time()
    with _lock:
        if now - _last_sent.get(key, 0) < COOLDOWN_SEC:
            return False
        _last_sent[key] = now
        _sent_log.append({'key': key, 'message': message, 'at': _now_str()})
        del _sent_log[:-50]

    logging.warning(f"[ALERT] {message}")
    url = os.environ.get('ALERT_WEBHOOK_URL', '').strip()
    if url:
        def _post():
            try:
                text = f"【相席ナビ監視】{message}"
                # Discord は content、Slack は text を見る
                requests.post(url, data=json.dumps({'content': text, 'text': text}),
                              headers={'Content-Type': 'application/json'}, timeout=8)
            except Exception as e:
                logging.error(f"[health] webhook failed: {e}")
        threading.Thread(target=_post, daemon=True).start()
    return True


def record_fetch(results_by_name, errors, all_sources):
    """monitor.get_all_data() の1回分の結果を記録し、必要なら通知する。"""
    for src in all_sources:
        label = SOURCE_LABELS.get(src, src)
        res = results_by_name.get(src) or []
        with _lock:
            st = _state.setdefault(src, {'consecutive_fail': 0, 'down': False, 'last_ok': None,
                                         'last_error': None, 'last_count': 0})
        if res:
            was_down = st['down']
            st.update({'consecutive_fail': 0, 'down': False, 'last_ok': _now_str(),
                       'last_error': None, 'last_count': len(res)})
            if was_down:
                send_alert(f'recover:{src}', f"✅ {label} のデータ取得が復旧しました（{len(res)}店舗）")
            exp = EXPECTED_MIN_STORES.get(src)
            if exp and len(res) < exp:
                send_alert(f'shrink:{src}', f"⚠️ {label} の取得店舗数が {len(res)} 件に減っています（通常 {exp} 件以上）。公式サイトの構造変更の可能性があります。")
        else:
            st['consecutive_fail'] += 1
            st['last_error'] = errors.get(src, 'empty')
            # 昼間は店舗側が空データを返すことがあるため、営業時間帯（18時〜翌4時）のみ停止扱いにする
            if st['consecutive_fail'] >= FAIL_THRESHOLD and not st['down'] and _is_business_hours():
                st['down'] = True
                send_alert(f'down:{src}', f"🚨 {label} のデータ取得が {st['consecutive_fail']} 回連続で失敗しています（{st['last_error']}）")


def _is_business_hours():
    if os.environ.get('ALERT_ALWAYS', '') == '1':
        return True
    h = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).hour
    return h >= 18 or h <= 4


def check_unknown_stores(names):
    try:
        import store_master
        unknown = [n for n in store_master.unknown_stores(names) if n not in _known_unknowns]
    except Exception:
        return []
    if unknown:
        _known_unknowns.update(unknown)
        send_alert('unknown:' + ','.join(unknown), "🆕 店舗マスタ未登録の店舗名を検知しました: " + ' / '.join(unknown) +
                   "（static/store_master.json に追加してください）")
    return unknown


def snapshot():
    with _lock:
        return {
            'sources': {k: dict(v) for k, v in _state.items()},
            'recent_alerts': list(_sent_log[-10:]),
            'webhook_configured': bool(os.environ.get('ALERT_WEBHOOK_URL')),
        }


def reset_for_tests():
    with _lock:
        _state.clear()
        _last_sent.clear()
        _sent_log.clear()
        _known_unknowns.clear()
