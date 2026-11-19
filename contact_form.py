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

import request_guards

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
GLOBAL_LIMIT = 150             # サイト全体で1時間に150件まで
GLOBAL_WINDOW_SEC = 3600
PRIORITY_CATEGORIES = ('removal', 'correction')
PRIORITY_GLOBAL_LIMIT = 40     # 優先カテゴリにも別枠の全体上限（1時間40件）
PRIORITY_WEBHOOK_BYPASS_LIMIT = 5  # クールダウン回避は1時間5件まで
PRIORITY_WEBHOOK_BYPASS_WINDOW_SEC = 3600

_EMAIL_RE = re.compile(r'^[^@\s<>"]+@[^@\s<>"]+\.[^@\s<>"]+$')
_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_JP_KANA_RE = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')
_CYRILLIC_RUN_RE = re.compile(r'[\u0400-\u04FF]{6,}')
_DOMAIN_LIKE_RE = re.compile(r'\b[a-z0-9][a-z0-9-]{1,61}\.(?:com|net|org|live|online|info|io|co|ru|xyz|top|site|shop|biz)\b', re.I)

# 海外ボットによる有料インデックス登録詐欺・スパム送信元ドメイン断片（言語を問わず即サイレント破棄）
_SCAM_DOMAIN_MARKERS = (
    'domainindex.',
    'searchregister.',
    'search-index.',
    'submitdomain.',
    'indexsubmit.',
    'googlesubmit.',
    'addurlgoogle.',
    'domainlisting.',
)

# 日本語（ひらがな・カタカナ）を1文字も含まない海外ボット定型営業スパム句
_NON_JP_BOT_PHRASES = (
    'buy backlinks',
    'high da backlinks',
    'dofollow backlinks',
    'guest post on your',
    'sponsored post on your',
    'rank #1 on google',
    'first page of google',
    'boost your domain authority',
    'increase your domain rating',
    'seo audit for your website',
    'submit your website to search engines',
    'add your site to google',
    'crypto recovery',
    'stolen bitcoin',
    'binary options',
    'online casino',
    'buy viagra',
    'buy cialis',
    'whatsapp: +',
    'telegram: @',
)

_rl_lock = threading.Lock()
_ip_hits = defaultdict(deque)
_global_hits = deque()
_priority_global_hits = deque()
_priority_webhook_hits = deque()


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
    if (
        not (ts.isascii() and ts.isdigit())
        or not re.fullmatch(r'[0-9a-f]{40}', sig, re.ASCII)
        or not request_guards.safe_compare_digest(sig, _sign(secret, ts))
    ):
        return 'invalid'
    age = (now if now is not None else time.time()) - int(ts)
    if age < TOKEN_MIN_AGE_SEC:
        return 'too_fast'
    if age > TOKEN_MAX_AGE_SEC:
        return 'expired'
    return ''


def _parse_origin_like_url(raw_val: str):
    from urllib.parse import urlsplit
    cleaned = (raw_val or '').strip()
    if not cleaned or cleaned.lower() == 'null':
        return None
    try:
        parsed = urlsplit(cleaned)
        scheme = (parsed.scheme or '').lower()
        if scheme not in ('http', 'https'):
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = (parsed.hostname or '').lower()
        if not hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    return scheme, hostname, port


