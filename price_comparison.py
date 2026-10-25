"""
price_comparison.py - Cross-Brand Lounge Price, Men's Event & Live Cost-Performance Comparison Engine.

Compares standard pricing (charge, wait fee, 10-min weekday/weekend rates, 60-min total estimate),
active men's discounts & women's magnet events (from events.py), and real-time gender balance
across nearby competitor stores in major urban districts.
"""

import re
import store_master

# Brand-level standard price & vibe specifications (domestic stores)
BRAND_PRICE_SPECS = {
    "OLG": {
        "brand_code": "OLG",
        "brand_label": "オリエンタルラウンジ",
        "brand_slug": "oriental-lounge",
        "charge_yen": 550,
        "charge_str": "550円（公式アプリ会員0円）",
        "wait_10m_yen": 0,
        "wait_fee_str": "0円（待機中は完全無料）",
        "has_wait_fee": False,
        "weekday_10m_yen": 550,
        "weekday_10m_str": "550円 / 10分",
        "weekend_10m_yen": 660,
        "weekend_10m_str": "660円〜 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4400,
        "est_60m_weekday_label": "約3,300円〜",
        "est_60m_weekend_label": "約4,400円〜",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "王道ラグジュアリー・OL/社会人中心",
        "default_men_perk": "公式アプリ会員登録で男性チャージ550円が無料＆200種以上飲み放題込",
        "default_women_perk": "女性チャージ・相席・ドリンク完全0円＋季節スイーツ＆限定フード特典",
    },
    "ag": {
        "brand_code": "ag",
        "brand_label": "ag（アグ）",
        "brand_slug": "ag",
        "charge_yen": 550,
        "charge_str": "550円（公式アプリ会員0円）",
        "wait_10m_yen": 0,
        "wait_fee_str": "0円（待機中は完全無料）",
        "has_wait_fee": False,
        "weekday_10m_yen": 500,
        "weekday_10m_str": "500円〜 / 10分",
        "weekend_10m_yen": 600,
        "weekend_10m_str": "600円〜 / 10分",
        "est_60m_weekday": 3000,
        "est_60m_weekend": 3850,
        "est_60m_weekday_label": "約3,000円〜",
        "est_60m_weekend_label": "約3,850円〜",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "Z世代特化・カジュアル＆低価格",
        "default_men_perk": "3大ブランド最安水準の10分単価＋U-25/早割・ガチャ等の参加型企画",
        "default_women_perk": "女性完全0円＋韓国コスメ・カフェスイーツ・トレンドイベント多数",
    },
    "JIS": {
        "brand_code": "JIS",
        "brand_label": "JIS（ジス）",
        "brand_slug": "jis",
        "charge_yen": 550,
        "charge_str": "550円（テーブルチャージ）",
        "wait_10m_yen": 220,
        "wait_fee_str": "220円 / 10分（※待機課金あり）",
        "has_wait_fee": True,
        "weekday_10m_yen": 550,
        "weekday_10m_str": "550円 / 10分",
        "weekend_10m_yen": 660,
        "weekend_10m_str": "660円 / 10分",
        "est_60m_weekday": 3850,
        "est_60m_weekend": 4510,
        "est_60m_weekday_label": "約3,850円〜",
        "est_60m_weekend_label": "約4,510円〜",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "本格バー空間・店舗別の男性割引が豊富",
        "default_men_perk": "店舗別の男性早割・相席料金OFF・定額プラン等のキャンペーンが充実",
        "default_women_perk": "女性完全0円＋JCBギフト券・豪華プレゼント抽選・フード無料企画",
    },
    "XIX": {
        "brand_code": "XIX",
        "brand_label": "相席ラウンジ XIX",
        "brand_slug": "oriental-lounge",
        "charge_yen": 550,
        "charge_str": "550円",
        "wait_10m_yen": 0,
        "wait_fee_str": "0円（待機無料）",
        "has_wait_fee": False,
        "weekday_10m_yen": 550,
        "weekday_10m_str": "550円 / 10分",
        "weekend_10m_yen": 660,
        "weekend_10m_str": "660円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4400,
        "est_60m_weekday_label": "約3,300円〜",
        "est_60m_weekend_label": "約4,400円〜",
        "age_range": "20代〜30代前半",
        "vibe_short": "岡山エリア定番・アットホーム",
        "default_men_perk": "待機中無料＋飲み放題込みの明朗システム",
        "default_women_perk": "女性完全無料＋季節イベント・デザート特典",
    },
    "ヤタコイ": {
        "brand_code": "ヤタコイ",
        "brand_label": "ヤタコイ（YATAKOI）",
        "brand_slug": "oriental-lounge",
        "charge_yen": 550,
        "charge_str": "550円",
        "wait_10m_yen": 0,
        "wait_fee_str": "0円〜（プラン別）",
        "has_wait_fee": False,
        "weekday_10m_yen": 500,
        "weekday_10m_str": "500円〜 / 10分",
        "weekend_10m_yen": 600,
        "weekend_10m_str": "600円〜 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4150,
        "est_60m_weekday_label": "約3,300円〜",
        "est_60m_weekend_label": "約4,150円〜",
        "age_range": "20代前半〜後半",
        "vibe_short": "屋台×お祭り空間・ノリ重視で話しやすい",
        "default_men_perk": "茶屋町エリアでカジュアルに飲めるエンタメ相席スタイル",
        "default_women_perk": "女性無料＋屋台フード＆ゲーム感覚で盛り上がれる空間",
    },
    "CLOVERS": {
        "brand_code": "CLOVERS",
        "brand_label": "相席CLOVERS",
        "brand_slug": "oriental-lounge",
        "charge_yen": 500,
        "charge_str": "500円前後",
        "wait_10m_yen": 0,
        "wait_fee_str": "0円（待機無料）",
        "has_wait_fee": False,
        "weekday_10m_yen": 500,
        "weekday_10m_str": "500円〜 / 10分",
        "weekend_10m_yen": 600,
        "weekend_10m_str": "600円〜 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4000,
        "est_60m_weekday_label": "約3,300円〜",
        "est_60m_weekend_label": "約4,000円〜",
        "age_range": "20代〜30代",
        "vibe_short": "広島・流川のカジュアル相席バー",
        "default_men_perk": "10分単位の明朗会計・2軒目利用にも使いやすい価格設定",
        "default_women_perk": "女性無料・カクテル＆フード充実",
    },
    "ALFA": {
        "brand_code": "ALFA",
        "brand_label": "相席ラウンジ ALFA",
        "brand_slug": "oriental-lounge",
        "charge_yen": 550,
        "charge_str": "550円",
        "wait_10m_yen": 0,
        "wait_fee_str": "0円（待機無料）",
        "has_wait_fee": False,
        "weekday_10m_yen": 550,
        "weekday_10m_str": "550円 / 10分",
        "weekend_10m_yen": 660,
        "weekend_10m_str": "660円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4400,
        "est_60m_weekday_label": "約3,300円〜",
        "est_60m_weekend_label": "約4,400円〜",
        "age_range": "20代半ば〜30代",
        "vibe_short": "広島エリアの落ち着いたラウンジ空間",
        "default_men_perk": "待機無料＆200種以上のドリンク飲み放題",
        "default_women_perk": "女性完全無料＋各種キャンペーン",
    },
}

