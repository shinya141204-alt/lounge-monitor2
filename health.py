"""
health.py - データ取得の異常検知と通知

・系列（オリエンタルラウンジ/JIS/XIX/ALFA/ヤタコイ/CLOVERS）ごとに取得の成否を記録
・連続失敗が一定回数を超えたら「停止」、復旧したら「復旧」を通知
・オリエンタルラウンジ系の店舗数が急に減った（HTML構造変更の兆候）ときも通知
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
    'get_asobibar_data': 8,
}

SOURCE_LABELS = {
    'get_oriental_data': 'オリエンタルラウンジ/ag',
    'get_jis_data': 'JIS',
    'get_asobibar_data': 'ASOBIBAR',
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


def _resolve_webhook_url() -> str:
    try:
        import ops_governance
        eff = ops_governance.get_effective_webhook_url()
        if eff:
            return eff
    except Exception:
        pass
    return os.environ.get('ALERT_WEBHOOK_URL', '').strip()


def _revalidate_webhook_url(url: str) -> str:
    """送信直前に SSRF 検証を再実行する（登録後の DNS リバインディング対策）。エラー文字列 or ''。"""
    try:
        import ops_governance
        return ops_governance._validate_public_https_webhook(url)
    except Exception as e:
        return f'Webhook URL の検証に失敗しました: {type(e).__name__}'


def _post_webhook(url: str, text: str):
    # allow_redirects=False: 公開ホストから内部アドレスへの 30x リダイレクトによる SSRF を遮断
    return requests.post(url, data=json.dumps({'content': text, 'text': text}),
                         headers={'Content-Type': 'application/json'}, timeout=8, allow_redirects=False)


def send_alert(key, message, bypass_cooldown: bool = False):
    """クールダウン付きで通知。送信（またはログ記録）したら True。"""
    now = time.time()
    with _lock:
        if not bypass_cooldown and (now - _last_sent.get(key, 0) < COOLDOWN_SEC):
            return False
        _last_sent[key] = now
        _sent_log.append({'key': key, 'message': message, 'at': _now_str()})
        del _sent_log[:-50]

    logging.warning(f"[ALERT] {message}")
    url = _resolve_webhook_url()
    if url:
        def _post():
            try:
                err = _revalidate_webhook_url(url)
                if err:
                    logging.error(f"[health] webhook blocked by SSRF re-validation: {err}")
                    return
                text = f"【相席ナビ監視】{message}"
                # Discord は content、Slack は text を見る
                _post_webhook(url, text)
            except Exception as e:
                logging.error(f"[health] webhook failed: {e}")
        threading.Thread(target=_post, daemon=True).start()
    return True


def send_test_webhook() -> dict:
    """管理画面から同期テスト通知を送信し、HTTP応答を検証する。"""
    url = _resolve_webhook_url()
    if not url:
        return {'ok': False, 'error': '通知用Webhook URLが登録されていません。'}
    err = _revalidate_webhook_url(url)
    if err:
        return {'ok': False, 'error': err}
    try:
        text = f"【相席ナビ監視】テスト通知です（{_now_str()} JST）。お問い合わせ・削除依頼・取得異常が発生した際にこのチャンネルへ即時通知されます。"
        r = _post_webhook(url, text)
        if 200 <= r.status_code < 300:
            return {'ok': True, 'message': f'テスト通知の送信に成功しました（HTTP {r.status_code}）。'}
        return {'ok': False, 'error': f'送信先サーバーがエラーを返しました（HTTP {r.status_code}）。'}
    except Exception as e:
        return {'ok': False, 'error': f'送信エラー: {e}'}


def record_fetch(results_by_name, errors, all_sources):
    """monitor.get_all_data() の1回分の結果を記録し、必要なら通知する。"""
    alerts_to_send = []
    with _lock:
        for src in all_sources:
            label = SOURCE_LABELS.get(src, src)
            res = results_by_name.get(src) or []
            st = _state.setdefault(src, {'consecutive_fail': 0, 'down': False, 'last_ok': None,
                                         'last_error': None, 'last_count': 0})
            if res:
                was_down = st['down']
                st.update({'consecutive_fail': 0, 'down': False, 'last_ok': _now_str(),
                           'last_error': None, 'last_count': len(res)})
                if was_down:
                    alerts_to_send.append((f'recover:{src}', f"✅ {label} のデータ取得が復旧しました（{len(res)}店舗）"))
                exp = EXPECTED_MIN_STORES.get(src)
                if exp and len(res) < exp:
                    alerts_to_send.append((f'shrink:{src}', f"⚠️ {label} の取得店舗数が {len(res)} 件に減っています（通常 {exp} 件以上）。公式サイトの構造変更の可能性があります。"))
            else:
                st['consecutive_fail'] += 1
                st['last_error'] = errors.get(src, 'empty')
                # 昼間は店舗側が空データを返すことがあるため、営業時間帯（18時〜翌4時）のみ停止扱いにする
                if st['consecutive_fail'] >= FAIL_THRESHOLD and not st['down'] and _is_business_hours():
                    st['down'] = True
                    alerts_to_send.append((f'down:{src}', f"🚨 {label} のデータ取得が {st['consecutive_fail']} 回連続で失敗しています（{st['last_error']}）"))

    for key, msg in alerts_to_send:
        send_alert(key, msg)


def _is_business_hours():
    if os.environ.get('ALERT_ALWAYS', '') == '1':
        return True
    h = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).hour
    return h >= 18 or h <= 4


def check_unknown_stores(names):
    try:
        import store_master
        candidates = store_master.unknown_stores(names)
    except Exception:
        return []

    unknown = []
    with _lock:
        for n in (candidates or []):
            if n not in _known_unknowns:
                unknown.append(n)
        if unknown:
            _known_unknowns.update(unknown)

    if unknown:
        send_alert('unknown:' + ','.join(unknown), "🆕 店舗マスタ未登録の店舗名を検知しました: " + ' / '.join(unknown) +
                   "（static/store_master.json に追加してください）")
    return unknown


def snapshot():
    # 管理画面で登録した Webhook も反映（job_state 参照は _lock の外で行い、ロック保持中の I/O を避ける）
    webhook_configured = bool(_resolve_webhook_url())
    with _lock:
        return {
            'sources': {k: dict(v) for k, v in _state.items()},
            'recent_alerts': list(_sent_log[-10:]),
            'webhook_configured': webhook_configured,
        }


def reset_for_tests():
    with _lock:
        _state.clear()
        _last_sent.clear()
        _sent_log.clear()
        _known_unknowns.clear()
