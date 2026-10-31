"""
concierge_matcher.py - 「今夜どこ行く？3秒穴場＆ハシゴ診断エンジン」

全国60店舗超の現在リアルタイム人数（live_stores）＋ 過去ログ統計ベンチマーク
（local_db.get_all_stores_live_benchmarks(allow_blocking=False)）＋
store_master の緯度経度（Haversine距離×1.3÷80m/分）＋ store_hours の営業判定を統合し、
ユーザー条件に合う「第1候補（本命）」「第2候補（徒歩ハシゴ保険）」「2軒目＆事前準備プラン」を
外部通信ゼロ・5ms未満で即座に算出するモジュール。
"""

import datetime
import math

import local_db
import price_comparison
import store_hours
import store_master

JST = datetime.timezone(datetime.timedelta(hours=9))

# 同一都市圏（徒歩・1駅ハシゴ圏内）とみなす最大直線距離（メートル）
MAX_WALKABLE_STRAIGHT_METERS = 3200.0

# 対応エリアプリセット（全国10エリア・国内60店舗超を網羅）
CONCIERGE_AREAS = {
    "shinjuku": {
        "key": "shinjuku",
        "label": "新宿・西新宿",
        "short_label": "新宿・西新宿",
        "portal_slug": "shinjuku",
        "stores": ["OLG 新宿", "JIS SHINJUKU", "JIS NISHISHINJUKU", "ASOBIBAR 新宿"],
        "second_venue": {
            "spot_title": "新宿三丁目・靖国通り裏／西新宿ガード下〜野村ビル周辺",
            "spots": "東口側なら歌舞伎町の喧騒を抜けた新宿三丁目の隠れ家ビストロやダイニングバー、西口側なら西新宿一丁目の落ち着いたオーセンティックバーが徒歩3〜5分で移動しやすく成功率が高めです。",
            "invite_phrase": "「ここ少し声が通りにくいから、歩いて3分の静かなバーで1杯だけ飲み直さない？」",
            "timing_advice": "相席開始から45〜60分後、2杯目のグラスが空く直前に提案すると終電前でも自然に抜け出せます。",
            "guide_url": "/guide/second-venue",
        },
    },
    "shibuya": {
        "key": "shibuya",
        "label": "渋谷・恵比寿",
        "short_label": "渋谷・恵比寿",
        "portal_slug": "shibuya",
        "stores": ["OLG 渋谷本店", "OLG ag渋谷", "OLG 恵比寿", "ASOBIBAR 渋谷", "ASOBIBAR 恵比寿"],
        "second_venue": {
            "spot_title": "渋谷・道玄坂上の夜カフェ＆バー／恵比寿西口・裏路地ワインバー",
            "spots": "渋谷ならセンター街の混雑を避けて道玄坂上・神泉寄りのソファカフェバーへ。恵比寿なら駅西口から徒歩3分の裏恵比寿ワインバーや恵比寿横丁脇の落ち着いたバルが鉄板です。",
            "invite_phrase": "「甘いものか美味しいワインある静かな夜カフェがすぐ近くにあるんだけど、少し寄っていかない？」",
            "timing_advice": "渋谷・恵比寿は夜カフェ文化が強いため『もう1軒飲む』より『夜パフェ・カフェに行く』口実が圧倒的に通りやすい街です。",
            "guide_url": "/guide/second-venue",
        },
    },
    "ueno_east": {
        "key": "ueno_east",
        "label": "上野・錦糸町・町田",
        "short_label": "上野・錦糸町・町田",
        "portal_slug": "tokyo-east",
        "stores": ["OLG 上野", "OLG ag上野", "OLG 錦糸町", "OLG 町田"],
        "second_venue": {
            "spot_title": "上野・仲町通り〜湯島寄り／錦糸町南口バル／原町田ダイニング",
            "spots": "上野は仲町通りから湯島方面へ1本入った静かなバー、錦糸町は南口ピアきんしちょう周辺や北口のイタリアンバル、町田は原町田大通り裏の隠れ家カフェバーが徒歩3分圏内です。",
            "invite_phrase": "「駅の近くに落ち着いて話せるバルがあるから、終電まであと30分だけ乾杯しようよ」",
            "timing_advice": "埼玉・千葉・神奈川方面への終電が23時台前半と少し早いため、22:30までに声をかけるのが鉄則です。",
            "guide_url": "/guide/second-venue",
        },
    },
    "yokohama_omiya": {
        "key": "yokohama_omiya",
        "label": "横浜・大宮・北関東",
        "short_label": "横浜・大宮・北関東",
        "portal_slug": "kanto",
        "stores": ["OLG 横浜", "OLG 大宮", "JIS OMIYA", "OLG 柏", "OLG 宇都宮", "OLG 高崎"],
        "second_venue": {
            "spot_title": "横浜西口・鶴屋町のおしゃれバル／大宮東口・南銀脇の個室ダイニング",
            "spots": "横浜なら南幸の雑踏から少し離れた鶴屋町・岡野方面のダイニングバー、大宮なら東口すずらん通り・一番街周辺の個室バルや落ち着いたショットバーが改札にも近くスマートです。",
            "invite_phrase": "「駅の改札すぐ近くに雰囲気いいバルがあるから、電車の時間まで1杯だけ行かない？」",
            "timing_advice": "ターミナル駅直結エリアなので『駅に近づく方向のお店』を指定すると相手の帰宅心理ハードルが一気に下がります。",
            "guide_url": "/guide/second-venue",
        },
    },
    "umeda": {
        "key": "umeda",
        "label": "大阪キタ（梅田・茶屋町・天満）",
        "short_label": "大阪・梅田",
        "portal_slug": "umeda",
        "stores": [
            "OLG 大阪駅前",
            "OLG ag梅田",
            "JIS UMEDA",
            "JIS CHAYAMACHI",
            "YATAKOI UMEDA",
            "ASOBIBAR 梅田",
            "ASOBIBAR 茶屋町",
            "OLG 天満",
        ],
        "second_venue": {
            "spot_title": "お初天神裏参道／茶屋町の夜景バー／天満・裏天満のおしゃれバル",
            "spots": "大阪駅前・お初天神エリアなら『お初天神裏参道』の活気あるバルや西梅田寄りのホテルライクなバー、茶屋町なら高層階や路地裏のカフェバー、天満なら提灯通り脇のワインバルが徒歩すぐです。",
            "invite_phrase": "「お初天神の裏にめっちゃ美味しいバルあるねんけど、サクッと1杯だけハシゴせえへん？」",
            "timing_advice": "キタは店舗数も2軒目候補も密集しているため、ダラダラ1店舗に居座るより50分で切り上げて外に出る方が会話のテンポが上がります。",
            "guide_url": "/guide/second-venue",
        },
    },
    "namba_kansai": {
        "key": "namba_kansai",
        "label": "難波・心斎橋・京都・神戸",
        "short_label": "難波・心斎橋・京神",
        "portal_slug": "namba",
        "stores": ["OLG 心斎橋", "OLG 難波", "JIS NAMBA", "ASOBIBAR 心斎橋", "OLG 京都", "OLG 神戸", "ASOBIBAR 神戸 三宮"],
        "second_venue": {
            "spot_title": "裏なんば・東心斎橋の隠れ家バー／京都先斗町・木屋町／神戸三宮・北野坂",
            "spots": "ミナミなら東心斎橋の落ち着いたフルーツカクテルバーや裏なんばのビストロ、京都なら木屋町から1本入った先斗町の町家バー、神戸なら三宮・北野坂沿いの大人なバーが好評です。",
            "invite_phrase": "「近くにフルーツカクテル美味しい静かなバーがあるから、デザート代わりに1杯行こう！」",
            "timing_advice": "心斎橋・難波は深夜帯も開いている路面バーが多いため、甘いカクテルや〆パフェを提案するとスムーズです。",
            "guide_url": "/guide/second-venue",
        },
    },
    "nagoya": {
        "key": "nagoya",
        "label": "名古屋（栄・錦）・中部",
        "short_label": "名古屋・中部",
        "portal_slug": "nagoya",
        "stores": ["OLG 名古屋 栄", "OLG 名古屋 錦", "OLG ag名古屋", "ASOBIBAR 名古屋 栄", "ASOBIBAR 名古屋 住吉", "OLG 静岡", "OLG 浜松", "OLG ag金沢"],
        "second_venue": {
            "spot_title": "栄三丁目・住吉のカジュアルバル／錦三丁目のオーセンティックバー",
            "spots": "栄・住吉側（ag名古屋・オリエンタルラウンジ名古屋栄）ならプリンセス大通り裏のダイニングバーや夜カフェ、錦側（オリエンタルラウンジ名古屋錦）なら錦三丁目の静かなバーが徒歩2〜4分です。",
            "invite_phrase": "「栄駅の近くに夜カフェっぽい落ち着いたバーがあるんだけど、少しだけ寄っていかない？」",
            "timing_advice": "地下鉄東山線・名城線の終電（23:45前後）を意識する女性が多いため、22:45頃までに移動を切り出すと成功率が高まります。",
            "guide_url": "/guide/second-venue",
        },
    },
    "fukuoka": {
        "key": "fukuoka",
        "label": "福岡（天神）・九州・沖縄",
        "short_label": "福岡・九州・沖縄",
        "portal_slug": "fukuoka",
        "stores": [
            "OLG 福岡",
            "JIS FUKUOKA",
            "ASOBIBAR 福岡 天神",
            "OLG 小倉",
            "OLG 熊本",
            "JIS KUMAMOTO",
            "OLG 長崎",
            "OLG 大分",
            "OLG 宮崎",
            "OLG 鹿児島",
            "OLG ag沖縄",
        ],
        "second_venue": {
            "spot_title": "天神・大名〜今泉の隠れ家バー＆カフェ／春吉リバーサイド",
            "spots": "福岡・天神大名エリアは、お店を出て徒歩2〜3分圏内に大名の路地裏バーや今泉の夜カフェが無数にあります。タクシーで3分の春吉・中洲川端エリアで夜景バーへ流れるのも人気ルートです。",
            "invite_phrase": "「大名に雰囲気めっちゃ良い夜カフェバーがあるっちゃけど、1杯だけ行ってみらん？」",
            "timing_advice": "天神・大名は全国で最も2軒目への物理的距離が近い街。徒歩2分以内のお店を事前に1つ決めておくだけでスムーズに合流できます。",
            "guide_url": "/guide/second-venue",
        },
    },
    "sapporo_sendai": {
        "key": "sapporo_sendai",
        "label": "札幌・仙台",
        "short_label": "札幌・仙台",
        "portal_slug": "hokkaido-tohoku",
        "stores": ["JIS SAPPORO", "OLG ag仙台"],
        "second_venue": {
            "spot_title": "札幌すすきの・シメパフェ＆バー／仙台国分町・稲荷小路の隠れ家バル",
            "spots": "札幌すすきのは名物『シメパフェ』専門店や駅前通り脇の落ち着いたショットバーが徒歩2分圏内。仙台は国分町通りから稲荷小路・虎屋横丁へ1本入った静かなワインバーが使いやすいです。",
            "invite_phrase": "「すぐ近くに有名なシメパフェ（落ち着いたバー）があるから、帰りに少し寄っていかない？」",
            "timing_advice": "冬場や雨雪の日は外を歩かせないことが最優先。同じビル周辺かアーケード・地下歩行空間に近いお店を選ぶと好印象です。",
            "guide_url": "/guide/second-venue",
        },
    },
    "hiroshima_okayama": {
        "key": "hiroshima_okayama",
        "label": "広島・岡山・松山",
        "short_label": "広島・岡山・松山",
        "portal_slug": "chugoku-shikoku",
        "stores": [
            "OLG ag広島",
            "ALFA HIROSHIMA",
            "CLOVERS HIROSHIMA",
            "OLG 岡山",
            "XIX OKAYAMA",
            "JIS MATSUYAMA",
        ],
        "second_venue": {
            "spot_title": "広島・新天地〜流川裏バー／岡山・磨屋町バル／松山・大街道〜二番町",
            "spots": "広島は新天地公園・並木通り寄りの落ち着いたカフェバー、岡山は桃太郎大通りから磨屋町・本町へ入ったバル、松山は大街道アーケードから二番町・三番町へ抜けたダイニングバーが徒歩3分圏内です。",
            "invite_phrase": "「並木通り（駅寄り）の方に静かで美味しいバーがあるから、あと1杯だけ乾杯しようよ」",
            "timing_advice": "歓楽街の呼び込みが多い通りを避け、大通りや並木通り・アーケード側の明るいルートで案内すると安心感を与えられます。",
            "guide_url": "/guide/second-venue",
        },
    },
}

