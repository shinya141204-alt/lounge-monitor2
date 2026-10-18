import os
import time
import hmac
import hashlib
import base64
import secrets
import urllib.parse
import datetime
import logging
import requests

logger = logging.getLogger(__name__)

# Japanese localization map for English store names so X posts look natural and searchable
EN_TO_JA_MAP = {
    'NISHISHINJUKU': '西新宿',
    'SHINJUKU': '新宿',
    'SHIBUYA': '渋谷',
    'EBISU': '恵比寿',
    'UENO': '上野',
    'MACHIDA': '町田',
    'KASHIWA': '柏',
    'OMIYA': '大宮',
    'YOKOHAMA': '横浜',
    'UTSUNOMIYA': '宇都宮',
    'TAKASAKI': '高崎',
    'SAPPORO': '札幌',
    'SENDAI': '仙台',
    'NAGOYA': '名古屋',
    'SHIZUOKA': '静岡',
    'HAMAMATSU': '浜松',
    'KANAZAWA': '金沢',
    'CHAYAMACHI': '梅田茶屋町',
    'UMEDA': '梅田',
    'NAMBA': '難波',
    'SHINSAIBASHI': '心斎橋',
    'TENMA': '天満',
    'KYOTO': '京都',
    'KOBE': '神戸',
    'OKAYAMA': '岡山',
    'HIROSHIMA': '広島',
    'MATSUYAMA': '松山',
    'FUKUOKA': '福岡天神',
    'KOKURA': '小倉',
    'NAGASAKI': '長崎',
    'OITA': '大分',
    'KUMAMOTO': '熊本',
    'MIYAZAKI': '宮崎',
    'KAGOSHIMA': '鹿児島',
    'OKINAWA': '沖縄',
}


def format_ja_store_name(raw_name: str) -> str:
    """Convert raw store name like 'JIS OMIYA' to 'JIS 大宮'."""
    name = (raw_name or '').strip()
    parts = name.split(' ', 1)
    if len(parts) == 2:
        prefix, rest = parts[0], parts[1].strip()
        upper_rest = rest.upper()
        if upper_rest in EN_TO_JA_MAP:
            return f"{prefix} {EN_TO_JA_MAP[upper_rest]}"
        for en_key, ja_val in EN_TO_JA_MAP.items():
            if upper_rest == en_key:
                rest = ja_val
        return f"{prefix} {rest}"
    return name


def build_scheduled_tweet_text(stores: list, slot_label: str = "21:00", app_url: str = "") -> str:
    """
    Build a high-engagement X post tailored to the time slot:
    - '21:00': Early evening national TOP5 + Kanto/Kansai highlights
    - '22:30': Peak time high-female-ratio 'Anaba' TOP3 + National TOP3
    - '00:00': Midnight late-night fever TOP5
    """
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://lounge-monitor2.onrender.com").rstrip("/")
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    date_label = f"{now_jst.month}/{now_jst.day}({wd_str}) {slot_label}"

    if not stores:
        return ""

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

    if slot_label == "22:30":
        # Peak time: Highlight high female-ratio spots (10+ women) + Top 2 overall
        candidates = [s for s in stores if s.get("women", 0) >= 12]
        candidates.sort(
            key=lambda x: (
                x.get("women", 0) / max(1, x.get("women", 0) + x.get("men", 0)),
                x.get("women", 0),
            ),
            reverse=True,
        )
        top_anaba = candidates[:3] if len(candidates) >= 3 else stores[:3]
        lines = [f"💎【{date_label} 穴場＆ピーク速報】", "いま女性比率が高くて狙い目のラウンジTOP3✨", ""]
        for i, s in enumerate(top_anaba):
            w = s.get("women", 0)
            m = s.get("men", 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}（女性{pct}%｜👩{w}名/👨{m}名）")

        if stores:
            top1 = stores[0]
            lines.append(f"\n🔥全国人数1位：{format_ja_store_name(top1.get('name', ''))}（👩{top1.get('women', 0)}名）")

        lines.append(f"\n👇全65店舗のリアルタイム人数＆グラフ\n{site_url}/\n#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS")
        return "\n".join(lines)

    elif slot_label == "00:00":
        lines = [f"🌙【{date_label} 深夜速報】", "終電後も盛り上がってる相席ラウンジTOP5🍾", ""]
        for i, s in enumerate(stores[:5]):
            w = s.get("women", 0)
            m = s.get("men", 0)
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}（👩{w}名 / 👨{m}名）")
        lines.append(f"\n👇いま入れる店舗＆男女比をチェック\n{site_url}/\n#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag")
        return "\n".join(lines)

    else:
        # Default / 21:00 slot
        lines = [f"🔥【{date_label} 全国速報】", "いま女性が多い相席ラウンジランキングTOP5！", ""]
        for i, s in enumerate(stores[:5]):
            w = s.get("women", 0)
            m = s.get("men", 0)
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}（👩{w}名 / 👨{m}名）")
        lines.append(f"\n👇全店舗のリアルタイム男女比＆曜日別分析\n{site_url}/\n#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS")
        return "\n".join(lines)


def _percent_encode(val: str) -> str:
    return urllib.parse.quote(str(val), safe="")


def _build_oauth1_header(method: str, url: str, api_key: str, api_secret: str, access_token: str, access_secret: str) -> str:
    """Generate OAuth 1.0a Authorization header for X API v2 using standard library."""
    oauth_params = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    param_str = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}"
        for k, v in sorted(oauth_params.items())
    )
    base_str = "&".join([
        method.upper(),
        _percent_encode(url),
        _percent_encode(param_str),
    ])
    signing_key = f"{_percent_encode(api_secret)}&{_percent_encode(access_secret)}"
    hashed = hmac.new(signing_key.encode("utf-8"), base_str.encode("utf-8"), hashlib.sha1)
    signature = base64.b64encode(hashed.digest()).decode("utf-8")
    oauth_params["oauth_signature"] = signature

    header_parts = ", ".join(
        f'{_percent_encode(k)}="{_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )
    return f"OAuth {header_parts}"


def post_to_x(text: str) -> dict:
    """
    Post a tweet via X API v2 (`POST https://api.twitter.com/2/tweets`).
    Requires environment variables:
      - X_API_KEY
      - X_API_SECRET
      - X_ACCESS_TOKEN
      - X_ACCESS_TOKEN_SECRET
    """
    api_key = os.environ.get("X_API_KEY", "").strip()
    api_secret = os.environ.get("X_API_SECRET", "").strip()
    access_token = os.environ.get("X_ACCESS_TOKEN", "").strip()
    access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()

    if not all([api_key, api_secret, access_token, access_secret]):
        return {
            "status": "skipped",
            "reason": "Missing X API credentials in environment variables (X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)",
        }

    url = "https://api.twitter.com/2/tweets"
    auth_header = _build_oauth1_header("POST", url, api_key, api_secret, access_token, access_secret)
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json={"text": text}, timeout=15)
        if resp.status_code in (200, 201):
            data = resp.json()
            logger.info(f"Successfully posted to X: {data}")
            return {"status": "success", "data": data}
        else:
            logger.error(f"X API returned {resp.status_code}: {resp.text}")
            return {"status": "error", "code": resp.status_code, "detail": resp.text}
    except Exception as e:
        logger.error(f"Failed to post to X: {e}")
        return {"status": "error", "detail": str(e)}
