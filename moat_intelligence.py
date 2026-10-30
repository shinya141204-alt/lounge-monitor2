"""
moat_intelligence.py - 相席ナビ 独自データモート＆リアルタイム推論エンジン

【本モジュールが担う4大コア機能】
1. エリア別リアルタイム気象コレクター（Open-Meteo無料API・GCP不要・非ブロッキング）
   - 全国10主要都市（札幌・仙台・東京・横浜・名古屋・大阪・神戸・広島・福岡・那覇）の
     気温・降水量(mm)・天気コード・風速を30分TTLでキャッシュし、10分刻みDBログに自動保存。
2. 全国10大ドーム・アリーナのイベント終演連動＆女性急増シグナル検知
   - 東京ドーム・武道館・Kアリーナ横浜・SSA・バンテリンドーム・京セラドーム・大阪城ホール・
     神戸ワールド/GLION・マリンメッセ/PayPayドーム・プレミストドームの終演時間帯と
     周辺ターミナル店舗の普段比急増（usual_w_diff / women_diff）をクロス検知。
3. 60分後の男女比予測バッジ＆群衆殺到注意アラート（98万件実測遷移モデル）
   - 曜日区分×時間帯×現在男女数×15分モメンタム×雨天地下直結ブースト×終電後維持率から
     60分後の予測女性比率・男余り転落リスク（flip_risk_pct）・アラート種別をマイクロ秒で算出。
4. JIS実測POS待機データを教師にした全店リアル待ち時間推論（オンライン自己学習）
   - JIS全10店の公式POS実測値（待ち組数・待ち人数・相席成立組数・1組平均人数・VIP稼働）をそのまま表示しつつ、
     「収容率×男女差→実測待ち発生率」の回帰係数をオンライン更新し、オリエンタルラウンジ・ag等の
     全従量制店舗の推定待ち組数・推定待ち分数・即相席確率を高精度推論。
   - ASOBIBAR全10店・ヤタコイ梅田は「スタンディング自由回遊（席待ちなし）」として業態別に区別。
"""

import datetime
import json
import logging
import math
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

# ==============================================================================
# 1. 全63店舗の実測P99収容人数（979,768件ログの99パーセンタイル実績＋新設ASOBIBAR基準）
# ==============================================================================
STORE_P99_CAPACITY = {
    "JIS UMEDA": 377,
    "OLG 大阪駅前": 288,
    "OLG 恵比寿": 275,
    "JIS SAPPORO": 273,
    "OLG ag上野": 247,
    "OLG 渋谷本店": 245,
    "JIS SHINJUKU": 242,
    "JIS FUKUOKA": 226,
    "OLG 名古屋 錦": 222,
    "JIS NAMBA": 201,
    "OLG 熊本": 184,
    "OLG 横浜": 176,
    "OLG 難波": 175,
    "OLG 福岡": 174,
    "JIS CHAYAMACHI": 171,
    "OLG 神戸": 144,
    "OLG ag渋谷": 141,
    "OLG ag仙台": 140,
    "JIS OMIYA": 134,
    "OLG 静岡": 134,
    "OLG 上野": 132,
    "OLG 新宿": 129,
    "OLG ag名古屋": 128,
    "OLG 浜松": 126,
    "JIS NISHISHINJUKU": 124,
    "OLG 名古屋 栄": 124,
    "OLG 京都": 123,
    "OLG 天満": 120,
    "OLG 大宮": 113,
    "OLG ag広島": 113,
    "ALFA HIROSHIMA": 112,
    "OLG ag金沢": 111,
    "OLG 鹿児島": 110,
    "OLG ソウル カンナム": 109,
    "OLG ag沖縄": 104,
    "OLG ソウル ホンデ": 99,
    "OLG 町田": 98,
    "CLOVERS HIROSHIMA": 97,
    "YATAKOI UMEDA": 96,
    "OLG ag梅田": 95,
    "OLG 岡山": 91,
    "OLG 大分": 88,
    "OLG 錦糸町": 83,
    "OLG 宮崎": 80,
    "OLG 高崎": 80,
    "OLG 心斎橋": 69,
    "XIX OKAYAMA": 67,
    "OLG 長崎": 66,
    "JIS KUMAMOTO": 64,
    "OLG 小倉": 62,
    "OLG 宇都宮": 57,
    "OLG 柏": 36,
    "JIS MATSUYAMA": 50,
    # ASOBIBAR 10店舗（スタンディング＋VIP席のフロア収容目安）
    "ASOBIBAR 新宿": 130,
    "ASOBIBAR 渋谷": 135,
    "ASOBIBAR 恵比寿": 100,
    "ASOBIBAR 梅田": 140,
    "ASOBIBAR 茶屋町": 95,
    "ASOBIBAR 心斎橋": 150,
    "ASOBIBAR 名古屋 栄": 115,
    "ASOBIBAR 名古屋 住吉": 110,
    "ASOBIBAR 神戸 三宮": 105,
    "ASOBIBAR 福岡 天神": 120,
}

# 終電後の深夜維持率（21-22時平均に対する0-1時女性維持率%）実測プロファイル
STORE_MIDNIGHT_RETENTION = {
    "OLG ag金沢": 151,
    "OLG ag沖縄": 148,
    "OLG 熊本": 139,
    "OLG ソウル ホンデ": 138,
    "OLG 鹿児島": 138,
    "OLG 宇都宮": 132,
    "JIS UMEDA": 126,
    "CLOVERS HIROSHIMA": 125,
    "OLG 福岡": 123,
    "OLG 宮崎": 121,
    "OLG 大分": 120,
    "OLG 高崎": 118,
    "JIS FUKUOKA": 118,
    "OLG 浜松": 116,
    "ALFA HIROSHIMA": 115,
    "OLG 心斎橋": 111,
    "OLG 岡山": 110,
    "OLG 長崎": 109,
    "OLG 名古屋 錦": 109,
    "OLG ag渋谷": 107,
    "OLG 静岡": 107,
    "OLG 大阪駅前": 106,
    "OLG 難波": 104,
    "OLG ag広島": 104,
    "OLG 小倉": 103,
    "JIS SAPPORO": 101,
    "JIS KUMAMOTO": 99,
    "JIS NAMBA": 95,
    "JIS SHINJUKU": 93,
    "OLG 渋谷本店": 91,
    "OLG ag名古屋": 89,
    "OLG 京都": 88,
    "OLG ag仙台": 87,
    "OLG ag上野": 71,
    "OLG 恵比寿": 70,
    "OLG 町田": 70,
    "JIS OMIYA": 68,
    "XIX OKAYAMA": 66,
    "OLG ag梅田": 65,
    "OLG 新宿": 62,
    "OLG 神戸": 61,
    "OLG 横浜": 60,
    "JIS CHAYAMACHI": 59,
    "OLG 柏": 58,
    "OLG 大宮": 56,
    "OLG 天満": 55,
    "OLG 名古屋 栄": 47,
    "JIS NISHISHINJUKU": 40,
    "YATAKOI UMEDA": 37,
    "OLG 錦糸町": 33,
    "OLG 上野": 32,
    # ASOBIBAR（クラブ・バー回遊層が多く深夜維持率が高め）
    "ASOBIBAR 新宿": 112,
    "ASOBIBAR 渋谷": 118,
    "ASOBIBAR 恵比寿": 92,
    "ASOBIBAR 梅田": 115,
    "ASOBIBAR 茶屋町": 82,
    "ASOBIBAR 心斎橋": 124,
    "ASOBIBAR 名古屋 栄": 102,
    "ASOBIBAR 名古屋 住吉": 108,
    "ASOBIBAR 神戸 三宮": 88,
    "ASOBIBAR 福岡 天神": 116,
}