# エリアスラッグ（portal_guides 側等）から CONCIERGE_AREAS キーへの柔軟なエイリアス解決
_AREA_ALIASES = {
    "shinjuku": "shinjuku",
    "nishishinjuku": "shinjuku",
    "shibuya": "shibuya",
    "ebisu": "shibuya",
    "ueno_east": "ueno_east",
    "ueno-east": "ueno_east",
    "tokyo-east": "ueno_east",
    "ueno": "ueno_east",
    "kinshicho": "ueno_east",
    "machida": "ueno_east",
    "yokohama_omiya": "yokohama_omiya",
    "omiya-yokohama": "yokohama_omiya",
    "kanto": "yokohama_omiya",
    "yokohama": "yokohama_omiya",
    "omiya": "yokohama_omiya",
    "umeda": "umeda",
    "osaka": "umeda",
    "osaka-kita": "umeda",
    "chayamachi": "umeda",
    "tenma": "umeda",
    "namba_kansai": "namba_kansai",
    "osaka-minami": "namba_kansai",
    "namba": "namba_kansai",
    "shinsaibashi": "namba_kansai",
    "kyoto": "namba_kansai",
    "kobe": "namba_kansai",
    "nagoya": "nagoya",
    "chubu": "nagoya",
    "sakae": "nagoya",
    "nishiki": "nagoya",
    "fukuoka": "fukuoka",
    "kyushu": "fukuoka",
    "tenjin": "fukuoka",
    "kumamoto": "fukuoka",
    "okinawa": "fukuoka",
    "sapporo_sendai": "sapporo_sendai",
    "hokkaido-tohoku": "sapporo_sendai",
    "sapporo-regional": "sapporo_sendai",
    "sapporo": "sapporo_sendai",
    "sendai": "sapporo_sendai",
    "hiroshima_okayama": "hiroshima_okayama",
    "hiroshima-okayama": "hiroshima_okayama",
    "chugoku-shikoku": "hiroshima_okayama",
    "hiroshima": "hiroshima_okayama",
    "okayama": "hiroshima_okayama",
    "matsuyama": "hiroshima_okayama",
}

