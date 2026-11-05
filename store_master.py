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
    "ASOBIBAR 新宿": "asobibar-shinjuku",
    "ASOBIBAR 渋谷": "asobibar-shibuya",
    "ASOBIBAR 恵比寿": "asobibar-ebisu",
    "ASOBIBAR 梅田": "asobibar-umeda",
    "ASOBIBAR 茶屋町": "asobibar-chayamachi",
    "ASOBIBAR 心斎橋": "asobibar-shinsaibashi",
    "ASOBIBAR 名古屋 栄": "asobibar-nagoya-sakae",
    "ASOBIBAR 名古屋 住吉": "asobibar-nagoya-sumiyoshi",
    "ASOBIBAR 神戸 三宮": "asobibar-kobe-sannomiya",
    "ASOBIBAR 福岡 天神": "asobibar-fukuoka-tenjin",
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
        elif brand == "ASOBIBAR":
            return f"ASOBIBAR（アソビバー）{disp}"
        return f"{brand} {disp}".strip()

    # マスタ未登録時のフォールバック変換（大文字小文字・全角空白・単体ブランド名にも対応）
    s = store_name.strip()
    m_ag = re.match(r"^(?:OLG\s+ag|ORIENTAL\s+ag|ag)(?:\s+|(?=[^a-zA-Z0-9])|$)(.*)$", s, flags=re.IGNORECASE)
    if m_ag:
        rest = m_ag.group(1).strip()
        return f"ag（アグ）{rest}" if rest else "ag（アグ）"
    m_olg = re.match(r"^(?:OLG|ORIENTAL)(?:\s+(.*))?$", s, flags=re.IGNORECASE)
    if m_olg:
        rest = (m_olg.group(1) or "").strip()
        return f"オリエンタルラウンジ {rest}" if rest else "オリエンタルラウンジ"
    m_jis = re.match(r"^JIS(?:\s+(.*))?$", s, flags=re.IGNORECASE)
    if m_jis:
        rest = (m_jis.group(1) or "").strip()
        return f"JIS（ジス）{rest}" if rest else "JIS（ジス）"
    m_asobi = re.match(r"^ASOBIBAR(?:\s+(.*))?$", s, flags=re.IGNORECASE)
    if m_asobi:
        rest = (m_asobi.group(1) or "").strip()
        return f"ASOBIBAR（アソビバー）{rest}" if rest else "ASOBIBAR（アソビバー）"
    m_yata = re.match(r"^YATAKOI(?:\s+(.*))?$", s, flags=re.IGNORECASE)
    if m_yata:
        rest = (m_yata.group(1) or "").strip()
        return f"ヤタコイ {rest}" if rest else "ヤタコイ"
    return s


_seo_reverse_cache = {"master_id": None, "map": {}}


def _get_seo_reverse_map(master: dict) -> dict:
    m_id = id(master)
    if _seo_reverse_cache["master_id"] == m_id and _seo_reverse_cache["map"]:
        return _seo_reverse_cache["map"]
    rev = {}
    all_keys = list(master.keys()) + [k for k in STORE_SLUGS.keys() if k not in master]
    for internal_key in all_keys:
        sn = seo_name(internal_key)
        if sn and sn not in rev:
            rev[sn] = internal_key
    _seo_reverse_cache["master_id"] = m_id
    _seo_reverse_cache["map"] = rev
    return rev


