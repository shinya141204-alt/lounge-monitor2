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


def compute_twitter_weight(text: str) -> int:
    """Calculate Twitter-weighted character count (ASCII=1, East-Asian/Emoji=2, URL=23)."""
    import re
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, text)
    clean_text = re.sub(url_pattern, '', text)
    weight = len(urls) * 23
    for ch in clean_text:
        weight += 1 if ord(ch) <= 127 else 2
    return weight


def build_scheduled_tweet_text(stores: list, slot_label: str = "20:00", app_url: str = "", compact: bool = True) -> str:
    """
    Build high-engagement X post from live store data.
    When compact=True (default for automated X API v2 posting), ensures Twitter weight <= 270.
    When compact=False, generates rich long-form format suitable for manual X Premium posting.
    - '20:00': Early evening start dash TOP3
    - '22:00': Peak time high-female-ratio TOP3
    - '00:00': Midnight late-night fever TOP3
    - '02:00': All-night morning fever TOP3
    """
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    date_label = f"{now_jst.month}/{now_jst.day}({wd_str}) {slot_label}"
    time_tag = f"⏱️{now_jst.strftime('%H:%M')}"

    if not stores:
        return ""

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

    # --- Compact Mode for Automated X API Posting (Weight <= 280) ---
    if compact:
        if slot_label in ("22:00", "22:30"):
            candidates = [s for s in stores if s.get("women", 0) >= 10]
            candidates.sort(
                key=lambda x: (
                    x.get("women", 0) / max(1, x.get("women", 0) + x.get("men", 0)),
                    x.get("women", 0),
                ),
                reverse=True,
            )
            top_stores = candidates[:3] if len(candidates) >= 3 else stores[:3]
            header = f"{date_label} いま女性比率が高めで入りやすい3店舗✨"
        elif slot_label == "00:00":
            top_stores = stores[:3]
            header = f"{date_label} 終電後も女性が多く残っている3店舗🌙"
        elif slot_label == "02:00":
            top_stores = stores[:3]
            header = f"{date_label} 深夜2時でも賑わっている3店舗🦉"
        else:
            top_stores = stores[:3]
            header = f"{date_label} 早い時間から女性が集まってる3店舗🍻"

        lines = [header, ""]
        for i, s in enumerate(top_stores):
            w = s.get("women", 0)
            m = s.get("men", 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}：女{w} / 男{m} ({pct}%)")

        lines.extend([
            "",
            "全店の現在人数・男女比👇",
            f"{site_url}/",
            f"{time_tag} #相席ナビ #相席ラウンジ"
        ])
        return "\n".join(lines)

    # --- Rich Long-Form Mode for Manual X Premium Posting ---
    if slot_label == "20:00":
        lines = [
            f"{date_label}時点、早い時間から女性が集まっている相席ラウンジTOP5🍻",
            "",
            "いま女性人数が多い上位店舗はこちら👇"
        ]
        for i, s in enumerate(stores[:5]):
            w = s.get("women", 0)
            m = s.get("men", 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}（女性{w}名 / 男性{m}名・女性比{pct}%）")

        lines.extend([
            "",
            "💡 今夜のひとこと",
            "20時台は仕事帰りの女性が早めに入ってきていて、男性の入店待ちもほぼない時間帯です。21時半を過ぎると一気に混み始めるので、落ち着いて話したい人は早めに入るのがおすすめです。",
            "",
            "📖 オリエンタルラウンジ・ag・JISの違いまとめ：",
            f"{site_url}/guide/brand-comparison",
            "",
            "👇全店舗のリアルタイム人数・男女比",
            f"{site_url}/",
            "",
            f"{time_tag} #相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS #ag"
        ])
        return "\n".join(lines)

    elif slot_label in ("22:00", "22:30"):
        candidates = [s for s in stores if s.get("women", 0) >= 10]
        candidates.sort(
            key=lambda x: (
                x.get("women", 0) / max(1, x.get("women", 0) + x.get("men", 0)),
                x.get("women", 0),
            ),
            reverse=True,
        )
        top_anaba = candidates[:5] if len(candidates) >= 5 else stores[:5]
        lines = [
            f"{date_label}時点、いま女性比率が高めで入りやすい穴場ラウンジTOP5✨",
            "",
            "女性の割合が高い狙い目店舗をピックアップしました👇"
        ]
        for i, s in enumerate(top_anaba[:5]):
            w = s.get("women", 0)
            m = s.get("men", 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}（女性比{pct}% ｜ 女性{w}名 / 男性{m}名）")

        lines.extend([
            "",
            "💡 ピーク時間の動き方",
            "一番人が増える時間帯なので、1店舗目で男性待ちが長そうなときは無理に並ばず、近くの女性比率が高い店舗へ回る方がスムーズに座れます。",
            "",
            "📖 水曜・木曜の20時〜22時が穴場な理由：",
            f"{site_url}/guide/weekday-strategy",
            "",
            "👇全店舗のリアルタイム人数・男女比",
            f"{site_url}/",
            "",
            f"{time_tag} #相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS"
        ])
        return "\n".join(lines)

    elif slot_label == "00:00":
        lines = [
            f"{date_label}時点、終電後も女性が多く残っている店舗TOP5🌙",
            "",
            "0時を過ぎても賑わっている店舗はこちら👇"
        ]
        for i, s in enumerate(stores[:5]):
            w = s.get("women", 0)
            m = s.get("men", 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}（女性{w}名 / 男性{m}名・女性比{pct}%）")

        lines.extend([
            "",
            "💡 深夜帯のポイント",
            "終電後に残っている方は「もうちょっと飲みたい」という空気感の人が多い時間帯。入店待ちも落ち着くので、2軒目を探している人にもおすすめです。",
            "",
            "📖 相席ラウンジの基本ガイド：",
            f"{site_url}/guide",
            "",
            "👇深夜営業中ラウンジの現在人数",
            f"{site_url}/",
            "",
            f"{time_tag} #相席ナビ #深夜営業 #相席ラウンジ"
        ])
        return "\n".join(lines)

    elif slot_label == "02:00":
        lines = [
            f"{date_label}時点、深夜2時でも盛り上がっている店舗TOP5🦉",
            "",
            "朝まで営業している店舗の現在人数です👇"
        ]
        for i, s in enumerate(stores[:5]):
            w = s.get("women", 0)
            m = s.get("men", 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}（女性{w}名 / 男性{m}名・女性比{pct}%）")

        lines.extend([
            "",
            "💡 深夜2時以降の空気感",
            "この時間は人が集まっている店舗と静かな店舗がはっきり分かれます。行く前に人数を見てから向かうのが確実です。",
            "",
            "👇朝まで営業店舗のリアルタイム人数",
            f"{site_url}/",
            "",
            f"{time_tag} #相席ナビ #朝まで営業 #相席ラウンジ"
        ])
        return "\n".join(lines)

    else:
        lines = [
            f"{date_label}時点、いま人が集まっている相席ラウンジTOP5🍻",
            "",
            "最新の女性人数ランキングはこちら👇"
        ]
        for i, s in enumerate(stores[:5]):
            w = s.get("women", 0)
            m = s.get("men", 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            lines.append(f"{medals[i]} {ja_name}（女性{w}名 / 男性{m}名・女性比{pct}%）")

        lines.extend([
            "",
            "👇全店舗のリアルタイム人数・男女比グラフ",
            f"{site_url}/",
            "",
            f"{time_tag} #相席ナビ #相席ラウンジ"
        ])
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

    # Guard against X API 280-weighted-character limit (prevents HTTP 400 Text Too Long errors)
    weight = compute_twitter_weight(text)
    if weight > 280:
        logger.warning(f"Tweet text exceeds 280 weight limit ({weight}). Truncating safely for X API...")
        while compute_twitter_weight(text + "...") > 280 and len(text) > 20:
            text = text[:-1]
        text += "..."

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
