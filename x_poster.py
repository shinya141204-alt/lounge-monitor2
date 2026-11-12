import os
import time
import random
import hmac
import hashlib
import base64
import secrets
import urllib.parse
import datetime
import logging
import re
import requests

logger = logging.getLogger(__name__)

# X API v2 media upload endpoint (v1.1 `upload.twitter.com/1.1/media/upload.json` is deprecated)
X_MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"

# URL patterns counted as 23 weight by X's t.co shortener (scheme URLs and bare domains like aiseki-navi.com/...)
_TWITTER_URL_RE = re.compile(
    r"https?://\S+"
    r"|(?<![A-Za-z0-9_@./-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|jp|io|co|me|app|dev|info|biz|tv|ly|link|site|xyz)\b(?:/[^\s]*)?"
)

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


DIRECT_STORE_JA_MAP = {
    'YATAKOI UMEDA': 'ヤタコイ梅田',
    'CLOVERS HIROSHIMA': 'CLOVERS広島',
    'ALFA HIROSHIMA': 'ALFA広島',
    'XIX OKAYAMA': 'XIX岡山',
}


def format_ja_store_name(raw_name: str) -> str:
    """Convert raw store name like 'JIS OMIYA' -> 'JIS 大宮', 'OLG ag渋谷' -> 'ag渋谷', 'OLG 渋谷本店' -> 'オリエンタルラウンジ 渋谷本店', 'YATAKOI UMEDA' -> 'ヤタコイ梅田'."""
    import re
    name = re.sub(r'\s+', ' ', (raw_name or '').strip())
    if not name:
        return ''
    upper_full = name.upper()
    if upper_full in ('OLG', 'ORIENTAL'):
        return 'オリエンタルラウンジ'
    if upper_full == 'YATAKOI':
        return 'ヤタコイ'
    if upper_full in DIRECT_STORE_JA_MAP:
        return DIRECT_STORE_JA_MAP[upper_full]
    if upper_full.startswith('ASOBIBAR '):
        ab_rest = name[9:].strip().replace(' ', '')
        return f"アソビバー{ab_rest}"
    if upper_full.startswith('OLG AG') or upper_full.startswith('ORIENTAL AG'):
        ag_rest = re.sub(r'^(?:OLG|ORIENTAL)\s+AG\s*', '', name, flags=re.IGNORECASE).strip()
        ag_ja = EN_TO_JA_MAP.get(ag_rest.upper(), ag_rest)
        return f"ag{ag_ja}"
    if upper_full.startswith('OLG ') or upper_full.startswith('ORIENTAL '):
        olg_rest = re.sub(r'^(?:OLG|ORIENTAL)\s+', '', name, flags=re.IGNORECASE).strip()
        olg_ja = EN_TO_JA_MAP.get(olg_rest.upper(), olg_rest)
        return f"オリエンタルラウンジ {olg_ja}"

    parts = name.split(' ', 1)
    if len(parts) == 2:
        prefix, rest = parts[0], parts[1].strip()
        upper_prefix = prefix.upper()
        upper_rest = rest.upper()
        ja_rest = EN_TO_JA_MAP.get(upper_rest, rest)
        if upper_prefix in ('OLG', 'ORIENTAL'):
            return f"オリエンタルラウンジ {ja_rest}"
        if upper_prefix == 'YATAKOI':
            return f"ヤタコイ{ja_rest}"
        if upper_prefix in ('CLOVERS', 'ALFA', 'XIX') and upper_rest in EN_TO_JA_MAP:
            return f"{upper_prefix}{ja_rest}"
        if upper_rest in EN_TO_JA_MAP:
            return f"{prefix} {ja_rest}"
        return f"{prefix} {rest}"
    return name


def compute_twitter_weight(text: str) -> int:
    """Calculate Twitter-weighted character count (ASCII=1, East-Asian/Emoji=2, URL=23).

    Both scheme URLs (`https://...`) and bare domains (`aiseki-navi.com/stats`) are auto-linked by X
    and wrapped by t.co, so each counts as a flat 23 weight.
    """
    text = text or ""
    urls = _TWITTER_URL_RE.findall(text)
    clean_text = _TWITTER_URL_RE.sub('', text)
    weight = len(urls) * 23
    for ch in clean_text:
        weight += 1 if ord(ch) <= 127 else 2
    return weight


def _build_east_west_summary_line(stores: list) -> str:
    """
    Build a concise East (Kanto: Shinjuku/Shibuya/Omiya/etc.) vs West (Kansai: Umeda/Namba/Shinsaibashi/etc.)
    top-store comparison line so followers in both regions immediately see a familiar local store.
    """
    kanto_kws = ("新宿", "渋谷", "恵比寿", "銀座", "上野", "大宮", "横浜", "町田", "柏", "錦糸町", "宇都宮", "高崎", "SHINJUKU", "SHIBUYA", "EBISU", "UENO", "OMIYA", "YOKOHAMA")
    kansai_kws = ("梅田", "茶屋町", "難波", "心斎橋", "天満", "大阪", "京都", "神戸", "UMEDA", "CHAYAMACHI", "NAMBA", "SHINSAIBASHI", "KYOTO", "KOBE")

    kanto_stores = [
        s for s in stores
        if s.get("region") == "Kanto" or any(k in (s.get("name") or "").upper() or k in (s.get("name") or "") for k in kanto_kws)
    ]
    kansai_stores = [
        s for s in stores
        if s.get("region") == "Kinki" or any(k in (s.get("name") or "").upper() or k in (s.get("name") or "") for k in kansai_kws)
    ]
    kanto_stores.sort(key=lambda x: (int(x.get("women") or 0), int(x.get("men") or 0)), reverse=True)
    kansai_stores.sort(key=lambda x: (int(x.get("women") or 0), int(x.get("men") or 0)), reverse=True)

    if kanto_stores and kansai_stores:
        k_top = kanto_stores[0]
        w_top = kansai_stores[0]
        k_w = int(k_top.get("women") or 0)
        w_w = int(w_top.get("women") or 0)
        if k_w <= 0 and w_w <= 0:
            return ""
        k_name = format_ja_store_name(k_top.get("name", ""))
        w_name = format_ja_store_name(w_top.get("name", ""))
        return f"🗼関東1位 {k_name}(女性{k_w}名)｜🐙関西1位 {w_name}(女性{w_w}名)"
    return ""


def _build_live_insight_line(stores: list, slot_label: str = "20:00") -> str:
    """
    Generate a concise 1-line dynamic hook from live store counts:
      1) Highlight real-time gender-ratio gaps within the same city hub (store-hopping alpha)
      2) Highlight East vs West (Kanto #1 vs Kansai #1) live store comparison
      3) Highlight lively male-lead stores from a female / fast-match perspective
      4) Fallback to time-slot specific practical tip
    """
    hubs = [
        ("新宿", ("新宿", "SHINJUKU")),
        ("渋谷", ("渋谷", "恵比寿", "SHIBUYA", "EBISU")),
        ("梅田", ("梅田", "茶屋町", "UMEDA", "CHAYAMACHI")),
        ("難波・心斎橋", ("難波", "心斎橋", "NAMBA", "SHINSAIBASHI")),
        ("名古屋", ("名古屋", "NAGOYA", "栄", "名古屋 錦", "名古屋錦")),
        ("福岡", ("福岡", "天神", "FUKUOKA")),
        ("札幌", ("札幌", "SAPPORO")),
    ]
    best_hub = None
    best_gap = 0.0
    for hub_label, kws in hubs:
        hub_stores = [
            s for s in stores
            if any(k in (s.get("name") or "").upper() or k in (s.get("name") or "") for k in kws)
            and (int(s.get("women") or 0) + int(s.get("men") or 0)) >= 10
        ]
        if len(hub_stores) >= 2:
            has_male_heavy = any(int(s.get("men") or 0) >= int(s.get("women") or 0) + 4 for s in hub_stores)
            has_female_lead = any(int(s.get("women") or 0) > int(s.get("men") or 0) and int(s.get("women") or 0) >= 8 for s in hub_stores)
            if has_male_heavy and has_female_lead:
                ratios = [int(s.get("women") or 0) / max(1, int(s.get("women") or 0) + int(s.get("men") or 0)) for s in hub_stores]
                gap = max(ratios) - min(ratios)
                if gap > best_gap:
                    best_gap = gap
                    best_hub = hub_label

    ew_line = _build_east_west_summary_line(stores)
    if best_hub and best_gap >= 0.12 and slot_label in ("22:00", "22:30"):
        return f"💡いま{best_hub}は店舗間で男女比差大！比較が吉"
    if ew_line:
        return ew_line
    if best_hub and best_gap >= 0.12:
        return f"💡いま{best_hub}は店舗間で男女比差大！比較が吉"

    male_lead_candidates = [
        s for s in stores
        if int(s.get("men") or 0) >= 15 and int(s.get("men") or 0) >= int(s.get("women") or 0) + 4
    ]
    if male_lead_candidates:
        male_lead_candidates.sort(key=lambda x: int(x.get("men") or 0), reverse=True)
        top_m = male_lead_candidates[0]
        ja_name = format_ja_store_name(top_m.get("name", ""))
        return f"💡女性目線なら男性{int(top_m.get('men') or 0)}名の{ja_name}が即相席◎"

    if slot_label in ("22:00", "22:30"):
        return "💡混む時間は徒歩圏の別ブランド比較がおすすめ"
    if slot_label == "00:00":
        return "💡終電後は2軒目組が増えて待ち列も落ち着く時間"
    if slot_label == "02:00":
        return "💡深夜帯は人が集まる店に絞って向かうのが確実"
    return "💡21時前の今なら待ち時間なしで座りやすい時間帯"


