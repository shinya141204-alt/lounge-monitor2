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


def _normalize_jst(dt: datetime.datetime = None) -> datetime.datetime:
    """任意のdatetime（None / naive / UTC等のaware）を確実にJST awareへ正規化する。"""
    if dt is None:
        return datetime.datetime.now(JST)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def detect_arena_event_surges(live_stores: list, now_jst: datetime.datetime = None) -> dict:
    """
    ライブ終演後ゴールデンタイム（19:00〜22:50）において、ドーム・アリーナ周辺店舗で
    普段の同曜日・同時間帯平均を大きく上回る女性急増（usual_w_diff >= 10 または 15分比+6以上）が
    発生しているハブを自動検知し、{store_name: event_signal_dict} を返す。
    また job_state / 環境変数 `ARENA_ACTIVE_VENUES` に明示登録された会場があれば優先付与する。
    """
    now_jst = _normalize_jst(now_jst)
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
    now_jst = _normalize_jst(now_jst)
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
# 5. 実測マルコフ状態空間モデル（v2.2）に基づく「60分後の男女比予測エンジン」
# ==============================================================================
FORECAST_MODEL_VERSION = "v2.2-statespace"

# 時系列ホールドアウト検証ベンチマーク（学習 <=2026-10-15 12:00 / 未来未見テスト >2026-10-15 12:00, n=14,542 遷移ペアでの実測値）
HOLDOUT_BACKTEST_SUMMARY = {
    "test_samples": 14542,
    "v1_mae_wpct": 11.17,
    "v1_rmse_wpct": 15.55,
    "v1_within_5pt_pct": 37.2,
    "v1_within_10pt_pct": 61.1,
    "v1_brier_flip": 0.1845,
    "naive_mae_wpct": 10.68,
    "v2_mae_wpct": 7.91,
    "v2_rmse_wpct": 10.89,
    "v2_within_5pt_pct": 47.7,
    "v2_within_10pt_pct": 74.0,
    "v2_brier_flip": 0.1353,
    "mae_reduction_pct": 29.2,
}

# 旧v1モデル係数（リアルタイム精度監査で v1 vs v2.1 の改善幅を同一条件比較するために保持）
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

# v2.1 連続時刻ノット状態空間係数（各時台 h:30 を中心にリッジ正則化学習）
# 特徴量(9次元): [early_factor*(p99/100), w, m, wd15, md15, mid_r*w, closing*w, closing*m, occ*(w+m)]
_V2_KNOTS_W = {
    (0, 17): (2.34399, 1.44222, 0.46245, 0.55623, -0.21589, 0.0109, 0.0, 0.0, -2.0551),
    (0, 18): (2.57971, 1.49022, 0.67255, 0.6048, -0.31031, -0.08018, 0.0, 0.0, -2.44284),
    (0, 19): (4.25965, 1.1873, 0.69845, 0.5764, 0.01246, 0.05096, 0.0, 0.0, -1.07291),
    (0, 20): (6.65422, 0.98308, 0.7252, 0.45296, 0.13602, 0.20851, 0.0, 0.0, -0.58227),
    (0, 21): (8.71981, 0.95528, 0.48948, 0.34536, 0.24108, 0.33915, 0.0, 0.0, -0.46635),
    (0, 22): (1.52262, 0.90716, 0.12149, 0.30518, 0.46015, 0.47101, -0.72714, -0.48935, -0.27349),
    (0, 23): (0.0, 0.49187, 0.41312, 0.15942, -0.0382, 0.62673, -0.48761, -0.30234, -0.08542),
    (0, 0): (0.0, 0.63421, 0.28103, 0.23999, -0.00702, 0.27899, -0.62912, -0.32079, -0.01194),
    (0, 1): (0.0, 0.64372, 0.17671, 0.3941, 0.10142, 0.09706, -0.53416, -0.17731, 0.01681),
    (0, 2): (0.0, 0.58693, 0.11405, 0.07549, 0.04512, 0.04309, -0.43459, 0.05482, 0.05396),
    (0, 3): (0.0, 0.50396, 0.10973, -0.02669, 0.01323, -0.12116, -0.46333, 0.08552, 0.14055),
    (0, 4): (0.0, 0.41134, 0.12123, -0.10843, 0.04805, -0.13836, -0.31328, -0.08617, 0.22649),
    (0, 5): (0.0, 0.32188, 0.10286, -0.14149, 0.0847, -0.03286, -0.18704, -0.10702, 0.14313),
    (1, 17): (5.41426, 1.66463, 1.01242, 0.71046, -0.41115, -0.26776, 0.0, 0.0, -1.34711),
    (1, 18): (7.71045, 1.54141, 0.77991, 0.61065, -0.12202, -0.2608, 0.0, 0.0, -0.99055),
    (1, 19): (10.09483, 1.41759, 0.82012, 0.40396, 0.0529, -0.08348, 0.0, 0.0, -0.76075),
    (1, 20): (12.85457, 1.33751, 0.82783, 0.10503, 0.05249, -0.0018, 0.0, 0.0, -0.62927),
    (1, 21): (10.45512, 1.32702, 0.63528, 0.08066, 0.27509, 0.01779, 0.0, 0.0, -0.55006),
    (1, 22): (1.27986, 1.15374, 0.35584, 0.17031, 0.46364, 0.19108, 0.0, 0.0, -0.33811),
    (1, 23): (0.0, 0.82137, 0.47857, 0.01246, -0.00997, 0.32492, 0.0, 0.0, -0.21368),
    (1, 0): (0.0, 0.82295, 0.32711, 0.15331, 0.09323, 0.15183, -0.58841, -0.62277, -0.12432),
    (1, 1): (0.0, 0.62152, 0.11023, 0.32852, 0.11417, -0.01473, -0.07135, -0.52047, 0.0904),
    (1, 2): (0.0, 0.5566, 0.05225, 0.06622, -0.04086, -0.0457, -0.07776, -0.35248, 0.13658),
    (1, 3): (0.0, 0.30702, 0.14031, -0.00947, -0.09373, -0.05133, -0.55462, -0.14056, 0.25225),
    (1, 4): (0.0, 0.0876, 0.2148, 0.02671, -0.14544, -0.03186, -0.2246, -0.41592, 0.3691),
    (1, 5): (0.0, 0.02393, 0.29163, 0.10029, -0.0608, -0.00818, -0.06556, -0.45622, 0.25214),
}