# 定額・スタンディング自由回遊型（着席ペア固定案内ではなく回遊スタイル）の店舗集合
FLAT_STANDING_STORES = {
    "YATAKOI UMEDA",
    "ASOBIBAR 新宿",
    "ASOBIBAR 渋谷",
    "ASOBIBAR 恵比寿",
    "ASOBIBAR 梅田",
    "ASOBIBAR 茶屋町",
    "ASOBIBAR 心斎橋",
    "ASOBIBAR 名古屋 栄",
    "ASOBIBAR 名古屋 住吉",
    "ASOBIBAR 神戸 三宮",
    "ASOBIBAR 福岡 天神",
}

# 地下街・駅ビル直結またはアーケード直結で「雨天時に女性比率が上昇しやすい」店舗集合
UNDERGROUND_DIRECT_STORES = {
    "OLG 大阪駅前",
    "JIS UMEDA",
    "OLG ag梅田",
    "ASOBIBAR 梅田",
    "OLG 難波",
    "JIS NAMBA",
    "OLG 新宿",
    "JIS SHINJUKU",
    "JIS NISHISHINJUKU",
    "OLG 渋谷本店",
    "OLG 横浜",
    "OLG 大宮",
    "JIS OMIYA",
    "OLG 名古屋 栄",
    "ASOBIBAR 名古屋 栄",
    "JIS SAPPORO",
    "OLG ag仙台",
    "OLG 福岡",
    "JIS FUKUOKA",
    "OLG 神戸",
    "ASOBIBAR 神戸 三宮",
    "OLG 京都",
    "OLG ag広島",
    "ALFA HIROSHIMA",
    "CLOVERS HIROSHIMA",
}

# ==============================================================================
# 2. 全国10主要都市 Open-Meteo 気象コレクター（無料・APIキー不要・GCP不要）
# ==============================================================================
WEATHER_CITIES = {
    "sapporo": {"label": "札幌", "lat": 43.0555, "lng": 141.3533},
    "sendai": {"label": "仙台", "lat": 38.2636, "lng": 140.8694},
    "tokyo": {"label": "東京", "lat": 35.6895, "lng": 139.6917},
    "yokohama": {"label": "横浜", "lat": 35.4658, "lng": 139.6221},
    "nagoya": {"label": "名古屋", "lat": 35.1681, "lng": 136.9066},
    "osaka": {"label": "大阪", "lat": 34.7025, "lng": 135.4959},
    "kobe": {"label": "神戸", "lat": 34.6937, "lng": 135.1955},
    "hiroshima": {"label": "広島", "lat": 34.3914, "lng": 132.4623},
    "fukuoka": {"label": "福岡", "lat": 33.5892, "lng": 130.3988},
    "naha": {"label": "那覇", "lat": 26.2185, "lng": 127.6791},
}

_WEATHER_CITY_ORDER = list(WEATHER_CITIES.keys())
_WEATHER_TTL_SEC = 1800.0  # 30分キャッシュ
_weather_lock = threading.Lock()
_weather_state = {
    "ts": 0.0,
    "cities": {},
    "fetching": False,
}


def city_key_for_store(store_name: str) -> str:
    """店舗名から対応する気象観測都市キーを返す（韓国店舗は空文字）。"""
    s = (store_name or "").upper()
    if any(k in s for k in ("ソウル", "カンナム", "ホンデ", "SEOUL", "GANGNAM", "HONGDAE")):
        return ""
    if "SAPPORO" in s or "札幌" in s:
        return "sapporo"
    if "SENDAI" in s or "仙台" in s:
        return "sendai"
    if "横浜" in s or "YOKOHAMA" in s:
        return "yokohama"
    if "錦糸町" in s or "KINSHICHO" in s:
        return "tokyo"
    if any(k in s for k in ("名古屋", "名古屋 栄", "名古屋栄", "名古屋 錦", "名古屋錦", "栄", "住吉", "静岡", "浜松", "金沢", "NAGOYA", "SAKAE", "SUMIYOSHI")):
        return "nagoya"
    if "神戸" in s or "三宮" in s or "SANNOMIYA" in s or "KOBE" in s:
        return "kobe"
    if any(k in s for k in ("大阪", "梅田", "茶屋町", "難波", "心斎橋", "天満", "京都", "UMEDA", "NAMBA", "CHAYAMACHI", "SHINSAIBASHI")):
        return "osaka"
    if any(k in s for k in ("広島", "岡山", "松山", "HIROSHIMA", "OKAYAMA", "MATSUYAMA")):
        return "hiroshima"
    if "沖縄" in s or "OKINAWA" in s:
        return "naha"
    if any(k in s for k in ("福岡", "天神", "小倉", "長崎", "大分", "熊本", "宮崎", "鹿児島", "FUKUOKA", "KUMAMOTO", "TENJIN")):
        return "fukuoka"
    return "tokyo"


def _weather_code_label(wcode: int, precip_mm: float) -> tuple:
    """WMO weather_code と降水量から (日本語ラベル, is_rainy: bool) を返す。"""
    if precip_mm >= 0.4 or wcode in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99):
        if precip_mm >= 3.0 or wcode in (65, 82, 95, 96, 99):
            return "本降りの雨", True
        return "雨", True
    if wcode in (71, 73, 75, 77, 85, 86):
        return "雪", True
    if wcode in (1, 2, 3, 45, 48):
        return "曇り", False
    return "晴れ", False