def build_scheduled_tweet_text(stores: list, slot_label: str = "20:00", app_url: str = "", compact: bool = True) -> str:
    """
    Build high-engagement X post from live store data.
    When compact=True (default for automated X API v2 posting), ensures Twitter weight <= 274.
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

    # Exclude overseas (Korea: Gangnam / Hongdae) stores from X auto-posts
    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    stores = [
        s for s in stores
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]
    if not stores:
        return ""

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

    # --- Compact Mode for Automated X API Posting (Weight <= 280) ---
    if compact:
        if slot_label in ("22:00", "22:30"):
            candidates = [s for s in stores if int(s.get("women") or 0) >= 10]
            candidates.sort(
                key=lambda x: (
                    int(x.get("women") or 0) / max(1, int(x.get("women") or 0) + int(x.get("men") or 0)),
                    int(x.get("women") or 0),
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

        rank_lines = [header, ""]
        for i, s in enumerate(top_stores):
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            rank_lines.append(f"{medals[i]} {ja_name}：女性{w}・男性{m} ({pct}%)")

        footer_variants = [
            "全国60店舗超の現在人数はリプ欄へ👇",
            "詳しい店舗別データ・料金比較はリプ欄へ👇",
            "各エリアの現在の男女比・穴場はリプ欄へ👇",
        ]
        tag_variants = [
            f"{time_tag} #相席ナビ #相席ラウンジ",
            f"{time_tag} #相席ラウンジ #オリエンタルラウンジ",
            f"{time_tag} #相席ラウンジ #相席ナビ",
        ]
        v_idx = (now_jst.day + len(slot_label)) % 3
        footer_lines = [
            footer_variants[v_idx],
            tag_variants[v_idx],
        ]
        insight_line = _build_live_insight_line(stores, slot_label=slot_label)
        if insight_line:
            candidate = "\n".join(rank_lines + ["", insight_line] + footer_lines)
            if compute_twitter_weight(candidate) <= 274:
                return candidate

        base_text = "\n".join(rank_lines + [""] + footer_lines)
        if compute_twitter_weight(base_text) <= 274:
            return base_text

        for short_footer in (
            ["全店の現在人数・穴場はリプ欄へ👇", f"{time_tag} #相席ナビ #相席ラウンジ"],
            ["全店の現在人数はリプ欄へ👇", f"{time_tag} #相席ナビ"],
            [f"{time_tag} #相席ナビ"],
        ):
            compact_cand = "\n".join(rank_lines + [""] + short_footer)
            if compute_twitter_weight(compact_cand) <= 274:
                return compact_cand
        return "\n".join(rank_lines + [f"{time_tag} #相席ナビ"])

    # --- Rich Long-Form Mode for Manual X Premium Posting ---
    if slot_label == "20:00":
        lines = [
            f"{date_label}時点、早い時間から女性が集まっている相席ラウンジTOP5🍻",
            "",
            "いま女性人数が多い上位店舗はこちら👇"
        ]
        for i, s in enumerate(stores[:5]):
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
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
        candidates = [s for s in stores if int(s.get("women") or 0) >= 10]
        candidates.sort(
            key=lambda x: (
                int(x.get("women") or 0) / max(1, int(x.get("women") or 0) + int(x.get("men") or 0)),
                int(x.get("women") or 0),
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
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
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
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
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
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
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
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
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


def build_scheduled_reply_text(slot_label: str = "20:00", app_url: str = "") -> str:
    """Build the 2nd post (self-reply) containing the timestamped site URL + price/event comparison board link."""
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    slot_code = (slot_label or "20:00").replace(":", "")
    live_url = f"{site_url}/?s={now_jst.strftime('%Y%m%d')}_{slot_code}"
    return (
        f"🔍 全国60店舗超のリアルタイム人数・男女比ランキング\n{live_url}\n\n"
        f"💴 店舗別の時間帯料金・開催中イベント比較ボード\n{site_url}/price-comparison"
    )


def build_pinned_tweet_thread(app_url: str = "") -> tuple:
    """
    Build the 2-post Pinned Tweet thread:
      - 1st post: Clean, URL-free overview of Aiseki Navi (strictly <= 276 weight)
      - 2nd post: Self-reply with main URL & top column links
    """
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    main_text = (
        "🍸全国60店舗超の相席ラウンジ人数速報『相席ナビ』\n\n"
        "・オリエンタルラウンジ / ag / JIS / ASOBIBAR 等の現在人数＆男女比をリアルタイム集計\n"
        "・時間帯料金・ソロ入店可否・イベント比較\n"
        "・曜日×時間帯別の平均グラフで「いま狙い目の店舗」がわかる\n\n"
        "サイトURL・比較ボードはリプ欄へ👇\n"
        "#相席ナビ #相席ラウンジ"
    )
    reply_text = (
        f"🔍 全国60店舗超リアルタイム人数ランキング\n{site_url}/\n\n"
        f"💴 料金・開催中イベント比較ボード\n{site_url}/price-comparison\n\n"
        f"📖 人気の攻略コラム一覧\n{site_url}/columns"
    )
    return main_text, reply_text


COLUMN_PROMO_LIST = [
    {
        "slug": "shibuya-ebisu-comparison",
        "path": "/guide/shibuya-ebisu-comparison",
        "eyecatch": "/static/eyecatch-shibuya-ebisu-comparison.jpg",
        "title": "渋谷・恵比寿の相席ラウンジ・回遊バー5店舗比較｜山手線1駅で変わる年齢層・料金・時間波形",
        "main_text": (
            "渋谷で50分待ちだった夜、山手線で1駅3分の恵比寿へ移動したら待ち時間なしで案内された。\n\n"
            "オリエンタルラウンジ・JIS・ASOBIBARがひしめく渋谷と、落ち着いた空気が保たれる恵比寿。5店舗の時間波形と料金差はリプ欄へ。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "ueno-kinshicho-machida",
        "path": "/guide/ueno-kinshicho-machida",
        "eyecatch": "/static/eyecatch-ueno-kinshicho-machida.jpg",
        "title": "上野・錦糸町・町田・柏の相席ラウンジ5店舗比較｜仲町通りの隣接2店と沿線ターミナルの実測検証",
        "main_text": (
            "新宿や渋谷まで出なくても、上野・錦糸町・町田・柏のターミナル駅には地元と沿線帰りが集まるラウンジがある。\n\n"
            "上野・仲町通りで徒歩1分に並ぶオリエンタルラウンジとagの使い分けや、東東京・郊外5店舗の波形はリプ欄で。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "yokohama-omiya-kanto",
        "path": "/guide/yokohama-omiya-kanto",
        "eyecatch": "/static/eyecatch-yokohama-omiya-kanto.jpg",
        "title": "横浜・大宮・宇都宮・高崎の相席ラウンジ5店舗比較｜都心へ出ずに選ぶ時間波形・料金・ソロ入店",
        "main_text": (
            "金曜の夜、わざわざ都心まで出ずに横浜・大宮・宇都宮・高崎で飲むとき、終電前後の人の動きは都内とは少し違う。\n\n"
            "大宮のオリエンタルラウンジとJISの年齢層の違いや、関東4都市5店舗の時間帯ごとの傾向はリプ欄にまとめました。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "nagoya-sakae-nishiki",
        "path": "/guide/nagoya-sakae-nishiki",
        "eyecatch": "/static/eyecatch-nagoya-sakae-nishiki.jpg",
        "title": "名古屋（栄・錦・住吉）の相席ラウンジ・回遊バー5店舗比較｜南北700mの年齢層・料金・ソロ境界",
        "main_text": (
            "名古屋の栄・錦・住吉エリアは、南北700m圏内にオリエンタルラウンジ2店舗・ag・JIS・ASOBIBARの5店舗が集まっている。\n\n"
            "1人入店の可否や20時台と深夜0時以降の男女比の移り変わりを実測ログで比べました。リプ欄から読めます。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "fukuoka-tenjin-kyushu",
        "path": "/guide/fukuoka-tenjin-kyushu",
        "eyecatch": "/static/eyecatch-fukuoka-tenjin-kyushu.jpg",
        "title": "福岡・天神と九州・沖縄11店舗の相席ラウンジ比較｜大名徒歩2分の3ブランド使い分けと全店ソロ対応",
        "main_text": (
            "福岡・天神の大名エリアは、徒歩2分圏内にオリエンタルラウンジ・ag・JISの3ブランドが並び、終電後も2回転目が続く街。\n\n"
            "熊本・鹿児島・宮崎・長崎・大分・沖縄を含めた九州・沖縄11店舗の時間波形と料金の違いはリプ欄へ。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "hiroshima-okayama-matsuyama",
        "path": "/guide/hiroshima-okayama-matsuyama",
        "eyecatch": "/static/eyecatch-hiroshima-okayama-matsuyama.jpg",
        "title": "広島・岡山・松山の相席ラウンジ6店舗比較｜新天地半径185mの3店競合と料金構造の実測検証",
        "main_text": (
            "広島の新天地・流川エリアは、半径185mの中にオリエンタルラウンジ・ag・JISの3店舗が集まる中四国屈指の密集地帯。\n\n"
            "岡山・松山を含めた6店舗の男女比推移と、入店時間によって変わる料金システムの違いをリプ欄に整理しました。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "sapporo-sendai-north",
        "path": "/guide/sapporo-sendai-north",
        "eyecatch": "/static/eyecatch-sapporo-sendai-north.jpg",
        "title": "札幌すすきの・仙台国分町の相席ラウンジ比較｜JIS札幌・ag仙台の時間波形と曜日ルール",
        "main_text": (
            "札幌すすきののJISと、仙台国分町のag。どちらも街の1店舗に人が集まるぶん、早い時間と深夜0時過ぎで店内の空気が大きく変わる。\n\n"
            "北日本2都市の実測ログから見えた狙い目の時間帯と、曜日ごとの入店ルールはリプ欄に書いています。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "lounge-vs-screened-date",
        "path": "/guide/lounge-vs-screened-date",
        "eyecatch": "/static/eyecatch-lounge-vs-screened-date.jpg",
        "title": "相席ラウンジと審査制1対1デート、実測ログで見る使い分けの境界線",
        "main_text": (
            "金曜20時台に友達と2人で入る相席ラウンジ（平均女性比70.6%）と、週末に1人で60分だけ話す審査制の1対1カフェデート。\n\n"
            "9.6万回のログと料金を並べると、向く時間帯も性格もきれいに分かれていた。\n\n"
            "どこで線を引くかはリプ欄に置いておきます。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "dress-code-cleanliness",
        "path": "/guide/dress-code-cleanliness",
        "eyecatch": "/static/eyecatch-dress-code-cleanliness.jpg",
        "title": "相席ラウンジの服装と清潔感｜ドレスコードの線引きと、70cmソファ席の間接照明",
        "main_text": (
            "「スニーカーやパーカーでも入れる？」「暗い店内なら夕方のヒゲやテカリも目立たない？」\n\n"
            "テーブル真上の暖色ピンスポットと距離70cmは、昼のオフィスより陰影がくっきり出る。\n\n"
            "5ブランドの服装基準と入店3分前の確認はリプ欄へ。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "brand-waveform-asymmetry",
        "path": "/guide/brand-waveform-asymmetry",
        "eyecatch": "/static/eyecatch-brand-waveform-asymmetry.jpg",
        "title": "20時は女性68%、深夜0時には逆転する｜5ブランドの時間波形と、男女で真逆になる店選び",
        "main_text": (
            "20時は女性比率68%だったのに、深夜0時に覗いたら逆転していた。同じ街でも、ブランドごとに賑わう時間の波形はまるで違う。\n\n"
            "オリエンタルラウンジ・ag・JIS・ヤタコイ・ASOBIBARの波形と、男女で店選びが真逆になる理由はリプ欄で。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "crowd-rush-paradox",
        "path": "/guide/crowd-rush-paradox",
        "eyecatch": "/static/eyecatch-crowd-rush-paradox.jpg",
        "title": "金曜22時の「女性比60%」に向かうと、3回に1回は男性余りになる｜98万件ログで見た群衆殺到のパラドックス",
        "main_text": (
            "金曜22時、「女性比60%」を見て向かったのに、着いたら男性の待ちができていた。\n\n"
            "98万件のログでは、22時台の60%の店は29.8%が60分後に男性余りへ。70%超の店は97.7%がそのまま保たれていた。\n\n"
            "逆転を避ける条件はリプ欄にまとめました。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "last-train-cliff",
        "path": "/guide/last-train-cliff",
        "eyecatch": "/static/eyecatch-last-train-cliff.jpg",
        "title": "23時20分、店内の空気が変わる｜9.6万件のログで見えた「40分間」と街ごとのハシゴの流れ",
        "main_text": (
            "23時半にラウンジへ入って、乾杯から10分もしないうちに女性客が一斉に帰り支度を始める。\n\n"
            "9.6万件のログを10分刻みで見ると、23:20の終電の崖と0:30の第2波がはっきり浮かぶ。\n\n"
            "街ごとの回り方はリプ欄へ。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "price-event-comparison",
        "path": "/guide/price-event-comparison",
        "eyecatch": "/static/eyecatch-price-comparison.jpg",
        "title": "全国60店舗超の相席ラウンジ、料金・イベント・現在人数を横並びで見る",
        "main_text": (
            "1人で入ろうとして「2名様からで…」と断られたり、隣の店のイベントで女性が増えていたのに気づかなかったり。\n\n"
            "全国60店舗超の時間帯料金、1名入店の可否、今の人数を1画面で見比べる方法を書きました。リプ欄から読めます。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "male-lead-mechanics",
        "path": "/guide/male-lead-mechanics",
        "eyecatch": "/static/eyecatch-male-lead.jpg",
        "title": "「男性先行」の店ほど、30分後に女性が増えている｜女性は待たないという実測データ",
        "main_text": (
            "「女性25人・男性2人」の店に急いで向かったら、着いた頃には女性が減っていた。\n\n"
            "女性側に聞くと「20分待つなら隣の店に行く」が本音らしい。実際、男性先行の店ほど30分後に女性が増えている。\n\n"
            "推移の数字はリプ欄に載せています。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "beauty-area-stage",
        "path": "/guide/beauty-area-stage",
        "eyecatch": "/static/eyecatch-beauty-stage.jpg",
        "title": "美容サロンの多い街の相席ラウンジに、身なりの整った人が集まる理由｜「今日ビジュいい日」の行動データ",
        "main_text": (
            "土日の18時半〜20時頃、茶屋町・渋谷・新宿・心斎橋のラウンジでは「美容院帰りで、まっすぐ帰るのがもったいなくて」という人によく出会う。\n\n"
            "サロン帰りの第1波と、距離70cmで伝わる清潔感の話をリプ欄に続けました。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "club-area-midnight",
        "path": "/guide/club-area-midnight",
        "eyecatch": "/static/eyecatch-club-midnight.jpg",
        "title": "梅田・渋谷・新宿は、なぜ深夜0時を過ぎても女性が減らないのか｜ダンスクラブと相席ラウンジの回遊",
        "main_text": (
            "全国60店舗超の人数を夜中まで追うと、終電で静かになる街と、深夜0時〜2時に第2波が来る街にくっきり分かれる。\n\n"
            "分かれ目は、徒歩3分圏内に大型のダンスクラブがあるかどうか。\n\n"
            "深夜帯の流れはリプ欄で詳しく。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "weekend-strategy",
        "path": "/guide/weekend-strategy",
        "eyecatch": "/static/eyecatch-weekend.jpg",
        "title": "金・土の相席ラウンジで「男性待ち1時間」を避ける回り方",
        "main_text": (
            "金曜・土曜の21時半に何も見ずに入ると、男性待ち50分になりやすい。徒歩3分圏内の別ブランドと男女比を見比べるだけで、待たずに座れる夜も意外とある。\n\n"
            "時間帯ごとの動き方とハシゴの順番はリプ欄の記事に。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "brand-comparison",
        "path": "/guide/brand-comparison",
        "eyecatch": "/static/guide-brand-comparison.jpg",
        "title": "オリエンタルラウンジ・ag・JIS、3ブランドの客層と空気の違い",
        "main_text": (
            "オリエンタルラウンジ、ag、JIS。結局どこに行けばいいか迷う人向けに、客層と年齢層、店の空気の違いを整理しました。\n\n"
            "落ち着いて話したい夜。\n"
            "20代前半と賑やかに飲みたい夜。\n"
            "バーの感覚で過ごしたい夜。\n\n"
            "どれが合うかはリプ欄の記事で。\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "weekday-strategy",
        "path": "/guide/weekday-strategy",
        "eyecatch": "/static/note-eyecatch-weekday.jpg",
        "title": "水曜・木曜の20時〜22時が穴場になる理由",
        "main_text": (
            "相席ラウンジは金土の夜が一番賑わう印象があるけれど、待ち時間なしでゆっくり話せるのは、案外水曜・木曜の20時〜22時だったりする。\n\n"
            "男女比データから見た平日の過ごし方は、リプ欄に置いてあります。\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "shinjuku",
        "path": "/guide/shinjuku",
        "eyecatch": "/static/eyecatch-shinjuku.jpg",
        "title": "新宿の3店舗（オリエンタルラウンジ・JIS東口・JIS西口）をどう使い分けるか",
        "main_text": (
            "新宿の相席ラウンジ3店舗、オリエンタルラウンジ・JIS東口・JIS西口。\n\n"
            "客層もピークの時間もかなり違うので、1軒目が男性待ちだったときの移動ルートを知っておくと空振りがぐっと減る。\n\n"
            "ハシゴの順番はリプ欄で紹介しています。\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "solo-vs-pair",
        "path": "/guide/solo-vs-pair",
        "eyecatch": "/static/eyecatch-solo-pair.jpg",
        "title": "1人で行くか、2人で行くか｜ソロとペアの立ち回り",
        "main_text": (
            "「1人で行っても浮かない？」「やっぱり友達と2人のほうがいい？」\n\n"
            "1人には1人の身軽さがあり、2人には2人の役割分担がある。向いている店舗や時間帯も少しずつ違う。\n\n"
            "それぞれのコツはリプ欄に書きました。\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "second-venue",
        "path": "/guide/second-venue",
        "eyecatch": "/static/eyecatch-second-venue.jpg",
        "title": "相席から2軒目へ、自然に誘うタイミングと声のかけ方",
        "main_text": (
            "相席で話が弾んだのに、最後の誘い方で迷ってそのまま解散。あの帰り道はけっこう引きずる。\n\n"
            "「もう1軒飲もう」より頷いてもらいやすい言い方と、声をかけやすい時間帯（22時半〜23時前）について。続きはリプ欄へ。\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "first-10-minutes",
        "path": "/guide/first-10-minutes",
        "eyecatch": "/static/eyecatch-first-10min.jpg",
        "title": "最初の10分で印象が決まる｜会話の入り方と清潔感",
        "main_text": (
            "相席ラウンジの印象は、乾杯してからの最初の10分でほぼ固まる。\n\n"
            "面接のようにならない会話の入り方と、暗い照明のソファ席だからこそ意外と見られている身だしなみのこと。リプ欄の記事で詳しく書いています。\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "grooming",
        "path": "/grooming",
        "eyecatch": "/static/note-eyecatch.jpg",
        "title": "相席ラウンジに入る前の身だしなみ・清潔感チェックリスト",
        "main_text": (
            "暗いソファ席だと油断しがちだけど、距離70cmで隣に座ると、夕方の肌のテカリや眉、青ヒゲ、口元は思っている以上に目に入る。\n\n"
            "入店前にトイレで3分あればできる確認は、リプ欄にリストにしておきました。\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
]


def get_active_column_promo_list() -> list:
    """Return currently public column promo items (published scheduled articles newest-first + permanent list)."""
    dynamic_items = []
    try:
        import scheduled_articles
        for s in scheduled_articles.get_published_articles():
            dynamic_items.append({
                "slug": s["slug"],
                "path": s["path"],
                "eyecatch": s.get("eyecatch_jpg") or "",
                "title": s["x_title"],
                "main_text": s["x_main_text"],
            })
    except Exception:
        pass
    return dynamic_items + COLUMN_PROMO_LIST


def load_column_eyecatch_bytes(item: dict) -> tuple:
    """Load local eyecatch image bytes and MIME type for a column promo/scheduled item if present."""
    if not item:
        return None, "image/jpeg"
    rel_path = str(item.get("eyecatch") or item.get("eyecatch_jpg") or "").strip()
    if not rel_path:
        return None, "image/jpeg"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    static_root = os.path.realpath(os.path.join(base_dir, "static"))
    abs_path = os.path.realpath(os.path.join(base_dir, rel_path.lstrip("/")))
    # Path traversal guard: only image files physically under <repo>/static/ are allowed
    if os.path.commonpath([static_root, abs_path]) != static_root:
        logger.warning(f"Rejected column eyecatch outside static/: {rel_path!r}")
        return None, "image/jpeg"
    lower = abs_path.lower()
    if not lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        logger.warning(f"Rejected non-image column eyecatch: {rel_path!r}")
        return None, "image/jpeg"
    if not os.path.isfile(abs_path):
        return None, "image/jpeg"
    if lower.endswith(".png"):
        mime = "image/png"
    elif lower.endswith(".webp"):
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    try:
        with open(abs_path, "rb") as f:
            return f.read(), mime
    except Exception as e:
        logger.warning(f"Failed to read column eyecatch {abs_path}: {e}")
        return None, mime


def build_column_tweet_thread(col_index: int = 0, app_url: str = "") -> tuple:
    """Build a 2-post thread (URL-free 1st post + URL reply) introducing a site column article."""
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    promo_list = get_active_column_promo_list()
    item = promo_list[col_index % len(promo_list)]
    main_text = item["main_text"]
    reply_text = (
        f"コラム本編はこちら\n"
        f"『{item['title']}』\n"
        f"{site_url}{item['path']}"
    )
    return main_text, reply_text, item


def build_weekly_report_tweet_thread(report: dict, app_url: str = "", use_ai: bool = False) -> tuple:
    """Build a 2-post Sunday Weekly Ranking Report thread with dynamic weekly OGP card URL."""
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    report = report or {}
    stores = report.get("stores") or []

    def _measured_wpct(s):
        try:
            v = s.get("avg_wpct")
            return None if v is None else int(round(float(v)))
        except (TypeError, ValueError):
            return None

    # Exclude anaba candidates without a measured female ratio (never fabricate a fallback %)
    anaba_stores = [a for a in (report.get("anaba_stores") or []) if _measured_wpct(a) is not None]

    week_key = now_jst.strftime("%Y%m%d")
    reply_text = (
        f"📈 今週の全国60店舗超ピーク推移＆曜日別データまとめはこちら👇\n"
        f"{site_url}/stats?w={week_key}"
    )

    if use_ai and stores:
        try:
            import sns_editorial
            if sns_editorial.has_any_ai_key(allow_blocking=True):
                top_facts = [
                    {
                        "name": format_ja_store_name(s.get("name", "")),
                        "avg_women": int(round(float(s.get("avg_women") or 0))),
                        "max_women": int(s.get("max_women") or 0),
                    }
                    for s in stores[:3]
                ]
                anaba_fact = None
                if anaba_stores:
                    a1 = anaba_stores[0]
                    anaba_fact = {
                        "name": format_ja_store_name(a1.get("name", "")),
                        "avg_wpct": _measured_wpct(a1),
                    }
                prompt = (
                    "あなたは全国60店舗超の相席ラウンジ人数を記録している『相席ナビ』の運営中の人です。\n"
                    "日曜の夜、今週1週間分のSQLite計測ログを見返しながらふと呟いた『人間味あふれる週間振り返りポスト（95〜124文字以内、X換算260ウェイト以下）』を1本書いてください。\n\n"
                    f"{sns_editorial.HUMAN_INSIDER_VOICE_GUIDE}\n\n"
                    "【必須ルール】\n"
                    "1. 『🥇🥈🥉』メダルや『📊 今週の統計レポート』のようなボット見出しは使わず、『今週1週間分のログ見返してたんだけど…』のような生の語り口から入ること。\n"
                    "2. 下の検証済み数値（平均女性XX名・最大YY名・女性比ZZ%）から2〜3個を1人の狂いもなく正確に織り交ぜ、URLは入れずリプ欄へ自然に流して『#相席ナビ #相席ラウンジ』で締めること。\n\n"
                    f"【今週の検証済み数値】\n{sns_editorial.json.dumps({'top3': top_facts, 'anaba_no1': anaba_fact}, ensure_ascii=False)}\n\n"
                    '出力はJSON形式のみ：{"main_text": "投稿本文"}'
                )
                parsed, _, _ = sns_editorial.call_editorial_ai_json(
                    prompt=prompt,
                    max_output_tokens=1800,
                    thinking_budget=1152,
                    total_deadline_sec=28.0,
                    per_call_timeout_sec=19.0,
                    now_jst=now_jst,
                )
                if parsed and parsed.get("main_text"):
                    ai_txt = sns_editorial._postprocess_ai_main_text(parsed["main_text"], channel="x")
                    wt = compute_twitter_weight(ai_txt)
                    allowed_nums = {x["avg_women"] for x in top_facts} | {x["max_women"] for x in top_facts}
                    if anaba_fact:
                        allowed_nums.add(anaba_fact["avg_wpct"])
                    found_nums = {int(n) for n in re.findall(r"(\d+)\s*(?:名|%|人)", ai_txt)}
                    if "http" not in ai_txt and 115 <= wt <= 274 and found_nums and found_nums.issubset(allowed_nums):
                        return ai_txt, reply_text
        except Exception as e:
            logger.warning(f"Weekly X AI generation fallback: {e}")

    lines = ["今週1週間分のモニターログを見返してたんだけど、やっぱり集まり方のクセがはっきり出てる。", ""]
    for s in stores[:2]:
        ja_name = format_ja_store_name(s.get("name", ""))
        avg_w = int(round(float(s.get("avg_women") or 0)))
        max_w = int(s.get("max_women") or 0)
        lines.append(f"・{ja_name}（平均女性{avg_w}名 / ピーク{max_w}名）")

    if anaba_stores:
        a1 = anaba_stores[0]
        a1_name = format_ja_store_name(a1.get("name", ""))
        a1_pct = _measured_wpct(a1)
        lines.append(f"・一番バランス良かった穴場：{a1_name}（女性比{a1_pct}%）")

    lines.extend([
        "",
        "曜日ごとの細かい波はリプ欄置いときます👇",
        "#相席ナビ #相席ラウンジ",
    ])
    main_text = "\n".join(lines)
    if compute_twitter_weight(main_text) > 274:
        lines[-2] = "詳細はリプ欄へ👇"
        main_text = "\n".join(lines)
    if compute_twitter_weight(main_text) > 274:
        lines[0] = "今週の相席モニターログ集計📊"
        main_text = "\n".join(lines)
    if compute_twitter_weight(main_text) > 274 and anaba_stores:
        a1_name = format_ja_store_name(anaba_stores[0].get("name", ""))
        a1_pct = _measured_wpct(anaba_stores[0])
        anaba_idx = next((i for i, ln in enumerate(lines) if ln.startswith("・一番バランス良かった穴場")), None)
        if anaba_idx is not None:
            lines[anaba_idx] = f"・穴場：{a1_name}（女性比{a1_pct}%）"
        main_text = "\n".join(lines)
    return main_text, reply_text


def upload_media_to_x(media_bytes: bytes, mime_type: str = "image/png") -> str:
    """
    Upload binary image bytes via X API v2 Media Upload (`POST https://api.x.com/2/media/upload`).
    Uses OAuth 1.0a multipart/form-data (RFC 5849 §3.4.1.3.1: multipart body params excluded from signature base string).
    Returns the media id (v2 `data.id`, or legacy `media_id_string`) on success, or `None` on failure so callers
    safely fall back to text-only tweets.
    """
    if not media_bytes:
        return None

    api_key = os.environ.get("X_API_KEY", "").strip()
    api_secret = os.environ.get("X_API_SECRET", "").strip()
    access_token = os.environ.get("X_ACCESS_TOKEN", "").strip()
    access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()
    if not all([api_key, api_secret, access_token, access_secret]):
        return None

    upload_url = X_MEDIA_UPLOAD_URL
    auth_header = _build_oauth1_header("POST", upload_url, api_key, api_secret, access_token, access_secret)
    mt = (mime_type or "image/png").lower()
    ext = "png" if "png" in mt else ("webp" if "webp" in mt else "jpg")
    try:
        resp = requests.post(
            upload_url,
            headers={"Authorization": auth_header},
            files={
                "media": (f"card.{ext}", media_bytes, mime_type or "image/png"),
                "media_category": (None, "tweet_image"),
            },
            timeout=20,
        )
        if resp.status_code in (200, 201, 202):
            try:
                data = resp.json() or {}
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            media_id = (
                (data.get("data") or {}).get("id")
                or data.get("media_id_string")
                or (str(data["media_id"]) if data.get("media_id") else None)
            )
            if media_id:
                logger.info(f"Uploaded media to X successfully: media_id={media_id}")
                return str(media_id)
        logger.warning(f"X media upload returned {resp.status_code}: {resp.text[:200]} (falling back to text-only tweet)")
    except Exception as e:
        logger.warning(f"X media upload failed ({e}); falling back to text-only tweet")
    return None


def post_to_x(
    text: str,
    reply_to_tweet_id: str = None,
    reply_text: str = None,
    media_bytes: bytes = None,
    media_mime_type: str = "image/png",
) -> dict:
    """
    Post a tweet via X API v2 (`POST https://api.twitter.com/2/tweets`).
    If `media_bytes` is provided (for main tweets), uploads native image first and attaches `media_ids`
    (gracefully falling back to text-only if media upload fails).
    If `reply_text` is provided, automatically posts `reply_text` as a reply (2nd tweet) to the main tweet.
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

    uploaded_media_id = None
    if media_bytes and not reply_to_tweet_id:
        uploaded_media_id = upload_media_to_x(media_bytes, mime_type=media_mime_type)

    url = "https://api.twitter.com/2/tweets"
    auth_header = _build_oauth1_header("POST", url, api_key, api_secret, access_token, access_secret)
    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }
    payload = {"text": text}
    if uploaded_media_id:
        payload["media"] = {"media_ids": [str(uploaded_media_id)]}
    if reply_to_tweet_id:
        payload["reply"] = {"in_reply_to_tweet_id": str(reply_to_tweet_id)}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            data = resp.json()
            logger.info(f"Successfully posted to X: {data}")
            result = {"status": "success", "data": data}
            if uploaded_media_id:
                result["media_id"] = str(uploaded_media_id)
            tweet_id = (data.get("data") or {}).get("id")
            if reply_text and tweet_id and not reply_to_tweet_id:
                delay_sec = 0.05 if os.environ.get("DISABLE_SCHEDULER") == "1" else random.uniform(3.0, 6.0)
                time.sleep(delay_sec)
                reply_res = post_to_x(reply_text, reply_to_tweet_id=tweet_id)
                result["reply"] = reply_res
                result["reply_text"] = reply_text
                if reply_res.get("status") != "success":
                    err_detail = reply_res.get("detail") or reply_res.get("reason") or str(reply_res)
                    logging.warning(f"X self-reply failed for tweet_id={tweet_id}: {err_detail}")
                    result["reply_error"] = err_detail
            return result
        else:
            logger.error(f"X API returned {resp.status_code}: {resp.text}")
            return {"status": "error", "code": resp.status_code, "detail": resp.text}
    except Exception as e:
        logger.error(f"Failed to post to X: {e}")
        return {"status": "error", "detail": str(e)}