_V2_KNOTS_M = {
    (0, 17): (0.5345, 0.36889, 0.96879, 0.0198, 0.08135, -0.03205, 0.0, 0.0, -0.70421),
    (0, 18): (0.23561, 0.46439, 1.13751, 0.01242, 0.02578, 0.02814, 0.0, 0.0, -1.15322),
    (0, 19): (0.09991, 0.48022, 1.13746, -0.01027, 0.16726, 0.19732, 0.0, 0.0, -0.87378),
    (0, 20): (0.84633, 0.41816, 1.04447, 0.0393, 0.17664, 0.22284, 0.0, 0.0, -0.52026),
    (0, 21): (1.59991, 0.42741, 0.81532, 0.19863, 0.26971, 0.20716, 0.0, 0.0, -0.31013),
    (0, 22): (0.55325, 0.45277, 0.4617, 0.22617, 0.43758, 0.31938, -0.74521, -0.61765, -0.10402),
    (0, 23): (0.0, 0.22568, 0.55229, 0.08148, 0.02039, 0.42801, -0.37128, -0.49606, 0.06356),
    (0, 0): (0.0, 0.21313, 0.55038, 0.13408, -0.05047, 0.16514, -0.28115, -0.64452, 0.1044),
    (0, 1): (0.0, 0.16126, 0.48797, 0.35774, 0.04493, 0.04303, -0.11941, -0.58596, 0.14126),
    (0, 2): (0.0, 0.09784, 0.41131, 0.12434, -0.00404, 0.05859, -0.07065, -0.4506, 0.18271),
    (0, 3): (0.0, 0.05951, 0.37928, -0.01493, -0.12247, 0.00102, -0.06757, -0.33028, 0.18423),
    (0, 4): (0.0, 0.0694, 0.32082, -0.05021, -0.08796, -0.0374, -0.04959, -0.31636, 0.21163),
    (0, 5): (0.0, 0.10944, 0.20425, -0.08092, -0.04537, -0.02062, -0.05631, -0.21169, 0.1482),
    (1, 17): (0.84593, 0.54663, 1.08878, 0.06351, -0.08404, -0.37316, 0.0, 0.0, -0.477),
    (1, 18): (1.14921, 0.53753, 1.15312, 0.00724, -0.09342, -0.28279, 0.0, 0.0, -0.40356),
    (1, 19): (0.70557, 0.5302, 1.34919, 0.02815, -0.13884, -0.02443, 0.0, 0.0, -0.36403),
    (1, 20): (-1.50492, 0.61733, 1.32238, 0.0661, 0.00306, 0.04401, 0.0, 0.0, -0.44349),
    (1, 21): (-0.59035, 0.68253, 1.092, 0.11926, 0.2143, 0.04003, 0.0, 0.0, -0.41211),
    (1, 22): (0.51003, 0.68048, 0.80341, 0.20827, 0.36818, 0.10687, 0.0, 0.0, -0.29947),
    (1, 23): (0.0, 0.46605, 0.79533, 0.07458, 0.11754, 0.17724, 0.0, 0.0, -0.18542),
    (1, 0): (0.0, 0.31861, 0.75575, 0.10243, 0.05574, 0.09562, -0.40128, -0.42205, -0.0994),
    (1, 1): (0.0, 0.18815, 0.5553, 0.39614, 0.07912, -0.03749, 0.46369, -0.86066, 0.04548),
    (1, 2): (0.0, 0.11243, 0.45348, 0.10706, -0.07732, -0.06547, 0.41986, -0.73328, 0.09795),
    (1, 3): (0.0, -0.05106, 0.50897, 0.03159, -0.18691, -0.03466, -0.17039, -0.444, 0.13989),
    (1, 4): (0.0, -0.12988, 0.51934, 0.07713, -0.19687, -0.02328, 0.02109, -0.62983, 0.18448),
    (1, 5): (0.0, -0.04662, 0.48906, 0.16222, -0.08237, -0.05775, 0.01566, -0.55504, 0.0585),
}