def is_same_origin(origin: str, referer: str, host: str, strict: bool = False) -> bool:
    """Origin（無ければ Referer）がこのサイトのホスト・ポートと一致するか。Origin: null は即拒否。strict=True時は両方欠落で False。"""
    from urllib.parse import urlsplit
    raw_host = (host or '').strip()
    if not raw_host:
        return False
    try:
        parsed_host = urlsplit(f'//{raw_host}')
        if (
            parsed_host.username is not None
            or parsed_host.password is not None
            or parsed_host.path
            or parsed_host.query
            or parsed_host.fragment
        ):
            return False
        expected_host = (parsed_host.hostname or '').lower()
        expected_port = parsed_host.port
    except ValueError:
        return False
    if not expected_host:
        return False

    def _matches(raw_val: str) -> bool:
        parsed = _parse_origin_like_url(raw_val)
        if not parsed:
            return False
        scheme, cand_host, cand_port = parsed
        if cand_host != expected_host:
            return False
        default_port = 443 if scheme == 'https' else 80
        eff_cand_port = cand_port if cand_port is not None else default_port
        if expected_port is not None:
            return eff_cand_port == expected_port
        return cand_port is None or cand_port == default_port

    origin_clean = (origin or '').strip()
    if origin_clean:
        return _matches(origin_clean)
    referer_clean = (referer or '').strip()
    if referer_clean:
        return _matches(referer_clean)
    return not strict


def check_rate_limit(ip_addr: str, now: float = None, category: str = '') -> bool:
    """送信可能なら True を返し、送信を記録する。上限超過なら False（Redis稼働時は全ワーカー横断）。"""
    import turbo_store
    norm_ip = turbo_store.normalize_ip_for_rate_limit(ip_addr)
    is_priority = (category or '').strip() in PRIORITY_CATEGORIES
    if now is None and turbo_store.is_shared_bus_active():
        if turbo_store.get_rate_limit_count('contact_ip', norm_ip, IP_WINDOW_SEC) >= IP_LIMIT:
            return False
        if is_priority:
            if turbo_store.get_rate_limit_count('contact_prio_global', 'all', GLOBAL_WINDOW_SEC) >= PRIORITY_GLOBAL_LIMIT:
                return False
        elif turbo_store.get_rate_limit_count('contact_global', 'all', GLOBAL_WINDOW_SEC) >= GLOBAL_LIMIT:
            return False
        ip_cnt = turbo_store.record_rate_limit_hit('contact_ip', norm_ip, IP_WINDOW_SEC)
        if ip_cnt > IP_LIMIT:
            return False
        if is_priority:
            pr_cnt = turbo_store.record_rate_limit_hit('contact_prio_global', 'all', GLOBAL_WINDOW_SEC)
            if pr_cnt > PRIORITY_GLOBAL_LIMIT:
                return False
        else:
            gl_cnt = turbo_store.record_rate_limit_hit('contact_global', 'all', GLOBAL_WINDOW_SEC)
            if gl_cnt > GLOBAL_LIMIT:
                return False
        return True

    now = now if now is not None else time.time()
    with _rl_lock:
        while _global_hits and now - _global_hits[0] >= GLOBAL_WINDOW_SEC:
            _global_hits.popleft()
        while _priority_global_hits and now - _priority_global_hits[0] >= GLOBAL_WINDOW_SEC:
            _priority_global_hits.popleft()
        hits = _ip_hits[norm_ip]
        while hits and now - hits[0] >= IP_WINDOW_SEC:
            hits.popleft()
        if len(hits) >= IP_LIMIT:
            return False
        if is_priority:
            if len(_priority_global_hits) >= PRIORITY_GLOBAL_LIMIT:
                return False
        elif len(_global_hits) >= GLOBAL_LIMIT:
            return False
        hits.append(now)
        if is_priority:
            _priority_global_hits.append(now)
        else:
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
        turbo_store.clear_rate_limit('contact_prio_global')
        turbo_store.clear_rate_limit('contact_prio_webhook')
    except Exception:
        pass
    with _rl_lock:
        _ip_hits.clear()
        _global_hits.clear()
        _priority_global_hits.clear()
        _priority_webhook_hits.clear()


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
        if len(data['email']) > MAX_EMAIL_LEN or (not data['email'].isascii()) or not _EMAIL_RE.match(data['email']):
            errors.append('メールアドレスの形式をご確認ください。')
    if len(data['message']) < MIN_MESSAGE_LEN:
        errors.append(f'お問い合わせ内容は{MIN_MESSAGE_LEN}文字以上で入力してください。')
    elif len(data['message']) > MAX_MESSAGE_LEN:
        errors.append(f'お問い合わせ内容は{MAX_MESSAGE_LEN}文字以内で入力してください。')
    elif len(re.findall(r'https?://', data['message'])) > 3:
        errors.append('URL は3件までにしてください。')
    return data, errors


