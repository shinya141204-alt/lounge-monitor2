"""
push_watch.py - Zero-Cost Retention & Re-engagement Engine for Aiseki Navi.

Provides two 0-yen repeat-visitor channels without third-party SaaS monthly fees:
  1. Standards-compliant Web Push (RFC 8292 VAPID ES256 + RFC 8291 `aes128gcm` encryption)
     for background store condition-watch alerts (`female_55`, `zero_wait`, `evening_peak`).
  2. Official LINE Messaging API Webhook Auto-Responder (`Reply API` = 0 yen / unlimited,
     does not consume the free plan's 200 monthly Push quota).
"""

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.parse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import job_state
import local_db
import store_hours
import store_master

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))
VAPID_STATE_KEY = "webpush_vapid_keys_v1"
VAPID_DEFAULT_SUBJECT = "mailto:info@aiseki-navi.com"
PUSH_COOLDOWN_MINUTES = 180

# Strict allowlist of official browser Push Service hosts to prevent SSRF completely
ALLOWED_PUSH_EXACT_HOSTS = frozenset({
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
})
ALLOWED_PUSH_HOST_SUFFIXES = (
    ".push.services.mozilla.com",
    ".notify.windows.com",
    ".push.apple.com",
    ".fcm.googleapis.com",
)

CONDITION_LABELS_JA = {
    "female_55": "女性比55%以上",
    "zero_wait": "女性優位・待ち緩和",
    "evening_peak": "女性14名以上",
}

_vapid_lock = threading.Lock()
_cached_vapid_keys = None  # {"public_key": str, "private_pem": str}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    s = (raw or "").strip()
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode(s + pad)


def validate_push_endpoint(endpoint: str) -> tuple[bool, str]:
    """Validate that `endpoint` is an HTTPS URL on a trusted browser Push Service host (SSRF defense)."""
    u = (endpoint or "").strip()
    if not u or len(u) > 2048:
        return False, "invalid_length"
    try:
        parsed = urllib.parse.urlparse(u)
    except Exception:
        return False, "parse_error"
    if parsed.scheme != "https":
        return False, "https_required"
    if parsed.username or parsed.password:
        return False, "userinfo_forbidden"
    try:
        port = parsed.port
    except ValueError:
        return False, "port_forbidden"
    if port not in (None, 443):
        return False, "port_forbidden"
    host = (parsed.hostname or "").lower().strip(".")
    if not host or not parsed.path or parsed.path == "/":
        return False, "invalid_host_or_path"
    if host in ALLOWED_PUSH_EXACT_HOSTS or any(host.endswith(sfx) for sfx in ALLOWED_PUSH_HOST_SUFFIXES):
        return True, host
    return False, "untrusted_push_host"


def validate_client_keys(p256dh: str, auth: str) -> tuple[bool, str]:
    """Verify client ECDH P-256 public key (65-byte uncompressed) and 16-byte auth secret."""
    try:
        pub_bytes = _b64url_decode(p256dh)
        auth_bytes = _b64url_decode(auth)
    except Exception:
        return False, "invalid_base64"
    if len(pub_bytes) != 65 or pub_bytes[0] != 0x04:
        return False, "invalid_p256dh_length"
    if len(auth_bytes) != 16:
        return False, "invalid_auth_length"
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_bytes)
    except Exception:
        return False, "invalid_curve_point"
    return True, "ok"


def _generate_vapid_keypair() -> dict:
    priv_key = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    pub_bytes = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "public_key": _b64url_encode(pub_bytes),
        "private_pem": priv_pem,
    }