def _fetch_open_meteo_all_cities(timeout_sec: float = 3.5) -> dict:
    """Open-Meteo APIで全国10都市の現在気象を1リクエストで一括取得する。"""
    lats = ",".join(str(WEATHER_CITIES[k]["lat"]) for k in _WEATHER_CITY_ORDER)
    lngs = ",".join(str(WEATHER_CITIES[k]["lng"]) for k in _WEATHER_CITY_ORDER)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lats}&longitude={lngs}"
        "&current=temperature_2m,precipitation,weather_code,wind_speed_10m"
        "&timezone=Asia%2FTokyo"
    )
    resp = requests.get(url, timeout=timeout_sec)
    if resp.status_code != 200:
        return {}
    payload = resp.json()
    items = payload if isinstance(payload, list) else [payload]
    parsed = {}
    for idx, city_key in enumerate(_WEATHER_CITY_ORDER):
        if idx >= len(items):
            break
        cur = (items[idx] or {}).get("current") or {}
        temp_c = round(float(cur.get("temperature_2m") or 0.0), 1)
        precip_mm = round(float(cur.get("precipitation") or 0.0), 2)
        wcode = int(cur.get("weather_code") or 0)
        wind_kmh = round(float(cur.get("wind_speed_10m") or 0.0), 1)
        label_ja, is_rainy = _weather_code_label(wcode, precip_mm)
        parsed[city_key] = {
            "city_key": city_key,
            "city_label": WEATHER_CITIES[city_key]["label"],
            "temp_c": temp_c,
            "precip_mm": precip_mm,
            "weather_code": wcode,
            "wind_kmh": wind_kmh,
            "condition_ja": label_ja,
            "is_rainy": is_rainy,
        }
    return parsed


def get_city_weather_map(allow_blocking: bool = False) -> dict:
    """
    全国10都市の最新気象マップを返す。
    allow_blocking=False の場合は決してHTTPスレッドをブロックせずキャッシュのみ返し、
    期限切れ時はバックグラウンドスレッドで非同期更新する。
    """
    if os.environ.get("DISABLE_EXTERNAL_WEATHER") == "1":
        return {}
    now_mono = time.monotonic()
    with _weather_lock:
        cached = dict(_weather_state["cities"])
        age = now_mono - _weather_state["ts"]
        already_fetching = _weather_state["fetching"]

    if cached and age < _WEATHER_TTL_SEC:
        return cached

    if allow_blocking:
        try:
            fresh = _fetch_open_meteo_all_cities(timeout_sec=3.5)
            if fresh:
                with _weather_lock:
                    _weather_state["cities"] = fresh
                    _weather_state["ts"] = time.monotonic()
                return fresh
        except Exception as e:
            logger.debug(f"[moat_intelligence] weather fetch skipped: {e}")
        return cached

    # Non-blocking background refresh (skip during unit tests to avoid stray network threads)
    if not already_fetching and os.environ.get("DISABLE_SCHEDULER") != "1":
        with _weather_lock:
            _weather_state["fetching"] = True

        def _bg():
            try:
                fresh = _fetch_open_meteo_all_cities(timeout_sec=3.5)
                if fresh:
                    with _weather_lock:
                        _weather_state["cities"] = fresh
                        _weather_state["ts"] = time.monotonic()
            except Exception:
                pass
            finally:
                with _weather_lock:
                    _weather_state["fetching"] = False

        threading.Thread(target=_bg, daemon=True).start()

    return cached


# ==============================================================================
# 3. 全国10大ドーム・アリーナの大型ライブ/イベント連動＆終演後流入シグナル検知
# ==============================================================================
MAJOR_ARENA_HUBS = [
    {
        "venue_id": "tokyo_dome_budokan",
        "venue_name": "東京ドーム・日本武道館",
        "city_key": "tokyo",
        "stores": ["OLG 新宿", "JIS SHINJUKU", "ASOBIBAR 新宿", "OLG 上野", "OLG ag上野"],
    },
    {
        "venue_id": "yokohama_arenas",
        "venue_name": "Kアリーナ横浜・ぴあアリーナMM",
        "city_key": "yokohama",
        "stores": ["OLG 横浜"],
    },
    {
        "venue_id": "saitama_super_arena",
        "venue_name": "さいたまスーパーアリーナ",
        "city_key": "tokyo",
        "stores": ["OLG 大宮", "JIS OMIYA"],
    },
    {
        "venue_id": "shibuya_nhk_yoyogi",
        "venue_name": "国立代々木競技場・NHKホール",
        "city_key": "tokyo",
        "stores": ["OLG 渋谷本店", "OLG ag渋谷", "ASOBIBAR 渋谷", "OLG 恵比寿", "ASOBIBAR 恵比寿"],
    },
    {
        "venue_id": "vantelin_dome_nagoya",
        "venue_name": "バンテリンドーム ナゴヤ・ガイシホール",
        "city_key": "nagoya",
        "stores": ["OLG 名古屋 栄", "OLG 名古屋 錦", "OLG ag名古屋", "ASOBIBAR 名古屋 栄", "ASOBIBAR 名古屋 住吉"],
    },
    {
        "venue_id": "osaka_jo_hall",
        "venue_name": "大阪城ホール",
        "city_key": "osaka",
        "stores": ["OLG 大阪駅前", "JIS UMEDA", "JIS CHAYAMACHI", "OLG ag梅田", "OLG 天満", "ASOBIBAR 梅田", "ASOBIBAR 茶屋町"],
    },
    {
        "venue_id": "kyocera_dome_osaka",
        "venue_name": "京セラドーム大阪",
        "city_key": "osaka",
        "stores": ["OLG 難波", "JIS NAMBA", "OLG 心斎橋", "ASOBIBAR 心斎橋"],
    },
    {
        "venue_id": "kobe_glion_world",
        "venue_name": "GLION ARENA KOBE・ワールド記念ホール",
        "city_key": "kobe",
        "stores": ["OLG 神戸", "ASOBIBAR 神戸 三宮"],
    },
    {
        "venue_id": "fukuoka_paypay_marine",
        "venue_name": "みずほPayPayドーム・マリンメッセ福岡",
        "city_key": "fukuoka",
        "stores": ["OLG 福岡", "JIS FUKUOKA", "ASOBIBAR 福岡 天神"],
    },
    {
        "venue_id": "sapporo_premist_dome",
        "venue_name": "大和ハウス プレミストドーム（札幌）",
        "city_key": "sapporo",
        "stores": ["JIS SAPPORO"],
    },
]

_STORE_TO_ARENA = {}
for _hub in MAJOR_ARENA_HUBS:
    for _st in _hub["stores"]:
        _STORE_TO_ARENA[_st] = _hub