# 個別店舗から最も粒度の細かいエリアガイドURL（/area/<slug>）を返すマップ
_STORE_FINE_AREA_SLUG = {
    "OLG 大阪駅前": "umeda",
    "OLG ag梅田": "umeda",
    "JIS UMEDA": "umeda",
    "JIS CHAYAMACHI": "umeda",
    "YATAKOI UMEDA": "umeda",
    "ASOBIBAR 梅田": "umeda",
    "ASOBIBAR 茶屋町": "umeda",
    "OLG 天満": "umeda",
    "OLG 心斎橋": "namba",
    "OLG 難波": "namba",
    "JIS NAMBA": "namba",
    "ASOBIBAR 心斎橋": "namba",
    "OLG 横浜": "yokohama",
    "OLG 大宮": "omiya",
    "JIS OMIYA": "omiya",
    "JIS SAPPORO": "sapporo",
    "OLG ag仙台": "sendai",
    "OLG ag広島": "hiroshima",
    "ALFA HIROSHIMA": "hiroshima",
    "CLOVERS HIROSHIMA": "hiroshima",
}

# 男性1名（ソロ）入店不可（2名以上必須・公式推奨）の店舗セット
_MEN_SOLO_RESTRICTED = {
    "OLG 恵比寿",
    "OLG 渋谷本店",
    "OLG ag渋谷",
    "OLG 新宿",
    "JIS SHINJUKU",
    "OLG ag上野",
    "OLG 大阪駅前",
    "OLG 名古屋 錦",
    "OLG ag名古屋",
}

# 大箱・グループ（3名以上）でも案内されやすい大型店舗セット
_LARGE_FLOOR_STORES = {
    "OLG 新宿",
    "JIS SHINJUKU",
    "OLG 渋谷本店",
    "OLG ag渋谷",
    "OLG 上野",
    "OLG 横浜",
    "OLG 大宮",
    "JIS OMIYA",
    "OLG 大阪駅前",
    "JIS UMEDA",
    "JIS CHAYAMACHI",
    "YATAKOI UMEDA",
    "OLG 心斎橋",
    "OLG 難波",
    "JIS NAMBA",
    "OLG 名古屋 栄",
    "OLG 名古屋 錦",
    "OLG 福岡",
    "JIS FUKUOKA",
    "JIS SAPPORO",
    "OLG ag仙台",
    "OLG ag広島",
}

# 大人め・落ち着いた会話向きの代表店舗
_CALM_ADULT_STORES = {
    "OLG 恵比寿",
    "JIS NISHISHINJUKU",
    "OLG 名古屋 錦",
    "JIS CHAYAMACHI",
    "OLG 大阪駅前",
    "OLG 京都",
    "OLG 神戸",
    "OLG 横浜",
    "OLG 大宮",
    "OLG 福岡",
    "OLG 岡山",
    "OLG 上野",
    "OLG 錦糸町",
}