def get_or_create_vapid_keys(allow_blocking: bool = True) -> dict:
    """Return active VAPID keypair (env -> persistent `job_state` -> auto-generated fallback)."""
    global _cached_vapid_keys
    with _vapid_lock:
        if _cached_vapid_keys is not None:
            return dict(_cached_vapid_keys)

    env_pub = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
    env_priv = (os.environ.get("VAPID_PRIVATE_KEY_PEM") or "").strip()
    if env_pub and env_priv:
        pair = {"public_key": env_pub, "private_pem": env_priv.replace("\\n", "\n")}
        with _vapid_lock:
            _cached_vapid_keys = pair
        return dict(pair)

    is_test_isolated = os.environ.get("DISABLE_SHEETS") == "1"
    if not is_test_isolated:
        raw_saved = job_state.get_value(VAPID_STATE_KEY, allow_blocking=allow_blocking)
        if raw_saved:
            try:
                parsed = json.loads(raw_saved)
                if isinstance(parsed, dict) and parsed.get("public_key") and parsed.get("private_pem"):
                    with _vapid_lock:
                        _cached_vapid_keys = {
                            "public_key": str(parsed["public_key"]),
                            "private_pem": str(parsed["private_pem"]),
                        }
                    return dict(_cached_vapid_keys)
            except Exception:
                pass

    generated = _generate_vapid_keypair()
    if not is_test_isolated:
        if not allow_blocking:
            return dict(generated)
        logger.error("[push_watch] VAPID keys auto-generated. Set VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY_PEM as Fly secrets.")
        try:
            job_state.set_value(VAPID_STATE_KEY, json.dumps(generated, ensure_ascii=False))
            raw_after = job_state.get_value(VAPID_STATE_KEY, allow_blocking=True)
            parsed_after = json.loads(raw_after) if raw_after else None
            if isinstance(parsed_after, dict) and parsed_after.get("public_key") and parsed_after.get("private_pem"):
                generated = {
                    "public_key": str(parsed_after["public_key"]),
                    "private_pem": str(parsed_after["private_pem"]),
                }
        except Exception as err:
            logger.warning(f"[push_watch] Failed to persist generated VAPID keys: {err}")
            return dict(generated)

    with _vapid_lock:
        _cached_vapid_keys = generated
    return dict(generated)


def get_vapid_public_key(allow_blocking: bool = True) -> str:
    return get_or_create_vapid_keys(allow_blocking=allow_blocking)["public_key"]


def build_vapid_authorization_header(
    endpoint: str,
    private_pem: str,
    public_key_b64: str,
    subject: str = None,
    exp_seconds: int = 43200,
) -> str:
    """Build RFC 8292 `vapid t=<jwt>, k=<public_key>` Authorization header."""
    parsed = urllib.parse.urlparse(endpoint)
    aud = f"{parsed.scheme}://{(parsed.hostname or '').lower()}"
    sub = (subject or os.environ.get("VAPID_SUBJECT") or VAPID_DEFAULT_SUBJECT).strip()
    now_ts = int(time.time())
    header_b64 = _b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode("utf-8"))
    claims_b64 = _b64url_encode(
        json.dumps(
            {"aud": aud, "exp": now_ts + max(300, min(86400, int(exp_seconds))), "sub": sub},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    priv_key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    der_sig = priv_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r_int, s_int = decode_dss_signature(der_sig)
    raw_sig = r_int.to_bytes(32, "big") + s_int.to_bytes(32, "big")
    jwt_token = f"{header_b64}.{claims_b64}.{_b64url_encode(raw_sig)}"
    return f"vapid t={jwt_token}, k={public_key_b64}"


def encrypt_rfc8291_aes128gcm(
    plaintext: bytes,
    client_p256dh_b64: str,
    client_auth_b64: str,
    salt: bytes = None,
) -> bytes:
    """Encrypt Web Push payload using RFC 8291 (`aes128gcm` content-encoding)."""
    ua_pub_bytes = _b64url_decode(client_p256dh_b64)
    auth_secret = _b64url_decode(client_auth_b64)
    ua_public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_bytes)

    as_private_key = ec.generate_private_key(ec.SECP256R1())
    as_pub_bytes = as_private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    ecdh_secret = as_private_key.exchange(ec.ECDH(), ua_public_key)
    salt16 = salt if (salt is not None and len(salt) == 16) else os.urandom(16)

    key_info = b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=key_info).derive(ecdh_secret)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt16, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt16, info=b"Content-Encoding: nonce\x00").derive(ikm)

    padded = bytes(plaintext) + b"\x02"
    ciphertext = AESGCM(cek).encrypt(nonce, padded, None)
    rs_bytes = (4096).to_bytes(4, "big")
    idlen_byte = len(as_pub_bytes).to_bytes(1, "big")
    return salt16 + rs_bytes + idlen_byte + as_pub_bytes + ciphertext


