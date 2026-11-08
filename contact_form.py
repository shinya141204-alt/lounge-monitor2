"""contact_form.py - /contact お問い合わせフォームのサーバー側ロジック

- CSRF: セッションを使わないステートレス HMAC トークン（発行時刻つき）＋ Origin/Referer 照合
- スパム対策: ハニーポット項目・送信までの最短秒数・IP 単位／全体のレート制限
- 入力長制限: 各項目の最大文字数とリクエスト全体のサイズ上限
- 保存先: 既存の Google Sheets 永続化（sheets_store）＋ ALERT_WEBHOOK_URL 通知（health.send_alert）

外部サービスが使えない環境（テスト・ローカル）では保存処理は安全に何もしない。
"""
import datetime
import hashlib
import hmac
import logging
import re
import threading
import time
from collections import defaultdict, deque

SHEET_TITLE = 'Contact Inquiries'
SHEET_HEADER = ['received_at_jst', 'category', 'name', 'email', 'message', 'ip_hash', 'user_agent']

CATEGORIES = (
    ('correction', '掲載内容の誤り・訂正'),
    ('removal', '店舗関係者からの掲載に関するご依頼'),
    ('feature', 'サイトの不具合・機能のご要望'),
    ('business', '取材・広告・提携のご相談'),
    ('other', 'その他'),
)
CATEGORY_LABELS = dict(CATEGORIES)

MAX_NAME_LEN = 50
MAX_EMAIL_LEN = 120
MIN_MESSAGE_LEN = 10
MAX_MESSAGE_LEN = 2000
MAX_REQUEST_BYTES = 32 * 1024  # URL エンコードで日本語は1文字9バイトになるため 2,000字 ≒ 18KB

TOKEN_MIN_AGE_SEC = 3          # 人間がフォームを読んで入力するより速い送信は機械的とみなす
TOKEN_MAX_AGE_SEC = 2 * 3600   # 2時間でトークン失効

IP_LIMIT = 3                   # 同一IPから10分間に3件まで
IP_WINDOW_SEC = 600
GLOBAL_LIMIT = 150             # サイト全体で1時間に150件まで（削除・訂正依頼は全体上限の対象外）
GLOBAL_WINDOW_SEC = 3600
PRIORITY_CATEGORIES = ('removal', 'correction')

_EMAIL_RE = re.compile(r'^[^@\s<>"]+@[^@\s<>"]+\.[^@\s<>"]+$')
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

_rl_lock = threading.Lock()
_ip_hits = defaultdict(deque)
_global_hits = deque()


def _jst_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


def _sign(secret: str, ts: str) -> str:
    return hmac.new(secret.encode('utf-8'), f'contact:{ts}'.encode('utf-8'), hashlib.sha256).hexdigest()[:40]


def issue_token(secret: str, now: float = None) -> str:
    ts = str(int(now if now is not None else time.time()))
    return f'{ts}.{_sign(secret, ts)}'


def verify_token(secret: str, token: str, now: float = None) -> str:
    """Return '' when valid, otherwise an error code ('invalid' / 'too_fast' / 'expired')."""
    token = (token or '').strip()
    if '.' not in token or len(token) > 80:
        return 'invalid'
    ts, sig = token.split('.', 1)
    if not ts.isdigit() or not hmac.compare_digest(sig, _sign(secret, ts)):
        return 'invalid'
    age = (now if now is not None else time.time()) - int(ts)
    if age < TOKEN_MIN_AGE_SEC:
        return 'too_fast'
    if age > TOKEN_MAX_AGE_SEC:
        return 'expired'
    return ''


def is_same_origin(origin: str, referer: str, host: str) -> bool:
    """Origin（無ければ Referer）がこのサイトのホストと一致するか。どちらも無い場合はトークン検証に委ねる。"""
    host = (host or '').split(':')[0].lower()
    for src in (origin, referer):
        src = (src or '').strip()
        if not src or src == 'null':
            continue
        m = re.match(r'^https?://([^/:]+)', src, re.I)
        return bool(m) and m.group(1).lower() == host
    return True


def check_rate_limit(ip_addr: str, now: float = None, category: str = '') -> bool:
    """送信可能なら True を返し、送信を記録する。上限超過なら False（Redis稼働時は全ワーカー横断）。"""
    import turbo_store
    is_priority = (category or '').strip() in PRIORITY_CATEGORIES
    if now is None and turbo_store.is_shared_bus_active():
        if turbo_store.get_rate_limit_count('contact_ip', ip_addr, IP_WINDOW_SEC) >= IP_LIMIT:
            return False
        if not is_priority and turbo_store.get_rate_limit_count('contact_global', 'all', GLOBAL_WINDOW_SEC) >= GLOBAL_LIMIT:
            return False
        ip_cnt = turbo_store.record_rate_limit_hit('contact_ip', ip_addr, IP_WINDOW_SEC)
        if ip_cnt > IP_LIMIT:
            return False
        if not is_priority:
            gl_cnt = turbo_store.record_rate_limit_hit('contact_global', 'all', GLOBAL_WINDOW_SEC)
            if gl_cnt > GLOBAL_LIMIT:
                return False
        return True

    now = now if now is not None else time.time()
    with _rl_lock:
        while _global_hits and now - _global_hits[0] >= GLOBAL_WINDOW_SEC:
            _global_hits.popleft()
        hits = _ip_hits[ip_addr]
        while hits and now - hits[0] >= IP_WINDOW_SEC:
            hits.popleft()
        if len(hits) >= IP_LIMIT or (not is_priority and len(_global_hits) >= GLOBAL_LIMIT):
            return False
        hits.append(now)
        if not is_priority:
            _global_hits.append(now)
        if len(_ip_hits) > 2048:
            for k in [k for k, v in _ip_hits.items() if not v or now - v[-1] >= IP_WINDOW_SEC]:
                _ip_hits.pop(k, None)
        return True