# 20代前半・ワイワイ盛り上がり向きの代表店舗
_LIVELY_PARTY_STORES = {
    "OLG ag渋谷",
    "OLG 渋谷本店",
    "OLG 新宿",
    "JIS SHINJUKU",
    "OLG ag上野",
    "OLG ag梅田",
    "YATAKOI UMEDA",
    "JIS UMEDA",
    "JIS NAMBA",
    "OLG 難波",
    "OLG ag名古屋",
    "OLG ag仙台",
    "OLG ag広島",
    "CLOVERS HIROSHIMA",
    "ALFA HIROSHIMA",
    "OLG ag金沢",
    "OLG ag沖縄",
    "ASOBIBAR 新宿",
    "ASOBIBAR 渋谷",
    "ASOBIBAR 梅田",
    "ASOBIBAR 茶屋町",
    "ASOBIBAR 心斎橋",
    "ASOBIBAR 名古屋 栄",
    "ASOBIBAR 名古屋 住吉",
    "ASOBIBAR 神戸 三宮",
    "ASOBIBAR 福岡 天神",
}


def normalize_concierge_area(area: str) -> str:
    clean = (area or "").strip().lower()
    if clean in CONCIERGE_AREAS:
        return clean
    return _AREA_ALIASES.get(clean, "shinjuku")


def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """2地点の緯度経度からHaversine直線距離（メートル）を返す。"""
    r_earth = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    return 2.0 * r_earth * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def estimate_walk_minutes(store_a: str, store_b: str):
    """
    2店舗間の推定徒歩分数（直線距離×1.3 ÷ 80m/分）を返す。
    同一都市圏外（直線3.2km超）または座標未登録時は None を返す。
    """
    if not store_a or not store_b or store_a == store_b:
        return None
    info_a = store_master.get(store_a)
    info_b = store_master.get(store_b)
    if not info_a or not info_b:
        return None
    lat1, lng1 = info_a.get("lat"), info_a.get("lng")
    lat2, lng2 = info_b.get("lat"), info_b.get("lng")
    if not isinstance(lat1, (int, float)) or not isinstance(lng1, (int, float)):
        return None
    if not isinstance(lat2, (int, float)) or not isinstance(lng2, (int, float)):
        return None
    dist_m = haversine_meters(float(lat1), float(lng1), float(lat2), float(lng2))
    if dist_m > MAX_WALKABLE_STRAIGHT_METERS:
        return None
    return max(1, int(round((dist_m * 1.3) / 80.0)))


def _is_men_solo_allowed(sname: str, is_weekend_like: bool) -> bool:
    if sname in _MEN_SOLO_RESTRICTED:
        return False
    if sname == "OLG ag仙台" and is_weekend_like:
        return False
    return True


def _brand_display_label(sname: str, info: dict) -> str:
    brand = (info or {}).get("brand", "")
    if brand == "OLG":
        return "オリエンタルラウンジ"
    if brand == "ag":
        return "ag（アグ）"
    if brand == "JIS":
        return "JIS（ジス）"
    if brand == "XIX":
        return "相席ラウンジXIX"
    if brand == "ヤタコイ":
        return "ヤタコイ"
    if brand == "CLOVERS":
        return "相席CLOVERS"
    if brand == "ALFA":
        return "相席ラウンジALFA"
    if brand == "ASOBIBAR":
        return "ASOBIBAR（アソビバー）"
    return "オリエンタルラウンジ" if sname.startswith("OLG ") else (brand or "相席ラウンジ")


def _store_area_url(sname: str, info: dict, fallback_portal_slug: str) -> str:
    fine = _STORE_FINE_AREA_SLUG.get(sname)
    if fine:
        return f"/area/{fine}"
    mslug = (info or {}).get("area_slug") or fallback_portal_slug or "shinjuku"
    return f"/area/{mslug}"