# 女性比率直接キャリブレーション係数
# 特徴量(6次元): [1.0, cur_wp, (wd_c-md_c)*(100/max(16,tot)), mid_r*10, stab*(cur_wp-55), jackpot_buf]
_V2_KNOTS_WP = {
    (0, 17): (32.86978, 0.49775, 0.0416, 0.30299, 0.17716, -0.13924),
    (0, 18): (33.51383, 0.4856, 0.05231, -0.04997, 0.06148, -0.10227),
    (0, 19): (36.12369, 0.42276, 0.08403, -0.50407, 0.02359, -0.0391),
    (0, 20): (40.00738, 0.3262, 0.06954, -0.40545, -0.00187, 0.11608),
    (0, 21): (40.96908, 0.26043, 0.00743, -0.12163, 0.09032, 0.11679),
    (0, 22): (33.43485, 0.35504, -0.00413, 0.10766, -0.00478, 0.05279),
    (0, 23): (26.56177, 0.48403, -0.01036, 0.3084, -0.2211, 0.17037),
    (0, 0): (30.23789, 0.45729, -0.04384, 0.01672, 0.03806, -0.17979),
    (0, 1): (34.30722, 0.39701, -0.05363, -0.23682, 0.20443, -0.25363),
    (0, 2): (33.18626, 0.44355, -0.07142, -0.38854, 0.213, -0.32218),
    (0, 3): (39.75985, 0.36524, -0.03591, -0.63679, 0.28737, -0.50893),
    (0, 4): (42.07911, 0.33493, -0.03462, -0.63884, 0.25313, -0.55673),
    (0, 5): (44.80472, 0.29217, -0.03163, -0.57963, 0.03243, -0.4209),
    (1, 17): (54.88083, 0.25112, 0.0249, 0.67374, -0.40278, 0.14367),
    (1, 18): (52.65061, 0.26518, 0.03115, 0.55593, -0.32473, 0.1645),
    (1, 19): (43.90808, 0.32181, 0.05293, 0.10318, -0.23202, 0.21316),
    (1, 20): (36.27466, 0.36198, 0.01566, -0.11996, -0.13419, 0.26586),
    (1, 21): (34.15768, 0.33688, -0.04009, -0.08611, 0.08, 0.07532),
    (1, 22): (39.95293, 0.20385, -0.0411, 0.15926, 0.23944, -0.06678),
    (1, 23): (42.52641, 0.16503, -0.02004, 0.27913, 0.24686, -0.56318),
    (1, 0): (47.69001, 0.10209, -0.02261, 0.02453, 0.42235, -0.68018),
    (1, 1): (36.01555, 0.32912, -0.04359, -0.15851, 0.16513, -0.97816),
    (1, 2): (30.82593, 0.46017, -0.04962, -0.25044, 0.10565, -1.43367),
    (1, 3): (36.20453, 0.38556, -0.06274, -0.27449, 0.27063, 0.1356),
    (1, 4): (34.68636, 0.40082, -0.051, -0.19909, 0.19967, 0.36763),
    (1, 5): (28.90384, 0.45496, -0.07112, 0.19971, 0.11198, 0.65882),
}