# Major urban comparison clusters (ordered for tabs on /price-comparison)
COMPARISON_CLUSTERS = [
    {
        "id": "shinjuku",
        "tab_label": "🗼 新宿・西新宿（3店）",
        "title": "新宿・西新宿エリア（徒歩5分圏内・3店舗の料金＆イベント比較）",
        "desc": "東口（歌舞伎町）のオリエンタルラウンジ新宿・JIS新宿と、西口側のJIS西新宿が徒歩圏内で競合する日本一の激戦区。待機無料のオリエンタルか、男性割引プランが豊富なJIS（東口・西口）かを現在の女性比率とあわせて比較するのが鉄則です。",
        "stores": ["OLG 新宿", "JIS SHINJUKU", "JIS NISHISHINJUKU"],
    },
    {
        "id": "shibuya",
        "tab_label": "✨ 渋谷・恵比寿（3店）",
        "title": "渋谷・恵比寿エリア（3店舗の料金＆イベント比較）",
        "desc": "渋谷駅周辺の2店舗（オリエンタル渋谷本店・ag渋谷）は互いに徒歩3分圏内。10分単価が安いZ世代向けのag渋谷、王道大箱の渋谷本店、さらに1駅隣の大人な恵比寿店まで、その日の予算と年齢層で使い分けられます。",
        "stores": ["OLG 渋谷本店", "OLG ag渋谷", "OLG 恵比寿"],
    },
    {
        "id": "osaka-kita",
        "tab_label": "🐙 梅田・茶屋町（6店）",
        "title": "大阪キタ（梅田・茶屋町・天満 6店舗の料金＆イベント比較）",
        "desc": "西日本最大の激戦区。オリエンタル大阪駅前、ag梅田、JIS 2店舗（梅田・茶屋町）、ヤタコイ梅田、天満店が徒歩圏内に密集しており、同じ時刻でも店舗間で男女比と待ち時間が大きく開きます。",
        "stores": [
            "OLG 大阪駅前",
            "OLG ag梅田",
            "JIS UMEDA",
            "JIS CHAYAMACHI",
            "YATAKOI UMEDA",
            "OLG 天満",
        ],
    },
    {
        "id": "osaka-minami",
        "tab_label": "🥂 難波・心斎橋・京神（5店）",
        "title": "大阪ミナミ（心斎橋・難波）・京都・神戸エリア（5店舗比較）",
        "desc": "心斎橋・難波はオリエンタル2店舗とJIS難波が徒歩圏内。JIS難波で男性割引イベントが出ているか、オリエンタル側で女性が先行しているかを比較してから入店するのが鉄則です。",
        "stores": ["OLG 心斎橋", "OLG 難波", "JIS NAMBA", "OLG 京都", "OLG 神戸"],
    },
    {
        "id": "nagoya",
        "tab_label": "🏯 名古屋 栄・錦（7店）",
        "title": "名古屋（栄・錦）・静岡・浜松・金沢エリア（料金＆イベント比較）",
        "desc": "名古屋の栄・錦エリアにはオリエンタル2店舗（栄・錦）とag名古屋が徒歩数分圏内に集中。20代前半中心で10分単価が安いag名古屋か、社会人OL層が多い栄・錦店かを現在の男女比とあわせて比較できます。",
        "stores": ["OLG 名古屋 栄", "OLG 名古屋 錦", "OLG ag名古屋", "OLG 静岡", "OLG 浜松", "OLG 金沢", "OLG ag金沢"],
    },
    {
        "id": "fukuoka",
        "tab_label": "🍜 福岡天神・九州（12店）",
        "title": "福岡（天神・小倉）・熊本・九州・沖縄エリア（料金＆イベント比較）",
        "desc": "福岡・天神（大名）エリアにはオリエンタル・ag・JISの3大ブランドがすべて徒歩3分圏内に集結！熊本もオリエンタルとJISの2店舗が並び、イベント開催日と男女比の比較が勝敗を分けます。",
        "stores": [
            "OLG 福岡",
            "OLG ag福岡",
            "JIS FUKUOKA",
            "OLG 熊本",
            "JIS KUMAMOTO",
            "OLG 小倉",
            "OLG 沖縄",
            "OLG ag沖縄",
            "OLG 長崎",
            "OLG 大分",
            "OLG 宮崎",
            "OLG 鹿児島",
        ],
    },
    {
        "id": "ueno-east",
        "tab_label": "🏮 上野・錦糸町・町田（5店）",
        "title": "上野・錦糸町・町田・柏エリア（東京東＆近郊の料金比較）",
        "desc": "上野駅前にはオリエンタル上野とag上野が隣接。新宿や渋谷より男性待ちが発生しにくく、1軒目からコスパ良く相席しやすい穴場エリアです。",
        "stores": ["OLG 上野", "OLG ag上野", "OLG 錦糸町", "OLG 町田", "OLG 柏"],
    },
    {
        "id": "omiya-yokohama",
        "tab_label": "🌉 大宮・横浜・北関東（5店）",
        "title": "大宮・横浜・宇都宮・高崎エリア（料金＆イベント比較）",
        "desc": "大宮駅東口には全国トップクラスの集客を誇る「オリエンタル大宮」と「JIS大宮」が至近距離で競合。JIS大宮の待機課金リスクをオリエンタル大宮の女性比率と見比べるのが定石です。",
        "stores": ["OLG 大宮", "JIS OMIYA", "OLG 横浜", "OLG 宇都宮", "OLG 高崎"],
    },
    {
        "id": "sapporo-regional",
        "tab_label": "❄️ 札幌・仙台・広島・岡山（12店）",
        "title": "札幌・仙台・広島・岡山・松山エリア（料金＆イベント比較）",
        "desc": "札幌すすきの（OLG・ag・JISの3店）、仙台国分町（OLG・agの2店）、広島流川（OLG・ag・ALFA・CLOVERSの4店）、岡山（OLG・XIXの2店）など、複数ブランドが競い合う主要都市の比較ボードです。",
        "stores": [
            "OLG 札幌",
            "OLG ag札幌",
            "JIS SAPPORO",
            "OLG 仙台",
            "OLG ag仙台",
            "OLG 広島",
            "OLG ag広島",
            "ALFA HIROSHIMA",
            "CLOVERS HIROSHIMA",
            "OLG 岡山",
            "XIX OKAYAMA",
            "JIS MATSUYAMA",
        ],
    },
]