def _resolve_alias_without_slug_lookup(name_str: str) -> str:
    """SEO正式名称や表記ゆれ（'ag（アグ）上野', 'ag 上野', 'JIS（ジス）新宿' 等）を内部キーへO(1)逆引きする（再帰なし）。"""
    if not name_str:
        return ""
    s_clean = str(name_str).strip()
    if s_clean in STORE_SLUGS:
        return s_clean
    master = load()
    if s_clean in master:
        return s_clean
    rev_map = _get_seo_reverse_map(master)
    if s_clean in rev_map:
        return rev_map[s_clean]
    m_ag = re.match(r"^(?:ag（アグ）|OLG\s+ag|ORIENTAL\s+ag|ag)(?:\s+|(?=[^a-zA-Z0-9])|$)(.*)$", s_clean, flags=re.IGNORECASE)
    if m_ag and m_ag.group(1).strip():
        cand = "OLG ag" + m_ag.group(1).strip()
        if cand in master or cand in STORE_SLUGS:
            return cand
    if s_clean.startswith("オリエンタルラウンジ"):
        branch = s_clean[len("オリエンタルラウンジ"):].strip()
        cand = f"OLG {branch}"
        if cand in master or cand in STORE_SLUGS:
            return cand
    if s_clean.startswith("JIS（ジス）"):
        branch = s_clean[len("JIS（ジス）"):].strip()
        cand = f"JIS {branch}"
        if cand in master or cand in STORE_SLUGS:
            return cand
    if s_clean.startswith("ASOBIBAR（アソビバー）") or s_clean.startswith("アソビバー"):
        branch = s_clean.replace("ASOBIBAR（アソビバー）", "").replace("アソビバー", "").strip()
        cand = f"ASOBIBAR {branch}"
        if cand in master or cand in STORE_SLUGS:
            return cand
    if s_clean in ("ヤタコイ", "YATAKOI") or (s_clean.startswith("ヤタコイ") and ("梅田" in s_clean or "UMEDA" in s_clean.upper())):
        if not master or "YATAKOI UMEDA" in master:
            return "YATAKOI UMEDA"
    if s_clean in ("相席CLOVERS", "CLOVERS") or (s_clean.startswith("相席CLOVERS") and ("広島" in s_clean or "HIROSHIMA" in s_clean.upper())):
        if not master or "CLOVERS HIROSHIMA" in master:
            return "CLOVERS HIROSHIMA"
    if s_clean in ("相席ラウンジALFA", "ALFA") or (s_clean.startswith("相席ラウンジALFA") and ("広島" in s_clean or "HIROSHIMA" in s_clean.upper())):
        if not master or "ALFA HIROSHIMA" in master:
            return "ALFA HIROSHIMA"
    if s_clean in ("相席ラウンジXIX", "XIX") or (s_clean.startswith("相席ラウンジXIX") and ("岡山" in s_clean or "OKAYAMA" in s_clean.upper())):
        if not master or "XIX OKAYAMA" in master:
            return "XIX OKAYAMA"
    return s_clean


def slug_of(store_name: str) -> str:
    """店舗名（内部キー・SEO正式名称・各種エイリアス）からSEO用URLスラッグ（例: oriental-lounge-shinjuku）を返す。"""
    if not store_name:
        return ""
    s_raw = str(store_name).strip()
    if s_raw in STORE_SLUGS:
        return STORE_SLUGS[s_raw]
    if s_raw in _SLUG_TO_STORE or s_raw in LEGACY_SLUG_REDIRECTS:
        return s_raw
    resolved = _resolve_alias_without_slug_lookup(s_raw)
    if resolved in STORE_SLUGS:
        return STORE_SLUGS[resolved]
    # 動的フォールバック
    s = s_raw.lower()
    s = re.sub(r'[^a-z0-9\u3040-\u30ff\u4e00-\u9faf]+', '-', s).strip('-')
    return s or urllib.parse.quote(s_raw)


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
    """店舗のブランド識別子（'oriental-lounge' / 'ag' / 'jis' / 'asobibar' / 'other'）を返す。"""
    info = get(store_name)
    if info:
        b = info.get("brand", "")
        if b == "OLG":
            return "oriental-lounge"
        if b == "ag":
            return "ag"
        if b == "JIS":
            return "jis"
        if b == "ASOBIBAR":
            return "asobibar"
        return "other"
    if store_name.startswith("OLG ag"):
        return "ag"
    if store_name.startswith("OLG "):
        return "oriental-lounge"
    if store_name.startswith("JIS "):
        return "jis"
    if store_name.startswith("ASOBIBAR "):
        return "asobibar"
    return "other"


def to_internal_store_name(name_or_slug: str) -> str:
    """正式名称（'オリエンタルラウンジ 渋谷本店'）、スラッグ、内部キーのいずれからでも内部店舗キー（'OLG 渋谷本店'）を逆引き解決する。"""
    if not name_or_slug:
        return ""
    name_clean = str(name_or_slug).strip()
    master = load()
    if name_clean in master:
        return name_clean
    from_slug = store_by_slug(name_clean)
    if from_slug:
        return from_slug
    return _resolve_alias_without_slug_lookup(name_clean)