def _evaluate_store_candidate(
    sname: str,
    live_entry: dict,
    bm: dict,
    now_jst: datetime.datetime,
    audience: str,
    timing: str,
    party_size: str,
    vibe: str,
    fallback_portal_slug: str,
    is_weekend_like: bool,
) -> dict:
    info = store_master.get(sname) or {}
    p_spec = price_comparison.STORE_PRICE_TABLE.get(sname) or {}
    b_code = info.get("brand") or "OLG"
    b_spec = price_comparison.BRAND_PRICE_SPECS.get(b_code) or {}

    w = max(0, int((live_entry or {}).get("women") or 0))
    m = max(0, int((live_entry or {}).get("men") or 0))
    tot = w + m
    w_diff = int((live_entry or {}).get("women_diff") or 0)
    m_diff = int((live_entry or {}).get("men_diff") or 0)
    female_pct = int(round((w / tot) * 100)) if tot > 0 else 0

    h_status = store_hours.get_business_hours_status(sname, now_jst=now_jst, total_guests=tot)
    is_open = bool(h_status.get("is_open_now"))
    today_hours = h_status.get("today_hours_label") or "18:00〜翌5:00"
    sched = h_status.get("schedule_spec") or {}
    close_h = (sched.get("fs" if is_weekend_like else "mt") or [18, 29])[1]

    # ベンチマーク統計値（非ブロッキング取得・未計測時は堅牢な推定デフォルト）
    usual_w = bm.get("usual_women")
    usual_slot_label = bm.get("usual_slot_label") or "普段の同時間帯"
    usual_w_diff = round(w - float(usual_w), 1) if (usual_w is not None and tot > 0) else None

    club_kw = ("梅田", "茶屋町", "心斎橋", "難波", "渋谷", "新宿", "栄", "名古屋 錦", "名古屋錦", "福岡", "札幌", "UMEDA", "CHAYAMACHI", "NAMBA", "SHIBUYA", "SHINJUKU", "FUKUOKA", "SAPPORO")
    is_club_zone = any(k in sname.upper() for k in club_kw)
    mid_ret = bm.get("midnight_retention_pct")
    if mid_ret is None:
        mid_ret = 84 if is_club_zone else 58
    else:
        mid_ret = int(mid_ret)

    core_short = bm.get("core_time_short") or "20-22時"
    core_wpct = int(bm.get("core_time_wpct") or 57)
    peak_avg_w = float(bm.get("peak_band_avg_w") or (usual_w if usual_w else 16.0))

    solo_allowed_men = _is_men_solo_allowed(sname, is_weekend_like)
    solo_rule_str = p_spec.get("solo_rule") or b_spec.get("solo_rule") or "店舗規定に準拠"
    vibe_short = p_spec.get("vibe_short") or b_spec.get("vibe_short") or "人気の定番ラウンジ"
    age_range = p_spec.get("age_range") or b_spec.get("age_range") or "20代〜30代前半"
    est_price_lbl = (
        (p_spec.get("est_60m_weekend_label") or b_spec.get("est_60m_weekend_label"))
        if is_weekend_like
        else (p_spec.get("est_60m_weekday_label") or b_spec.get("est_60m_weekday_label"))
    ) or "約3,600円〜5,280円"

    # ---- スコア計算 ----
    raw_score = 0.0

    if audience == "women":
        # 女性目線：男性先行（受付待ちゼロ・即ソファ着席）＋店舗快適度を最重視
        if timing == "now":
            if is_open:
                raw_score += 12.0
            else:
                raw_score -= 22.0
            if tot > 0:
                raw_score += min(24.0, m * 0.95)
                male_lead = m - w
                if male_lead >= 2 and m >= 6:
                    raw_score += min(18.0, 8.0 + male_lead * 0.7)
                elif tot >= 8 and 35 <= female_pct <= 52:
                    raw_score += 8.0
                elif female_pct >= 66 and w >= 12:
                    # 女性過多による受付待機リスク
                    raw_score -= 16.0
                if m_diff > 0:
                    raw_score += min(5.0, m_diff * 1.1)
            else:
                raw_score += min(14.0, peak_avg_w * 0.45)
        elif timing == "prime_21":
            raw_score += min(14.0, m * 0.45) + min(14.0, peak_avg_w * 0.5)
            if tot >= 8 and female_pct >= 68:
                raw_score -= 8.0
            elif tot >= 6 and m >= w:
                raw_score += 8.0
        else:  # late_23
            raw_score += (mid_ret - 60) * 0.32
            if close_h <= 26:
                raw_score -= 14.0
            else:
                raw_score += 7.0
            if tot > 0 and m >= 5:
                raw_score += min(14.0, m * 0.6)

        # 女性快適度（内装・スイーツ特典）
        if b_code == "OLG":
            raw_score += 6.0
        elif b_code in ("ag", "JIS"):
            raw_score += 4.5
        else:
            raw_score += 3.5

        if party_size == "group" and sname in _LARGE_FLOOR_STORES:
            raw_score += 9.0
        elif party_size == "solo":
            # 女性ソロは落ち着いたバー型や大型店が馴染みやすい
            if b_code == "JIS" or sname in _CALM_ADULT_STORES:
                raw_score += 6.0

    else:
        # 男性目線：女性実数（回転余地）＋女性比率50%以上（入店待ち＆待機課金回避）を最重視
        if timing == "now":
            if is_open:
                raw_score += 12.0
            else:
                raw_score -= 24.0
            if tot > 0:
                raw_score += min(26.0, w * 0.85)
                if w >= 12:
                    raw_score += 7.0
                elif w < 6:
                    raw_score -= 7.0
                if tot >= 6:
                    raw_score += (female_pct - 50) * 0.85
                    if female_pct < 42:
                        raw_score -= 9.0  # 男性過多・入店待ちリスク
                if w_diff > 0:
                    raw_score += min(6.0, w_diff * 1.2)
                if usual_w_diff is not None and usual_w_diff >= 3:
                    raw_score += 4.5
            else:
                raw_score += min(15.0, peak_avg_w * 0.55) + (core_wpct - 50) * 0.45
        elif timing == "prime_21":
            live_part = (min(16.0, w * 0.55) + ((female_pct - 50) * 0.45 if tot >= 6 else 0.0)) if tot > 0 else 6.0
            hist_part = min(16.0, peak_avg_w * 0.55) + (core_wpct - 50) * 0.55
            raw_score += live_part * 0.45 + hist_part * 0.55 + 6.0
        else:  # late_23
            raw_score += (mid_ret - 60) * 0.48
            if close_h <= 26:
                raw_score -= 16.0  # 翌1〜2時閉店は23時以降ハシゴに不向き
            elif close_h >= 30:
                raw_score += 7.0   # 翌6〜7時営業の深夜強い店舗に加点
            if tot > 0:
                raw_score += min(15.0, w * 0.5) + ((female_pct - 50) * 0.4 if tot >= 6 else 0.0)
            else:
                raw_score += min(10.0, peak_avg_w * 0.35)

        # 男性人数条件（ソロ入店規定の厳密反映！）
        if party_size == "solo":
            if solo_allowed_men:
                raw_score += 16.0
                if b_code in ("JIS", "ASOBIBAR", "ヤタコイ", "XIX", "CLOVERS", "ALFA"):
                    raw_score += 4.0
            else:
                raw_score -= 36.0
        elif party_size == "group":
            if sname in _LARGE_FLOOR_STORES:
                raw_score += 11.0
            else:
                raw_score -= 3.0
        else:  # pair
            raw_score += 4.0

    # 雰囲気（vibe）補正（男女共通）
    if vibe == "lively":
        if b_code in ("ag", "ヤタコイ") or sname in _LIVELY_PARTY_STORES:
            raw_score += 14.0
        elif sname in _CALM_ADULT_STORES:
            raw_score -= 4.0
    elif vibe == "calm":
        if sname in _CALM_ADULT_STORES:
            raw_score += 15.0
        elif b_code == "OLG":
            raw_score += 9.0
        elif b_code in ("ag", "ヤタコイ"):
            raw_score -= 6.0

    final_score = int(max(45, min(98, round(56.0 + raw_score * 0.72))))
    display_name = store_master.seo_name(sname)
    slug = store_master.slug_of(sname)

    # ---- 見出し理由（headline_reason）と3つの数値根拠（reasons）の組み立て ----
    reasons = []
    if audience == "women":
        if tot > 0 and m >= w and m >= 5:
            headline = f"現在 男性{m}名・女性{w}名（男性先行）で受付待ち0分・即着席しやすい好条件"
            r1 = f"リアルタイム人数は男性{m}名 vs 女性{w}名（女性比{female_pct}%）。男性が先行して待機しているため、受付待ちゼロで広いソファ席へ案内されやすい状態です。"
        elif tot > 0:
            headline = f"現在 男性{m}名・女性{w}名稼働中！フロア活気と快適度のバランスが良好"
            r1 = f"現在の店内稼働は計{tot}名（男性{m}名・女性{w}名／女性比{female_pct}%）。極端な女性過多による長時間待機の心配が少ない水準です。"
        else:
            headline = f"本日 {today_hours} 営業｜女性完全無料＆快適度の高い定番人気ラウンジ"
            r1 = f"現在は営業時間外または集計立ち上がり前（本日 {today_hours}）。ピーク帯は平均女性{peak_avg_w:.0f}名規模で賑わう安定店舗です。"
        reasons.append(r1)

        if timing == "late_23":
            reasons.append(f"23時以降の終電後定着率は{mid_ret}%（本日 {today_hours}）。2軒目・深夜帯からでも男性客が途切れにくく安心して楽しめます。")
        else:
            reasons.append(f"コアタイム（{core_short}）の稼働が安定しており、女性チャージ・飲み放題0円に加えて季節フードや限定特典も充実しています。")

        reasons.append(f"主な客層は{age_range}（{vibe_short}）。不快な席に当たった際もスタッフへ席替え相談がしやすい安心運営です。")

    else:
        # 男性向け根拠3項目
        if tot > 0 and w >= 8 and female_pct >= 52:
            headline = f"女性{w}名・女性比{female_pct}%！男性待ちを回避して席替えも狙える今夜の本命穴場"
        elif tot > 0 and tot >= 6:
            headline = f"現在 女性{w}名・男性{m}名（女性比{female_pct}%）｜エリア内で最もバランス優位な第1候補"
        elif is_open:
            headline = f"営業中（本日 {today_hours}）｜ピーク平均女性{peak_avg_w:.0f}名・コア帯女性比{core_wpct}%の実力店"
        else:
            headline = f"本日 {today_hours} 営業｜過去ログ分析でコア帯（{core_short}）女性比{core_wpct}%の本命店"

        if tot > 0:
            diff_note = ""
            if usual_w_diff is not None and abs(usual_w_diff) >= 1.0:
                sign = "+" if usual_w_diff > 0 else ""
                diff_note = f"（{usual_slot_label}平均比 {sign}{usual_w_diff:.0f}名）"
            elif w_diff > 0:
                diff_note = f"（直近比 +{w_diff}名増加中）"
            reasons.append(
                f"現在 女性{w}名 : 男性{m}名（計{tot}名・女性比率{female_pct}%）{diff_note}。単独待機課金や男性入店待ちリスクを抑えやすい数値バランスです。"
            )
        else:
            reasons.append(
                f"過去30日ログ統計でピーク帯平均女性{peak_avg_w:.0f}名・コアタイム（{core_short}）平均女性比{core_wpct}%を記録している実績上位店です。"
            )

        if party_size == "solo":
            if solo_allowed_men:
                reasons.append(f"男性1名（ソロ）参戦に対応（{solo_rule_str}）。終電後定着率{mid_ret}%で1人でも合流・回転しやすい環境です。")
            else:
                reasons.append(f"※注意：公式規定は「{solo_rule_str}」です。1人で向かう場合は第2候補のソロ対応店もあわせてご確認ください。")
        elif timing == "late_23":
            reasons.append(f"23時以降の終電後定着率は{mid_ret}%（本日 {today_hours}）。終電崖の後も2回転目の女性流入が期待できる深夜強い店舗です。")
        else:
            reasons.append(f"狙い目コアタイムは{core_short}（平均女性比{core_wpct}%・終電後定着率{mid_ret}%）。60分相席目安は{est_price_lbl}です。")

        reasons.append(f"客層は{age_range}（{vibe_short}）。希望条件に最も噛み合うフロア空気感です。")

    return {
        "raw_name": sname,
        "display_name": display_name,
        "brand_label": _brand_display_label(sname, info),
        "slug": slug,
        "store_url": f"/store/{slug}",
        "area_url": _store_area_url(sname, info, fallback_portal_slug),
        "women": w,
        "men": m,
        "total": tot,
        "female_pct": female_pct,
        "women_diff": w_diff,
        "men_diff": m_diff,
        "is_open_now": is_open,
        "today_hours_label": today_hours,
        "score": final_score,
        "headline_reason": headline,
        "reasons": reasons[:3],
        "usual_w_diff": usual_w_diff,
        "usual_slot_label": usual_slot_label,
        "midnight_retention_pct": mid_ret,
        "core_time_short": core_short,
        "solo_ok": solo_allowed_men,
        "solo_rule_short": solo_rule_str,
        "age_range": age_range,
        "vibe_short": vibe_short,
        "price_60m_label": est_price_lbl,
        "_raw_tiebreak": (final_score, w if audience == "men" else m, -m if audience == "men" else -w),
    }