# IRLS ロジスティック回帰による「男余り転落確率（60分後 女性比50%未満）」校正重み
# [1.0, (50 - pred_wp)/10, (50 - cur_wp)/10, 1/sqrt(tot), is_male_now, jackpot_safe]
_V2_FLIP_LOGISTIC_W = (-0.46813, 2.2327, -0.40703, 0.07081, 0.42114, -0.66526)


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


def _mins_until_close(store_name: str, now_jst: datetime.datetime) -> int:
    """公式営業時間（store_hours.py）に基づき、当営業シフトの閉店時刻までの残り分数を返す。"""
    try:
        import store_hours
        spec = store_hours.get_store_schedule_spec(store_name)
        is_korea = ("ソウル" in (store_name or "")) or ("カンナム" in (store_name or "")) or ("ホンデ" in (store_name or ""))
        if now_jst.hour < 12:
            biz_date = (now_jst - datetime.timedelta(days=1)).date()
            cur_m = (24 + now_jst.hour) * 60 + now_jst.minute
        else:
            biz_date = now_jst.date()
            cur_m = now_jst.hour * 60 + now_jst.minute
        wd = biz_date.weekday()
        next_iso = (biz_date + datetime.timedelta(days=1)).isoformat()
        biz_iso = biz_date.isoformat()
        is_pre = (not is_korea) and (next_iso in store_hours.JP_HOLIDAYS)
        is_hol = (not is_korea) and (biz_iso in store_hours.JP_HOLIDAYS)
        if is_pre or wd in (4, 5):
            _, close_h = spec["fs"]
        elif wd == 6 or is_hol:
            _, close_h = spec["sh"]
        else:
            _, close_h = spec["mt"]
        return int(close_h * 60 - cur_m)
    except Exception:
        cur_m = ((24 + now_jst.hour) if now_jst.hour < 12 else now_jst.hour) * 60 + now_jst.minute
        return int(29 * 60 - cur_m)


def _night_fractional_hour(now_jst: datetime.datetime) -> float:
    """夜間連続時刻（17.0〜30.99、深夜0〜11時は +24）に変換する。"""
    fhr = now_jst.hour + (now_jst.minute / 60.0)
    if now_jst.hour < 12:
        return fhr + 24.0
    if now_jst.hour < 17:
        return 17.0
    return fhr


def _interp_v2_knot(tbl: dict, wk_key: int, now_jst: datetime.datetime) -> list:
    """各時台中央（h:30）の学習ノット間を連続線形補間し、正時の不連続ジャンプを除去する。"""
    fhr = _night_fractional_hour(now_jst)
    if fhr <= 17.5:
        return list(tbl[(wk_key, 17)])
    if fhr >= 29.5:
        return list(tbl[(wk_key, 5)])
    h_lo_c = int(math.floor(fhr - 0.5))
    alpha = (fhr - 0.5) - h_lo_c
    h0 = h_lo_c - 24 if h_lo_c >= 24 else h_lo_c
    h1_c = h_lo_c + 1
    h1 = h1_c - 24 if h1_c >= 24 else h1_c
    k0 = tbl.get((wk_key, h0), tbl[(wk_key, 22)])
    k1 = tbl.get((wk_key, h1), k0)
    return [k0[j] * (1.0 - alpha) + k1[j] * alpha for j in range(len(k0))]