def send_web_push_message(subscription: dict, payload_obj: dict, timeout_sec: float = 6.0) -> tuple[bool, int]:
    """Send an encrypted Web Push notification to a single validated subscription."""
    endpoint = str(subscription.get("endpoint") or "").strip()
    ok_ep, _reason = validate_push_endpoint(endpoint)
    if not ok_ep:
        return False, 400
    p256dh = str(subscription.get("p256dh") or "").strip()
    auth = str(subscription.get("auth") or "").strip()
    ok_keys, _k_reason = validate_client_keys(p256dh, auth)
    if not ok_keys:
        return False, 400

    vapid = get_or_create_vapid_keys(allow_blocking=True)
    plaintext = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encrypted_body = encrypt_rfc8291_aes128gcm(plaintext, p256dh, auth)
    auth_header = build_vapid_authorization_header(
        endpoint=endpoint,
        private_pem=vapid["private_pem"],
        public_key_b64=vapid["public_key"],
    )
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/octet-stream",
        "Content-Encoding": "aes128gcm",
        "TTL": "1800",
        "Urgency": "normal",
    }
    try:
        resp = requests.post(
            endpoint,
            data=encrypted_body,
            headers=headers,
            timeout=timeout_sec,
            allow_redirects=False,
        )
        code = int(resp.status_code)
        return (200 <= code < 300), code
    except Exception as err:
        logger.warning(f"[push_watch] Web Push send failed: {err}")
        return False, 0


def does_store_match_watch_condition(store_obj: dict, target_cond: str) -> bool:
    """Evaluate whether a live store snapshot currently satisfies `target_cond`."""
    if not isinstance(store_obj, dict):
        return False
    sname = str(store_obj.get("name") or "")
    w = int(store_obj.get("women") or 0)
    m = int(store_obj.get("men") or 0)
    tot = w + m
    if tot <= 0:
        return False
    is_open = store_obj.get("is_open_now")
    if is_open is None and sname:
        is_open = store_hours.get_business_hours_status(sname, total_guests=tot).get("is_open_now", True)
    if not is_open:
        return False

    if target_cond == "female_55":
        return w >= 6 and tot >= 10 and (w * 100 >= tot * 55)
    if target_cond == "zero_wait":
        we = store_obj.get("wait_estimate")
        if not isinstance(we, dict):
            try:
                import moat_intelligence
                we = moat_intelligence.estimate_store_wait_and_pos(
                    store_name=sname, women=w, men=m, is_open_now=bool(is_open)
                )
            except Exception:
                return False
        wait_groups = int((we.get("est_men_wait_groups") if isinstance(we, dict) else 0) or 0)
        return w >= 6 and w > m and wait_groups == 0
    if target_cond == "evening_peak":
        return w >= 14
    return False