def detect_arena_event_surges(live_stores: list, now_jst: datetime.datetime = None) -> dict:
    """
    ライブ終演後ゴールデンタイム（19:00〜22:50）において、ドーム・アリーナ周辺店舗で
    普段の同曜日・同時間帯平均を大きく上回る女性急増（usual_w_diff >= 10 または 15分比+6以上）が
    発生しているハブを自動検知し、{store_name: event_signal_dict} を返す。
    また job_state / 環境変数 `ARENA_ACTIVE_VENUES` に明示登録された会場があれば優先付与する。
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    hr = now_jst.hour
    explicit_venues = set(
        v.strip() for v in (os.environ.get("ARENA_ACTIVE_VENUES") or "").split(",") if v.strip()
    )
    try:
        import job_state
        raw_ev = job_state.get_value("moat:arena_events_today", allow_blocking=False)
        if raw_ev:
            data_ev = json.loads(raw_ev)
            if isinstance(data_ev, dict) and data_ev.get("date") == now_jst.strftime("%Y-%m-%d"):
                for vid in data_ev.get("venues") or []:
                    explicit_venues.add(str(vid))
    except Exception:
        pass

    store_map = {s.get("name"): s for s in (live_stores or []) if s.get("name")}
    signals_by_store = {}
    is_post_concert_window = 19 <= hr <= 22

    for hub in MAJOR_ARENA_HUBS:
        vid = hub["venue_id"]
        vname = hub["venue_name"]
        is_explicit = vid in explicit_venues

        hub_usual_surges = 0
        max_w_diff = 0
        for sname in hub["stores"]:
            st = store_map.get(sname)
            if not st:
                continue
            w = int(st.get("women") or 0)
            uw_diff = st.get("usual_w_diff")
            w_diff_15 = int(st.get("women_diff") or 0)
            if w >= 16 and ((uw_diff is not None and uw_diff >= 10) or w_diff_15 >= 6):
                hub_usual_surges += 1
                max_w_diff = max(max_w_diff, int(uw_diff or w_diff_15))

        needed = 1 if len(hub["stores"]) <= 2 else 2
        if is_explicit or (is_post_concert_window and hub_usual_surges >= needed):
            for sname in hub["stores"]:
                st = store_map.get(sname)
                if not st:
                    continue
                if is_explicit or int(st.get("women") or 0) >= 12:
                    signals_by_store[sname] = {
                        "venue_id": vid,
                        "venue_name": vname,
                        "is_explicit_schedule": is_explicit,
                        "surge_women_delta": max_w_diff,
                        "label_short": f"🎸{vname.split('・')[0]}終演圏・女性流入中",
                    }
    return signals_by_store


def capture_external_context_snapshot(live_stores: list = None, allow_blocking: bool = True, now_jst: datetime.datetime = None) -> list:
    """
    10分刻みのDB保存・Moat Telemetryシート追記用に、全国10都市の現在気象＋イベント流入検知結果の
    リスト `[{city_key, temp_c, precip_mm, weather_code, wind_kmh, is_rainy, active_arenas_json}, ...]` を返す。
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    weather_map = get_city_weather_map(allow_blocking=allow_blocking)
    arena_signals = detect_arena_event_surges(live_stores or [], now_jst=now_jst)

    city_arenas = {}
    for sname, sig in arena_signals.items():
        ck = city_key_for_store(sname)
        if ck:
            city_arenas.setdefault(ck, set()).add(sig["venue_id"])

    rows = []
    for ck in _WEATHER_CITY_ORDER:
        w = weather_map.get(ck)
        if not w:
            continue
        arenas_list = sorted(city_arenas.get(ck) or [])
        rows.append({
            "city_key": ck,
            "city_label": w["city_label"],
            "temp_c": w["temp_c"],
            "precip_mm": w["precip_mm"],
            "weather_code": w["weather_code"],
            "wind_kmh": w["wind_kmh"],
            "condition_ja": w["condition_ja"],
            "is_rainy": 1 if w["is_rainy"] else 0,
            "active_arenas_json": json.dumps(arenas_list, ensure_ascii=False),
        })
    return rows


# ==============================================================================
# 4. JIS実測POSデータのオンライン自己キャリブレーション＆全店リアル待ち時間推論
# ==============================================================================
_calib_lock = threading.Lock()
_jis_calibration = {
    # 初期事前分布（JIS全10店のPOS構造に基づくベースライン係数）
    # 収容率 occ と男性超過人数 male_excess から待ち組数を推論する重み
    "excess_to_wait_group_ratio": 0.42,   # 男性超過10名（約5組）あたりの実測待ち組数係数
    "high_occ_threshold": 0.68,           # 満席詰まりで男女双方に席待ちが出始めるP99収容率
    "avg_men_party_size": 1.82,           # 実測1組あたり男性平均人数（ソロ1名とペア2名の混合）
    "avg_women_party_size": 1.94,         # 実測1組あたり女性平均人数（ペア中心）
    "minutes_per_wait_group": 6.5,        # 待ち1組あたりの平均解消分数（大箱基準）
    "samples_observed": 0,
    "last_calibrated_at": "",
}


def calibrate_from_jis_pos(stores_data: list) -> dict:
    """
    JIS各店のリアルタイム `pos_telemetry` から、
    ・男性1組あたり平均人数 / 女性1組あたり平均人数
    ・男性超過人数に対する実測待ち組数の発生比率
    を指数移動平均（EMA）でオンライン学習する。
    """
    if not stores_data:
        return dict(_jis_calibration)

    men_party_samples = []
    women_party_samples = []
    wait_ratio_samples = []

    for s in stores_data:
        pos = s.get("pos_telemetry")
        if not isinstance(pos, dict) or pos.get("telemetry_type") != "jis_pos":
            continue
        m_cust = int(pos.get("men_total_guests") or s.get("men") or 0)
        w_cust = int(pos.get("women_total_guests") or s.get("women") or 0)
        m_grp = int(pos.get("men_total_groups") or 0)
        w_grp = int(pos.get("women_total_groups") or 0)
        m_wait_grp = int(pos.get("men_waiting_groups") or 0)

        if m_cust >= 4 and m_grp >= 2:
            men_party_samples.append(max(1.0, min(3.5, m_cust / float(m_grp))))
        if w_cust >= 4 and w_grp >= 2:
            women_party_samples.append(max(1.0, min(3.5, w_cust / float(w_grp))))

        excess_men = m_cust - w_cust
        if excess_men >= 4 and (m_cust + w_cust) >= 16:
            wait_ratio_samples.append(max(0.05, min(0.85, m_wait_grp / float(excess_men))))

    if not (men_party_samples or women_party_samples or wait_ratio_samples):
        return dict(_jis_calibration)

    with _calib_lock:
        alpha = 0.22  # 安定性と最新トレンド追従を両立するEMA学習率
        if men_party_samples:
            obs_mp = sum(men_party_samples) / len(men_party_samples)
            _jis_calibration["avg_men_party_size"] = round(
                (1.0 - alpha) * _jis_calibration["avg_men_party_size"] + alpha * obs_mp, 2
            )
        if women_party_samples:
            obs_wp = sum(women_party_samples) / len(women_party_samples)
            _jis_calibration["avg_women_party_size"] = round(
                (1.0 - alpha) * _jis_calibration["avg_women_party_size"] + alpha * obs_wp, 2
            )
        if wait_ratio_samples:
            obs_wr = sum(wait_ratio_samples) / len(wait_ratio_samples)
            _jis_calibration["excess_to_wait_group_ratio"] = round(
                (1.0 - alpha) * _jis_calibration["excess_to_wait_group_ratio"] + alpha * obs_wr, 3
            )
        _jis_calibration["samples_observed"] += len(men_party_samples) + len(wait_ratio_samples)
        _jis_calibration["last_calibrated_at"] = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        return dict(_jis_calibration)