def _select_backup_candidate(primary: dict, rest_candidates: list, audience: str, party_size: str) -> dict:
    """
    第1候補（primary）に対する最適な「徒歩ハシゴ保険（backup）」を選出する。
    同一都市圏（直線3.2km以内）に候補店舗が存在する場合は、実際に歩いてハシゴできる店舗を最優先する。
    """
    if not rest_candidates:
        return None

    p_name = primary["raw_name"]
    walkable = []
    non_walkable = []
    for cand in rest_candidates:
        w_min = estimate_walk_minutes(p_name, cand["raw_name"])
        c_copy = dict(cand)
        c_copy["walk_minutes_from_primary"] = w_min
        if w_min is not None:
            walkable.append(c_copy)
        else:
            non_walkable.append(c_copy)

    # 男性ソロ時に万一 primary がソロ制限店だった場合、ソロOK店を最優先バックアップにする
    if audience == "men" and party_size == "solo":
        walkable.sort(key=lambda x: (bool(x.get("solo_ok")), x["score"], -(x["walk_minutes_from_primary"] or 99)), reverse=True)
        non_walkable.sort(key=lambda x: (bool(x.get("solo_ok")), x["score"]), reverse=True)
    else:
        walkable.sort(key=lambda x: (x["score"], -(x["walk_minutes_from_primary"] or 99)), reverse=True)
        non_walkable.sort(key=lambda x: x["score"], reverse=True)

    chosen = walkable[0] if walkable else non_walkable[0]
    w_min = chosen.get("walk_minutes_from_primary")

    if audience == "women":
        if w_min is not None:
            chosen["switch_trigger"] = (
                f"もし本命店の受付に女性ペアが2組以上並んでいたら、徒歩{w_min}分の『{chosen['display_name']}』へ即移動すると待ち時間ゼロで座れます"
            )
        else:
            chosen["switch_trigger"] = (
                f"同エリア内の比較第2候補。本命店が女性過多（女性比65%超）の夜はこちらのリアルタイム男性数をチェック"
            )
    else:
        if w_min is not None:
            if party_size == "solo" and chosen.get("solo_ok") and not primary.get("solo_ok"):
                chosen["switch_trigger"] = (
                    f"1人参戦なら徒歩{w_min}分の『{chosen['display_name']}』が男性ソロ入店OKで最も確実です"
                )
            else:
                chosen["switch_trigger"] = (
                    f"もし本命店の男性比率が55%を超えたら（または受付で15分以上待つ案内なら）、徒歩{w_min}分のこちらへ即移動が鉄則です"
                )
        else:
            chosen["switch_trigger"] = (
                f"広域エリア内の第2有力候補。向かう路線や現在地に近い場合は『{chosen['display_name']}』の男女比を優先チェック"
            )

    chosen.pop("_raw_tiebreak", None)
    return chosen


