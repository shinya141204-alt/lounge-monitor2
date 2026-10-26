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
        k_name = format_ja_store_name(k_top.get("name", ""))
        w_name = format_ja_store_name(w_top.get("name", ""))
        return f"🗼関東1位 {k_name}(女{k_top.get('women', 0)})｜🐙関西1位 {w_name}(女{w_top.get('women', 0)})"
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
        ("名古屋", ("名古屋", "NAGOYA", "栄", "錦")),
        ("福岡", ("福岡", "天神", "FUKUOKA")),
        ("札幌", ("札幌", "SAPPORO")),
    ]
    best_hub = None
    best_gap = 0.0
    for hub_label, kws in hubs:
        hub_stores = [
            s for s in stores
            if any(k in (s.get("name") or "").upper() or k in (s.get("name") or "") for k in kws)
            and (s.get("women", 0) + s.get("men", 0)) >= 10
        ]
        if len(hub_stores) >= 2:
            has_male_heavy = any(s.get("men", 0) >= s.get("women", 0) + 4 for s in hub_stores)
            has_female_lead = any(s.get("women", 0) > s.get("men", 0) and s.get("women", 0) >= 8 for s in hub_stores)
            if has_male_heavy and has_female_lead:
                ratios = [s.get("women", 0) / max(1, s.get("women", 0) + s.get("men", 0)) for s in hub_stores]
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
        if s.get("men", 0) >= 15 and s.get("men", 0) >= s.get("women", 0) + 4
    ]
    if male_lead_candidates:
        male_lead_candidates.sort(key=lambda x: x.get("men", 0), reverse=True)
        top_m = male_lead_candidates[0]
        ja_name = format_ja_store_name(top_m.get("name", ""))
        return f"💡女性目線なら男性{top_m.get('men', 0)}名の{ja_name}が即相席◎"

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

        rank_lines = [header, ""]
        for i, s in enumerate(top_stores):
            w = s.get("women", 0)
            m = s.get("men", 0)
            tot = w + m
            pct = round((w / tot) * 100) if tot > 0 else 0
            ja_name = format_ja_store_name(s.get("name", ""))
            rank_lines.append(f"{medals[i]} {ja_name}：女性{w}・男性{m} ({pct}%)")

        footer_lines = [
            "全53店舗の現在人数はリプ欄へ👇",
            f"{time_tag} #相席ナビ #相席ラウンジ",
        ]
        insight_line = _build_live_insight_line(stores, slot_label=slot_label)
        if insight_line:
            candidate = "\n".join(rank_lines + ["", insight_line] + footer_lines)
            if compute_twitter_weight(candidate) <= 274:
                return candidate

        return "\n".join(rank_lines + [""] + footer_lines)

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


def build_scheduled_reply_text(slot_label: str = "20:00", app_url: str = "") -> str:
    """Build the 2nd post (self-reply) containing the timestamped site URL + price/event comparison board link."""
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    slot_code = (slot_label or "20:00").replace(":", "")
    live_url = f"{site_url}/?s={now_jst.strftime('%Y%m%d')}_{slot_code}"
    return (
        f"🔍 全国53店舗のリアルタイム人数・男女比ランキング\n{live_url}\n\n"
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
        "🍸全国53店舗の相席ラウンジ人数速報『相席ナビ』\n\n"
        "・オリエンタル / ag / JIS 等の現在人数＆男女比をリアルタイム集計\n"
        "・全51店舗の時間帯料金・ソロ入店可否・イベント比較\n"
        "・曜日×時間帯別の平均グラフで「いま狙い目の店舗」がわかる\n\n"
        "サイトURL・比較ボードはリプ欄へ👇\n"
        "#相席ナビ #相席ラウンジ"
    )
    reply_text = (
        f"🔍 全国53店舗リアルタイム人数ランキング\n{site_url}/\n\n"
        f"💴 全51店舗の料金・開催中イベント比較ボード\n{site_url}/price-comparison\n\n"
        f"📖 人気の攻略コラム一覧\n{site_url}/columns"
    )
    return main_text, reply_text