def dispatch_matching_watch_pushes(
    stores_list: list[dict],
    cooldown_minutes: int = PUSH_COOLDOWN_MINUTES,
    now_jst: datetime.datetime = None,
) -> dict:
    """Evaluate active Web Push subscriptions against current live store data and send notifications."""
    global _cached_vapid_keys
    if not stores_list:
        return {"checked": 0, "sent": 0, "purged": 0}

    now_dt = now_jst or datetime.datetime.now(JST).replace(tzinfo=None)
    if getattr(now_dt, "tzinfo", None) is not None:
        now_dt = now_dt.astimezone(JST).replace(tzinfo=None)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    cooldown_cutoff = (now_dt - datetime.timedelta(minutes=max(15, int(cooldown_minutes)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    subs = local_db.get_active_web_push_subscriptions(
        limit=300, cooldown_cutoff_str=cooldown_cutoff, now_str=now_str
    )
    if not subs:
        return {"checked": 0, "sent": 0, "purged": 0}

    store_map = {}
    for s in stores_list:
        raw_n = str(s.get("name") or "")
        if raw_n:
            canon = local_db.resolve_canonical_store_name(raw_n)
            store_map[canon] = s
            store_map[raw_n] = s

    notified_hashes = []
    dead_endpoints = []
    sent_count = 0
    auth_fail = 0

    for sub in subs:
        last_ts = str(sub.get("last_notified_at") or "")
        if last_ts and last_ts >= cooldown_cutoff:
            continue
        sname = sub["store_name"]
        st = store_map.get(sname)
        if not st:
            continue
        cond = sub["target_cond"]
        if not does_store_match_watch_condition(st, cond):
            continue

        w = int(st.get("women") or 0)
        m = int(st.get("men") or 0)
        tot = max(1, w + m)
        wpct = round((w / tot) * 100)
        seo_n = store_master.seo_name(sname)
        slug = store_master.slug_of(sname)
        cond_ja = CONDITION_LABELS_JA.get(cond, "希望条件")
        target_url = f"/store/{slug}?s=push_watch" if slug else "/?s=push_watch"

        payload = {
            "title": f"{seo_n}｜{cond_ja}に到達",
            "body": f"現在 女性{w}名 : 男性{m}名（女性比{wpct}%）。リアルタイム推移と近隣比較を確認できます。",
            "url": target_url,
            "tag": f"aiseki-watch-{slug or 'top'}",
        }
        ok, status_code = send_web_push_message(sub, payload)
        if ok:
            sent_count += 1
            notified_hashes.append(sub["endpoint_hash"])
        elif status_code in (404, 410):
            dead_endpoints.append(sub["endpoint"])
        elif status_code in (401, 403):
            auth_fail += 1
            if auth_fail >= 3:
                logger.error("[push_watch] Repeated 401/403: VAPID key mismatch suspected; refreshing cache and aborting run")
                with _vapid_lock:
                    _cached_vapid_keys = None
                break

    if notified_hashes:
        local_db.mark_web_push_notified(notified_hashes, notified_at_str=now_str)
    purged_count = local_db.purge_dead_web_push_endpoints(dead_endpoints) if dead_endpoints else 0
    return {"checked": len(subs), "sent": sent_count, "purged": purged_count}


# =====================================================================
# Official LINE Messaging API Zero-Cost Auto-Responder (Reply API = 0 yen)
# =====================================================================
_LINE_AREA_KEYWORDS = (
    ("新宿", ("新宿", "SHINJUKU", "NISHISHINJUKU")),
    ("西新宿", ("NISHISHINJUKU", "西新宿")),
    ("渋谷", ("渋谷", "Shibuya")),
    ("恵比寿", ("恵比寿", "Ebisu")),
    ("上野", ("上野", "Ueno")),
    ("錦糸町", ("錦糸町", "Kinshicho")),
    ("町田", ("町田", "Machida")),
    ("横浜", ("横浜", "Yokohama")),
    ("大宮", ("大宮", "Omiya", "OMIYA")),
    ("柏", ("柏", "Kashiwa")),
    ("宇都宮", ("宇都宮", "Utsunomiya")),
    ("高崎", ("高崎", "Takasaki")),
    ("梅田", ("梅田", "大阪駅前", "茶屋町", "UMEDA", "CHAYAMACHI")),
    ("茶屋町", ("茶屋町", "CHAYAMACHI")),
    ("天満", ("天満", "Tenma")),
    ("難波", ("難波", "ミナミ", "NAMBA")),
    ("心斎橋", ("心斎橋", "Shinsaibashi")),
    ("大阪", ("大阪", "梅田", "茶屋町", "天満", "難波", "心斎橋", "UMEDA", "NAMBA", "CHAYAMACHI")),
    ("京都", ("京都", "Kyoto")),
    ("神戸", ("神戸", "三宮", "Kobe")),
    ("名古屋", ("名古屋", "栄", "錦", "Nagoya")),
    ("栄", ("栄", "住吉")),
    ("静岡", ("静岡", "浜松")),
    ("金沢", ("金沢", "Kanazawa")),
    ("福岡", ("福岡", "天神", "FUKUOKA", "Fukuoka")),
    ("天神", ("福岡", "天神", "FUKUOKA")),
    ("小倉", ("小倉", "Kokura")),
    ("熊本", ("熊本", "KUMAMOTO")),
    ("長崎", ("長崎", "Nagasaki")),
    ("大分", ("大分", "Oita")),
    ("宮崎", ("宮崎", "Miyazaki")),
    ("鹿児島", ("鹿児島", "Kagoshima")),
    ("沖縄", ("沖縄", "那覇", "Okinawa")),
    ("札幌", ("札幌", "すすきの", "SAPPORO", "Sapporo")),
    ("仙台", ("仙台", "国分町", "Sendai")),
    ("広島", ("広島", "HIROSHIMA", "Hiroshima")),
    ("岡山", ("岡山", "OKAYAMA")),
    ("松山", ("松山", "MATSUYAMA")),
)


_LINE_KW_EXCLUDE = {
    "錦": ("錦糸町",),
    "栄": ("栄町",),
}


def _kw_hit(kw: str, hay: str) -> bool:
    return (kw in hay) and not any(ex in hay for ex in _LINE_KW_EXCLUDE.get(kw, ()))


def is_line_bot_configured() -> bool:
    return bool(
        (os.environ.get("LINE_CHANNEL_SECRET") or "").strip()
        and (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
    )


def verify_line_signature(raw_body: bytes, signature_header: str, channel_secret: str = None) -> bool:
    """Verify LINE `X-Line-Signature` header using constant-time HMAC-SHA256 comparison."""
    secret = (channel_secret if channel_secret is not None else (os.environ.get("LINE_CHANNEL_SECRET") or "")).strip()
    sig = (signature_header or "").strip()
    if not secret or not sig or raw_body is None:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest)
    return hmac.compare_digest(expected, sig.encode("utf-8", "ignore"))


def _format_formal_first_mention(seo_name: str, seen_brands: set) -> str:
    """Ensure first mention of ag / JIS / ASOBIBAR includes formal katakana reading per AGENTS.md §3."""
    name = str(seo_name or "")
    if name.startswith("ag ") and "ag" not in seen_brands:
        seen_brands.add("ag")
        return "ag（アグ）" + name[3:]
    if name.startswith("JIS ") and "JIS" not in seen_brands:
        seen_brands.add("JIS")
        return "JIS（ジス）" + name[4:]
    if name.startswith("ASOBIBAR ") and "ASOBIBAR" not in seen_brands:
        seen_brands.add("ASOBIBAR")
        return "ASOBIBAR（アソビバー）" + name[9:]
    return name


def build_line_auto_reply_text(user_text: str, live_stores: list[dict], last_updated: str = "") -> str:
    """Compose a concise, high-utility live crowd summary for LINE Reply API (0-yen unlimited)."""
    q = (user_text or "").strip()
    domestic = [s for s in (live_stores or []) if s.get("region") != "Korea"]
    upd_note = f"（{last_updated} 更新）" if last_updated else ""
    seen_brands = set()

    # 1. Women's zero-wait mode
    if any(k in q for k in ("女子", "女性", "待ちゼロ", "無料")):
        cand = [
            s for s in domestic
            if int(s.get("men") or 0) >= 5 and int(s.get("men") or 0) >= int(s.get("women") or 0)
        ]
        cand.sort(key=lambda s: (int(s.get("men") or 0) - int(s.get("women") or 0), int(s.get("men") or 0)), reverse=True)
        lines = [f"【女性向け・待ち時間ゼロ圏の上位店】{upd_note}"]
        if cand:
            for idx, s in enumerate(cand[:4], 1):
                sname = str(s.get("name") or "")
                disp = _format_formal_first_mention(store_master.seo_name(sname), seen_brands)
                w = int(s.get("women") or 0)
                m = int(s.get("men") or 0)
                lines.append(f"{idx}. {disp}｜女性{w}名 : 男性{m}名")
        else:
            lines.append("現在はオープン前または集計待機中です。夕方以降にリアルタイム反映されます。")
        lines.append("\n女性向け待ちゼロ一覧＆ガイド：\nhttps://aiseki-navi.com/for-women?s=line_bot")
        return "\n".join(lines)

    # 2. Area-specific lookup
    for area_label, kw_tuple in _LINE_AREA_KEYWORDS:
        if area_label in q:
            matched = []
            for s in domestic:
                sname = str(s.get("name") or "")
                disp_raw = store_master.seo_name(sname)
                if any(_kw_hit(kw, sname) or _kw_hit(kw, disp_raw) for kw in kw_tuple):
                    matched.append(s)
            matched.sort(
                key=lambda s: (
                    (int(s.get("women") or 0) + int(s.get("men") or 0)) > 0,
                    int(s.get("women") or 0),
                    -int(s.get("men") or 0),
                ),
                reverse=True,
            )
            if matched:
                lines = [f"【{area_label}周辺の現在人数】{upd_note}"]
                for s in matched[:5]:
                    sname = str(s.get("name") or "")
                    disp = _format_formal_first_mention(store_master.seo_name(sname), seen_brands)
                    slug = store_master.slug_of(sname)
                    w = int(s.get("women") or 0)
                    m = int(s.get("men") or 0)
                    tot = w + m
                    if tot > 0:
                        wpct = round((w / tot) * 100)
                        lines.append(f"・{disp}：女性{w}名 / 男性{m}名（女性比{wpct}%）")
                    else:
                        lines.append(f"・{disp}：0 : 0（営業時間外または開店直後）")
                    if slug and len(matched) <= 3:
                        lines.append(f"  https://aiseki-navi.com/store/{slug}?s=line_bot")
                top_slug = store_master.slug_of(str(matched[0].get("name") or ""))
                if len(matched) > 3 and top_slug:
                    lines.append(f"\n詳細・ハシゴ比較：\nhttps://aiseki-navi.com/store/{top_slug}?s=line_bot")
                return "\n".join(lines)

    # 3. Default: National sweet-spot TOP 4 + usage guide
    sweet = [
        s for s in domestic
        if int(s.get("women") or 0) >= 6 and (int(s.get("women") or 0) + int(s.get("men") or 0)) >= 10
    ]
    sweet.sort(
        key=lambda s: (
            int(s.get("women") or 0) / max(1, int(s.get("women") or 0) + int(s.get("men") or 0)),
            int(s.get("women") or 0),
        ),
        reverse=True,
    )
    lines = [f"【いま女性比率が高い全国注目店】{upd_note}"]
    if sweet:
        for idx, s in enumerate(sweet[:4], 1):
            sname = str(s.get("name") or "")
            disp = _format_formal_first_mention(store_master.seo_name(sname), seen_brands)
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
            wpct = round((w / max(1, w + m)) * 100)
            lines.append(f"{idx}. {disp}｜女{w}:男{m}（{wpct}%）")
    else:
        lines.append("現在コアタイム前です。「新宿」「渋谷」「梅田」「福岡」など街の名前を送ると各店の状況を確認できます。")
    lines.append(
        "\n街の名前（例：渋谷、新宿、梅田、福岡）や「女子」と送ると即座に人数を返信します。\n"
        "3秒穴場診断：https://aiseki-navi.com/concierge?s=line_bot"
    )
    return "\n".join(lines)


def send_line_reply(reply_token: str, text: str, access_token: str = None, timeout_sec: float = 3.0) -> bool:
    """Call LINE Messaging API `message/reply` (free unlimited Reply API)."""
    token = (access_token if access_token is not None else (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or "")).strip()
    rt = (reply_token or "").strip()
    if not token or not rt or rt == "00000000000000000000000000000000":
        return False
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": rt,
                "messages": [{"type": "text", "text": str(text or "")[:4900]}],
            },
            timeout=timeout_sec,
        )
        return 200 <= resp.status_code < 300
    except Exception as err:
        logger.warning(f"[push_watch] LINE reply failed: {err}")
        return False