# 店舗×夜間フェーズ（序盤17-21h / ピーク22-00h / 深夜01-06h）の階層Empirical-Bayes事前残差オフセット
# 2026-10-01〜2026-10-15 12:00 の学習期間のみで推定（未来ホールドアウト期間のデータは一切不使用）
_V2_STORE_PHASE_PRIORS = {
    "ALFA HIROSHIMA": (1.55, 1.39, 3.25),
    "CLOVERS HIROSHIMA": (1.41, 1.08, 1.17),
    "JIS CHAYAMACHI": (3.54, 3.11, 4.5),
    "JIS FUKUOKA": (0.6, -0.97, 0.73),
    "JIS NAMBA": (1.74, 1.68, 3.06),
    "JIS NISHISHINJUKU": (-0.06, -0.53, 0.51),
    "JIS OMIYA": (2.72, 0.55, 4.18),
    "JIS SAPPORO": (-0.84, -0.26, 1.94),
    "JIS SHINJUKU": (0.77, -0.01, 2.2),
    "JIS UMEDA": (2.31, 0.05, 1.81),
    "OLG ag上野": (0.9, -1.74, 2.07),
    "OLG ag仙台": (2.04, 1.16, 4.5),
    "OLG ag名古屋": (-1.27, -3.02, -3.05),
    "OLG ag広島": (-0.3, 0.79, 0.95),
    "OLG ag梅田": (0.19, 1.42, 0.38),
    "OLG ag沖縄": (-1.26, -1.73, -0.27),
    "OLG ag渋谷": (1.53, -0.88, 1.63),
    "OLG ag金沢": (-0.85, 0.22, -0.43),
    "OLG 上野": (1.88, -0.21, 4.03),
    "OLG 京都": (-0.55, -1.79, 1.1),
    "OLG 名古屋 栄": (-1.46, 0.87, -0.04),
    "OLG 名古屋 錦": (0.7, -0.82, 0.35),
    "OLG 大分": (-1.59, -1.36, -0.54),
    "OLG 大宮": (2.93, 1.63, 1.7),
    "OLG 大阪駅前": (-1.46, -0.11, -0.52),
    "OLG 天満": (-2.81, 0.69, -0.35),
    "OLG 宇都宮": (-0.42, -2.74, 4.5),
    "OLG 宮崎": (-0.38, -0.28, -1.15),
    "OLG 小倉": (0.57, 2.43, 3.3),
    "OLG 岡山": (2.88, 3.57, 2.76),
    "OLG 心斎橋": (0.42, 1.14, -0.18),
    "OLG 恵比寿": (-0.6, -1.63, -0.03),
    "OLG 新宿": (4.2, 4.5, 3.71),
    "OLG 柏": (-0.26, 1.01, 1.27),
    "OLG 横浜": (-0.16, -1.36, 1.88),
    "OLG 浜松": (-3.35, -0.4, 4.32),
    "OLG 渋谷本店": (3.28, 0.54, -0.97),
    "OLG 熊本": (0.73, 1.74, 1.58),
    "OLG 町田": (0.48, 1.84, 2.25),
    "OLG 神戸": (-1.29, -0.99, 0.78),
    "OLG 福岡": (2.21, 0.65, 4.25),
    "OLG 錦糸町": (0.39, -1.28, 3.27),
    "OLG 長崎": (0.58, 0.06, 0.55),
    "OLG 難波": (2.85, 1.88, 1.34),
    "OLG 静岡": (-2.21, -0.42, 0.56),
    "OLG 高崎": (4.5, 4.5, 4.5),
    "OLG 鹿児島": (-0.26, 0.51, 4.5),
    "XIX OKAYAMA": (3.78, 4.5, 4.29),
    "YATAKOI UMEDA": (1.1, -1.19, -0.08),
}


def _interp_store_phase_prior(store_name: str, now_jst: datetime.datetime) -> float:
    """店舗固有フェーズ事前オフセットを連続時刻 fhr 上で滑らかに線形補間し、正時の不連続ジャンプを防ぐ。"""
    tup = _V2_STORE_PHASE_PRIORS.get(store_name)
    if not tup:
        return 0.0
    p0, p1, p2 = tup
    fhr = _night_fractional_hour(now_jst)
    if fhr <= 19.5:
        return p0
    if fhr < 22.5:
        alpha = (fhr - 19.5) / 3.0
        return p0 * (1.0 - alpha) + p1 * alpha
    if fhr <= 24.0:
        return p1
    if fhr < 26.5:
        alpha = (fhr - 24.0) / 2.5
        return p1 * (1.0 - alpha) + p2 * alpha
    return p2