def detect_spam_reason(data: dict) -> str:
    """既知の海外SEO登録詐欺ボット・英語定型営業スパムを検知し、該当時は理由コード（非該当時は ''）を返す。

    日本語（ひらがな・カタカナ）を含む正規ユーザーや店舗関係者からの問い合わせを誤遮断しないよう、
    汎用ボット句の判定は「日本語かな文字ゼロ」の場合にのみ適用する。
    """
    if not isinstance(data, dict):
        return ''
    msg = str(data.get('message') or '')
    name = str(data.get('name') or '')
    email = str(data.get('email') or '')
    combined_low = f'{name}\n{email}\n{msg}'.lower()
    msg_low = msg.lower()

    # 1. 言語不問で確定スパムの詐欺ドメイン（domainindex.live 等）
    for marker in _SCAM_DOMAIN_MARKERS:
        if marker in combined_low:
            return f'scam_domain:{marker.rstrip(".")}'

    # 2. Google Search Index 登録勧誘ボットの定番英語テンプレート
    if ("google's search index" in msg_low or 'googles search index' in msg_low) and any(
        kw in msg_low for kw in ('submit ', 'feature ', 'show up in web search')
    ):
        return 'seo_index_scam_template'
    if 'submit ' in msg_low and ' today:' in msg_low and (
        'search index' in msg_low or 'search results' in msg_low
    ):
        return 'seo_submit_today_template'

    # 3. 日本語かな文字を含まない海外ボット定型スパム（日本語の正規問い合わせは100%通過）
    if not _JP_KANA_RE.search(msg):
        for phrase in _NON_JP_BOT_PHRASES:
            if phrase in msg_low:
                return 'foreign_bot_pitch'
        if _CYRILLIC_RUN_RE.search(msg) and ('http' in msg_low or _DOMAIN_LIKE_RE.search(msg_low)):
            return 'cyrillic_link_spam'

    return ''


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
        import turbo_store
        bypass_webhook = False
        if is_prio:
            if turbo_store.is_shared_bus_active():
                bypass_webhook = turbo_store.check_sliding_rate_limit(
                    'contact_prio_webhook',
                    'all',
                    PRIORITY_WEBHOOK_BYPASS_LIMIT,
                    PRIORITY_WEBHOOK_BYPASS_WINDOW_SEC,
                )
            else:
                now_wh = time.time()
                with _rl_lock:
                    while _priority_webhook_hits and now_wh - _priority_webhook_hits[0] >= PRIORITY_WEBHOOK_BYPASS_WINDOW_SEC:
                        _priority_webhook_hits.popleft()
                    if len(_priority_webhook_hits) < PRIORITY_WEBHOOK_BYPASS_LIMIT:
                        _priority_webhook_hits.append(now_wh)
                        bypass_webhook = True
        preview = data['message'].replace('\n', ' ')[:140]
        prefix = '【至急・削除/訂正依頼】' if is_prio else ''
        routes['webhook'] = bool(health.send_alert(
            f"contact:{received_at}:{ip_hash}",
            f"{prefix}お問い合わせを受信しました（{cat_label}）{' 返信先あり' if data['email'] else ''}：{preview}",
            bypass_cooldown=bypass_webhook,
        ))
    except Exception as e:
        logging.error(f'[contact] webhook notify failed: {e}')
    logging.info(f"[contact] inquiry received category={data['category']} len={len(data['message'])} routes={routes} inbox={inbox_saved}")
    return routes