def reset_rate_limits_for_tests():
    try:
        import turbo_store
        turbo_store.clear_rate_limit('contact_ip')
        turbo_store.clear_rate_limit('contact_global')
    except Exception:
        pass
    with _rl_lock:
        _ip_hits.clear()
        _global_hits.clear()


def _clean(value: str, max_len: int, multiline: bool = False) -> str:
    value = (value or '').replace('\r\n', '\n').replace('\r', '\n')
    value = _CTRL_RE.sub('', value)
    if not multiline:
        value = value.replace('\n', ' ')
    return value.strip()[: max_len + 1]


def validate(form) -> tuple:
    """Return (cleaned_dict, errors_list)."""
    data = {
        'category': (form.get('category') or '').strip(),
        'name': _clean(form.get('name'), MAX_NAME_LEN),
        'email': _clean(form.get('email'), MAX_EMAIL_LEN),
        'message': _clean(form.get('message'), MAX_MESSAGE_LEN, multiline=True),
    }
    errors = []
    if data['category'] not in CATEGORY_LABELS:
        errors.append('お問い合わせの種類を選択してください。')
    if len(data['name']) > MAX_NAME_LEN:
        errors.append(f'お名前は{MAX_NAME_LEN}文字以内で入力してください。')
    if data['email']:
        if len(data['email']) > MAX_EMAIL_LEN or not _EMAIL_RE.match(data['email']):
            errors.append('メールアドレスの形式をご確認ください。')
    if len(data['message']) < MIN_MESSAGE_LEN:
        errors.append(f'お問い合わせ内容は{MIN_MESSAGE_LEN}文字以上で入力してください。')
    elif len(data['message']) > MAX_MESSAGE_LEN:
        errors.append(f'お問い合わせ内容は{MAX_MESSAGE_LEN}文字以内で入力してください。')
    elif len(re.findall(r'https?://', data['message'])) > 3:
        errors.append('URL は3件までにしてください。')
    return data, errors


def hash_ip(secret: str, ip_addr: str) -> str:
    """標準 HMAC-SHA256 による IP 匿名化（`sha256(secret|ip)` 連結方式は長さ拡張・区切り曖昧性の懸念があるため廃止）。"""
    return hmac.new(
        str(secret or '').encode('utf-8'),
        f'contact_ip:{ip_addr or ""}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()[:32]


def store_inquiry(data: dict, ip_hash: str, user_agent: str = '') -> dict:
    """既存の永続化（Sheets ＋ 管理画面受信箱）と通知（Webhook）へ渡す。戻り値は保存経路の記録。"""
    received_at = _jst_now().strftime('%Y-%m-%d %H:%M:%S')
    cat_label = CATEGORY_LABELS.get(data['category'], data['category'])
    is_prio = data.get('category') in PRIORITY_CATEGORIES
    row = [
        received_at,
        cat_label,
        data['name'],
        data['email'],
        data['message'],
        ip_hash,
        (user_agent or '')[:160],
    ]
    routes = {'sheets': False, 'webhook': False}
    inbox_saved = False
    try:
        import ops_governance
        ops_governance.record_inquiry(data, received_at=received_at, ip_hash=ip_hash, category_label=cat_label)
        inbox_saved = True
    except Exception as e:
        logging.error(f'[contact] inbox record failed: {e}')
    try:
        import sheets_store
        if not sheets_store.is_disabled():
            sheets_store.append_rows_async(SHEET_TITLE, SHEET_HEADER, [row])
            routes['sheets'] = True
    except Exception as e:
        logging.error(f'[contact] sheets append failed: {e}')
    try:
        import health
        preview = data['message'].replace('\n', ' ')[:140]
        prefix = '【至急・削除/訂正依頼】' if is_prio else ''
        routes['webhook'] = bool(health.send_alert(
            f"contact:{received_at}:{ip_hash}",
            f"{prefix}お問い合わせを受信しました（{cat_label}）{' 返信先あり' if data['email'] else ''}：{preview}",
            bypass_cooldown=is_prio,
        ))
    except Exception as e:
        logging.error(f'[contact] webhook notify failed: {e}')
    logging.info(f"[contact] inquiry received category={data['category']} len={len(data['message'])} routes={routes} inbox={inbox_saved}")
    return routes