COLUMN_PROMO_LIST = [
    {
        "slug": "price-event-comparison",
        "path": "/guide/price-event-comparison",
        "eyecatch": "/static/eyecatch-price-comparison.jpg",
        "title": "【全51店舗】相席ラウンジ料金・イベント・現在人数の横並び比較ガイド",
        "main_text": (
            "同じ街の相席ラウンジでも「10分あたりの相席料金」「男性1名（ソロ）入店の可否」「開催中の割引・北海道フェア等の対象店舗」って実はかなり違う📊\n\n"
            "全51店舗の4時間帯別料金とリアルタイム男女人数を1画面で見比べる方法をまとめました👇\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "male-lead-mechanics",
        "path": "/guide/male-lead-mechanics",
        "eyecatch": "/static/eyecatch-male-lead.jpg",
        "title": "「男性先行」の店ほど女性が増える？データが証明する「女性は待たない」新常識",
        "main_text": (
            "「女性25人・男性2人」の店に急いで向かったら、着いた頃には女の子が減ってた…って経験ない？\n\n"
            "27万件のログを解析すると、今の女の子は20分相席できないとすぐ別店舗へ移動してて、逆に「男性が先行してる店」ほど30分後に女性が急増（確率66.5%）してる📊\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "beauty-area-stage",
        "path": "/guide/beauty-area-stage",
        "eyecatch": "/static/eyecatch-beauty-stage.jpg",
        "title": "なぜ美容サロン激戦区の相席ラウンジは美人が多いのか？「今日ビジュいい日」の行動データ",
        "main_text": (
            "梅田茶屋町・渋谷・恵比寿・新宿・心斎橋・栄・大名など、人気美容サロンやクリニックが密集してる街のラウンジって、18時半〜20時頃にサロン帰りの可愛い子が本当に多い💄\n\n"
            "「せっかく可愛くした日に直帰するのはもったいない」の行動データと、選ばれる男性の清潔感基準をまとめました👇\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "club-area-midnight",
        "path": "/guide/club-area-midnight",
        "eyecatch": "/static/eyecatch-club-midnight.jpg",
        "title": "なぜ梅田・渋谷・新宿は深夜0時過ぎても女性が減らない？「ダンスクラブ×相席」の回遊法則",
        "main_text": (
            "全国53店舗の相席ラウンジの人数データを夜中まで追っていると、「終電で一気に過疎る街」と「深夜0時〜2時にむしろ第2波が来る街」がはっきり分かれる。\n\n"
            "その決定打になってるのが、徒歩3分圏内にある「大型ダンスクラブ」の存在🎧\n\n"
            "深夜ボーナスタイムの法則はリプ欄へ👇\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "weekend-strategy",
        "path": "/guide/weekend-strategy",
        "eyecatch": "/static/eyecatch-weekend.jpg",
        "title": "金・土の相席ラウンジで「男性待ち1時間」を回避して勝つ方法",
        "main_text": (
            "金曜・土曜の夜に何も見ないで21時半に突撃すると「男性待ち50分」になりがちだけど、徒歩3分圏内の別ブランドと男女比を見比べるだけで待ち時間ゼロで座れることが本当に多い🔥\n\n"
            "週末3つの時間帯別ムーブとハシゴ術をまとめました。\n\n"
            "記事リンクはリプ欄へ👇\n"
            "#相席ナビ #相席ラウンジ"
        ),
    },
    {
        "slug": "brand-comparison",
        "path": "/guide/brand-comparison",
        "eyecatch": "/static/guide-brand-comparison.jpg",
        "title": "オリエンタルラウンジ・ag・JISの違い徹底比較",
        "main_text": (
            "「オリエンタルラウンジ・ag・JIS、結局どこに行けばいい？」って迷う人向けに、3大ブランドの客層・年齢層・雰囲気の違いをまとめました🍸\n\n"
            "・落ち着いて話したい夜\n"
            "・20代前半とワイワイ飲みたい夜\n"
            "・バー感覚で楽しみたい夜\n\n"
            "記事リンクはリプ欄へ👇\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "weekday-strategy",
        "path": "/guide/weekday-strategy",
        "eyecatch": "/static/note-eyecatch-weekday.jpg",
        "title": "なぜ水曜・木曜の20時〜22時が一番の穴場なのか？",
        "main_text": (
            "相席ラウンジって金土の夜が一番盛り上がるイメージあるけど、実は「待ち時間なしで可愛い子とゆっくり話せる」のは水曜・木曜の20時〜22時だったりする🍷\n\n"
            "実際の男女比データから見た、平日の賢い立ち回りをまとめました。\n\n"
            "記事リンクはリプ欄へ👇\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "shinjuku",
        "path": "/guide/shinjuku",
        "eyecatch": "/static/eyecatch-shinjuku.jpg",
        "title": "新宿エリア4店舗（オリエンタル・ag・JIS東口/西口）の使い分け",
        "main_text": (
            "日本一の激戦区「新宿」にある相席ラウンジ4店舗（オリエンタル・ag・JIS東口・JIS西口）。\n\n"
            "実はそれぞれ客層もピーク時間もかなり違うから、1店舗目で男性待ちだったときのハシゴルートを知っておくだけで空振りが一気に減るよ🗼\n\n"
            "記事リンクはリプ欄へ👇\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "solo-vs-pair",
        "path": "/guide/solo-vs-pair",
        "eyecatch": "/static/eyecatch-solo-pair.jpg",
        "title": "1人（ソロ）vs 2人ペア、どっちが勝てる？立ち回り比較",
        "main_text": (
            "「相席ラウンジって1人で行っても浮かない？」「やっぱり友達と2人で行くべき？」\n\n"
            "実は1人（ソロ）には1人の強み、2人ペアにはペアの役割分担があって、向いている店舗や時間帯も少し違います👥\n\n"
            "それぞれの立ち回りのコツはリプ欄へ👇\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "second-venue",
        "path": "/guide/second-venue",
        "eyecatch": "/static/eyecatch-second-venue.jpg",
        "title": "相席から2軒目へ自然に誘うタイミングと声かけのコツ",
        "main_text": (
            "相席でせっかく盛り上がっても、最後の誘い方で迷ってそのまま解散になっちゃうのはもったいない。\n\n"
            "「もう1軒飲もう」よりOKをもらいやすい声かけや、声をかけるベストな時間帯（22時半〜23時前）をまとめました🍨\n\n"
            "記事リンクはリプ欄へ👇\n"
            "#相席ラウンジ"
        ),
    },
    {
        "slug": "first-10-minutes",
        "path": "/guide/first-10-minutes",
        "eyecatch": "/static/eyecatch-first-10min.jpg",
        "title": "最初の10分で差がつく会話の入り方＆清潔感チェック",
        "main_text": (
            "相席ラウンジの印象って、実は乾杯してから「最初の10分」でほぼ決まる。\n\n"
            "面接っぽくならない最初の会話の入り方と、暗い照明のソファ席だからこそ意外と見られている身だしなみのポイントをまとめました🪞\n\n"
            "記事リンクはリプ欄へ👇\n"
            "#相席ラウンジ"
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
    rel_path = (item.get("eyecatch") or item.get("eyecatch_jpg") or "").strip()
    if not rel_path:
        return None, "image/jpeg"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    abs_path = os.path.join(base_dir, rel_path.lstrip("/"))
    if not os.path.isfile(abs_path):
        return None, "image/jpeg"
    mime = "image/png" if abs_path.lower().endswith(".png") else "image/jpeg"
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
        f"📖 コラム本編はこちら👇\n"
        f"『{item['title']}』\n"
        f"{site_url}{item['path']}"
    )
    return main_text, reply_text, item


def build_weekly_report_tweet_thread(report: dict, app_url: str = "") -> tuple:
    """Build a 2-post Sunday Weekly Ranking Report thread with dynamic weekly OGP card URL."""
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    report = report or {}
    stores = report.get("stores") or []
    anaba_stores = report.get("anaba_stores") or []

    medals = ["🥇", "🥈", "🥉"]
    lines = ["📊 今週の全国相席ラウンジ統計レポート", ""]
    for idx, s in enumerate(stores[:3]):
        ja_name = format_ja_store_name(s.get("name", ""))
        avg_w = int(round(float(s.get("avg_women") or 0)))
        max_w = int(s.get("max_women") or 0)
        lines.append(f"{medals[idx]} {ja_name}：平均女性{avg_w}名 / 最大{max_w}名")

    if anaba_stores:
        a1 = anaba_stores[0]
        a1_name = format_ja_store_name(a1.get("name", ""))
        a1_pct = int(round(float(a1.get("avg_wpct") or 56)))
        lines.extend(["", f"💎 週間穴場1位：{a1_name}（女性比{a1_pct}%）"])

    lines.extend([
        "",
        "全53店舗の週間データ白書はリプ欄へ👇",
        "#相席ナビ #相席ラウンジ",
    ])
    main_text = "\n".join(lines)
    week_key = now_jst.strftime("%Y%m%d")
    reply_text = (
        f"📈 全国53店舗の週間ピークランキングTOP30＆曜日別データ白書はこちら👇\n"
        f"{site_url}/stats?w={week_key}"
    )
    return main_text, reply_text


def upload_media_to_x(media_bytes: bytes, mime_type: str = "image/png") -> str:
    """
    Upload binary image bytes via X Media Upload API v1.1 (`POST https://upload.twitter.com/1.1/media/upload.json`).
    Uses OAuth 1.0a multipart/form-data (RFC 5849 §3.4.1.3.1: body params excluded from signature base string).
    Returns `media_id_string` on success, or `None` on failure so callers safely fall back to text-only tweets.
    """
    if not media_bytes:
        return None

    api_key = os.environ.get("X_API_KEY", "").strip()
    api_secret = os.environ.get("X_API_SECRET", "").strip()
    access_token = os.environ.get("X_ACCESS_TOKEN", "").strip()
    access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "").strip()
    if not all([api_key, api_secret, access_token, access_secret]):
        return None

    upload_url = "https://upload.twitter.com/1.1/media/upload.json"
    auth_header = _build_oauth1_header("POST", upload_url, api_key, api_secret, access_token, access_secret)
    ext = "png" if "png" in (mime_type or "").lower() else "jpg"
    try:
        resp = requests.post(
            upload_url,
            headers={"Authorization": auth_header},
            files={"media": (f"card.{ext}", media_bytes, mime_type or "image/png")},
            timeout=20,
        )
        if resp.status_code in (200, 201, 202):
            data = resp.json() or {}
            media_id = data.get("media_id_string") or (str(data["media_id"]) if data.get("media_id") else None)
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
                time.sleep(1.5)
                reply_res = post_to_x(reply_text, reply_to_tweet_id=tweet_id)
                result["reply"] = reply_res
                result["reply_text"] = reply_text
            return result
        else:
            logger.error(f"X API returned {resp.status_code}: {resp.text}")
            return {"status": "error", "code": resp.status_code, "detail": resp.text}
    except Exception as e:
        logger.error(f"Failed to post to X: {e}")
        return {"status": "error", "detail": str(e)}
