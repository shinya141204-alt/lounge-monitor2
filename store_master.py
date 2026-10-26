"""
store_master.py - 店舗マスタ（static/store_master.json）の読み込み＆SEO名称・スラッグ変換

店舗ごとの地域・エリア表示・座標・マップ検索の起点は、この1ファイルを正とします。
フロントエンド（static/app.js）も同じ JSON を読み込みます。
"""
import os
import re
import json
import threading
import urllib.parse

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'store_master.json')
_lock = threading.Lock()
_cache = None

STORE_SLUGS = {
    "OLG 渋谷本店": "oriental-lounge-shibuya-honten",
    "OLG 渋谷駅前": "oriental-lounge-shibuya-ekimae",
    "OLG 恵比寿": "oriental-lounge-ebisu",
    "OLG ag渋谷": "ag-shibuya",
    "OLG 新宿": "oriental-lounge-shinjuku",
    "OLG ag新宿": "ag-shinjuku",
    "OLG 上野": "oriental-lounge-ueno",
    "OLG ag上野": "ag-ueno",
    "OLG 錦糸町": "oriental-lounge-kinshicho",
    "OLG 柏": "oriental-lounge-kashiwa",
    "OLG 町田": "oriental-lounge-machida",
    "OLG 横浜": "oriental-lounge-yokohama",
    "OLG 大宮": "oriental-lounge-omiya",
    "OLG 宇都宮": "oriental-lounge-utsunomiya",
    "OLG 高崎": "oriental-lounge-takasaki",
    "OLG 大阪駅前": "oriental-lounge-osaka-ekimae",
    "OLG 梅田": "oriental-lounge-umeda",
    "OLG 茶屋町": "oriental-lounge-chayamachi",
    "OLG ag梅田": "ag-umeda",
    "OLG 天満": "oriental-lounge-tenma",
    "OLG 心斎橋": "oriental-lounge-shinsaibashi",
    "OLG 難波": "oriental-lounge-namba",
    "OLG 京都": "oriental-lounge-kyoto",
    "OLG 神戸": "oriental-lounge-kobe",
    "OLG 岡山": "oriental-lounge-okayama",
    "OLG 広島": "oriental-lounge-hiroshima",
    "OLG ag広島": "ag-hiroshima",
    "OLG ag名古屋": "ag-nagoya",
    "OLG 名古屋 錦": "oriental-lounge-nagoya-nishiki",
    "OLG 名古屋 栄": "oriental-lounge-nagoya-sakae",
    "OLG 静岡": "oriental-lounge-shizuoka",
    "OLG 浜松": "oriental-lounge-hamamatsu",
    "OLG 金沢": "oriental-lounge-kanazawa",
    "OLG ag金沢": "ag-kanazawa",
    "OLG 仙台": "oriental-lounge-sendai",
    "OLG ag仙台": "ag-sendai",
    "OLG 札幌": "oriental-lounge-sapporo",
    "OLG ag札幌": "ag-sapporo",
    "OLG 福岡": "oriental-lounge-fukuoka",
    "OLG ag福岡": "ag-fukuoka",
    "OLG 小倉": "oriental-lounge-kokura",
    "OLG 長崎": "oriental-lounge-nagasaki",
    "OLG 大分": "oriental-lounge-oita",
    "OLG 熊本": "oriental-lounge-kumamoto",
    "OLG 宮崎": "oriental-lounge-miyazaki",
    "OLG 鹿児島": "oriental-lounge-kagoshima",
    "OLG 沖縄": "oriental-lounge-okinawa",
    "OLG ag沖縄": "ag-okinawa",
    "OLG ソウル カンナム": "oriental-lounge-seoul-gangnam",
    "OLG ソウル ホンデ": "oriental-lounge-seoul-hongdae",
    "JIS SAPPORO": "jis-sapporo",
    "JIS OMIYA": "jis-omiya",
    "JIS SHINJUKU": "jis-shinjuku",
    "JIS NISHISHINJUKU": "jis-nishishinjuku",
    "JIS UMEDA": "jis-umeda",
    "JIS NAMBA": "jis-namba",
    "JIS CHAYAMACHI": "jis-chayamachi",
    "JIS FUKUOKA": "jis-fukuoka",
    "JIS KUMAMOTO": "jis-kumamoto",
    "JIS MATSUYAMA": "jis-matsuyama",
    "XIX OKAYAMA": "xix-okayama",
    "YATAKOI UMEDA": "yatakoi-umeda",
    "CLOVERS HIROSHIMA": "clovers-hiroshima",
    "ALFA HIROSHIMA": "alfa-hiroshima",
}

_SLUG_TO_STORE = {v: k for k, v in STORE_SLUGS.items()}