def _predict_store_60m_v1_legacy(
    store_name: str,
    women: int,
    men: int,
    is_open_now: bool = True,
    women_diff: int = 0,
    men_diff: int = 0,
    now_jst: datetime.datetime = None,
    weather_map: dict = None,
    arena_signal: dict = None,
) -> dict:
    """旧v1予測ロジック（精度比較ベンチマーク監査用に保持）。"""
    now_jst = _normalize_jst(now_jst)
    women = max(0, int(women or 0))
    men = max(0, int(men or 0))
    total = women + men
    if not is_open_now or total < 6:
        return {"pred_60m_wpct": None, "pred_60m_women": women, "pred_60m_men": men}

    cur_wpct = int(round((women / float(total)) * 100))
    hr = now_jst.hour
    is_wknd = _is_weekend_or_preholiday(now_jst)
    drift_tbl = _WEEKEND_DRIFT_BY_HOUR if is_wknd else _WEEKDAY_DRIFT_BY_HOUR
    base_dw, base_dm, _ = drift_tbl.get(hr, (0.0, 0.0, 12))
    p99_cap = max(40, int(STORE_P99_CAPACITY.get(store_name, 120)))
    scale = max(0.38, min(1.45, p99_cap / 165.0))
    mid_ret = int(STORE_MIDNIGHT_RETENTION.get(store_name, 85))
    dw = base_dw * scale
    dm = base_dm * scale
    if hr in (22, 23):
        if mid_ret < 70:
            dw -= (75 - mid_ret) * 0.14 * scale
        elif mid_ret >= 115:
            dw += (mid_ret - 105) * 0.10 * scale
    w_d15 = int(women_diff or 0)
    m_d15 = int(men_diff or 0)
    dw += max(-8.0, min(10.0, w_d15 * 0.85))
    dm += max(-8.0, min(12.0, m_d15 * 1.05))
    if women >= 20 and cur_wpct >= 70:
        dw = max(dw, 9.0 * scale)
        dm = min(dm, 14.0 * scale)
    elif women >= 12 and men <= 3:
        dw -= 4.5
    elif cur_wpct <= 40 and men >= 15:
        dm = max(dm, -2.0)
    ck = city_key_for_store(store_name)
    city_w = (weather_map or {}).get(ck) if ck else None
    if bool(city_w and city_w.get("is_rainy") and store_name in UNDERGROUND_DIRECT_STORES):
        dw += 3.2 * scale
        dm -= 1.2 * scale
    if arena_signal and 19 <= hr <= 22:
        dw += 3.5 * scale
    pred_w = max(1, min(int(p99_cap * 0.72), int(round(women + dw))))
    pred_m = max(1, min(int(p99_cap * 0.72), int(round(men + dm))))
    pred_tot = max(2, pred_w + pred_m)
    return {
        "pred_60m_wpct": int(round((pred_w / float(pred_tot)) * 100)),
        "pred_60m_women": pred_w,
        "pred_60m_men": pred_m,
    }


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
    apply_online_bias: bool = True,
) -> dict:
    """
    状態空間マルコフ遷移モデル（v2.2）に基づき、当該店舗の「60分後の予測女性比・予測男女数・男余り転落リスク・アラートバッジ」を返す。
    1) 連続時刻ノット補間（h:30基準）により正時の階段状ジャンプを解消
    2) 男女クロス結合（現在の女性数→60分後男性流入、稼働率飽和、閉店前ハザード減衰）を反映
    3) 階層Empirical-Bayes店舗×フェーズ事前オフセット＋店舗別30日普段カーブ縮小＋直近残差のオンライン自己補正を統合
    """
    now_jst = _normalize_jst(now_jst)
    women = max(0, int(women or 0))
    men = max(0, int(men or 0))
    total = women + men

    if not is_open_now or total < 6:
        return {
            "model_version": FORECAST_MODEL_VERSION,
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

    cur_wp_float = (women / float(total)) * 100.0
    cur_wpct = int(round(cur_wp_float))
    hr = now_jst.hour
    is_wknd = _is_weekend_or_preholiday(now_jst)
    wk_key = 1 if is_wknd else 0

    p99_cap = max(40, int(STORE_P99_CAPACITY.get(store_name, 120)))
    p99_f = float(p99_cap)
    scale = max(0.38, min(1.45, p99_f / 165.0))
    mid_ret = int(STORE_MIDNIGHT_RETENTION.get(store_name, 85))
    mid_r = (float(mid_ret) - 85.0) / 100.0

    w_d15 = int(women_diff or 0)
    m_d15 = int(men_diff or 0)
    wd_c = max(-16.0, min(18.0, float(w_d15)))
    md_c = max(-16.0, min(18.0, float(m_d15)))

    fhr = _night_fractional_hour(now_jst)
    if fhr <= 17.5:
        ef = 1.0
    elif fhr >= 21.5:
        ef = 0.0
    else:
        ef = max(0.0, min(1.0, (21.5 - fhr) / 4.0))

    mtc = _mins_until_close(store_name, now_jst)
    closing = max(0.0, min(1.5, (90.0 - float(mtc)) / 60.0)) if mtc < 90 else 0.0
    occ = min(1.2, float(total) / p99_f)

    feat_wm = (
        ef * (p99_f / 100.0),
        float(women),
        float(men),
        wd_c,
        md_c,
        mid_r * float(women),
        closing * float(women),
        closing * float(men),
        occ * float(total),
    )

    inv_tot = 100.0 / max(16.0, float(total))
    stab = min(1.0, max(0.0, (float(total) - 12.0) / 40.0))
    j_buf = (max(0.0, cur_wp_float - 66.0) * min(1.0, float(women) / 24.0)) if women >= 16 else 0.0
    feat_wp = (
        1.0,
        cur_wp_float,
        (wd_c - md_c) * inv_tot,
        mid_r * 10.0,
        stab * (cur_wp_float - 55.0),
        j_buf,
    )

    kw = _interp_v2_knot(_V2_KNOTS_W, wk_key, now_jst)
    km = _interp_v2_knot(_V2_KNOTS_M, wk_key, now_jst)
    kwp = _interp_v2_knot(_V2_KNOTS_WP, wk_key, now_jst)

    pw_raw = sum(a * b for a, b in zip(kw, feat_wm))
    pm_raw = sum(a * b for a, b in zip(km, feat_wm))
    pwp_dir = sum(a * b for a, b in zip(kwp, feat_wp))

    # 1) 普段比異常スパイクの平均回帰（usual_w_diff が与えられている場合、過剰上振れの減衰をわずかに織り込む）
    if usual_w_diff is not None:
        try:
            uwd = float(usual_w_diff)
            if abs(uwd) >= 10.0:
                pwp_dir -= max(-2.2, min(2.2, uwd * 0.05))
        except Exception:
            pass

    # 2) 「極端な女余り（男性不在）見切り退店」非線形補正
    if women >= 12 and men <= 2:
        pw_raw -= 3.5
        pwp_dir -= 3.0

    # 3) 雨天×地下街・駅直結ブースト ＆ ドーム・アリーナ終演後流入ブースト
    ck = city_key_for_store(store_name)
    city_w = (weather_map or {}).get(ck) if ck else None
    is_rain_shelter = bool(city_w and city_w.get("is_rainy") and store_name in UNDERGROUND_DIRECT_STORES)
    if is_rain_shelter:
        pw_raw += 2.4 * scale
        pm_raw -= 0.8 * scale
        pwp_dir += 1.4 if (m_d15 < w_d15 + 4) else 0.6

    if arena_signal and 19 <= hr <= 22:
        pw_raw += 2.8 * scale
        pwp_dir += 1.5

    # 4) 店舗別30日普段カーブ（t → t+60m）への非ブロッキング縮小推定
    if mtc >= 65 and store_name:
        try:
            import local_db
            usual_pair = local_db.get_cached_store_usual_pair(store_name, now_jst)
            if usual_pair:
                uw_t, um_t, uw_60, um_60, cnt_60 = usual_pair
                if cnt_60 >= 6 and (uw_60 + um_60) >= 8.0:
                    usual_wp_60 = (uw_60 / (uw_60 + um_60)) * 100.0
                    # 現在の普段乖離を65%維持しつつ、60分後の店舗固有定常バランスへ16%縮小
                    cur_anom = 0.0
                    if (uw_t + um_t) >= 8.0:
                        cur_anom = cur_wp_float - ((uw_t / (uw_t + um_t)) * 100.0)
                    emp_target_wp = max(15.0, min(85.0, usual_wp_60 + 0.55 * cur_anom))
                    shrink_w = min(0.16, 0.16 * (cnt_60 / (cnt_60 + 12.0)))
                    pwp_dir = (1.0 - shrink_w) * pwp_dir + shrink_w * emp_target_wp
        except Exception:
            pass

    # 5) 閉店直前ハザード減衰（公式閉店45分前以内は退店加速を厳密反映）
    if mtc <= 15:
        pw_raw *= 0.25
        pm_raw *= 0.25
    elif mtc <= 45:
        fac = 0.35 + 0.65 * ((float(mtc) - 15.0) / 30.0)
        pw_raw = min(pw_raw, float(women) * fac)
        pm_raw = min(pm_raw, float(men) * fac)

    pw = max(0.5, min(p99_f * 0.75, pw_raw))
    pm = max(0.5, min(p99_f * 0.75, pm_raw))
    wp_from_wm = (pw / (pw + pm)) * 100.0
    store_prior_adj = _interp_store_phase_prior(store_name, now_jst)
    wp_blend = max(8.0, min(92.0, 0.72 * pwp_dir + 0.28 * wp_from_wm + store_prior_adj))

    # 6) 蓄積された「予測 vs 60分後実測」残差ログからのオンラインEmpirical-Bayes自己補正
    online_bias_adj = 0.0
    if apply_online_bias and store_name:
        try:
            import local_db
            online_bias_adj = float(local_db.get_cached_forecast_residual_adjustment(store_name, hr) or 0.0)
            if online_bias_adj != 0.0:
                wp_blend = max(8.0, min(92.0, wp_blend + online_bias_adj))
        except Exception:
            online_bias_adj = 0.0

    cap_side = int(p99_f * 0.75)
    tot_int = max(2, min(cap_side * 2, int(round(max(2.0, pw + pm)))))
    raw_w = int(round(tot_int * (wp_blend / 100.0)))
    if men == 0 and wp_blend >= 75.0:
        pred_w = max(2, min(cap_side, max(raw_w, tot_int)))
        pred_m = 0
    elif women == 0 and wp_blend <= 25.0:
        pred_w = 0
        pred_m = max(2, min(cap_side, tot_int))
    else:
        pred_w = max(1, min(cap_side, min(tot_int - 1, raw_w)))
        pred_m = max(1, min(cap_side, tot_int - pred_w))
    pred_wpct = int(round(wp_blend))
    delta_pt = pred_wpct - cur_wpct

    # 男余り転落リスク（60分後に女性比50%未満になる校正済み確率%）の算出
    inv_sqrt_n = 1.0 / math.sqrt(max(6.0, float(total)))
    is_m_now = 1.0 if cur_wp_float < 50.0 else 0.0
    j_safe = 1.0 if (women >= 18 and cur_wp_float >= 68.0) else 0.0
    rush_z = 0.35 * max(0.0, float(m_d15 - w_d15) / 5.0) if m_d15 > w_d15 else 0.0
    z_flip = (
        _V2_FLIP_LOGISTIC_W[0]
        + _V2_FLIP_LOGISTIC_W[1] * ((50.0 - wp_blend) / 10.0)
        + _V2_FLIP_LOGISTIC_W[2] * ((50.0 - cur_wp_float) / 10.0)
        + _V2_FLIP_LOGISTIC_W[3] * inv_sqrt_n
        + _V2_FLIP_LOGISTIC_W[4] * is_m_now
        + _V2_FLIP_LOGISTIC_W[5] * j_safe
        + rush_z
    )
    z_flip = max(-12.0, min(12.0, z_flip))
    flip_risk = max(3, min(94, int(round(100.0 / (1.0 + math.exp(-z_flip))))))

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
    elif is_rain_shelter and pred_wpct >= 55 and women >= 10 and flip_risk < 22:
        alert_code = "rain_shelter_boost"
        badge_men = f"☔雨天駅直結◎ 60分後{pred_wpct}%"
        badge_women = "☔駅直結・濡れずに来店可"
        badge_tone = "rain"
        reason = f"現在{city_w.get('city_label', '')}エリアは{city_w.get('condition_ja', '雨')}のため、駅・地下街直結の本店舗に女性客が集中しやすく60分後も女性比{pred_wpct}%前後を維持する予測です。"
    elif women >= 18 and cur_wpct >= 68 and pred_wpct >= 58 and flip_risk <= 15:
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
        "model_version": FORECAST_MODEL_VERSION,
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
        "online_bias_adj_pt": round(online_bias_adj, 1),
        "reason_short": reason,
    }


def enrich_stores_with_moat_signals(stores: list, now_jst: datetime.datetime = None, allow_weather_blocking: bool = False) -> list:
    """
    店舗リスト（ranking または full_data）に対して一括で：
      1) JIS POS実測データからのオンライン学習キャリブレーション
      2) Open-Meteo 都市別気象キャッシュ参照（非ブロッキング）
      3) ドーム・アリーナ終演流入シグナル検知
      4) 全店舗の「リアル待ち時間推論（wait_info）」＆「60分後男女比予測バッジ（forecast_60m）」＋予測精度実績の付与
    を実行して返す（全63店舗合計で約0.5ms以内の超高速インメモリ処理）。
    """
    if not stores:
        return stores
    now_jst = _normalize_jst(now_jst)
    calibrate_from_jis_pos(stores)
    weather_map = get_city_weather_map(allow_blocking=allow_weather_blocking)
    arena_map = detect_arena_event_surges(stores, now_jst=now_jst)

    acc_map = {}
    try:
        import local_db
        acc_map = local_db.get_cached_store_accuracy_map() or {}
    except Exception:
        acc_map = {}

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
        st_acc = acc_map.get(sname)
        if st_acc:
            fc60["accuracy"] = st_acc
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