_MEN_KEYWORDS = (
    "男性", "メンズ", "men", "早割", "半額", "off", "割引", "チャージ無料",
    "定額", "飲み放題", "フリーパス", "ハッピーアワー", "相席料金", "1人", "ソロ",
    "学割", "20代", "u-25", "u25", "ガチャ", "ビンゴ", "ハロウィン", "祭", "フェス",
)

_WOMEN_ONLY_KEYWORDS = (
    "女性限定", "女子会", "レディース", "コスメ", "ネイル", "美容", "スイーツ",
    "パフェ", "アイス", "ギフト券", "jcb", "タクシー代", "プレゼント",
)


def classify_event_for_comparison(ev: dict) -> str:
    """Return 'men_perk' if the event directly benefits men or is a shared venue event,
    or 'women_magnet' if it is a women-targeted attraction campaign."""
    title = (ev.get("title") or "").lower()
    if any(k in title for k in ("男性", "メンズ", "早割", "相席料金", "チャージ無料", "定額", "ハッピーアワー")):
        return "men_perk"
    if any(k in title for k in _WOMEN_ONLY_KEYWORDS):
        return "women_magnet"
    if any(k in title for k in _MEN_KEYWORDS):
        return "men_perk"
    return "women_magnet"


def _clean_event_title(raw_title: str) -> str:
    t = re.sub(r"\s+", " ", raw_title or "").strip()
    if len(t) > 52:
        return t[:50] + "…"
    return t