# Closed / consolidated legacy store slugs -> 301 redirect destination (active nearby store or area portal)
LEGACY_SLUG_REDIRECTS = {
    "oriental-lounge-shibuya-ekimae": "/store/oriental-lounge-shibuya-honten",
    "ag-shinjuku": "/area/shinjuku",
    "oriental-lounge-umeda": "/store/oriental-lounge-osaka-ekimae",
    "oriental-lounge-chayamachi": "/store/oriental-lounge-osaka-ekimae",
    "oriental-lounge-hiroshima": "/store/ag-hiroshima",
    "oriental-lounge-kanazawa": "/store/ag-kanazawa",
    "oriental-lounge-sendai": "/store/ag-sendai",
    "oriental-lounge-sapporo": "/store/jis-sapporo",
    "ag-sapporo": "/store/jis-sapporo",
    "ag-fukuoka": "/store/oriental-lounge-fukuoka",
    "oriental-lounge-okinawa": "/store/ag-okinawa",
}


def legacy_redirect_for_slug(slug: str):
    """Return 301 redirect target path if slug belongs to a closed/consolidated legacy store."""
    if not slug:
        return None
    return LEGACY_SLUG_REDIRECTS.get(slug.strip())


def load():
    global _cache
    with _lock:
        if _cache is None:
            try:
                with open(_PATH, 'r', encoding='utf-8') as f:
                    _cache = json.load(f).get('stores', {})
            except Exception:
                _cache = {}
        return _cache


def get(store_name):
    return load().get(store_name)


def region_of(store_name):
    info = get(store_name)
    return info.get('region') if info else None


def unknown_stores(names):
    """マスタ未登録の店舗名（新店舗・表記変更の検知用）。"""
    master = load()
    return sorted({n for n in names if n and n not in master})


def seo_name(store_name: str) -> str:
    """Google検索キーワード（『オリエンタルラウンジ 新宿』『ag（アグ）渋谷』等）に合致する正式名称を返す。"""
    if not store_name:
        return ""
    info = get(store_name)
    if info:
        brand = info.get("brand", "")
        disp = info.get("display_name", "")
        if brand == "OLG":
            return f"オリエンタルラウンジ {disp}"
        elif brand == "ag":
            return f"ag（アグ）{disp}"
        elif brand == "JIS":
            return f"JIS（ジス）{disp}"
        elif brand == "XIX":
            return f"相席ラウンジXIX {disp}"
        elif brand == "ヤタコイ":
            return f"ヤタコイ {disp}"
        elif brand == "CLOVERS":
            return f"相席CLOVERS {disp}"
        elif brand == "ALFA":
            return f"相席ラウンジALFA {disp}"
        return f"{brand} {disp}".strip()

    # マスタ未登録時のフォールバック変換
    s = store_name.strip()
    if s.startswith("OLG ag"):
        return "ag（アグ）" + s[len("OLG ag"):].strip()
    if s.startswith("OLG "):
        return "オリエンタルラウンジ " + s[len("OLG "):].strip()
    if s.startswith("JIS "):
        return "JIS（ジス）" + s[len("JIS "):].strip()
    return s


def slug_of(store_name: str) -> str:
    """店舗名からSEO用URLスラッグ（例: oriental-lounge-shinjuku）を返す。"""
    if not store_name:
        return ""
    if store_name in STORE_SLUGS:
        return STORE_SLUGS[store_name]
    # 動的フォールバック
    s = store_name.strip().lower()
    s = re.sub(r'[^a-z0-9\u3040-\u30ff\u4e00-\u9faf]+', '-', s).strip('-')
    return s or urllib.parse.quote(store_name)


def store_by_slug(slug: str, live_stores=None) -> str:
    """URLスラッグから現役稼働中の内部店舗名（例: 'OLG 新宿'）を逆引きする。閉店済み旧店舗は返さない。"""
    if not slug:
        return None
    slug_clean = slug.strip()
    master = load()
    live_names = {s.get("name", "") for s in (live_stores or []) if s.get("name")}
    if slug_clean in _SLUG_TO_STORE:
        candidate = _SLUG_TO_STORE[slug_clean]
        if candidate in master or candidate in live_names:
            return candidate
        return None
    for sname in master.keys():
        if slug_of(sname) == slug_clean:
            return sname
    for sname in live_names:
        if slug_of(sname) == slug_clean or sname == slug_clean:
            return sname
    return None


def brand_key_of(store_name: str) -> str:
    """店舗のブランド識別子（'oriental-lounge' / 'ag' / 'jis' / 'other'）を返す。"""
    info = get(store_name)
    if info:
        b = info.get("brand", "")
        if b == "OLG":
            return "oriental-lounge"
        if b == "ag":
            return "ag"
        if b == "JIS":
            return "jis"
        return "other"
    if store_name.startswith("OLG ag"):
        return "ag"
    if store_name.startswith("OLG "):
        return "oriental-lounge"
    if store_name.startswith("JIS "):
        return "jis"
    return "other"