LAST_X_AUTO_POST_KEY = "x_last_auto_post_ts"
_JST = datetime.timezone(datetime.timedelta(hours=9))


def _coerce_jst_naive(dt: datetime.datetime = None) -> datetime.datetime:
    if dt is None:
        return datetime.datetime.now(_JST).replace(tzinfo=None)
    if dt.tzinfo is not None:
        return dt.astimezone(_JST).replace(tzinfo=None)
    return dt


def record_x_auto_post_timestamp(now_jst: datetime.datetime = None):
    """Persist the timestamp & causal per-business-date log of automated X posts to prevent burst clustering and daily spam."""
    try:
        import job_state
        ts_naive = _coerce_jst_naive(now_jst)
        ts_str = ts_naive.strftime("%Y-%m-%d %H:%M:%S")
        job_state.set_value(LAST_X_AUTO_POST_KEY, ts_str)
        biz_date = (ts_naive - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        ts_list_key = f"x_daily_posts:{biz_date}"
        raw_list = (job_state.get_value(ts_list_key, allow_blocking=False) or "").strip()
        items = [x.strip() for x in raw_list.split(",") if x.strip()] if raw_list else []
        if ts_str not in items:
            items.append(ts_str)
            items.sort()
            job_state.set_value(ts_list_key, ",".join(items[-24:]))
        cnt_key = f"x_daily_count:{biz_date}"
        job_state.set_value(cnt_key, str(len(items)))
    except Exception as e:
        logger.warning(f"Failed to record last X auto-post timestamp: {e}")


def get_x_daily_auto_post_count(now_jst: datetime.datetime = None) -> int:
    """Return how many automated X posts have fired on the current business date at or before `now_jst`."""
    try:
        import job_state
        ts_naive = _coerce_jst_naive(now_jst)
        ts_cutoff = ts_naive.strftime("%Y-%m-%d %H:%M:%S")
        biz_date = (ts_naive - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        raw_list = (job_state.get_value(f"x_daily_posts:{biz_date}", allow_blocking=False) or "").strip()
        if raw_list:
            items = [x.strip() for x in raw_list.split(",") if x.strip()]
            return sum(1 for x in items if x <= ts_cutoff)
        raw = (job_state.get_value(f"x_daily_count:{biz_date}", allow_blocking=False) or "0").strip()
        return int(raw) if raw.isdigit() else 0
    except Exception:
        return 0


def check_x_daily_post_budget(
    max_daily: int = None,
    now_jst: datetime.datetime = None,
    is_primary_peak: bool = False,
) -> tuple:
    """
    Return `(over_budget: bool, reason: str)` if today's automated X posts have reached the anti-bot daily ceiling.
    Defaults:
      - Mon–Thu: non-peak cap 3 + 1 primary peak reserve = max 4 automated X posts/day total.
      - Fri–Sun: non-peak cap 4 + 1 primary peak reserve = max 5 automated X posts/day total.
    """
    try:
        ts_naive = _coerce_jst_naive(now_jst)
        biz_dt = ts_naive - datetime.timedelta(hours=7)
        if max_daily is None:
            non_peak_cap = 4 if biz_dt.weekday() in (4, 5, 6) else 3
            limit = non_peak_cap + (1 if is_primary_peak else 0)
        else:
            limit = int(max_daily)
        cur = get_x_daily_auto_post_count(now_jst=now_jst)
        if cur >= limit:
            return True, f"Daily X auto-post cap reached ({cur} >= {limit} for {biz_dt.strftime('%Y-%m-%d')})"
    except Exception:
        pass
    return False, "ok"


def check_x_auto_post_cooldown(min_interval_minutes: int = 48, now_jst: datetime.datetime = None) -> tuple:
    """
    Return `(in_cooldown: bool, reason: str)` if an automated X post already fired within `min_interval_minutes`.
    Prevents automated X posts (live slots, tips, column releases, note shares, bonus alerts) from clustering back-to-back.
    """
    try:
        import job_state
        raw = (job_state.get_value(LAST_X_AUTO_POST_KEY, allow_blocking=False) or "").strip()
        if not raw:
            return False, "ok"
        last_naive = datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        cur_naive = _coerce_jst_naive(now_jst)
        diff_min = (cur_naive - last_naive).total_seconds() / 60.0
        if 0 <= diff_min < float(min_interval_minutes):
            return True, f"X auto-post cooldown active ({int(diff_min)}m < {min_interval_minutes}m since last X post)"
    except Exception:
        pass
    return False, "ok"


# ==============================================================================
# Option-1 Pivot (2026-11):
#   1. `format_official_searchable_store_name` — Official brand names + readings
#   2. Pillar A (`build_x_data_law_post`) — Standalone 1-Tweet Data-Law / Insight
#      (zero URL, zero self-reply, zero "リプ欄へ👇", zero hashtags)
#   3. Pillar B (`build_x_search_catcher_post`) — Fri/Sat 22:00 Store-Search Catcher
#      (clean searchable hub store counts, zero "👇" bait, quiet source reply)
# ==============================================================================

X_DATA_LAW_RECENT_KEY = "x_data_law_recent_keys"


def format_official_searchable_store_name(raw_name: str) -> str:
    """
    Format raw store name with official brand readings (`AGENTS.md §3`) while keeping
    Japanese area names searchable on X:
      - 'OLG 新宿' -> 'オリエンタルラウンジ 新宿'
      - 'OLG ag渋谷' -> 'ag（アグ）渋谷'
      - 'JIS SHINJUKU' -> 'JIS（ジス）新宿'
      - 'ASOBIBAR 心斎橋' -> 'ASOBIBAR（アソビバー）心斎橋'
      - 'YATAKOI UMEDA' -> 'ヤタコイ梅田'
      - 'XIX OKAYAMA' -> 'XIX（ジックス）岡山'
      - 'ALFA HIROSHIMA' -> 'ALFA（アルファ）広島'
      - 'CLOVERS HIROSHIMA' -> 'CLOVERS（クローバーズ）広島'
    """
    base = format_ja_store_name(raw_name)
    if not base:
        return ""
    if base.startswith("ag（アグ）") or base.startswith("JIS（ジス）") or base.startswith("ASOBIBAR（アソビバー）"):
        return base
    if base.startswith("ag"):
        rest = base[2:].strip()
        return f"ag（アグ）{rest}" if rest else "ag（アグ）"
    if base.upper().startswith("JIS"):
        rest = re.sub(r"^JIS\s*", "", base, flags=re.IGNORECASE).strip()
        return f"JIS（ジス）{rest}" if rest else "JIS（ジス）"
    if base.startswith("アソビバー"):
        rest = base[len("アソビバー"):].strip()
        return f"ASOBIBAR（アソビバー）{rest}" if rest else "ASOBIBAR（アソビバー）"
    if base.upper().startswith("ASOBIBAR"):
        rest = re.sub(r"^ASOBIBAR\s*", "", base, flags=re.IGNORECASE).strip()
        return f"ASOBIBAR（アソビバー）{rest}" if rest else "ASOBIBAR（アソビバー）"
    if base.upper().startswith("XIX") and "（ジックス）" not in base:
        rest = re.sub(r"^XIX\s*", "", base, flags=re.IGNORECASE).strip()
        return f"XIX（ジックス）{rest}" if rest else "XIX（ジックス）"
    if base.upper().startswith("ALFA") and "（アルファ）" not in base:
        rest = re.sub(r"^ALFA\s*", "", base, flags=re.IGNORECASE).strip()
        return f"ALFA（アルファ）{rest}" if rest else "ALFA（アルファ）"
    if base.upper().startswith("CLOVERS") and "（クローバーズ）" not in base:
        rest = re.sub(r"^CLOVERS\s*", "", base, flags=re.IGNORECASE).strip()
        return f"CLOVERS（クローバーズ）{rest}" if rest else "CLOVERS（クローバーズ）"
    return re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", base)


X_DATA_LAW_POSTS = (
    {
        "key": "crowd_rush_paradox",
        "eyecatch": "/static/eyecatch-crowd-rush-paradox.jpg",
        "text": (
            "金曜22時に「女性比60%」の表示を見て急いで向かうと、着いた頃には3回に1回（29.8%）が男性余りにひっくり返っている。\n\n"
            "逆に70%を超えている店は60分後も97.7%が崩れない。画面の数字だけ見て全員が同じ店へ走ると、到着した瞬間が一番混む。"
        ),
    },
    {
        "key": "male_lead_30m",
        "eyecatch": "/static/eyecatch-male-lead.jpg",
        "text": (
            "「女性25人・男性2人」の店を見つけて走ったのに、着いたら女性が減っていた夜。\n\n"
            "女性側からすると「20分待たされるならすぐ乾杯できる隣の店へ移る」が本音なので、実は男性が先行して入っている店の方が30分後に女性が増えやすい。"
        ),
    },
    {
        "key": "last_train_2320",
        "eyecatch": "/static/eyecatch-last-train-cliff.jpg",
        "text": (
            "夜のラウンジは23時20分を境に店内の空気が一度リセットされる。\n\n"
            "23時半に入って乾杯から10分で終電組が一斉に席を立つあの切なさを避けるなら、22時台前半に入るか、最初から第2波が戻ってくる深夜0時半以降に絞る方がずっと話しやすい。"
        ),
    },
    {
        "key": "line_3_groups_memory",
        "eyecatch": "/static/eyecatch-line-exchange-reality.jpg",
        "text": (
            "その場では盛り上がったのに翌日の昼にお礼LINEを送ると既読スルーされる理由、意外とシンプルで、男性は1組しか話していなくても女性は1晩で平均3組（6名）と話しているから。\n\n"
            "翌朝には顔の記憶が混ざるので、帰りの電車に乗る30分以内の短い一言が一番残る。"
        ),
    },
    {
        "key": "payday_before_anaba",
        "eyecatch": "/static/eyecatch-rain-payday.jpg",
        "text": (
            "25日の給料日直後の週末はスーツ姿の男性待ちが伸びやすいのに、15〜24日の給料日前になるとライバルが減って平均女性比が62.1%（+7.4pt）まで上がる。\n\n"
            "みんなが財布の紐を締める週ほど、実は待たずに落ち着いて話せる。"
        ),
    },
    {
        "key": "cut_at_40m_rule",
        "eyecatch": "/static/eyecatch-cost-break-even.jpg",
        "text": (
            "少し噛み合わない席で「すぐ出るのも悪いかな」と90分粘っても、向かいの女性も化粧室のLINEで「いつ出る？」と時計を見ていることが多い。\n\n"
            "40分で明るく切り上げて徒歩3分の別店舗へ移る方が、お財布も空気もすり減らない。"
        ),
    },
    {
        "key": "local_midnight_121",
        "eyecatch": "/static/eyecatch-local-vs-urban.jpg",
        "text": (
            "東京の感覚で地方都市のラウンジに20時に行くとまだ静かなのに、地元の居酒屋が終わる23時半を過ぎると急に席が埋まり始める。\n\n"
            "地方18店舗のログを見ると、深夜0〜2時の方が20時台より女性が121.1%まで増えていた。街によって夜の時計がまるで違う。"
        ),
    },
    {
        "key": "sunday_2130_switch",
        "eyecatch": "/static/eyecatch-sunday-lounge-strategy.jpg",
        "text": (
            "日曜夜のラウンジは土曜より人数が落ち着くぶん平均女性比は61.2%と高いけれど、「21時半」を境に翌朝仕事の会社員が一斉に帰途につく。\n\n"
            "日曜に行くなら19時台の早めサク飲みか、22時以降の月曜休み組（美容・アパレル）に合わせる二択になる。"
        ),
    },
    {
        "key": "thirties_brand_split",
        "eyecatch": "/static/eyecatch-thirties-lounge-guide.jpg",
        "text": (
            "「30代で行くと浮くかな」と迷う人は、年齢よりもブランドと時間帯の選び方でほぼ決まる。\n\n"
            "ag（アグ）は20代前半が中心で賑やかだけれど、オリエンタルラウンジやJIS（ジス）の水曜・木曜20時台は仕事帰りの社会人が6割を超えて空気が落ち着く。"
        ),
    },
    {
        "key": "table_change_10_women",
        "eyecatch": "/static/eyecatch-table-change-protocol.jpg",
        "text": (
            "席替え（チェンジ）を頼むとき、店内の女性が10名未満（5組未満）の状態でお願いすると、次の案内まで1人席で待機メーターだけが回りやすい。\n\n"
            "席替えが自然に回るのは「女性10名以上」が入っている夜だけ、というのは覚えておいて損がない基準。"
        ),
    },
    {
        "key": "distance_70cm_spot",
        "eyecatch": "/static/eyecatch-dress-code-cleanliness.jpg",
        "text": (
            "暗いソファ席なら夕方の青ヒゲやテカリも目立たないと思いがちだけれど、テーブル真上の暖色ピンスポットと距離70cmの近さは、昼のオフィスより陰影がくっきり出る。\n\n"
            "入店3分前に洗面所で鏡を一度見るだけで、最初の10分の印象がまるで変わる。"
        ),
    },
    {
        "key": "shibuya_to_ebisu_3min",
        "eyecatch": "/static/eyecatch-shibuya-ebisu-comparison.jpg",
        "text": (
            "金曜の渋谷で男性50分待ちだった夜、山手線で1駅3分の恵比寿へ移動したら待ち時間ゼロで案内されたことがある。\n\n"
            "同じ22時台でも渋谷とオリエンタルラウンジ 恵比寿では客層も混み方の波も別世界なので、1店舗目で並ばない判断が一番効く。"
        ),
    },
    {
        "key": "osaka_kita_to_minami",
        "eyecatch": "/static/eyecatch-osaka-kita-vs-minami.jpg",
        "text": (
            "大阪の夜は時間帯で主役の街が北から南へ動く。\n\n"
            "20時台は梅田・茶屋町に仕事帰りの女性が先に集まり、深夜0時を過ぎるとJIS（ジス）梅田や難波・心斎橋へ第2波が移る。梅田で待ちが出たら御堂筋線で9分降りるだけで流れが変わる。"
        ),
    },
    {
        "key": "club_radius_midnight",
        "eyecatch": "/static/eyecatch-club-midnight.jpg",
        "text": (
            "終電を過ぎた深夜0時〜2時にそのまま静かになる街と、もう一度女性が増える街の分かれ目を調べたら、徒歩3分圏内に大型ダンスクラブがあるかどうかだった。\n\n"
            "渋谷・新宿・梅田・心斎橋だけは、終電後にクラブ前後の回遊で第2波がやってくる。"
        ),
    },
    {
        "key": "second_venue_parfait_call",
        "eyecatch": "/static/eyecatch-second-venue.jpg",
        "text": (
            "会話が弾んだ席の終盤、「もう1軒飲みに行こう」と誘うより「夜カフェかパフェ寄らない？」と声をかけた方が警戒心が下がって頷いてもらいやすい。\n\n"
            "切り出すタイミングは終電の30分前（22時45分ごろ）、お会計に立つ少し前が一番自然。"
        ),
    },
)

_X_PIVOT_BANNED_RE = re.compile(
    r"(?:リプ欄|詳細は|プロフ|👇|https?://|aiseki-navi\.com|#[^\s#]+)",
    re.IGNORECASE,
)


def _sanitize_x_standalone_text(raw_text: str) -> str:
    """Guarantee standalone X post has zero URLs, zero hashtags, zero 'リプ欄/👇', official brands, and <= 272 weight."""
    if not raw_text:
        return ""
    lines = []
    for raw_ln in str(raw_text).splitlines():
        ln = _TWITTER_URL_RE.sub("", raw_ln)
        ln = re.sub(r"#[^\s#]+", "", ln)
        ln = re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", ln)
        ln = re.sub(r"(?<![A-Za-z])OLG(?![A-Za-z])", "オリエンタルラウンジ", ln, flags=re.IGNORECASE)
        ln = re.sub(r"(?<![A-Za-z])YATAKOI(?![A-Za-z])", "ヤタコイ", ln, flags=re.IGNORECASE)
        ln = ln.replace("オリラジ", "オリエンタルラウンジ").replace("👇", "").strip()
        if not ln:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if _X_PIVOT_BANNED_RE.search(ln):
            continue
        lines.append(ln)
    while lines and lines[-1] == "":
        lines.pop()
    text = "\n".join(lines).strip()
    while compute_twitter_weight(text) > 272 and len(text) > 30:
        text = text[:-1].rstrip()
    return text


def _get_recent_x_data_law_keys() -> list:
    try:
        import job_state
        raw = (job_state.get_value(X_DATA_LAW_RECENT_KEY, allow_blocking=False) or "").strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]
    except Exception:
        return []


def record_x_data_law_key(law_key: str) -> None:
    if not law_key:
        return
    try:
        import job_state
        cur = [k for k in _get_recent_x_data_law_keys() if k != law_key]
        cur.insert(0, law_key)
        job_state.set_value(X_DATA_LAW_RECENT_KEY, ",".join(cur[:10]))
    except Exception as e:
        logger.warning(f"Failed to record X data-law key: {e}")


def build_x_data_law_post(now_jst: datetime.datetime = None, attach_eyecatch: bool = True) -> dict:
    """
    Build Pillar A standalone 1-tweet Data-Law / Counter-Intuitive Nightlife Insight:
      - Strictly 1 standalone tweet (`reply_text=None`, zero URLs, zero `リプ欄へ👇`, zero hashtags)
      - Uses verified empirical column metrics only (zero fabricated numbers)
      - Optionally attaches the clean column infographic image on even business days for visual dwell
    """
    now_jst = now_jst or datetime.datetime.now(_JST)
    biz_dt = _coerce_jst_naive(now_jst) - datetime.timedelta(hours=7)
    recent = set(_get_recent_x_data_law_keys())
    available = [item for item in X_DATA_LAW_POSTS if item["key"] not in recent]
    pool = available if available else list(X_DATA_LAW_POSTS)
    idx = (biz_dt.date().toordinal() * 5 + biz_dt.weekday()) % len(pool)
    chosen = dict(pool[idx])
    text = _sanitize_x_standalone_text(chosen["text"])

    media_bytes, media_mime = None, "image/jpeg"
    # Attach visual infographic on ~half of posts so timeline mixes crisp text-only observations with visual cards
    if attach_eyecatch and (biz_dt.date().toordinal() % 2 == 0) and chosen.get("eyecatch"):
        media_bytes, media_mime = load_column_eyecatch_bytes(chosen)

    return {
        "law_key": chosen["key"],
        "text": text,
        "reply_text": None,
        "media_bytes": media_bytes,
        "media_mime_type": media_mime,
    }


def build_x_search_catcher_post(
    stores: list,
    slot_label: str = "22:00",
    app_url: str = "",
    now_jst: datetime.datetime = None,
) -> tuple:
    """
    Build Pillar B Fri/Sat 22:00 Weekend Peak Store-Search Catcher (`(main_text, reply_text)`):
      - Captures high-intent X keyword searches (`新宿`, `渋谷`, `恵比寿`, `梅田`, `難波`, `JIS`, `ag`, `オリエンタルラウンジ`)
      - Zero `詳細はリプ欄へ👇` / `👇` bait and zero `#hashtags` in `main_text`
      - Quiet `reply_text` provides clean source URL (`?s=x22_search_YYYYMMDD`)
    """
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    now_jst = now_jst or datetime.datetime.now(_JST)
    ts_naive = _coerce_jst_naive(now_jst)
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_ja = weekdays_ja[ts_naive.weekday()]
    hhmm = ts_naive.strftime("%H:%M")

    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    dom = [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]
    if not dom:
        return "", ""

    hub_defs = (
        ("新宿", ("新宿", "西新宿", "SHINJUKU")),
        ("渋谷・恵比寿", ("渋谷", "恵比寿", "SHIBUYA", "EBISU")),
        ("梅田", ("梅田", "茶屋町", "天満", "UMEDA", "CHAYAMACHI")),
        ("難波・心斎橋", ("難波", "なんば", "心斎橋", "NAMBA", "SHINSAIBASHI")),
        ("名古屋", ("名古屋", "栄", "錦", "NAGOYA")),
        ("福岡", ("福岡", "天神", "FUKUOKA")),
        ("札幌", ("札幌", "SAPPORO")),
    )

    picked = []
    used_names = set()
    for hub_label, kws in hub_defs:
        candidates = [
            s for s in dom
            if s.get("name") not in used_names
            and any(k in (s.get("name") or "").upper() or k in format_ja_store_name(s.get("name", "")) for k in kws)
            and (int(s.get("women") or 0) + int(s.get("men") or 0)) >= 8
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda s: (int(s.get("women") or 0), int(s.get("men") or 0)), reverse=True)
        top_s = candidates[0]
        used_names.add(top_s.get("name"))
        picked.append((hub_label, top_s))
        if len(picked) >= 3:
            break

    if not picked:
        ranked = sorted(dom, key=lambda s: (int(s.get("women") or 0), int(s.get("men") or 0)), reverse=True)
        for s in ranked[:3]:
            if (int(s.get("women") or 0) + int(s.get("men") or 0)) > 0:
                picked.append(("", s))
    if not picked:
        return "", ""

    header = f"{ts_naive.month}/{ts_naive.day}({wd_ja}) {hhmm}時点 主要エリアの店内人数メモ"
    body_lines = [header, ""]
    for _, s in picked:
        w = int(s.get("women") or 0)
        m = int(s.get("men") or 0)
        official_name = format_official_searchable_store_name(s.get("name", ""))
        body_lines.append(f"・{official_name}：女性{w}名 / 男性{m}名")

    # Add 1 short asymmetric observation line without any link bait
    top_female = max(picked, key=lambda pair: int(pair[1].get("women") or 0) - int(pair[1].get("men") or 0))[1]
    tf_w = int(top_female.get("women") or 0)
    tf_m = int(top_female.get("men") or 0)
    male_lead = [
        s for s in dom
        if int(s.get("men") or 0) >= 12 and int(s.get("men") or 0) >= int(s.get("women") or 0) + 4
    ]
    if tf_w >= tf_m + 5:
        obs = "同じ街でも店舗間で男女比が割れているので、男性待ちが出ている店は徒歩圏の別店舗へ回る方が早いです。"
    elif male_lead:
        ml_name = format_official_searchable_store_name(male_lead[0].get("name", ""))
        obs = f"女性グループなら男性先行の{ml_name}あたりが待ち時間なしで座りやすい時間帯。"
    else:
        obs = "22時台は15分単位で男女比が動くので、1店舗目で並ばず空き席のある店舗を選ぶのが確実です。"

    candidate_full = "\n".join(body_lines + ["", obs])
    if compute_twitter_weight(candidate_full) <= 272:
        main_text = candidate_full
    else:
        short_obs = "同じ駅でも店舗ごとに男女比が割れている時間帯です。"
        cand_short = "\n".join(body_lines + ["", short_obs])
        main_text = cand_short if compute_twitter_weight(cand_short) <= 272 else "\n".join(body_lines)
        while compute_twitter_weight(main_text) > 272 and len(body_lines) > 3:
            body_lines.pop()
            main_text = "\n".join(body_lines)

    biz_ymd = (ts_naive - datetime.timedelta(hours=7)).strftime("%Y%m%d")
    reply_text = (
        f"出典・全国60店舗超のリアルタイム人数推移\n"
        f"{site_url}/?s=x22_search_{biz_ymd}"
    )
    return main_text, reply_text