def get_store_price_profile(
    store_name: str,
    live_store: dict = None,
    all_events_Pool: list = None,
) -> dict:
    """Build a complete price + active events + live cost-performance verdict object for one store."""
    info = store_master.get(store_name) or {}
    brand_raw = info.get("brand") or ("ag" if "ag" in store_name else store_name.split(" ")[0])
    if brand_raw == "YATAKOI":
        brand_raw = "ヤタコイ"
    spec = BRAND_PRICE_SPECS.get(brand_raw, BRAND_PRICE_SPECS["OLG"])

    live_store = live_store or {}
    women = int(live_store.get("women") or 0)
    men = int(live_store.get("men") or 0)
    total = women + men
    wpct = round((women / total) * 100) if total > 0 else 0
    w_diff = int(live_store.get("women_diff") or 0)
    m_diff = int(live_store.get("men_diff") or 0)

    # Collect store-matched events non-blockingly
    store_evs = []
    try:
        import events
        if all_events_Pool is not None:
            # Fast filter using events.get_events_for_store logic without re-reading disk
            store_evs = events.get_events_for_store(store_name, store_only=False, allow_blocking=False)
        else:
            store_evs = events.get_events_for_store(store_name, store_only=False, allow_blocking=False)
    except Exception:
        store_evs = []

    men_perks = []
    women_events = []
    for ev in store_evs:
        cat = classify_event_for_comparison(ev)
        item = {
            "title": _clean_event_title(ev.get("title", "")),
            "full_title": ev.get("title", ""),
            "tag": ev.get("tag", "キャンペーン"),
            "url": ev.get("url", ""),
            "is_store_specific": bool(ev.get("store_name") or ev.get("target_stores")),
            "is_live_scraped": True,
        }
        if cat == "men_perk":
            men_perks.append(item)
        else:
            women_events.append(item)

    has_live_men_perk = len(men_perks) > 0
    has_live_women_event = len(women_events) > 0

    if not men_perks:
        men_perks.append({
            "title": spec["default_men_perk"],
            "full_title": spec["default_men_perk"],
            "tag": "常設特典",
            "url": "",
            "is_store_specific": False,
            "is_live_scraped": False,
        })
    if not women_events:
        women_events.append({
            "title": spec["default_women_perk"],
            "full_title": spec["default_women_perk"],
            "tag": "女性特典",
            "url": "",
            "is_store_specific": False,
            "is_live_scraped": False,
        })

    # Compute effective 60-min cost & live verdict
    # For JIS, if men > women, waiting 20 mins adds +440 yen and reduces actual match time
    wait_penalty = 0
    if spec["has_wait_fee"] and men > women and total > 0:
        wait_penalty = 440

    effective_weekday_60m = spec["est_60m_weekday"] + wait_penalty
    effective_weekend_60m = spec["est_60m_weekend"] + wait_penalty

    # Score for ranking "Best Value Right Now" within an area cluster (higher is better)
    # Factors: female headcount & ratio, lower base price, no wait penalty, active men's discount
    price_advantage = max(0, (4800 - effective_weekend_60m) // 25)
    event_bonus = 18 if has_live_men_perk else (10 if has_live_women_event else 0)
    if total > 0:
        ratio_score = (wpct - 50) * 1.8 + min(women, 45) * 1.1
        if women > men:
            ratio_score += 20
        elif spec["has_wait_fee"] and men > women + 4:
            ratio_score -= 22
        value_score = round(price_advantage + event_bonus + ratio_score, 1)
    else:
        value_score = round(price_advantage + event_bonus, 1)

    # Human-readable real-time verification verdict
    if total == 0:
        verdict_badge = "🌙 営業時間外・事前比較"
        verdict_cls = "verdict-neutral"
        verdict_comment = f"通常60分目安は平日{spec['est_60m_weekday_label']}／金土{spec['est_60m_weekend_label']}。夕方オープン後に現在人数とイベント集客効果が自動反映されます。"
    elif spec["has_wait_fee"] and women >= men and women >= 8:
        verdict_badge = "🔥 即相席で待機課金0円チャンス"
        verdict_cls = "verdict-hot"
        verdict_comment = f"JISは待機中に220円/10分がかかりますが、現在は女性{women}名（女性比{wpct}%）と女性優位のため待機課金なしで即相席しやすい絶好機です！"
    elif spec["has_wait_fee"] and men > women + 3:
        verdict_badge = "⚠️ 男性先行・待機課金に注意"
        verdict_cls = "verdict-warn"
        verdict_comment = f"現在男性が+{men - women}名先行中。JISは待機中も220円/10分が加算されるため、徒歩圏の待機無料店（オリエンタル/ag）との比較がおすすめです。"
    elif brand_raw == "ag" and women >= men and women >= 6:
        verdict_badge = "💎 単価最安クラス×女性優位"
        verdict_cls = "verdict-hot"
        verdict_comment = f"10分単価が安いagで、さらに現在女性{women}名（女性比{wpct}%）と女性リード中！今このエリアで最も実質コスパが高い状態です。"
    elif has_live_women_event and women >= 15 and wpct >= 55:
        verdict_badge = "✨ イベント集客効果◎（女性多数）"
        verdict_cls = "verdict-hot"
        verdict_comment = f"キャンペーン開催効果で実際に女性{women}名（女性比{wpct}%）が集まっています。男性も待ち時間なしで座りやすい狙い目です。"
    elif women > men:
        verdict_badge = f"⭕ 女性+{women - men}名リード・即相席圏内"
        verdict_cls = "verdict-good"
        verdict_comment = f"現在女性{women}名／男性{men}名（女性比{wpct}%）。待機時間が発生しにくく、通常料金どおりスマートに飲めるバランスです。"
    else:
        verdict_badge = "💡 混雑中・近隣店と料金比較推奨"
        verdict_cls = "verdict-neutral"
        verdict_comment = f"現在女性{women}名／男性{men}名。待機中は無料ですが、すぐ座りたい場合は下の近隣ライバル店の女性比率もチェックしてみてください。"

    return {
        "name": store_name,
        "seo_name": store_master.seo_name(store_name),
        "short_name": info.get("display_name") or store_name,
        "slug": store_master.slug_of(store_name),
        "area_label": info.get("area_label") or "全国",
        "area_slug": info.get("area_slug") or "",
        "brand_code": spec["brand_code"],
        "brand_label": spec["brand_label"],
        "brand_slug": spec["brand_slug"],
        "women": women,
        "men": men,
        "total": total,
        "wpct": wpct,
        "women_diff": w_diff,
        "men_diff": m_diff,
        "charge_str": spec["charge_str"],
        "wait_fee_str": spec["wait_fee_str"],
        "has_wait_fee": spec["has_wait_fee"],
        "weekday_10m_str": spec["weekday_10m_str"],
        "weekend_10m_str": spec["weekend_10m_str"],
        "est_60m_weekday": spec["est_60m_weekday"],
        "est_60m_weekend": spec["est_60m_weekend"],
        "est_60m_weekday_label": spec["est_60m_weekday_label"],
        "est_60m_weekend_label": spec["est_60m_weekend_label"],
        "age_range": spec["age_range"],
        "vibe_short": spec["vibe_short"],
        "men_perks": men_perks[:2],
        "women_events": women_events[:2],
        "has_live_men_perk": has_live_men_perk,
        "has_live_women_event": has_live_women_event,
        "value_score": value_score,
        "verdict_badge": verdict_badge,
        "verdict_cls": verdict_cls,
        "verdict_comment": verdict_comment,
        "is_best_in_cluster": False,
    }


def build_area_comparison_clusters(live_stores: list = None) -> dict:
    """Build all 9 area comparison clusters + live event verification summary."""
    live_map = {s.get("name"): s for s in (live_stores or [])}
    all_evs = []
    try:
        import events
        all_evs = events.fetch_all_events(allow_blocking=False)
    except Exception:
        all_evs = []

    clusters_out = []
    all_profiles_map = {}

    for cl in COMPARISON_CLUSTERS:
        profiles = []
        for sname in cl["stores"]:
            if not store_master.get(sname):
                continue
            prof = get_store_price_profile(
                sname,
                live_store=live_map.get(sname),
                all_events_Pool=all_evs,
            )
            profiles.append(prof)
            all_profiles_map[sname] = prof

        if profiles:
            # Crown the top cost-performance store in this cluster
            best_prof = max(profiles, key=lambda x: (x["total"] > 0, x["value_score"], x["women"]))
            best_prof["is_best_in_cluster"] = True

        clusters_out.append({
            "id": cl["id"],
            "tab_label": cl["tab_label"],
            "title": cl["title"],
            "desc": cl["desc"],
            "stores": profiles,
            "best_store": best_prof if profiles else None,
        })

    # Build the Event Verification Board (stores with active scraped events + live crowd proof)
    verified_event_stores = [
        p for p in all_profiles_map.values()
        if p["has_live_men_perk"] or p["has_live_women_event"]
    ]
    verified_event_stores.sort(
        key=lambda x: (x["has_live_men_perk"], x["women"], x["wpct"]),
        reverse=True,
    )
    if len(verified_event_stores) < 6:
        # Supplement with top live stores so the verification board is always informative
        fallback_extra = [
            p for p in sorted(all_profiles_map.values(), key=lambda x: (x["women"], x["value_score"]), reverse=True)
            if p not in verified_event_stores
        ]
        verified_event_stores.extend(fallback_extra[: (6 - len(verified_event_stores))])

    return {
        "clusters": clusters_out,
        "verified_event_stores": verified_event_stores[:8],
        "brand_specs": [BRAND_PRICE_SPECS["OLG"], BRAND_PRICE_SPECS["ag"], BRAND_PRICE_SPECS["JIS"]],
    }


def get_nearby_comparison_for_store(store_name: str, live_stores: list = None) -> dict:
    """Return the store's own price profile + all sibling stores in the same urban cluster
    for embedding directly on `/store/<slug>`."""
    live_map = {s.get("name"): s for s in (live_stores or [])}
    all_evs = []
    try:
        import events
        all_evs = events.fetch_all_events(allow_blocking=False)
    except Exception:
        all_evs = []

    target_cluster = None
    for cl in COMPARISON_CLUSTERS:
        if store_name in cl["stores"]:
            target_cluster = cl
            break

    if not target_cluster:
        self_prof = get_store_price_profile(store_name, live_store=live_map.get(store_name), all_events_Pool=all_evs)
        return {
            "self_profile": self_prof,
            "cluster_id": "shinjuku",
            "cluster_title": f"{self_prof['area_label']}エリアの料金・イベント比較",
            "stores": [self_prof],
        }

    profiles = []
    self_prof = None
    for sname in target_cluster["stores"]:
        if not store_master.get(sname):
            continue
        p = get_store_price_profile(sname, live_store=live_map.get(sname), all_events_Pool=all_evs)
        p["is_current_store"] = (sname == store_name)
        if sname == store_name:
            self_prof = p
        profiles.append(p)

    if profiles:
        best_p = max(profiles, key=lambda x: (x["total"] > 0, x["value_score"], x["women"]))
        best_p["is_best_in_cluster"] = True

    return {
        "self_profile": self_prof or (profiles[0] if profiles else None),
        "cluster_id": target_cluster["id"],
        "cluster_title": target_cluster["title"],
        "stores": profiles,
    }