def get_jis_calibration_snapshot() -> dict:
    with _calib_lock:
        return dict(_jis_calibration)


def estimate_store_wait_and_pos(
    store_name: str,
    women: int,
    men: int,
    is_open_now: bool = True,
    women_diff: int = 0,
    men_diff: int = 0,
    pos_telemetry: dict = None,
) -> dict:
    """
    店舗のリアルタイム待ち状況＆POSインサイトを算出する。
    - JIS店舗で `pos_telemetry` が存在する場合は公式POSの実測値をそのまま使用（is_measured=True）。
    - 定額スタンディング店舗（ASOBIBAR / ヤタコイ）は自由回遊業態として席待ちなしを返す。
    - オリエンタルラウンジ・ag等の従量制着席店舗は、JIS実測待機モデル＋P99収容率からリアル待ち時間を推論。
    """
    women = max(0, int(women or 0))
    men = max(0, int(men or 0))
    total = women + men
    p99_cap = max(35, int(STORE_P99_CAPACITY.get(store_name, 120)))
    occ_pct = min(100, int(round((total / float(p99_cap)) * 100)))

    if not is_open_now and total == 0:
        return {
            "is_measured": False,
            "is_standing_free": store_name in FLAT_STANDING_STORES,
            "occupancy_pct": 0,
            "est_men_wait_groups": 0,
            "est_men_wait_min": 0,
            "est_women_wait_min": 0,
            "instant_match_prob_pct": 0,
            "wait_badge_men": "",
            "wait_badge_women": "",
            "wait_tone": "closed",
            "pos_summary_short": "",
        }

    # --- Case A: 定額・スタンディング自由回遊型（ASOBIBAR 10店 / ヤタコイ梅田） ---
    if store_name in FLAT_STANDING_STORES:
        vip_cnt = int((pos_telemetry or {}).get("vip_guests") or 0)
        vip_note = f"・VIP稼働{vip_cnt}" if vip_cnt > 0 else ""
        if total == 0:
            badge_m = "🥂自由回遊（空席）"
            badge_w = "🥂自由回遊（入場無料）"
        elif women >= men:
            badge_m = f"🥂自由回遊・女性優位{vip_note}"
            badge_w = "🥂自由回遊・入場無料"
        else:
            badge_m = f"🥂自由回遊（席待ちなし{vip_note}）"
            badge_w = "👑男性多数・即乾杯◎"
        return {
            "is_measured": bool(pos_telemetry),
            "is_standing_free": True,
            "occupancy_pct": occ_pct,
            "est_men_wait_groups": 0,
            "est_men_wait_min": 0,
            "est_women_wait_min": 0,
            "instant_match_prob_pct": 95 if women >= 4 else 50,
            "wait_badge_men": badge_m,
            "wait_badge_women": badge_w,
            "wait_tone": "standing",
            "pos_summary_short": f"スタンディング回遊制（フロア稼働率 約{occ_pct}%{vip_note}）",
        }

    calib = get_jis_calibration_snapshot()

    # --- Case B: JIS全10店（公式POSの実測待機・相席成立テレメトリあり） ---
    if isinstance(pos_telemetry, dict) and pos_telemetry.get("telemetry_type") == "jis_pos":
        m_wait_grp = int(pos_telemetry.get("men_waiting_groups") or 0)
        m_wait_gst = int(pos_telemetry.get("men_waiting_guests") or 0)
        w_wait_grp = int(pos_telemetry.get("women_waiting_groups") or 0)
        matched_grp = int(pos_telemetry.get("matched_groups") or 0)
        m_grp = int(pos_telemetry.get("men_total_groups") or 0)
        w_grp = int(pos_telemetry.get("women_total_groups") or 0)
        vip_gst = int(pos_telemetry.get("vip_guests") or 0)

        avg_m_party = round(men / float(m_grp), 1) if m_grp > 0 and men > 0 else None
        party_style = ""
        if avg_m_party is not None:
            party_style = "ソロ多め" if avg_m_party <= 1.45 else ("ペア中心" if avg_m_party <= 2.15 else "グループ多め")

        # 店舗規模が大きいほど1分あたりの席回転が速い（p99=240の大箱は1組あたり約5分、中型は約7.5分）
        size_factor = max(0.65, min(1.35, 160.0 / float(p99_cap)))
        est_m_min = int(round(m_wait_grp * calib["minutes_per_wait_group"] * size_factor))
        if m_wait_grp > 0:
            est_m_min = max(5, min(75, est_m_min))
        elif total > 0 and women == 0 and men >= 2:
            est_m_min = 15

        est_w_min = int(round(w_wait_grp * 5.0 * size_factor)) if w_wait_grp > 0 else 0

        if total == 0:
            badge_m = "実測:待ち0組（空席）"
            badge_w = "実測:待ち0組（空席）"
            tone = "neutral"
            prob = 40
        elif m_wait_grp == 0 and women >= max(4, men):
            badge_m = f"🟢実測:男待ち0組（相席{matched_grp}卓）" if matched_grp > 0 else "🟢実測:男待ち0組（即相席圏）"
            badge_w = "🟢実測:女待ち0組" if w_wait_grp == 0 else f"🟡実測:女待ち{w_wait_grp}組"
            tone = "safe"
            prob = 94
        elif m_wait_grp == 0:
            badge_m = "🟢実測:男待ち0組"
            badge_w = "👑実測:女子即案内（男先行）" if men > women else "🟢実測:女待ち0組"
            tone = "safe" if women >= 6 else "neutral"
            prob = 82 if women >= 6 else 55
        elif est_m_min <= 15:
            badge_m = f"🟡実測:男待ち{m_wait_grp}組（約{est_m_min}分）"
            badge_w = "👑実測:女子待ち0分（即相席）"
            tone = "warn"
            prob = 45
        else:
            badge_m = f"🔴実測:男待ち{m_wait_grp}組（約{est_m_min}分）"
            badge_w = "👑実測:女子待ち0分（大歓迎）"
            tone = "danger"
            prob = 18

        pos_parts = [f"公式POS実測：相席成立 {matched_grp}組 / 男性待機 {m_wait_grp}組({m_wait_gst}名) / 女性待機 {w_wait_grp}組"]
        if avg_m_party is not None:
            pos_parts.append(f"男性平均 {avg_m_party}名/組（{party_style}）")
        if vip_gst > 0:
            pos_parts.append(f"VIP稼働 {vip_gst}名")

        return {
            "is_measured": True,
            "is_standing_free": False,
            "occupancy_pct": occ_pct,
            "est_men_wait_groups": m_wait_grp,
            "est_men_wait_min": est_m_min,
            "est_women_wait_min": est_w_min,
            "matched_groups": matched_grp,
            "men_total_groups": m_grp,
            "women_total_groups": w_grp,
            "avg_men_party_size": avg_m_party,
            "vip_guests": vip_gst,
            "instant_match_prob_pct": prob,
            "wait_badge_men": badge_m,
            "wait_badge_women": badge_w,
            "wait_tone": tone,
            "pos_summary_short": " ｜ ".join(pos_parts),
        }

    # --- Case C: オリエンタルラウンジ・ag・XIX・ALFA・CLOVERS（JIS実測モデルによる推論） ---
    if total == 0:
        return {
            "is_measured": False,
            "is_standing_free": False,
            "occupancy_pct": 0,
            "est_men_wait_groups": 0,
            "est_men_wait_min": 0,
            "est_women_wait_min": 0,
            "instant_match_prob_pct": 35,
            "wait_badge_men": "空席（0 : 0）",
            "wait_badge_women": "空席（0 : 0）",
            "wait_tone": "neutral",
            "pos_summary_short": "現在店内 0 : 0（1組目待機になりやすい時間帯）",
        }

    male_excess = men - women
    momentum_excess = max(0, int(men_diff or 0) - int(women_diff or 0))
    occ_ratio = total / float(p99_cap)

    # 1) 男性超過による相席相手待ち組数（JISの学習済み excess_to_wait_group_ratio を使用）
    raw_wait_groups = 0.0
    if male_excess > 0:
        raw_wait_groups += male_excess * calib["excess_to_wait_group_ratio"]
    # 2) フロア満席詰まり（収容率72%超）による受付案内待ち加算
    if occ_ratio > calib["high_occ_threshold"]:
        congestion_over = (occ_ratio - calib["high_occ_threshold"]) / (1.0 - calib["high_occ_threshold"])
        raw_wait_groups += congestion_over * (2.8 if male_excess >= -4 else 1.2)
    # 3) 直近15分で男性が急増中の場合は受付列のタイムラグを加味
    if momentum_excess >= 4 and women <= men + 4:
        raw_wait_groups += momentum_excess * 0.22

    est_m_groups = int(round(raw_wait_groups)) if raw_wait_groups >= 0.75 else 0
    size_factor = max(0.68, min(1.32, 155.0 / float(p99_cap)))
    est_m_min = int(round(est_m_groups * calib["minutes_per_wait_group"] * size_factor))
    if est_m_groups > 0:
        est_m_min = max(5, min(70, int(round(est_m_min / 5.0) * 5)))
    elif women == 0 and men >= 2:
        est_m_groups = max(1, int(round(men / calib["avg_men_party_size"])))
        est_m_min = 15

    # 女性側の受付・相席待ち推論（極端な女余り＋高稼働時）
    female_excess = women - men
    est_w_min = 0
    if female_excess >= 14 and men < 8:
        est_w_min = min(30, int(round((female_excess - 8) * 1.1 / 5.0) * 5))
    elif occ_ratio >= 0.85 and female_excess >= 16:
        est_w_min = 10

    if est_m_min == 0 and women >= 10 and women >= men:
        badge_m = "🟢推定:待ち約0分（即相席圏）"
        tone = "safe"
        prob = min(96, 78 + min(18, women - men + 4))
    elif est_m_min == 0:
        badge_m = "🟢推定:男待ち少（約0〜5分）" if women >= 6 else "🟡少人数（相手組数に注意）"
        tone = "safe" if women >= 6 else "neutral"
        prob = 74 if women >= 6 else 48
    elif est_m_min <= 15:
        badge_m = f"🟡推定:男 約{est_m_min}分待ち（約{est_m_groups}組）"
        tone = "warn"
        prob = 44
    else:
        badge_m = f"🔴推定:男 約{est_m_min}分待ち（約{est_m_groups}組）"
        tone = "danger"
        prob = max(10, 35 - est_m_min // 2)

    if est_w_min >= 10:
        badge_w = f"🟡推定:女余り（相席まで約{est_w_min}分）"
    elif men >= women + 4 and men >= 10:
        badge_w = "👑推定:女子待ち0分（即相席◎）"
    else:
        badge_w = "🟢推定:女子待ち約0分"

    return {
        "is_measured": False,
        "is_standing_free": False,
        "occupancy_pct": occ_pct,
        "est_men_wait_groups": est_m_groups,
        "est_men_wait_min": est_m_min,
        "est_women_wait_min": est_w_min,
        "instant_match_prob_pct": prob,
        "wait_badge_men": badge_m,
        "wait_badge_women": badge_w,
        "wait_tone": tone,
        "pos_summary_short": f"JIS待機学習推論：フロア稼働率 約{occ_pct}% ／ 即相席確率 {prob}%",
    }


# ==============================================================================
# 5. 98万件実測遷移確率に基づく「60分後の男女比予測バッジ（群衆殺到注意アラート）」
# ==============================================================================
# 金土祝前の「女性比55〜65%（客数20名+）」出発後60分の実測ドリフト（n=19,791件）：
#   19時発: Δ女+20.6, Δ男+13.5, 男余り転落率 6.1%
#   20時発: Δ女+19.1, Δ男+18.1, 男余り転落率 11.0%
#   21時発: Δ女+12.8, Δ男+20.9, 男余り転落率 19.4%
#   22時発: Δ女 -0.3, Δ男+13.2, 男余り転落率 29.8%（群衆殺到パラドックス！）
#   23時発: Δ女 -7.0, Δ男 +5.2, 男余り転落率 30.3%（終電崖×男性居残り！）
_WEEKEND_DRIFT_BY_HOUR = {
    18: (12.0, 7.5, 6),
    19: (20.6, 13.5, 6),
    20: (19.1, 18.1, 11),
    21: (12.8, 20.9, 20),
    22: (-0.3, 13.2, 30),
    23: (-7.0, 5.2, 31),
    0: (-4.5, -6.2, 18),
    1: (-5.0, -7.8, 15),
    2: (-4.2, -8.5, 11),
    3: (-2.8, -6.4, 9),
    4: (-3.5, -5.0, 10),
}

_WEEKDAY_DRIFT_BY_HOUR = {
    18: (7.5, 4.8, 6),
    19: (12.4, 8.2, 7),
    20: (11.2, 9.6, 10),
    21: (5.4, 8.8, 15),
    22: (-3.2, 4.6, 22),
    23: (-8.5, -2.4, 25),
    0: (-4.8, -5.2, 16),
    1: (-3.5, -4.5, 14),
    2: (-2.2, -4.0, 12),
    3: (-1.5, -3.2, 10),
    4: (-1.5, -2.5, 10),
}


def _is_weekend_or_preholiday(now_jst: datetime.datetime) -> bool:
    """営業日基準（朝7時切替）で金・土・祝前日かどうかを判定する。"""
    biz_dt = now_jst - datetime.timedelta(days=1) if now_jst.hour < 7 else now_jst
    wd = biz_dt.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    if wd in (4, 5):
        return True
    try:
        import store_hours
        next_iso = (biz_dt.date() + datetime.timedelta(days=1)).isoformat()
        if next_iso in getattr(store_hours, "JP_HOLIDAYS", set()):
            return True
    except Exception:
        pass
    return False


def predict_store_60m_dynamics(
    store_name: str,
    women: int,
    men: int,
    is_open_now: bool = True,
    women_diff: int = 0,
    men_diff: int = 0,
    usual_w_diff: int = None,
    now_jst: datetime.datetime = None,
    weather_map: dict = None,
    arena_signal: dict = None,
) -> dict:
    """
    98万件の実測マルコフ遷移法則に基づき、当該店舗の「60分後の予測女性比・男余り転落リスク・アラートバッジ」を返す。
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    women = max(0, int(women or 0))
    men = max(0, int(men or 0))
    total = women + men

    if not is_open_now or total < 6:
        return {
            "pred_60m_wpct": None,
            "pred_60m_women": women,
            "pred_60m_men": men,
            "pred_delta_pt": 0,
            "flip_risk_pct": 0,
            "alert_code": "quiet",
            "badge_men": "",
            "badge_women": "",
            "badge_tone": "neutral",
            "reason_short": "",
        }

    cur_wpct = int(round((women / float(total)) * 100))
    hr = now_jst.hour
    is_wknd = _is_weekend_or_preholiday(now_jst)
    drift_tbl = _WEEKEND_DRIFT_BY_HOUR if is_wknd else _WEEKDAY_DRIFT_BY_HOUR
    base_dw, base_dm, base_flip = drift_tbl.get(hr, (0.0, 0.0, 12))

    p99_cap = max(40, int(STORE_P99_CAPACITY.get(store_name, 120)))
    scale = max(0.38, min(1.45, p99_cap / 165.0))
    mid_ret = int(STORE_MIDNIGHT_RETENTION.get(store_name, 85))

    dw = base_dw * scale
    dm = base_dm * scale

    # 1) 終電崖補正（22:30〜23:59）：深夜維持率が低い店舗ほど女性退店が強く出る
    if hr in (22, 23):
        if mid_ret < 70:
            dw -= (75 - mid_ret) * 0.14 * scale
        elif mid_ret >= 115:
            dw += (mid_ret - 105) * 0.10 * scale

    # 2) 直近15分モメンタム補正（流入加速度の減衰投影）
    w_d15 = int(women_diff or 0)
    m_d15 = int(men_diff or 0)
    dw += max(-8.0, min(10.0, w_d15 * 0.85))
    dm += max(-8.0, min(12.0, m_d15 * 1.05))

    # 3) 「女余り見切り退店」vs「70%超確変バッファ」非線形補正（98万件データ法則）
    if women >= 20 and cur_wpct >= 70:
        # 70%バッファの法則（n=33,555）：実測60分後ドリフト Δ女+9.3 / Δ男+13.9、男余り転落率わずか2.3%
        dw = max(dw, 9.0 * scale)
        dm = min(dm, 14.0 * scale)
    elif women >= 12 and men <= 3:
        # 男性が極端に少なすぎる女余り店は、女性が20〜30分で見切って他店へ移動する
        dw -= 4.5
    elif cur_wpct <= 40 and men >= 15:
        # 深刻な男余りロック現象（90分後も56.8%が男余りのまま滞留）
        dm = max(dm, -2.0)

    # 4) 雨天×地下街・駅直結ブースト（案Bクロスデータ活用）
    ck = city_key_for_store(store_name)
    city_w = (weather_map or {}).get(ck) if ck else None
    is_rain_shelter = bool(city_w and city_w.get("is_rainy") and store_name in UNDERGROUND_DIRECT_STORES)
    if is_rain_shelter:
        dw += 3.2 * scale
        dm -= 1.2 * scale

    # 5) 大型ドーム・アリーナ終演後女性流入ブースト
    if arena_signal and 19 <= hr <= 22:
        dw += 3.5 * scale

    pred_w = max(1, min(int(p99_cap * 0.72), int(round(women + dw))))
    pred_m = max(1, min(int(p99_cap * 0.72), int(round(men + dm))))
    pred_tot = max(2, pred_w + pred_m)
    pred_wpct = int(round((pred_w / float(pred_tot)) * 100))
    delta_pt = pred_wpct - cur_wpct

    # 男余り転落リスク（60分後に女性比50%未満になる確率%）の算出
    if cur_wpct < 50:
        # 既に男余りの場合：98万件ログで「男15名+・女性比40%以下」は60〜90分後も約57〜72%男余り継続
        flip_risk = min(92, max(45, 58 + (48 - cur_wpct) * 2 - max(0, w_d15 * 3)))
    elif women >= 20 and cur_wpct >= 70 and pred_wpct >= 58:
        # 70%超確変バッファ（実測転落率 2.3%〜6%）
        flip_risk = max(3, min(12, int(round(8 - (cur_wpct - 70) * 0.3))))
    else:
        margin_above_50 = cur_wpct - 50
        rush_penalty = max(0, (m_d15 - w_d15) * 4)
        drop_penalty = max(0, -delta_pt * 2.1)
        flip_risk = int(round(base_flip + drop_penalty + rush_penalty - margin_above_50 * 1.15))
        if is_rain_shelter:
            flip_risk -= 6
        flip_risk = max(4, min(88, flip_risk))

    # --- アラート分類とユーザー向けバッジ生成 ---
    if cur_wpct <= 44 and men >= 12 and pred_wpct <= 48:
        alert_code = "male_glut_lock"
        badge_men = f"⚠️60分後も男余り予測({pred_wpct}%)"
        badge_women = "👑1時間後も女子待ち0分圏"
        badge_tone = "danger"
        reason = f"実測データ上、男性先行が定着した店舗は60〜90分後も約57%が男余りを継続します（60分後予測：女性比{pred_wpct}%）。男性は近隣の女性優位店へのハシゴが安全です。"
    elif (
        cur_wpct >= 50
        and total >= 16
        and (
            (is_wknd and hr in (21, 22, 23) and cur_wpct <= 66 and flip_risk >= 22)
            or (delta_pt <= -6 and pred_wpct <= 53)
            or (m_d15 >= w_d15 + 4 and cur_wpct <= 64)
        )
    ):
        alert_code = "crowd_rush_warn"
        badge_men = f"⚡殺到注意・60分後{pred_wpct}%予測"
        badge_women = "💄これから男性増・即相席圏"
        badge_tone = "warn"
        reason = f"現在は女性優位（{cur_wpct}%）ですが、この時間帯は画面を見て向かう男性が集中しやすく、到着頃には女性比{pred_wpct}%前後へ低下する『群衆殺到リスク（逆転確率{flip_risk}%）』があります。"
    elif hr in (22, 23) and mid_ret <= 62 and delta_pt <= -5:
        alert_code = "last_train_cliff"
        badge_men = f"🚃終電減注意・60分後{pred_wpct}%"
        badge_women = "🟢落ち着いた会話狙い目"
        badge_tone = "warn"
        reason = f"終電前の帰宅波が出やすい1回転目中心店舗（深夜維持率{mid_ret}%）のため、23時台後半にかけて女性が減少する予測です。"
    elif is_rain_shelter and pred_wpct >= 55 and women >= 10:
        alert_code = "rain_shelter_boost"
        badge_men = f"☔雨天駅直結◎ 60分後{pred_wpct}%"
        badge_women = "☔駅直結・濡れずに来店可"
        badge_tone = "rain"
        reason = f"現在{city_w.get('city_label', '')}エリアは{city_w.get('condition_ja', '雨')}のため、駅・地下街直結の本店舗に女性客が集中しやすく60分後も女性比{pred_wpct}%前後を維持する予測です。"
    elif women >= 18 and cur_wpct >= 68 and pred_wpct >= 60:
        alert_code = "jackpot_hold"
        badge_men = f"🛡️確変持続・60分後{pred_wpct}%予測"
        badge_women = "⏳女性多数（相席待ち注意）"
        badge_tone = "safe"
        reason = f"女性{women}名＆女性比{cur_wpct}%の『70%安全バッファ』に入っており、これから男性が増えても60分後に女性優位を維持する確率は{100 - flip_risk}%と極めて安定しています。"
    elif hr in (2, 3, 4) and women >= 10 and pred_wpct >= 54:
        alert_code = "first_train_rebound"
        badge_men = f"🌅始発待ち女子優位({pred_wpct}%予測)"
        badge_women = "🌙始発までゆったり滞在可"
        badge_tone = "safe"
        reason = f"深夜2時以降は男性が先に退店し始発待ちの女性ペアが残るため、朝方まで女性比{pred_wpct}%前後の好バランスが続く予測です。"
    else:
        alert_code = "steady"
        badge_men = f"🔮60分後 {pred_wpct}%予測" if total >= 14 else ""
        badge_women = "🟢1時間後も待ち少なめ" if men >= women - 2 and total >= 12 else ""
        badge_tone = "safe" if pred_wpct >= 56 and women >= 12 else "neutral"
        reason = f"直近の流入ペースと時間帯波形から、60分後は女性約{pred_w}名・男性約{pred_m}名（女性比{pred_wpct}%前後）で推移する見込みです。"

    if arena_signal and alert_code in ("steady", "jackpot_hold", "rain_shelter_boost"):
        reason = f"【{arena_signal['label_short']}】" + reason

    return {
        "pred_60m_wpct": pred_wpct,
        "pred_60m_women": pred_w,
        "pred_60m_men": pred_m,
        "pred_delta_pt": delta_pt,
        "flip_risk_pct": flip_risk,
        "alert_code": alert_code,
        "badge_men": badge_men,
        "badge_women": badge_women,
        "badge_tone": badge_tone,
        "is_rain_shelter": is_rain_shelter,
        "arena_signal_label": arena_signal.get("label_short", "") if arena_signal else "",
        "reason_short": reason,
    }


def enrich_stores_with_moat_signals(stores: list, now_jst: datetime.datetime = None, allow_weather_blocking: bool = False) -> list:
    """
    店舗リスト（ranking または full_data）に対して一括で：
      1) JIS POS実測データからのオンライン学習キャリブレーション
      2) Open-Meteo 都市別気象キャッシュ参照（非ブロッキング）
      3) ドーム・アリーナ終演流入シグナル検知
      4) 全店舗の「リアル待ち時間推論（wait_info）」＆「60分後男女比予測バッジ（forecast_60m）」付与
    を実行して返す（全63店舗合計で約0.4ms以内の超高速インメモリ処理）。
    """
    if not stores:
        return stores
    now_jst = now_jst or datetime.datetime.now(JST)
    calibrate_from_jis_pos(stores)
    weather_map = get_city_weather_map(allow_blocking=allow_weather_blocking)
    arena_map = detect_arena_event_surges(stores, now_jst=now_jst)

    for s in stores:
        sname = s.get("name") or ""
        w = int(s.get("women") or 0)
        m = int(s.get("men") or 0)
        is_open = bool(s.get("is_open_now", True))
        w_diff = int(s.get("women_diff") or 0)
        m_diff = int(s.get("men_diff") or 0)
        uw_diff = s.get("usual_w_diff")
        pos_tel = s.get("pos_telemetry")

        wait_info = estimate_store_wait_and_pos(
            store_name=sname,
            women=w,
            men=m,
            is_open_now=is_open,
            women_diff=w_diff,
            men_diff=m_diff,
            pos_telemetry=pos_tel,
        )
        fc60 = predict_store_60m_dynamics(
            store_name=sname,
            women=w,
            men=m,
            is_open_now=is_open,
            women_diff=w_diff,
            men_diff=m_diff,
            usual_w_diff=uw_diff,
            now_jst=now_jst,
            weather_map=weather_map,
            arena_signal=arena_map.get(sname),
        )
        s["wait_estimate"] = wait_info
        s["forecast_60m"] = fc60
        ck = city_key_for_store(sname)
        if ck and ck in weather_map:
            cw = weather_map[ck]
            s["weather_brief"] = {
                "city": cw["city_label"],
                "condition": cw["condition_ja"],
                "temp_c": cw["temp_c"],
                "precip_mm": cw["precip_mm"],
                "is_rainy": cw["is_rainy"],
            }
    return stores