def _build_area_summary(evaluated: list, audience: str) -> dict:
    total_w = sum(x["women"] for x in evaluated)
    total_m = sum(x["men"] for x in evaluated)
    total_g = total_w + total_m
    open_cnt = sum(1 for x in evaluated if x["is_open_now"])
    avg_fpct = int(round((total_w / total_g) * 100)) if total_g > 0 else 50

    if total_g == 0 and open_cnt == 0:
        badge = "🌙 営業時間外・今夜の事前作戦タイム"
        level = "off_hours"
        comment = "現在は営業時間外です。過去30日の時間帯統計をベースに今夜の本命＆ハシゴルートを算出しています。"
    elif total_g == 0:
        badge = "✨ オープン直後・1軒目早割チャンス"
        level = "early"
        comment = "営業開始直後の立ち上がり時間帯です。20時前後の入店は早割単価でコストを抑えやすくなります。"
    elif audience == "women":
        if total_m >= total_w:
            badge = "💎 男性先行エリア・女性待ち0分チャンス"
            level = "good"
            comment = f"エリア全体で男性{total_m}名 vs 女性{total_w}名と男性が先行中！受付待ちなしで即案内されやすい好環境です。"
        elif avg_fpct >= 65:
            badge = "⚠️ 女性集中気味・男性先行店の厳選推奨"
            level = "caution"
            comment = f"エリア全体で女性比{avg_fpct}%と女性ペアが集中しています。受付待ちを避けるため本命店の男性数を必ず確認してください。"
        else:
            badge = "⚖️ 男女バランス良好・快適稼働中"
            level = "balanced"
            comment = f"エリア合計{total_g}名（女性{total_w}名・男性{total_m}名）が稼働中。スムーズに相席しやすい時間帯です。"
    else:
        if avg_fpct >= 55 and total_w >= 10:
            badge = "🔥 女性優位ボーナスタイム・今すぐ狙い目"
            level = "good"
            comment = f"エリア全体で女性{total_w}名・女性比{avg_fpct}%と女性優位！男性の入店待ちが少なく席替えも狙いやすい好況です。"
        elif avg_fpct <= 43 and total_g >= 12:
            badge = "⚠️ 男性過多注意・穴場ピンポイント選択必須"
            level = "caution"
            comment = f"エリア全体では男性{total_m}名に対し女性{total_w}名（女性比{avg_fpct}%）とやや男余り傾向。本命店と第2候補の見極めが重要です。"
        else:
            badge = "⚖️ 男女バランス安定・店舗間ギャップ注目"
            level = "balanced"
            comment = f"エリア合計{total_g}名（女性{total_w}名・男性{total_m}名／女性比{avg_fpct}%）。店舗ごとの男女比の偏りを突くのが有効です。"

    return {
        "store_count": len(evaluated),
        "open_store_count": open_cnt,
        "total_women": total_w,
        "total_men": total_m,
        "total_guests": total_g,
        "avg_female_pct": avg_fpct,
        "crowd_badge": badge,
        "crowd_level": level,
        "status_comment": comment,
    }


def _build_smart_action_card(audience: str, timing: str, area_summary: dict, primary: dict) -> dict:
    """
    スタッフ非表示時にのみ描画する、ユーザー状況に寄り添った実用準備＆代替プラン導線メタ情報。
    """
    if audience == "women":
        return {
            "mode": "women_comfort",
            "kicker": "💄 女子組の快適ラウンジ＆1対1デート活用メモ",
            "title": "角が立たない席替えフレーズ＆『審査制カフェデート』という選択肢",
            "body": "微妙な席に当たったときの自然な抜け方は『女子向け裏ガイド』をチェック。また、大人数の飲み会よりハイスペック男性と落ち着いて1対1でお茶したい週は、女性完全無料・顔写真非公開の審査制デートアプリを併用する女性が増えています。",
            "primary_label": "審査制・1対1デート（女性無料）を見る",
            "primary_url": "/link/women_dating?from=concierge",
            "secondary_label": "💄 女子向け裏ガイド＆席替え術を読む",
            "secondary_url": "/for-women",
        }

    is_off_or_tough = (
        area_summary["crowd_level"] in ("off_hours", "caution")
        or (primary.get("total", 0) > 0 and primary.get("women", 0) < 4)
    )
    if is_off_or_tough:
        return {
            "mode": "men_backup_app",
            "kicker": "💡 男余り・営業時間外・待機課金のスマート回避策",
            "title": "ラウンジが空振りしやすい夜は『今日これから近くで飲めるアプリ』も併用",
            "body": "エリア全体で男性比率が高すぎるときや営業時間外に無理して単独待機料金（10分220〜330円）を払うより、近くで今夜これから合流できる恋活アプリを保険に持っておく方が圧倒的にコスパ良く出会えます。",
            "primary_label": "今日これから近くで飲めるアプリを見る",
            "primary_url": "/link/dating_instant?from=concierge",
            "secondary_label": "👔 相席で好印象な服装・事前準備ガイド",
            "secondary_url": "/grooming",
        }

    return {
        "mode": "men_grooming_prep",
        "kicker": "✨ ソファ席70cmで差がつく入店5分前の最終準備",
        "title": "最初の10分で席替えされない『夕方の皮脂テカリ＆清潔感ケア』",
        "body": "オリエンタルラウンジやJISの間接照明は、距離70cmのソファ席で顔まわりのテカリや乾燥が意外と目立ちます。入店前にサッと整えておくだけで1組目からの会話継続率が大きく変わります。",
        "primary_label": "メンズ清潔感スキンケア（初回セット）を見る",
        "primary_url": "/link/orbis?from=concierge",
        "secondary_label": "👔 浮かない服装・身だしなみ特集を読む",
        "secondary_url": "/grooming",
    }


def diagnose_tonight_plan(
    live_stores: list,
    area: str = "shinjuku",
    audience: str = "men",
    timing: str = "now",
    party_size: str = "pair",
    vibe: str = "any",
    now_jst: datetime.datetime = None,
) -> dict:
    """
    ユーザー条件（エリア・目線・時間帯・人数・雰囲気）から
    今夜の「第1候補（primary）」「徒歩ハシゴ第2候補（backup）」「2軒目＆準備プラン」を即座に返す。
    """
    area_key = normalize_concierge_area(area)
    area_cfg = CONCIERGE_AREAS[area_key]

    aud = "women" if str(audience or "").strip().lower() == "women" else "men"
    tim = str(timing or "").strip().lower()
    if tim not in ("now", "prime_21", "late_23"):
        tim = "now"
    psize = str(party_size or "").strip().lower()
    if psize not in ("solo", "pair", "group"):
        psize = "pair"
    vb = str(vibe or "").strip().lower()
    if vb not in ("any", "lively", "calm"):
        vb = "any"

    if now_jst is None:
        now_jst = datetime.datetime.now(JST)
    elif now_jst.tzinfo is None:
        now_jst = now_jst.replace(tzinfo=JST)
    else:
        now_jst = now_jst.astimezone(JST)

    now_naive = now_jst.replace(tzinfo=None)
    biz_date = (now_naive.date() - datetime.timedelta(days=1)) if now_naive.hour < 12 else now_naive.date()
    next_iso = (biz_date + datetime.timedelta(days=1)).isoformat()
    is_weekend_like = (biz_date.weekday() in (4, 5)) or (next_iso in store_hours.JP_HOLIDAYS)

    live_map = {}
    for s in (live_stores or []):
        if isinstance(s, dict) and s.get("name"):
            live_map[s["name"]] = s

    try:
        benchmarks = local_db.get_all_stores_live_benchmarks(now_jst=now_naive, allow_blocking=False) or {}
    except Exception:
        benchmarks = {}

    portal_slug = area_cfg["portal_slug"]
    evaluated = []
    for sname in area_cfg["stores"]:
        cand = _evaluate_store_candidate(
            sname=sname,
            live_entry=live_map.get(sname) or {},
            bm=benchmarks.get(sname) or {},
            now_jst=now_jst,
            audience=aud,
            timing=tim,
            party_size=psize,
            vibe=vb,
            fallback_portal_slug=portal_slug,
            is_weekend_like=is_weekend_like,
        )
        evaluated.append(cand)

    evaluated.sort(key=lambda x: x["_raw_tiebreak"], reverse=True)

    primary = dict(evaluated[0])
    primary.pop("_raw_tiebreak", None)

    rest = evaluated[1:]
    backup = _select_backup_candidate(primary, rest, audience=aud, party_size=psize)

    other_candidates = []
    backup_raw = backup["raw_name"] if backup else None
    for item in rest:
        if item["raw_name"] == backup_raw:
            continue
        c_clean = dict(item)
        c_clean.pop("_raw_tiebreak", None)
        c_clean["walk_minutes_from_primary"] = estimate_walk_minutes(primary["raw_name"], c_clean["raw_name"])
        other_candidates.append(c_clean)

    area_summary = _build_area_summary(evaluated, audience=aud)
    smart_action = _build_smart_action_card(aud, tim, area_summary, primary)

    return {
        "ok": True,
        "area_key": area_key,
        "area_label": area_cfg["label"],
        "area_portal_url": f"/area/{portal_slug}",
        "audience": aud,
        "timing": tim,
        "party_size": psize,
        "vibe": vb,
        "generated_at": now_jst.strftime("%Y-%m-%d %H:%M"),
        "primary": primary,
        "backup": backup,
        "other_candidates": other_candidates,
        "area_summary": area_summary,
        "second_venue_tip": dict(area_cfg["second_venue"]),
        "smart_action_card": smart_action,
        "available_areas": [
            {
                "key": k,
                "label": v["label"],
                "short_label": v["short_label"],
                "portal_url": f"/area/{v['portal_slug']}",
            }
            for k, v in CONCIERGE_AREAS.items()
        ],
    }
