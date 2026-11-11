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
from typing import Any, Optional

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

# 終電後の深夜維持率（21-22時平均に対する0-1時女性維持率%）の初期事前分布（モデル内部用）。
# 過去ログから算出した静的スナップショットであり、推論の事前値としてのみ使用する。
# 公開UI・コラム・統計で「実測値」として直接表示しないこと（実測は store_hourly_rollups 由来の値を使う）。
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
    if not isinstance(store_name, str):
        return "tokyo"
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


def _safe_float(val, ndigits: int = 1, default=None):
    """異常文字列・非有限浮動小数点数（Inf/NaN）・不正型を安全に除外して丸める。"""
    if val is None:
        return default
    try:
        f = float(val)
        return round(f, ndigits) if math.isfinite(f) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_int(val, default: int = 0, min_val: int = None, max_val: int = None) -> int:
    """異常文字列・非有限浮動小数点数・多倍長整数・不正型を安全に整数化し、境界クランプする。"""
    if val is None:
        return default
    try:
        v = int(val)
        if min_val is not None and v < min_val:
            return min_val
        if max_val is not None and v > max_val:
            return max_val
        return v
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_round_int(val: Any, default: int = 0, min_val: int = None, max_val: int = None) -> int:
    """浮動小数点数を安全に四捨五入して整数へ変換（NaN/Inf/型異常時はdefaultへ安全フォールバック）。"""
    f = _safe_float(val)
    if f is None:
        return default
    try:
        rounded = round(f)
        return _safe_int(rounded, default=default, min_val=min_val, max_val=max_val)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_dict(val: Any) -> dict:
    """任意のオブジェクトを安全に dict 型として扱う（非辞書型は {} を返却）。"""
    return val if isinstance(val, dict) else {}


def get_store_weather_brief(store_name_or_city_key: str, weather_map: dict = None) -> dict:
    """店舗名または都市キーに対応する weather_brief 辞書を安全に返す。欠損時・破損時はデフォルトへ完全フォールバック。"""
    if not isinstance(store_name_or_city_key, str) or not store_name_or_city_key.strip():
        ck = ""
    else:
        ck = city_key_for_store(store_name_or_city_key) if store_name_or_city_key not in WEATHER_CITIES else store_name_or_city_key
    city_lbl = WEATHER_CITIES.get(ck, {}).get("label", "") if ck else ""
    default_brief = {
        "city": city_lbl,
        "condition": "晴れ",
        "temp_c": None,
        "precip_mm": 0.0,
        "is_rainy": False,
        "temp_prev_c": None,
        "temp_diff_c": None,
        "temp_diff_from_yesterday": None,
        "is_cold_surge": False,
        "temp_surge_flag": False,
        "temp_diff_label": "平年並み",
        "temp_surge_badge": None,
    }
    if not isinstance(weather_map, dict) or not ck:
        return default_brief

    cw = weather_map.get(ck)
    if not isinstance(cw, dict):
        return default_brief

    return {
        "city": cw.get("city_label") or city_lbl,
        "condition": cw.get("condition_ja", "晴れ"),
        "temp_c": cw.get("temp_c"),
        "precip_mm": cw.get("precip_mm", 0.0) if cw.get("precip_mm") is not None else 0.0,
        "is_rainy": bool(cw.get("is_rainy", False)),
        "temp_prev_c": cw.get("temp_prev_c"),
        "temp_diff_c": cw.get("temp_diff_c"),
        "temp_diff_from_yesterday": cw.get("temp_diff_from_yesterday"),
        "is_cold_surge": bool(cw.get("is_cold_surge")),
        "temp_surge_flag": bool(cw.get("temp_surge_flag")),
        "temp_diff_label": cw.get("temp_diff_label", "平年並み"),
        "temp_surge_badge": cw.get("temp_surge_badge"),
    }


def _fetch_open_meteo_all_cities(timeout_sec: float = 3.5) -> dict:
    """Open-Meteo APIで全国10都市の現在気象および前日気温を1リクエストで一括取得する（追加通信ゼロ）。"""
    lats = ",".join(str(WEATHER_CITIES[k]["lat"]) for k in _WEATHER_CITY_ORDER)
    lngs = ",".join(str(WEATHER_CITIES[k]["lng"]) for k in _WEATHER_CITY_ORDER)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lats}&longitude={lngs}"
        "&current=temperature_2m,precipitation,weather_code,wind_speed_10m"
        "&hourly=temperature_2m"
        "&past_days=2&forecast_days=1"
        "&timezone=Asia%2FTokyo"
    )
    resp = requests.get(url, timeout=timeout_sec)
    if resp.status_code != 200:
        return {}
    payload = resp.json()
    items = payload if isinstance(payload, list) else [payload]
    parsed = {}
    now_jst = datetime.datetime.now(JST)
    # 営業日基準（0〜4時台は前夜を今夜扱いするため2日前夜と比較）
    prev_night_base = (now_jst - datetime.timedelta(days=2 if now_jst.hour < 5 else 1)).strftime("%Y-%m-%d")

    for idx, city_key in enumerate(_WEATHER_CITY_ORDER):
        if idx >= len(items):
            break
        c_item = items[idx] if isinstance(items[idx], dict) else {}
        cur = c_item.get("current") if isinstance(c_item.get("current"), dict) else {}

        # 脆弱性4 防御: 不正文字列・NaN/Inf・破損型の安全パース
        temp_c = _safe_float(cur.get("temperature_2m"), 1)
        precip_mm = _safe_float(cur.get("precipitation"), 2)
        if precip_mm is not None and precip_mm < 0:
            precip_mm = 0.0
        wcode = _safe_int(cur.get("weather_code"), 0)
        if not (0 <= wcode <= 99):
            wcode = 0
        wind_kmh = _safe_float(cur.get("wind_speed_10m"), 1)
        if wind_kmh is not None and wind_kmh < 0:
            wind_kmh = 0.0

        label_ja, is_rainy = _weather_code_label(wcode, precip_mm if precip_mm is not None else 0.0)

        # 前日同時刻（24時間前）を第一優先とし、欠損時のみ前営業日夜（20〜23時平均）にフォールバック
        hourly = c_item.get("hourly") if isinstance(c_item.get("hourly"), dict) else {}
        h_times = hourly.get("time") if isinstance(hourly.get("time"), (list, tuple)) else []
        h_temps = hourly.get("temperature_2m") if isinstance(hourly.get("temperature_2m"), (list, tuple)) else []
        temp_prev_c = None
        ref_dt = now_jst
        if cur.get("time"):
            try:
                ref_dt = datetime.datetime.fromisoformat(str(cur["time"]))
            except Exception:
                ref_dt = now_jst
        prev_same_hour_iso = (ref_dt - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:00")
        used_24h_same_hour = False
        for t_str, t_val in zip(h_times, h_temps):
            if isinstance(t_str, str) and t_str.startswith(prev_same_hour_iso) and t_val is not None:
                v = _safe_float(t_val, 1)
                if v is not None:
                    temp_prev_c = v
                    used_24h_same_hour = True
                    break

        if temp_prev_c is None:
            prev_night_temps = []
            target_night_hours = {
                f"{prev_night_base}T20:00", f"{prev_night_base}T21:00",
                f"{prev_night_base}T22:00", f"{prev_night_base}T23:00"
            }
            for t_str, t_val in zip(h_times, h_temps):
                if t_str in target_night_hours and t_val is not None:
                    v = _safe_float(t_val, 1)
                    if v is not None:
                        prev_night_temps.append(v)
            if prev_night_temps:
                temp_prev_c = round(sum(prev_night_temps) / len(prev_night_temps), 1)

        # 前日比寒暖差（Δ℃）および急冷サージ判定（安全フォールバック保証）
        temp_diff_c = None
        is_cold_surge = False
        temp_diff_label = "平年並み"
        temp_surge_badge = None

        if temp_c is not None and temp_prev_c is not None:
            diff = _safe_float(temp_c - temp_prev_c, 1)
            if diff is not None:
                temp_diff_c = diff
                allow_surge = bool(used_24h_same_hour or not (isinstance(cur.get("time"), str) and ref_dt.hour < 6))
                is_cold_surge = bool(allow_surge and temp_diff_c <= -3.0)
                if is_cold_surge:
                    temp_diff_label = "急冷"
                    temp_surge_badge = f"前日比 {temp_diff_c:+.1f}℃ 急冷"
                elif allow_surge and temp_diff_c <= -2.0:
                    temp_diff_label = "寒暖差大"
                    temp_surge_badge = f"前日比 {temp_diff_c:+.1f}℃"
                elif allow_surge and temp_diff_c >= 3.0:
                    temp_diff_label = "温暖"
                    temp_surge_badge = f"前日比 {temp_diff_c:+.1f}℃ 温暖"
                elif allow_surge and temp_diff_c >= 2.0:
                    temp_diff_label = "寒暖差大"
                    temp_surge_badge = f"前日比 {temp_diff_c:+.1f}℃"
                else:
                    temp_diff_label = "平年並み"

        parsed[city_key] = {
            "city_key": city_key,
            "city_label": WEATHER_CITIES[city_key]["label"],
            "temp_c": temp_c,
            "precip_mm": precip_mm,
            "weather_code": wcode,
            "wind_kmh": wind_kmh,
            "condition_ja": label_ja,
            "is_rainy": is_rainy,
            "temp_prev_c": temp_prev_c,
            "temp_diff_c": temp_diff_c,
            "temp_diff_from_yesterday": temp_diff_c,
            "is_cold_surge": is_cold_surge,
            "temp_surge_flag": is_cold_surge,
            "temp_diff_label": temp_diff_label,
            "temp_surge_badge": temp_surge_badge,
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
        fail_age = now_mono - _weather_state.get("fail_ts", -999.0)
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
                    _weather_state["fail_ts"] = -999.0
                return fresh
            with _weather_lock:
                _weather_state["fail_ts"] = time.monotonic()
        except Exception as e:
            with _weather_lock:
                _weather_state["fail_ts"] = time.monotonic()
            logger.debug(f"[moat_intelligence] weather fetch skipped: {e}")
        return cached

    # Non-blocking background refresh (atomic claim inside lock to prevent duplicate thread bursts)
    should_spawn_bg = False
    if not already_fetching and fail_age >= 60.0 and os.environ.get("DISABLE_SCHEDULER") != "1":
        with _weather_lock:
            now_m2 = time.monotonic()
            if (
                not _weather_state["fetching"]
                and (now_m2 - _weather_state["ts"]) >= _WEATHER_TTL_SEC
                and (now_m2 - _weather_state.get("fail_ts", -999.0)) >= 60.0
            ):
                _weather_state["fetching"] = True
                should_spawn_bg = True

    if should_spawn_bg:
        def _bg():
            ok = False
            try:
                fresh = _fetch_open_meteo_all_cities(timeout_sec=3.5)
                if fresh:
                    ok = True
                    with _weather_lock:
                        _weather_state["cities"] = fresh
                        _weather_state["ts"] = time.monotonic()
                        _weather_state["fail_ts"] = -999.0
            except Exception:
                pass
            finally:
                with _weather_lock:
                    if not ok:
                        _weather_state["fail_ts"] = time.monotonic()
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
    # 明治神宮野球場（プロ野球ヤクルト・大学野球）
    {
        "venue_id": "meiji_jingu_stadium",
        "venue_name": "明治神宮野球場",
        "city_key": "tokyo",
        "requires_schedule": True,
        "stores": [
            "OLG 渋谷本店", "OLG ag渋谷", "ASOBIBAR 渋谷",
            "OLG 新宿", "JIS SHINJUKU", "ASOBIBAR 新宿",
            "OLG 恵比寿", "ASOBIBAR 恵比寿"
        ],
    },
    # 阪神甲子園球場（プロ野球阪神・高校野球）
    {
        "venue_id": "hanshin_koshien_stadium",
        "venue_name": "阪神甲子園球場",
        "city_key": "osaka",
        "requires_schedule": True,
        "stores": [
            "OLG 大阪駅前", "JIS UMEDA", "JIS CHAYAMACHI", "OLG ag梅田",
            "ASOBIBAR 梅田", "ASOBIBAR 茶屋町",
            "OLG 神戸", "ASOBIBAR 神戸 三宮",
            "OLG 難波", "JIS NAMBA"
        ],
    },
    # 日産スタジアム（横浜国際総合競技場）
    {
        "venue_id": "nissan_stadium",
        "venue_name": "日産スタジアム（横浜国際総合競技場）",
        "city_key": "yokohama",
        "requires_schedule": True,
        "stores": ["OLG 横浜"],
    },
    # 国立競技場（Jリーグ国立・サッカー日本代表戦）
    {
        "venue_id": "japan_national_stadium",
        "venue_name": "国立競技場",
        "city_key": "tokyo",
        "requires_schedule": True,
        "stores": [
            "OLG 新宿", "JIS SHINJUKU", "ASOBIBAR 新宿",
            "OLG 渋谷本店", "OLG ag渋谷", "ASOBIBAR 渋谷",
            "OLG 恵比寿"
        ],
    },
    # 大型花火大会（なにわ淀川、隅田川）
    {
        "venue_id": "yodogawa_fireworks",
        "venue_name": "なにわ淀川花火大会",
        "city_key": "osaka",
        "pattern_type": "fireworks",
        "requires_schedule": True,
        "stores": [
            "OLG 大阪駅前", "JIS UMEDA", "JIS CHAYAMACHI", "OLG ag梅田",
            "OLG 天満", "ASOBIBAR 梅田", "ASOBIBAR 茶屋町"
        ],
    },
    {
        "venue_id": "sumida_fireworks",
        "venue_name": "隅田川花火大会",
        "city_key": "tokyo",
        "pattern_type": "fireworks",
        "requires_schedule": True,
        "stores": ["OLG 上野", "OLG ag上野", "OLG 錦糸町", "OLG 新宿"],
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
    全国10大ドーム・アリーナの「公式公開スケジュール（事前予告）」と「ライブ終演後の実測女性急増（事後検証）」を統合検知する：
      1) `arena_calendar.get_arena_events_for_date`（またはDB `arena_schedule_events`）に当日公演がある場合：
         - 開演前〜公演中（14:00〜終演1時間前未満）：`phase="pre_announce"`（終演後の流入見込みを事前予告）
         - 終演ゴールデン帯（終演1時間前〜終演2時間後）：実測急増があれば `phase="verified_surge"`、なければ `phase="post_window"`
      2) スケジュール未登録会場でも、19:00〜22:50に普段比急増（`usual_w_diff >= 10` または 15分比 `+6` 以上）が
         発生しているハブは従来通り自動検知し、`{store_name: event_signal_dict}` を返す。
    """
    now_jst = _normalize_jst(now_jst)
    hr = now_jst.hour
    date_str = now_jst.strftime("%Y-%m-%d")
    b_date = _business_date(now_jst)
    explicit_venues = set(
        v.strip() for v in (os.environ.get("ARENA_ACTIVE_VENUES") or "").split(",") if v.strip()
    )
    try:
        import job_state
        raw_ev = job_state.get_value("moat:arena_events_today", allow_blocking=False)
        if raw_ev:
            data_ev = json.loads(raw_ev)
            if isinstance(data_ev, dict) and data_ev.get("date") in (date_str, b_date):
                for vid in data_ev.get("venues") or []:
                    explicit_venues.add(str(vid))
    except Exception:
        pass

    scheduled_by_venue = {}
    try:
        import arena_calendar
        scheduled_by_venue = arena_calendar.get_arena_events_for_date(b_date, allow_blocking=False) or {}
    except Exception:
        scheduled_by_venue = {}

    store_map = {}
    raw_names_by_canon = {}
    stores_list = live_stores if isinstance(live_stores, (list, tuple, set)) else []
    for s in stores_list:
        if not isinstance(s, dict):
            continue
        rname = str(s.get("name") or "").strip()
        if not rname:
            continue
        cname = _resolve_internal_store_key(rname) or rname
        store_map[cname] = s
        if rname != cname:
            store_map[rname] = s
            raw_names_by_canon.setdefault(cname, []).append(rname)

    signals_by_store = {}
    is_post_concert_window = 19 <= hr <= 22

    sched_map = _safe_dict(scheduled_by_venue)
    for hub in MAJOR_ARENA_HUBS:
        vid = hub["venue_id"]
        vname = hub["venue_name"]
        is_explicit = vid in explicit_venues
        sched_list = sched_map.get(vid) if isinstance(sched_map.get(vid), list) else []

        hub_usual_surges = 0
        max_w_diff = 0
        for sname in hub["stores"]:
            st = store_map.get(sname)
            if not st:
                continue
            w = _safe_int(st.get("women"), default=0, min_val=0, max_val=100000)
            raw_uw = st.get("usual_w_diff")
            uw_diff = _safe_float(raw_uw)
            w_diff_15 = _safe_int(st.get("women_diff"), default=0, min_val=-100000, max_val=100000)

            if w >= 16 and ((uw_diff is not None and uw_diff >= 10.0) or w_diff_15 >= 6):
                hub_usual_surges += 1
                uw_int = _safe_round_int(uw_diff, default=0, min_val=0, max_val=100000) if uw_diff is not None else 0
                max_w_diff = max(max_w_diff, uw_int, w_diff_15)

        needed = 1 if len(hub["stores"]) <= 2 else 2
        sig_template = None

        if sched_list:
            ev0 = sched_list[0] if isinstance(sched_list[0], dict) else {}
            ev_title = str(ev0.get("event_title") or "").strip()
            try:
                est_end = int(ev0.get("est_end_hour") or 21)
            except (TypeError, ValueError, OverflowError):
                est_end = 21
            v_short = str(ev0.get("venue_name") or vname.split("・")[0]).strip()
            win_start = max(18, est_end - 1)
            win_end_ext = est_end + 2
            eff_hr = hr + 24 if hr <= 1 else hr
            if 14 <= eff_hr < win_start:
                sig_template = {
                    "venue_id": vid,
                    "venue_name": vname,
                    "is_explicit_schedule": True,
                    "phase": "pre_announce",
                    "is_surge_now": False,
                    "event_title": ev_title,
                    "est_end_hour": est_end,
                    "surge_women_delta": max_w_diff,
                    "label_short": f"今夜 {v_short}公演（{est_end}時頃終演見込み）",
                }
            elif win_start <= eff_hr <= win_end_ext:
                verified = hub_usual_surges >= needed
                sig_template = {
                    "venue_id": vid,
                    "venue_name": vname,
                    "is_explicit_schedule": True,
                    "phase": "verified_surge" if verified else "post_window",
                    "is_surge_now": True,
                    "event_title": ev_title,
                    "est_end_hour": est_end,
                    "surge_women_delta": max_w_diff,
                    "label_short": (
                        f"{v_short}終演 · 女性流入中(+{max_w_diff}名)"
                        if (verified and max_w_diff > 0)
                        else f"{v_short}終演圏 · 女性流入警戒"
                    ),
                }

        requires_schedule = bool(hub.get("requires_schedule")) or (
            hub.get("pattern_type") in ("fireworks", "seasonal", "festival")
            or "fireworks" in vid
            or "花火" in vname
        )

        if sig_template is None:
            if requires_schedule and not is_explicit:
                pass  # 季節イベント・花火大会等は、公式スケジュールが存在しない日時の野良サージ発動を抑止
            elif is_explicit or (is_post_concert_window and hub_usual_surges >= needed):
                sig_template = {
                    "venue_id": vid,
                    "venue_name": vname,
                    "is_explicit_schedule": is_explicit,
                    "phase": "verified_surge" if hub_usual_surges >= needed else "post_window",
                    "is_surge_now": True,
                    "event_title": "",
                    "est_end_hour": 21,
                    "surge_women_delta": max_w_diff,
                    "label_short": f"{vname.split('・')[0]}終演圏 · 女性流入中",
                }

        if sig_template is not None:
            allow_low_headcount = bool(sig_template.get("is_explicit_schedule"))
            _PHASE_PRIORITY = {"verified_surge": 3, "post_window": 2, "pre_announce": 1}
            cur_prio = _PHASE_PRIORITY.get(sig_template.get("phase"), 0)

            for sname in hub["stores"]:
                st = store_map.get(sname)
                if not st:
                    continue
                try:
                    st_w = int(st.get("women") or 0)
                except (TypeError, ValueError, OverflowError):
                    st_w = 0

                if allow_low_headcount or st_w >= 12:
                    existing = signals_by_store.get(sname)
                    if existing:
                        ex_explicit = bool(existing.get("is_explicit_schedule"))
                        cur_explicit = bool(sig_template.get("is_explicit_schedule"))

                        # 1. 公式スケジュール登録ハブは、スケジュール未登録ハブ（野良サージ）で決して上書きされない
                        if ex_explicit and not cur_explicit:
                            continue

                        # 2. 既存がスケジュール未登録で新規が公式スケジュールの場合は優先昇格（上書き許可）
                        if not (not ex_explicit and cur_explicit):
                            # 両者が同一スケジュール種別（両方 explicit または両方 unexplicit）の場合
                            ex_prio = _PHASE_PRIORITY.get(existing.get("phase"), 0)
                            if ex_prio > cur_prio:
                                continue
                            if ex_prio == cur_prio and not cur_explicit:
                                ex_delta = int(existing.get("surge_women_delta") or 0)
                                cur_delta = int(sig_template.get("surge_women_delta") or 0)
                                # 未登録ハブ同士で流入急増数が同等以上の場合は既存を維持
                                if ex_delta >= cur_delta:
                                    continue

                    sig_obj = dict(sig_template)
                    signals_by_store[sname] = sig_obj
                    for r_alias in raw_names_by_canon.get(sname, ()):
                        signals_by_store[r_alias] = sig_obj
    return signals_by_store


def detect_city_event_surge(live_stores: list, now_jst: datetime.datetime = None) -> dict:
    """都市イベント（スタジアム・花火大会・アリーナ）の2次会サージ検知（detect_arena_event_surges への委譲）。"""
    return detect_arena_event_surges(live_stores, now_jst=now_jst)


def capture_external_context_snapshot(live_stores: list = None, allow_blocking: bool = True, now_jst: datetime.datetime = None) -> list:
    """
    10分刻みのDB保存・Moat Telemetryシート追記用に、全国10都市の現在気象＋イベント流入検知結果＋主要JR路線遅延状態の
    リスト `[{city_key, temp_c, precip_mm, weather_code, wind_kmh, is_rainy, active_arenas_json, transit_disrupted, transit_lines_json}, ...]` を返す。
    """
    now_jst = _normalize_jst(now_jst)
    weather_map = get_city_weather_map(allow_blocking=allow_blocking)
    arena_signals = detect_arena_event_surges(live_stores or [], now_jst=now_jst)
    transit_map = {}
    try:
        import arena_calendar
        transit_map = arena_calendar.get_city_transit_status(allow_blocking=False) or {}
    except Exception:
        transit_map = {}

    city_arenas = {}
    if isinstance(arena_signals, dict):
        for sname, sig in arena_signals.items():
            sig_d = _safe_dict(sig)
            if not sig_d.get("is_surge_now", True):
                continue
            ck = city_key_for_store(_resolve_internal_store_key(sname) or sname)
            vid = sig_d.get("venue_id")
            if ck and vid:
                city_arenas.setdefault(ck, set()).add(str(vid))

    rows = []
    w_map = _safe_dict(weather_map)
    tr_map = _safe_dict(transit_map)
    for ck in _WEATHER_CITY_ORDER:
        w = w_map.get(ck)
        if not isinstance(w, dict):
            continue
        arenas_list = sorted(city_arenas.get(ck) or [])
        tr_info = _safe_dict(tr_map.get(ck))
        delayed_lines = tr_info.get("delayed_lines") or []
        rows.append({
            "city_key": ck,
            "city_label": str(w.get("city_label") or ck),
            "temp_c": _safe_float(w.get("temp_c"), ndigits=1, default=None),
            "precip_mm": _safe_float(w.get("precip_mm"), ndigits=2, default=None),
            "weather_code": _safe_int(w.get("weather_code"), default=None),
            "wind_kmh": _safe_float(w.get("wind_kmh"), ndigits=1, default=None),
            "condition_ja": str(w.get("condition_ja") or "晴れ"),
            "is_rainy": 1 if w.get("is_rainy") else 0,
            "active_arenas_json": json.dumps(arenas_list, ensure_ascii=False),
            "transit_disrupted": 1 if tr_info.get("disrupted") else 0,
            "transit_lines_json": json.dumps(delayed_lines, ensure_ascii=False),
        })
    return rows


# ==============================================================================
# 4. JIS実測POSデータのオンライン自己キャリブレーション＆全店リアル待ち時間推論
# ==============================================================================
_calib_lock = threading.Lock()
_jis_calibration = {
    # 初期事前分布（JIS全10店のPOS構造に基づくベースライン係数）
    # 1組あたり人数差スラック控除後の実効男性超過人数 eff_male_excess から待ち組数を推論する重み
    "excess_to_wait_group_ratio": 0.36,   # 実効男性超過10名あたりの実測待ち組数係数
    "high_occ_threshold": 0.85,           # 満席回転詰まりで男性超過時の案内摩擦が出始める実効収容率
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
    ・実効男性超過人数（1組平均人数差スラック控除後）に対する実測待ち組数の発生比率
    を指数移動平均（EMA）でオンライン学習する。
    """
    if not stores_data:
        return dict(_jis_calibration)

    men_party_samples = []
    women_party_samples = []
    wait_ratio_samples = []

    stores_list = stores_data if isinstance(stores_data, (list, tuple, set)) else []
    for s in stores_list:
        if not isinstance(s, dict):
            continue
        pos = s.get("pos_telemetry")
        if not isinstance(pos, dict) or pos.get("telemetry_type") != "jis_pos":
            continue
        m_cust = _safe_int(pos.get("men_total_guests") or s.get("men"), default=0, min_val=0, max_val=100000)
        w_cust = _safe_int(pos.get("women_total_guests") or s.get("women"), default=0, min_val=0, max_val=100000)
        m_grp = _safe_int(pos.get("men_total_groups"), default=0, min_val=0, max_val=10000)
        w_grp = _safe_int(pos.get("women_total_groups"), default=0, min_val=0, max_val=10000)
        m_wait_grp = _safe_int(pos.get("men_waiting_groups"), default=0, min_val=0, max_val=10000)

        if m_cust >= 4 and m_grp >= 2:
            men_party_samples.append(max(1.0, min(3.5, m_cust / float(m_grp))))
        if w_cust >= 4 and w_grp >= 2:
            women_party_samples.append(max(1.0, min(3.5, w_cust / float(w_grp))))

        party_slack = min(8, max(1, int(round(w_cust * 0.05)))) if w_cust > 0 else 0
        eff_excess_men = (m_cust - w_cust) - party_slack
        if eff_excess_men >= 4 and (m_cust + w_cust) >= 16:
            wait_ratio_samples.append(max(0.05, min(0.65, m_wait_grp / float(eff_excess_men))))

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
    s_name = str(store_name or "").strip()
    canon_name = _resolve_internal_store_key(s_name) or s_name
    women = _safe_int(women, default=0, min_val=0, max_val=100000)
    men = _safe_int(men, default=0, min_val=0, max_val=100000)
    women_diff = _safe_int(women_diff, default=0, min_val=-100000, max_val=100000)
    men_diff = _safe_int(men_diff, default=0, min_val=-100000, max_val=100000)
    total = women + men
    p99_cap = max(35, int(STORE_P99_CAPACITY.get(canon_name, 120)))
    # STORE_P99_CAPACITY は全時間帯（平日・早朝深夜含む）の99%点のため、金土ピーク時の実効物理収容枠は約1.18倍
    physical_cap = max(45, int(round(p99_cap * 1.18)))
    occ_pct = min(100, int(round((total / float(physical_cap)) * 100)))

    if not is_open_now and total == 0:
        return {
            "is_measured": False,
            "is_standing_free": canon_name in FLAT_STANDING_STORES,
            "occupancy_pct": 0,
            "est_men_wait_groups": 0,
            "est_men_wait_min": 0,
            "est_women_wait_min": 0,
            "instant_match_prob_pct": 0,
            "wait_badge_men": "",
            "wait_badge_women": "",
            "wait_tone": "closed",
            "wait_tone_women": "closed",
            "pos_summary_short": "",
        }

    # --- Case A: 定額・スタンディング自由回遊型（ASOBIBAR 10店 / ヤタコイ梅田） ---
    if canon_name in FLAT_STANDING_STORES:
        pos_d = _safe_dict(pos_telemetry)
        vip_cnt = _safe_int(pos_d.get("vip_guests"), default=0, min_val=0, max_val=10000)
        vip_note = f"・VIP稼働{vip_cnt}" if vip_cnt > 0 else ""
        if total == 0:
            badge_m = "自由回遊（空席）"
            badge_w = "自由回遊（入場無料）"
        elif women >= men:
            badge_m = f"自由回遊 · 女性優位{vip_note}"
            badge_w = "自由回遊 · 入場無料"
        else:
            badge_m = f"自由回遊（席待ちなし{vip_note}）"
            badge_w = "男性多数 · 即乾杯圏"
        return {
            "is_measured": bool(isinstance(pos_telemetry, dict) and pos_telemetry),
            "is_standing_free": True,
            "occupancy_pct": occ_pct,
            "est_men_wait_groups": 0,
            "est_men_wait_min": 0,
            "est_women_wait_min": 0,
            "instant_match_prob_pct": 95 if women >= 4 else 50,
            "wait_badge_men": badge_m,
            "wait_badge_women": badge_w,
            "wait_tone": "standing",
            "wait_tone_women": "standing",
            "pos_summary_short": f"スタンディング回遊制（フロア稼働率 約{occ_pct}%{vip_note}）",
        }

    calib = get_jis_calibration_snapshot()

    # --- Case B: JIS全10店（公式POSの実測待機・相席成立テレメトリあり） ---
    if isinstance(pos_telemetry, dict) and pos_telemetry.get("telemetry_type") == "jis_pos":
        m_wait_grp = _safe_int(pos_telemetry.get("men_waiting_groups"), default=0, min_val=0, max_val=10000)
        m_wait_gst = _safe_int(pos_telemetry.get("men_waiting_guests"), default=0, min_val=0, max_val=100000)
        w_wait_grp = _safe_int(pos_telemetry.get("women_waiting_groups"), default=0, min_val=0, max_val=10000)
        matched_grp = _safe_int(pos_telemetry.get("matched_groups"), default=0, min_val=0, max_val=10000)
        m_grp = _safe_int(pos_telemetry.get("men_total_groups"), default=0, min_val=0, max_val=10000)
        w_grp = _safe_int(pos_telemetry.get("women_total_groups"), default=0, min_val=0, max_val=10000)
        vip_gst = _safe_int(pos_telemetry.get("vip_guests"), default=0, min_val=0, max_val=100000)

        avg_m_party = round(men / float(m_grp), 1) if m_grp > 0 and men > 0 else None
        party_style = ""
        if avg_m_party is not None:
            party_style = "ソロ多め" if avg_m_party <= 1.45 else ("ペア中心" if avg_m_party <= 2.15 else "グループ多め")

        # 店舗規模が大きいほど1分あたりの席回転が速い（p99=240の大箱は1組あたり約5分、中型は約7.5分）
        size_factor = max(0.65, min(1.35, 160.0 / float(p99_cap)))
        calib_m = _safe_float(_safe_dict(calib).get("minutes_per_wait_group"), default=5.0) or 5.0
        est_m_min = _safe_round_int(m_wait_grp * calib_m * size_factor, default=0, min_val=0, max_val=120)
        if m_wait_grp > 0:
            est_m_min = max(5, min(75, est_m_min))
        else:
            # POS実測で待ち0組のときは分数を捏造しない（実測バッジの純度維持）。
            # 女性0名で男性のみ在店の状態はラベル側（女性来店待ち）で示す。
            est_m_min = 0
        awaiting_women_only = bool(total > 0 and women == 0 and men >= 2 and m_wait_grp == 0)

        est_w_min = _safe_round_int(w_wait_grp * 5.0 * size_factor, default=0, min_val=0, max_val=60) if w_wait_grp > 0 else 0

        if total == 0:
            badge_m = "実測:待ち0組（空席）"
            badge_w = "実測:待ち0組（空席）"
            tone = "neutral"
            tone_w = "neutral"
            prob = 40
        else:
            if m_wait_grp == 0 and women >= max(4, men):
                badge_m = f"実測:男待ち0組（相席{matched_grp}卓）" if matched_grp > 0 else "実測:男待ち0組 · 即相席圏"
                tone = "safe"
                prob = 94
            elif awaiting_women_only:
                badge_m = "実測:男待ち0組 · 女性来店待ち"
                tone = "neutral"
                prob = 30
            elif m_wait_grp == 0:
                badge_m = "実測:男待ち0組"
                tone = "safe" if women >= 6 else "neutral"
                prob = 82 if women >= 6 else 55
            elif est_m_min <= 15:
                badge_m = f"実測:男待ち{m_wait_grp}組（約{est_m_min}分）"
                tone = "warn"
                prob = 45
            else:
                badge_m = f"実測:男待ち{m_wait_grp}組（約{est_m_min}分）"
                tone = "danger"
                prob = 18

            if w_wait_grp == 0:
                if m_wait_grp > 0 or men >= women + 4:
                    badge_w = "実測:女子待ち0組 · 即相席"
                else:
                    badge_w = "実測:女待ち0組"
                tone_w = "safe"
            elif est_w_min <= 15:
                badge_w = f"実測:女待ち{w_wait_grp}組（約{est_w_min}分）"
                tone_w = "warn"
            else:
                badge_w = f"実測:女待ち{w_wait_grp}組（約{est_w_min}分）"
                tone_w = "danger"

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
            "est_men_wait_min_is_estimated": False,
            "awaiting_women": awaiting_women_only,
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
            "wait_tone_women": tone_w,
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
            "wait_tone_women": "neutral",
            "pos_summary_short": "現在店内 0 : 0（1組目待機になりやすい時間帯）",
        }

    male_excess = men - women
    _m_d = _safe_int(men_diff, default=0, min_val=-100000, max_val=100000)
    _w_d = _safe_int(women_diff, default=0, min_val=-100000, max_val=100000)
    momentum_excess = max(0, _m_d - _w_d)
    # 収容率は必ず [0.0, 1.0] にクランプ（ピーク夜に total > cap となっても待機組数が発散しないようにする）
    occ_ratio = min(1.0, total / float(physical_cap))

    # --- 男性側の待ち組数・待ち分数推論 ---
    # 物理原則：女性人数 >= 男性人数（male_excess <= 0）のときは店内に単独着席中の女性卓が存在し、
    # 来店男性は新規空き卓を待たず既存女性卓へ即合流できるため、男性の相席相手待ち組数は常に 0 とする。
    if male_excess <= 0:
        est_m_groups = 0
        est_m_min = 0
        female_lead = women - men
        if women >= 8 and female_lead >= 3:
            badge_m = "待ち約0分 · 即相席圏"
            tone = "safe"
            prob = min(96, 82 + min(14, female_lead // 2 + 2))
            balance_note = f"女性+{female_lead}名優位・男性即案内圏"
        elif women >= 6:
            if occ_pct >= 92 or momentum_excess >= 6:
                badge_m = "待ち約0〜5分 · 受付順"
                tone = "safe"
                prob = 78
                balance_note = "女性優位〜同数（高稼働・受付順案内）"
            elif women >= 10:
                badge_m = "待ち約0分 · 即相席圏"
                tone = "safe"
                prob = 84
                balance_note = "女性優位〜同数・即相席圏"
            else:
                badge_m = "男待ち少 · 約0〜5分"
                tone = "safe"
                prob = 75
                balance_note = "女性優位〜同数・スムーズ案内圏"
        else:
            if female_lead >= 1 and women >= 3:
                badge_m = "待ち約0分 · 少人数"
                tone = "safe"
                prob = 68
                balance_note = f"女性+{female_lead}名先行（少人数稼働）"
            else:
                badge_m = "少人数 · 相手組数に注意"
                tone = "neutral"
                prob = 48
                balance_note = "立ち上がり少人数帯"
    elif women == 0:
        # 女性0名で男性のみ入店している状態
        calib_avg_m = _safe_float(_safe_dict(calib).get("avg_men_party_size"), default=1.82) or 1.82
        est_m_groups = max(1, _safe_round_int(men / max(1.2, calib_avg_m), default=1, min_val=1))
        est_m_min = max(10, min(45, _safe_round_int(est_m_groups * 7.5 / 5.0) * 5))
        tone = "warn" if est_m_min <= 15 else "danger"
        badge_m = f"女性待ち · 男約{est_m_groups}組"
        prob = 20
        balance_note = f"女性来店待ち（男性{men}名待機中）"
    else:
        # 男性超過（men > women > 0）
        # 1組あたり平均人数差・VIP同席による自然な男性人数バッファ（女性数の約5%、1〜8名）
        party_slack = min(8, max(1, _safe_round_int(women * 0.05, default=1, min_val=1, max_val=8)))
        eff_male_excess = max(0, male_excess - party_slack)

        if eff_male_excess == 0:
            est_m_groups = 0
            est_m_min = 0
            if occ_pct >= 90 or momentum_excess >= 6:
                badge_m = "男女拮抗 · 約0〜10分"
                tone = "neutral"
                prob = 62
                balance_note = "男女ほぼ同数（高稼働につき受付順）"
            else:
                badge_m = "男女均衡 · 約0〜5分"
                tone = "safe"
                prob = 70
                balance_note = "男女ほぼ同数でスムーズ推移中"
        else:
            calib_ratio = _safe_float(_safe_dict(calib).get("excess_to_wait_group_ratio"), default=0.45) or 0.45
            raw_wait_groups = eff_male_excess * calib_ratio
            high_thr = min(0.92, max(0.75, float(calib.get("high_occ_threshold", 0.85))))
            if occ_ratio > high_thr:
                congestion_over = (occ_ratio - high_thr) / max(0.05, 1.0 - high_thr)
                raw_wait_groups += congestion_over * 1.0
            if momentum_excess >= 6:
                raw_wait_groups += min(1.2, (momentum_excess - 4) * 0.15)

            calib_avg_m = _safe_float(_safe_dict(calib).get("avg_men_party_size"), default=1.82) or 1.82
            max_phys_groups = max(1, int(math.ceil(male_excess / max(1.4, calib_avg_m * 0.88))))
            est_m_groups = min(max_phys_groups, _safe_round_int(raw_wait_groups, default=0, min_val=0, max_val=max_phys_groups)) if raw_wait_groups >= 0.75 else 0

            if est_m_groups == 0:
                est_m_min = 0
                badge_m = "男待ち少 · 約0〜5分"
                tone = "safe"
                prob = 68
                balance_note = f"男性+{male_excess}名（1組人数差の範囲内）"
            else:
                size_factor = max(0.68, min(1.25, 155.0 / float(p99_cap)))
                calib_m = _safe_float(_safe_dict(calib).get("minutes_per_wait_group"), default=5.0) or 5.0
                est_m_min = _safe_round_int(est_m_groups * calib_m * size_factor, default=0, min_val=0, max_val=120)
                est_m_min = max(5, min(70, _safe_round_int(est_m_min / 5.0) * 5))
                if est_m_min <= 15:
                    badge_m = f"男 約{est_m_min}分待ち（約{est_m_groups}組）"
                    tone = "warn"
                    prob = max(38, 58 - est_m_min)
                else:
                    badge_m = f"男 約{est_m_min}分待ち（約{est_m_groups}組）"
                    tone = "danger"
                    prob = max(10, 42 - est_m_min // 2)
                balance_note = f"男性+{male_excess}名超過（待機 約{est_m_groups}組推計）"

    # --- 女性側の受付・相席待ち推論（女余り時 vs 男性先行時） ---
    female_excess = women - men
    est_w_min = 0
    if women >= 10 and female_excess >= max(10, int(round(men * 0.35))):
        f_slack = min(8, max(2, int(round(men * 0.08))))
        eff_f_excess = max(1, female_excess - f_slack)
        est_w_groups = max(1, int(round(eff_f_excess * 0.35)))
        est_w_min = min(35, max(10, int(round(est_w_groups * 6.0 / 5.0) * 5)))
    elif female_excess >= 14 and men < 8:
        est_w_min = min(30, max(10, int(round((female_excess - 8) * 1.1 / 5.0) * 5)))

    if est_w_min >= 20:
        badge_w = f"女余り · 相席まで約{est_w_min}分"
        tone_w = "danger"
    elif est_w_min >= 10:
        badge_w = f"女余り · 相席まで約{est_w_min}分"
        tone_w = "warn"
    elif men >= women + 4 and men >= 8:
        badge_w = "女子待ち0分 · 即相席圏"
        tone_w = "safe"
    else:
        badge_w = "女子待ち約0分"
        tone_w = "safe"

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
        "wait_tone_women": tone_w,
        "pos_summary_short": f"JIS待機学習推論：{balance_note} ／ フロア稼働率 約{occ_pct}%（即相席確率 {prob}%）",
    }


# ==============================================================================
# 5. 実測マルコフ状態空間＋都市内スピルオーバーモデル（v3.1）に基づく「60分後の男女比予測エンジン」
# ==============================================================================
FORECAST_MODEL_VERSION = "v3.1-statespace"

# 時系列ホールドアウト検証ベンチマーク（学習 <=2026-10-15 12:00 / 未来未見テスト >2026-10-15 12:00, n=15,331 クリーン遷移ペア t+60m>=4名 での実測値）
HOLDOUT_BACKTEST_SUMMARY = {
    "benchmark_model": "v2.0-holdout-baseline",
    "benchmark_note": "2026-10-15以降の未見15,331遷移ペア（t+60m>=4名）における固定ホールドアウト検証値。本番v3.0のリアルタイム実績は summary 側の実測集計を優先表示。",
    "test_samples": 15331,
    "v1_mae_wpct": 10.64,
    "v1_rmse_wpct": 14.47,
    "v1_within_5pt_pct": 37.1,
    "v1_within_10pt_pct": 61.5,
    "v1_brier_flip": 0.1846,
    "naive_mae_wpct": 9.87,
    "v2_mae_wpct": 7.3,
    "v2_rmse_wpct": 9.99,
    "v2_within_5pt_pct": 50.8,
    "v2_within_10pt_pct": 76.8,
    "v2_brier_flip": 0.1377,
    "mae_reduction_pct": 31.5,
}

# 旧v1モデル係数（リアルタイム精度監査で v1 vs v3.0 の改善幅を同一条件比較するために保持）
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

# v3.0 連続時刻ノット状態空間係数（各時台 h:30 を中心に階層IRLS Huberロバスト正則化学習）
# 特徴量(9次元): [early_factor*(p99/100), w, m, wd15, md15, mid_r*w, closing*w, closing*m, occ*(w+m)]
_V2_KNOTS_W = {
    (0, 17): (3.55825, 1.1877, 0.19558, 0.26875, -0.13596, 0.18655, 0.0, 0.0, 0.26932),
    (0, 18): (3.59446, 1.21717, 0.26781, 0.46154, -0.00242, 0.12375, 0.0, 0.0, -0.13552),
    (0, 19): (4.14828, 1.09731, 0.43389, 0.48948, 0.0677, 0.04066, 0.0, 0.0, -0.51149),
    (0, 20): (6.75887, 0.98443, 0.56933, 0.26966, 0.10107, 0.19623, 0.0, 0.0, -0.43975),
    (0, 21): (16.15014, 0.89654, 0.43092, 0.13849, 0.11578, 0.32705, 0.0, 0.0, -0.28081),
    (0, 22): (19.67766, 0.8476, 0.14275, 0.16688, 0.22013, 0.48327, -0.24455, -0.21285, -0.17603),
    (0, 23): (0.0, 0.58352, 0.31502, 0.10293, -0.03472, 0.67488, -0.46207, -0.21103, -0.08645),
    (0, 0): (0.0, 0.69403, 0.20502, 0.2073, -0.00071, 0.34874, -0.44189, -0.31595, 0.00085),
    (0, 1): (0.0, 0.66248, 0.09071, 0.19142, 0.0526, 0.17376, -0.17974, -0.36724, 0.06061),
    (0, 2): (0.0, 0.59215, 0.0732, -0.00227, 0.00227, 0.07021, -0.06526, -0.25874, 0.10486),
    (0, 3): (0.0, 0.54757, 0.08051, 0.04547, 0.00501, -0.19805, -0.26964, -0.07548, 0.16857),
    (0, 4): (0.0, 0.34347, 0.22922, 0.38723, 0.12424, -0.1331, 0.28591, -0.44607, 0.07575),
    (0, 5): (0.0, 0.24452, 0.22199, 0.31673, 0.27817, -0.18091, 0.51764, -0.47609, 0.02718),
    (1, 17): (5.72656, 1.15875, 0.36274, 0.39128, -0.31634, 0.27332, 0.0, 0.0, 0.44432),
    (1, 18): (5.3648, 1.40943, 0.37214, 0.45371, 0.03918, 0.1172, 0.0, 0.0, -0.3806),
    (1, 19): (10.55376, 1.36772, 0.42103, 0.248, 0.13208, -0.00251, 0.0, 0.0, -0.45994),
    (1, 20): (14.39107, 1.26526, 0.628, 0.09767, 0.06653, 0.09482, 0.0, 0.0, -0.43981),
    (1, 21): (22.32926, 1.22147, 0.54212, 0.1196, 0.25023, 0.06086, 0.0, 0.0, -0.40087),
    (1, 22): (13.94592, 1.12345, 0.23716, 0.23612, 0.54075, 0.19961, 0.0, 0.0, -0.24817),
    (1, 23): (0.0, 0.80909, 0.34262, -0.03845, -0.06164, 0.4246, 0.0, 0.0, -0.1389),
    (1, 0): (0.0, 0.80264, 0.20448, 0.12176, 0.02227, 0.19623, -0.11132, -0.11649, -0.05595),
    (1, 1): (0.0, 0.61465, 0.06245, 0.1455, 0.0115, 0.00369, -0.2346, -0.11168, 0.10745),
    (1, 2): (0.0, 0.58162, -0.00695, -0.0038, 0.00284, -0.05621, -0.41529, 0.27664, 0.14628),
    (1, 3): (0.0, 0.47351, 0.03153, -0.03624, -0.02108, -0.07979, -0.14291, -0.4028, 0.19109),
    (1, 4): (0.0, 0.17236, 0.30481, 0.39316, 0.00178, -0.06251, 0.71466, -0.92426, 0.1785),
    (1, 5): (0.0, 0.14558, 0.27436, 0.46397, 0.2352, -0.09402, 0.56785, -0.47055, 0.09329),
}

_V2_KNOTS_M = {
    (0, 17): (0.39873, 0.31521, 1.04999, 0.08076, -0.04134, -0.38911, 0.0, 0.0, 0.05682),
    (0, 18): (0.50847, 0.33257, 1.00933, 0.09475, 0.10282, -0.08572, 0.0, 0.0, -0.05612),
    (0, 19): (0.44953, 0.35482, 1.06732, 0.05499, 0.06754, 0.08848, 0.0, 0.0, -0.30432),
    (0, 20): (2.24947, 0.35345, 0.99265, 0.09574, 0.06851, 0.12084, 0.0, 0.0, -0.28241),
    (0, 21): (7.0906, 0.41278, 0.76805, 0.13134, 0.11327, 0.16324, 0.0, 0.0, -0.22655),
    (0, 22): (11.07286, 0.45557, 0.41991, 0.15717, 0.27139, 0.30942, -0.32735, -0.29968, -0.056),
    (0, 23): (0.0, 0.27958, 0.46026, 0.08835, 0.01375, 0.47073, -0.51363, -0.36676, 0.08422),
    (0, 0): (0.0, 0.21, 0.51366, 0.18975, -0.08034, 0.21637, -0.49242, -0.53567, 0.13295),
    (0, 1): (0.0, 0.18336, 0.41965, 0.17211, 0.04711, 0.06883, -0.17862, -0.45958, 0.16596),
    (0, 2): (0.0, 0.11648, 0.3899, 0.03068, -0.02984, 0.05735, 0.27461, -0.66, 0.18993),
    (0, 3): (0.0, 0.05418, 0.43037, 0.09365, -0.01186, -0.0529, 0.09993, -0.42761, 0.18889),
    (0, 4): (0.0, 0.0349, 0.41659, 0.24358, 0.1213, -0.03957, 0.29084, -0.44716, 0.09758),
    (0, 5): (0.0, 0.07405, 0.31772, 0.22267, 0.25261, -0.18477, 0.35841, -0.294, 0.06743),
    (1, 17): (0.64069, 0.49833, 1.26135, 0.00587, -0.0388, -0.54825, 0.0, 0.0, -0.62884),
    (1, 18): (0.77634, 0.4775, 1.11367, 0.03369, 0.11011, -0.28733, 0.0, 0.0, -0.37585),
    (1, 19): (1.028, 0.51918, 1.19032, -0.04195, 0.07199, -0.11137, 0.0, 0.0, -0.35686),
    (1, 20): (-1.13246, 0.57195, 1.25144, 0.0515, -0.04447, 0.0996, 0.0, 0.0, -0.34867),
    (1, 21): (0.11853, 0.67032, 1.00818, 0.16373, 0.1327, 0.05021, 0.0, 0.0, -0.33601),
    (1, 22): (7.33003, 0.69994, 0.64373, 0.20536, 0.42286, 0.1471, 0.0, 0.0, -0.22621),
    (1, 23): (0.0, 0.47706, 0.64882, 0.09082, 0.00796, 0.26052, 0.0, 0.0, -0.12009),
    (1, 0): (0.0, 0.3269, 0.58518, 0.09991, -0.01919, 0.11694, -0.0958, -0.09914, -0.01549),
    (1, 1): (0.0, 0.2105, 0.41198, 0.18462, -0.01165, 0.00264, 0.00501, 0.08535, 0.10732),
    (1, 2): (0.0, 0.15096, 0.36436, -0.02229, -0.02634, -0.02852, -0.25246, 0.41083, 0.10849),
    (1, 3): (0.0, 0.03886, 0.38576, 0.0285, -0.05961, -0.02261, 0.26293, -0.73678, 0.15365),
    (1, 4): (0.0, -0.06435, 0.47224, 0.2902, 0.01428, 0.00314, 0.64664, -0.8508, 0.14876),
    (1, 5): (0.0, 0.00593, 0.38455, 0.38802, 0.26963, -0.10376, 0.41416, -0.27889, 0.0923),
}

# 女性比率直接キャリブレーション係数（t+60m信頼度重み付きHuber IRLS推定・学習 <=2026-10-15 12:00）
# 特徴量(9次元): [1.0, cur_wp, wd_c*inv_tot, md_c*inv_tot, mid_r*10, k_gain*(cur_wp-53), jackpot_buf, max(0,42-cur_wp), occ*10]
_V2_KNOTS_WP = {
    (0, 17): (17.88582, 0.78394, 0.05406, 0.01819, 1.21829, -1.03347, 0.33615, 0.0551, -0.43799),
    (0, 18): (26.23835, 0.6117, 0.03344, -0.01357, 0.40464, -0.30002, 0.07808, 0.62426, -0.87306),
    (0, 19): (27.59606, 0.5577, 0.07246, -0.00482, -0.20294, -0.20104, 0.07912, 0.59234, -0.9077),
    (0, 20): (40.97231, 0.28584, 0.02946, -0.02306, -0.01663, 0.09297, 0.18946, 0.28291, -0.40192),
    (0, 21): (41.72796, 0.22765, -0.00159, -0.00943, 0.21184, 0.16335, 0.11426, 0.4957, -0.07902),
    (0, 22): (33.65819, 0.3673, 0.00536, -0.02572, 0.27424, -0.10752, 0.14238, 0.03333, -0.48117),
    (0, 23): (17.46503, 0.7137, -0.0029, -0.00577, 0.37244, -0.64785, 0.3997, 0.27955, -1.03041),
    (0, 0): (20.45869, 0.66991, -0.01114, 0.0512, 0.24149, -0.31933, -0.32476, 0.61206, -0.75187),
    (0, 1): (22.99874, 0.60782, -0.04674, 0.03486, 0.16404, -0.14632, -0.21881, 0.5289, -0.45454),
    (0, 2): (22.23347, 0.61625, -0.08436, 0.04917, -0.03916, -0.02067, -0.36705, 0.43256, -0.0776),
    (0, 3): (13.8676, 0.78239, -0.0638, -0.00222, -0.32606, -0.28074, -0.60556, 0.58054, 0.14595),
    (0, 4): (13.61525, 0.77818, -0.0193, -0.02145, -0.20255, -0.53898, -0.28595, 0.24272, 0.04416),
    (0, 5): (10.57156, 0.78642, -0.00039, 0.06072, 0.34897, -0.77578, 0.05142, 0.08995, -0.03958),
    (1, 17): (25.93294, 0.65581, 0.08414, -0.03161, 1.47288, -0.91061, 0.26042, 0.48494, 0.15146),
    (1, 18): (35.60689, 0.47527, 0.0447, -0.01484, 0.8771, -0.19852, 0.09083, 0.55994, -0.73182),
    (1, 19): (35.46181, 0.44385, 0.04616, 0.04178, 0.41215, -0.16072, 0.17613, 0.60553, -0.77412),
    (1, 20): (33.65306, 0.4185, 0.00876, 0.07508, -0.13852, -0.12401, 0.22057, 0.41589, -0.57871),
    (1, 21): (31.77356, 0.40295, -0.0345, 0.06145, -0.1034, -0.09921, 0.13166, -0.03334, -0.20409),
    (1, 22): (17.99451, 0.61988, -0.0169, 0.05862, 0.1175, -0.37771, 0.03313, -0.25061, -0.03881),
    (1, 23): (13.78408, 0.72149, -0.06421, -0.01981, 0.34279, -0.52489, -0.49635, 0.0033, -0.17839),
    (1, 0): (19.9552, 0.63576, -0.01461, 0.02009, 0.05909, -0.24073, -0.90092, 0.46024, -0.22357),
    (1, 1): (15.77834, 0.70493, -0.0132, 0.01712, -0.11007, -0.32861, -0.93593, 0.63384, -0.06009),
    (1, 2): (17.84641, 0.71651, -0.01845, 0.02586, -0.07856, -0.30327, -1.11782, 0.32247, -0.17753),
    (1, 3): (19.14299, 0.68659, -0.058, 0.02959, -0.15342, -0.20888, 0.09926, 0.29101, 0.07764),
    (1, 4): (13.71368, 0.76076, -0.05523, 0.01336, -0.18735, -0.46591, 0.04498, 0.41522, 0.17246),
    (1, 5): (9.35456, 0.79897, -0.00836, 0.02971, 0.24634, -0.74127, 0.21959, 0.60392, 0.06106),
}

# v3.0 グローバル状態空間カルマン平滑化・同都市リアルタイムスピルオーバー・連続普段カーブ重み
# [wp_wm-pwp, noise_rev30, mom30, lag60_diff, dw30_rate, dm30_rate, peer_gap, peer_regime, jis_pull, cont_usual_diff]
_V3_STRUCT_WEIGHTS = (0.20641, 0.1412, 0.15277, 0.06584, -0.50641, 0.0267, 0.07018, 0.06774, 0.03387, 0.23724)

# ブランド別フェーズ事前オフセット（未知店舗やサンプル少数店のEmpirical-Bayes収束先）
_V3_BRAND_PHASE_DEFAULTS = {
    "OLG": (-0.39, -0.18, -0.08),
    "JIS": (1.73, -0.03, 0.9),
    "AG": (1.15, -0.43, -1.28),
    "YATA": (1.76, -0.35, -1.06),
    "ASOBI": (0.0, 0.0, 0.0),
    "XIX": (0.9, 2.6, 0.7),
}

# IRLS ロジスティック回帰による「男余り転落確率（60分後 女性比50%未満）」校正重み
# [1.0, (50 - pred_wp)/10, (50 - cur_wp)/10, 1/sqrt(tot), is_male_now, jackpot_safe]
_V2_FLIP_LOGISTIC_W = (-0.361, 2.43908, -0.29022, 0.00642, 0.2825, -0.34978)


def _business_date(now_jst: datetime.datetime) -> datetime.date:
    """全店舗の早朝シフト終盤（最大翌7:00閉店＋余韻）と夕方開店（最短17:00）を跨がない朝9時境界の営業日を返す。"""
    return (now_jst - datetime.timedelta(days=1)).date() if now_jst.hour < 9 else now_jst.date()


def _is_weekend_or_preholiday(now_jst: datetime.datetime) -> bool:
    """営業日基準（朝9時切替）で金・土・祝前日かどうかを判定する。"""
    biz_date = _business_date(now_jst)
    wd = biz_date.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    if wd in (4, 5):
        return True
    try:
        import store_hours
        next_iso = (biz_date + datetime.timedelta(days=1)).isoformat()
        if next_iso in getattr(store_hours, "JP_HOLIDAYS", set()):
            return True
    except Exception:
        pass
    return False


def _store_shift_bounds_minutes(store_name: str, now_jst: datetime.datetime) -> tuple:
    """公式営業時間（store_hours.py）に基づき (open_m, close_m, cur_m) を分単位（深夜は24h+表記）で返す。"""
    cur_m = ((24 + now_jst.hour) if now_jst.hour < 9 else now_jst.hour) * 60 + now_jst.minute
    try:
        import store_hours
        canon = _resolve_internal_store_key(store_name) or store_name
        spec = store_hours.get_store_schedule_spec(canon)
        is_korea = ("ソウル" in (canon or "")) or ("カンナム" in (canon or "")) or ("ホンデ" in (canon or ""))
        biz_date = _business_date(now_jst)
        wd = biz_date.weekday()
        next_iso = (biz_date + datetime.timedelta(days=1)).isoformat()
        biz_iso = biz_date.isoformat()
        is_pre = (not is_korea) and (next_iso in store_hours.JP_HOLIDAYS)
        is_hol = (not is_korea) and (biz_iso in store_hours.JP_HOLIDAYS)
        if is_pre or wd in (4, 5):
            open_h, close_h = spec["fs"]
        elif wd == 6 or is_hol:
            open_h, close_h = spec["sh"]
        else:
            open_h, close_h = spec["mt"]
        return int(open_h * 60), int(close_h * 60), int(cur_m)
    except Exception:
        return 18 * 60, 29 * 60, int(cur_m)


def _mins_until_close(store_name: str, now_jst: datetime.datetime) -> int:
    """公式営業時間（store_hours.py）に基づき、当営業シフトの閉店時刻までの残り分数を返す。"""
    _, close_m, cur_m = _store_shift_bounds_minutes(store_name, now_jst)
    return int(close_m - cur_m)


def _mins_since_open(store_name: str, now_jst: datetime.datetime) -> int:
    """公式営業時間（store_hours.py）に基づき、当営業シフトの開店時刻からの経過分数を返す。"""
    open_m, _, cur_m = _store_shift_bounds_minutes(store_name, now_jst)
    return int(cur_m - open_m)


def _calendar_demand_adjustment(now_jst: datetime.datetime) -> tuple:
    """
    給料日直後（25〜27日）・月末最終金曜の二次会男性流入効果、および連休中日の深夜女性維持効果を
    連続時刻 fhr 上の滑らかな微調整量 (dw_base, dm_base, dwp_pt) として返す。
    """
    biz_date = _business_date(now_jst)
    fhr = _night_fractional_hour(now_jst)
    day = biz_date.day
    wd = biz_date.weekday()

    next_month_check = biz_date + datetime.timedelta(days=7)
    is_last_friday = (wd == 4 and next_month_check.month != biz_date.month)
    is_payday_window = (25 <= day <= 27) or is_last_friday

    is_multi_holiday_mid = False
    try:
        import store_hours
        hols = getattr(store_hours, "JP_HOLIDAYS", set())
        today_off = (wd in (5, 6)) or (biz_date.isoformat() in hols)
        tom_date = biz_date + datetime.timedelta(days=1)
        tom_off = (tom_date.weekday() in (5, 6)) or (tom_date.isoformat() in hols)
        if today_off and tom_off:
            is_multi_holiday_mid = True
    except Exception:
        is_multi_holiday_mid = False

    dw_adj = 0.0
    dm_adj = 0.0
    dwp_adj = 0.0

    # 給料日直後・月末金曜の 20:00〜24:30 は二次会男性流入がやや強まりやすい
    if is_payday_window and 19.5 < fhr < 25.0:
        # 22.25h を中心とした滑らかな三角窓 (0.0 .. 1.0)
        w_pay = max(0.0, 1.0 - abs(fhr - 22.25) / 2.75)
        dm_adj += 1.2 * w_pay
        dw_adj += 0.3 * w_pay
        dwp_adj -= 0.75 * w_pay

    # 連休中日の 22:00〜26:30 は終電帰宅の崖が緩やかで女性滞在が持続しやすい
    if is_multi_holiday_mid and 21.5 < fhr < 26.5:
        w_hol = max(0.0, 1.0 - abs(fhr - 24.0) / 2.5)
        dw_adj += 1.1 * w_hol
        dwp_adj += 0.65 * w_hol

    return dw_adj, dm_adj, dwp_adj


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


# 店舗×夜間フェーズ（序盤17-21h / ピーク22-24h / 深夜01-06h）の階層Huber Empirical-Bayes事前残差オフセット
# 2026-10-07〜2026-10-15 12:00 の全国マルチ店舗学習期間のみで推定（未来ホールドアウト期間のデータは一切不使用）
_V2_STORE_PHASE_PRIORS = {
    "ALFA HIROSHIMA": (1.65, 0.96, 0.78),
    "ASOBIBAR 名古屋 住吉": (0.0, 0.0, 0.0),
    "ASOBIBAR 名古屋 栄": (0.0, 0.0, 0.0),
    "ASOBIBAR 心斎橋": (0.0, 0.0, 0.0),
    "ASOBIBAR 恵比寿": (0.0, 0.0, 0.0),
    "ASOBIBAR 新宿": (0.0, 0.0, 0.0),
    "ASOBIBAR 梅田": (0.0, 0.0, 0.0),
    "ASOBIBAR 渋谷": (0.0, 0.0, 0.0),
    "ASOBIBAR 神戸 三宮": (0.0, 0.0, 0.0),
    "ASOBIBAR 福岡 天神": (0.0, 0.0, 0.0),
    "ASOBIBAR 茶屋町": (0.0, 0.0, 0.0),
    "CLOVERS HIROSHIMA": (0.74, 1.69, 0.11),
    "JIS CHAYAMACHI": (5.04, 3.02, 2.36),
    "JIS FUKUOKA": (1.31, -1.04, -0.28),
    "JIS KUMAMOTO": (1.73, -0.03, 0.9),
    "JIS MATSUYAMA": (1.73, -0.03, 0.9),
    "JIS NAMBA": (2.53, 0.77, 1.71),
    "JIS NISHISHINJUKU": (1.02, -0.08, -0.4),
    "JIS OMIYA": (3.28, 0.45, 1.41),
    "JIS SAPPORO": (-2.07, -1.5, 0.54),
    "JIS SHINJUKU": (0.92, -0.08, 0.98),
    "JIS UMEDA": (1.37, 0.11, 1.43),
    "OLG ag上野": (1.46, -1.37, 0.26),
    "OLG ag仙台": (3.0, 1.27, 2.88),
    "OLG ag名古屋": (-0.34, -2.03, -4.09),
    "OLG ag広島": (-0.6, 1.07, -1.47),
    "OLG ag新宿": (1.15, -0.43, -1.28),
    "OLG ag札幌": (1.15, -0.43, -1.28),
    "OLG ag梅田": (2.42, 1.65, -1.58),
    "OLG ag沖縄": (-0.8, -2.17, -1.16),
    "OLG ag渋谷": (1.8, 0.26, -1.68),
    "OLG ag福岡": (1.15, -0.43, -1.28),
    "OLG ag金沢": (-0.27, -0.77, -1.43),
    "OLG 上野": (1.79, -0.49, 1.38),
    "OLG 京都": (-0.68, -1.2, -1.61),
    "OLG 仙台": (-0.39, -0.18, -0.08),
    "OLG 名古屋 栄": (1.67, 0.98, -0.76),
    "OLG 名古屋 錦": (1.61, -0.8, -0.86),
    "OLG 大分": (-1.46, -0.07, -1.25),
    "OLG 大宮": (3.59, 0.87, 1.54),
    "OLG 大阪駅前": (-0.83, 0.59, 0.29),
    "OLG 天満": (-1.66, -0.47, 0.09),
    "OLG 宇都宮": (-2.36, -4.19, -0.15),
    "OLG 宮崎": (-0.27, -1.24, -0.76),
    "OLG 小倉": (-0.28, 0.67, 2.29),
    "OLG 岡山": (0.39, 1.49, 0.54),
    "OLG 広島": (-0.39, -0.18, -0.08),
    "OLG 心斎橋": (-0.26, 1.53, -0.73),
    "OLG 恵比寿": (0.17, -2.04, -1.17),
    "OLG 新宿": (3.19, 3.72, 0.57),
    "OLG 札幌": (-0.39, -0.18, -0.08),
    "OLG 柏": (-0.31, -1.17, 0.54),
    "OLG 梅田": (-0.39, -0.18, -0.08),
    "OLG 横浜": (0.44, -1.72, -0.38),
    "OLG 沖縄": (-0.39, -0.18, -0.08),
    "OLG 浜松": (-4.0, -1.82, 0.1),
    "OLG 渋谷本店": (2.78, -0.03, -3.1),
    "OLG 渋谷駅前": (-0.39, -0.18, -0.08),
    "OLG 熊本": (-0.28, 0.78, -0.93),
    "OLG 町田": (-0.1, 1.26, 0.33),
    "OLG 神戸": (0.35, -2.55, -1.31),
    "OLG 福岡": (1.22, -0.21, 2.27),
    "OLG 茶屋町": (-0.39, -0.18, -0.08),
    "OLG 金沢": (-0.39, -0.18, -0.08),
    "OLG 錦糸町": (2.9, -2.9, -0.4),
    "OLG 長崎": (-0.01, 0.15, -0.97),
    "OLG 難波": (1.97, 2.55, 0.53),
    "OLG 静岡": (-3.09, -1.4, -0.65),
    "OLG 高崎": (0.43, 0.29, 1.5),
    "OLG 鹿児島": (-1.64, -0.32, -0.25),
    "XIX OKAYAMA": (1.49, 4.45, 1.24),
    "YATAKOI UMEDA": (2.48, -0.49, -1.77),
}

_STORE_INTERNAL_KEY_MEMO = {}


def _resolve_internal_store_key(store_name: str) -> str:
    """SEO表示名（例: 'オリエンタルラウンジ 渋谷本店', 'ag（アグ）梅田'）やスラッグをO(1)で内部正規キー（'OLG 渋谷本店' 等）へ解決する。"""
    if not store_name:
        return ""
    s = str(store_name).strip()
    cached = _STORE_INTERNAL_KEY_MEMO.get(s)
    if cached is not None:
        return cached

    resolved = s
    if s in STORE_P99_CAPACITY or s in _V2_STORE_PHASE_PRIORS:
        resolved = s
    else:
        try:
            import store_master
            internal = store_master.to_internal_store_name(s)
            if internal and (internal in STORE_P99_CAPACITY or internal in _V2_STORE_PHASE_PRIORS or internal in getattr(store_master, "STORE_SLUGS", {})):
                resolved = internal
            else:
                by_slug = store_master.store_by_slug(s)
                if by_slug:
                    resolved = by_slug
        except Exception:
            pass

        if resolved == s:
            if s.startswith("オリエンタルラウンジ"):
                branch = s[len("オリエンタルラウンジ"):].strip()
                resolved = f"OLG {branch}" if branch else "OLG"
            elif s.startswith("ag（アグ）") or s.startswith("ag ") or (s.startswith("ag") and len(s) > 2 and not s[2].isascii()):
                branch = s.replace("ag（アグ）", "")
                if branch.startswith("ag "):
                    branch = branch[3:]
                elif branch.startswith("ag"):
                    branch = branch[2:]
                branch = branch.strip()
                resolved = f"OLG ag{branch}" if branch else "OLG ag"
            elif s.startswith("ASOBIBAR（アソビバー）") or s.startswith("アソビバー"):
                branch = s.replace("ASOBIBAR（アソビバー）", "").replace("アソビバー", "").strip()
                resolved = f"ASOBIBAR {branch}" if branch else "ASOBIBAR"
            elif "ヤタコイ" in s or "YATAKOI" in s.upper():
                if "梅田" in s or "UMEDA" in s.upper() or s in ("ヤタコイ", "YATAKOI", "yatakoi"):
                    resolved = "YATAKOI UMEDA"
                else:
                    branch = s.replace("ヤタコイ", "").replace("YATAKOI", "").replace("yatakoi", "").strip()
                    resolved = f"YATAKOI {branch}" if branch else "YATAKOI UMEDA"
            elif "XIX" in s.upper() or "ジックス" in s or "シックス" in s:
                if "岡山" in s or "OKAYAMA" in s.upper() or s.upper() in ("XIX", "ジックス", "シックス"):
                    resolved = "XIX OKAYAMA"
                else:
                    branch = s.replace("ジックス", "").replace("シックス", "").replace("XIX", "").replace("xix", "").strip()
                    resolved = f"XIX {branch}" if branch else "XIX OKAYAMA"
            elif "JIS" in s.upper() or "ジス" in s:
                jis_mapping = {
                    "西新宿": "JIS NISHISHINJUKU",
                    "茶屋町": "JIS CHAYAMACHI",
                    "福岡": "JIS FUKUOKA",
                    "熊本": "JIS KUMAMOTO",
                    "松山": "JIS MATSUYAMA",
                    "難波": "JIS NAMBA",
                    "大宮": "JIS OMIYA",
                    "札幌": "JIS SAPPORO",
                    "新宿": "JIS SHINJUKU",
                    "梅田": "JIS UMEDA",
                }
                matched_jis = None
                for city, jis_key in jis_mapping.items():
                    if city in s:
                        matched_jis = jis_key
                        break
                if matched_jis:
                    resolved = matched_jis
                else:
                    branch = s.replace("JIS（ジス）", "").replace("ジス", "").replace("JIS", "").replace("jis", "").strip()
                    resolved = f"JIS {branch}" if branch else "JIS"

    if len(_STORE_INTERNAL_KEY_MEMO) > 512:
        _STORE_INTERNAL_KEY_MEMO.clear()
    _STORE_INTERNAL_KEY_MEMO[s] = resolved
    return resolved


def _normalize_store_phase_prior_key(store_name: str) -> str:
    """後方互換エイリアス：内部正規キーへ解決する。"""
    return _resolve_internal_store_key(store_name)


def _brand_phase_default(canon_name: str) -> tuple:
    if canon_name.startswith("JIS"):
        return _V3_BRAND_PHASE_DEFAULTS["JIS"]
    if canon_name.startswith("OLG ag"):
        return _V3_BRAND_PHASE_DEFAULTS["AG"]
    if canon_name.startswith("ASOBIBAR"):
        return _V3_BRAND_PHASE_DEFAULTS["ASOBI"]
    if canon_name.startswith("YATAKOI"):
        return _V3_BRAND_PHASE_DEFAULTS["YATA"]
    if canon_name.startswith("XIX"):
        return _V3_BRAND_PHASE_DEFAULTS["XIX"]
    return _V3_BRAND_PHASE_DEFAULTS["OLG"]


def _interp_store_phase_prior(store_name: str, now_jst: datetime.datetime) -> float:
    """店舗固有フェーズ事前オフセットを連続時刻 fhr 上で滑らかに線形補間し、正時の不連続ジャンプを防ぐ。"""
    norm_key = _resolve_internal_store_key(store_name)
    tup = _V2_STORE_PHASE_PRIORS.get(norm_key) or _brand_phase_default(norm_key)
    p0, p1, p2 = tup
    fhr = _night_fractional_hour(now_jst)
    if fhr <= 20.5:
        return p0
    if fhr < 22.0:
        alpha = (fhr - 20.5) / 1.5
        return p0 * (1.0 - alpha) + p1 * alpha
    if fhr <= 23.5:
        return p1
    if fhr < 25.0:
        alpha = (fhr - 23.5) / 1.5
        return p1 * (1.0 - alpha) + p2 * alpha
    return p2


_STANDING_BAR_MEMO = {}


def _is_standing_bar_store(store_name: str) -> bool:
    """定額スタンディングバー業態（ASOBIBAR等）かを store_master 属性優先＋正規キーで O(1) 判定する。"""
    canon = _resolve_internal_store_key(store_name)
    if not canon:
        return False
    cached = _STANDING_BAR_MEMO.get(canon)
    if cached is not None:
        return cached
    is_st = canon.startswith("ASOBIBAR")
    if not is_st:
        try:
            import store_master
            info = store_master.get(canon)
            if isinstance(info, dict) and str(info.get("brand") or "").upper() == "ASOBIBAR":
                is_st = True
        except Exception:
            pass
    if len(_STANDING_BAR_MEMO) > 256:
        _STANDING_BAR_MEMO.clear()
    _STANDING_BAR_MEMO[canon] = is_st
    return is_st


def build_city_peer_context(stores_or_map) -> dict:
    """
    全店舗スナップショット（list[dict] または {store_name: (w, m)}）から、
    同一都市×同一業態（着席ラウンジ同士／スタンディングバー同士）の他店舗リアルタイム合計を O(N) で構築する。
    """
    if not stores_or_map:
        return {}

    entries = []
    if isinstance(stores_or_map, dict):
        for raw_name, pair in stores_or_map.items():
            if not raw_name or not isinstance(pair, (tuple, list)) or len(pair) < 2:
                continue
            cname = _resolve_internal_store_key(raw_name)
            w = _safe_int(pair[0], default=0, min_val=0, max_val=100000)
            m = _safe_int(pair[1], default=0, min_val=0, max_val=100000)
            entries.append((str(raw_name), cname, w, m, True, _is_standing_bar_store(cname)))
    elif isinstance(stores_or_map, (list, tuple, set)):
        for it in stores_or_map:
            if not isinstance(it, dict):
                continue
            raw_name = str(it.get("name") or it.get("store_name") or "").strip()
            if not raw_name:
                continue
            cname = _resolve_internal_store_key(raw_name)
            w = _safe_int(it.get("women"), default=0, min_val=0, max_val=100000)
            m = _safe_int(it.get("men"), default=0, min_val=0, max_val=100000)
            is_op = bool(it.get("is_open_now", True))
            entries.append((raw_name, cname, w, m, is_op, _is_standing_bar_store(cname)))
    else:
        return {}

    city_totals = {}
    seen_canon = set()
    for _, cname, w, m, is_op, is_st in entries:
        if cname in seen_canon:
            continue
        seen_canon.add(cname)
        tot = w + m
        if not is_op or tot < 4:
            continue
        ck = city_key_for_store(cname)
        if not ck or ck == "seoul":
            continue
        bucket_key = (ck, is_st)
        agg = city_totals.get(bucket_key)
        if agg is None:
            city_totals[bucket_key] = [w, m, 1]
        else:
            agg[0] += w
            agg[1] += m
            agg[2] += 1

    peer_map = {}
    for raw_name, cname, w, m, is_op, is_st in entries:
        ck = city_key_for_store(cname)
        agg = city_totals.get((ck, is_st))
        if not agg:
            continue
        tot = w + m
        contrib = (is_op and tot >= 4)
        pw = agg[0] - (w if contrib else 0)
        pm = agg[1] - (m if contrib else 0)
        pn = agg[2] - (1 if contrib else 0)
        if pn >= 1 and (pw + pm) >= 15:
            ctx = {"peer_women": pw, "peer_men": pm, "peer_stores": pn}
            peer_map[cname] = ctx
            if raw_name != cname:
                peer_map[raw_name] = ctx
    return peer_map


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
    canon_name = _resolve_internal_store_key(store_name)
    women = _safe_int(women, default=0, min_val=0, max_val=100000)
    men = _safe_int(men, default=0, min_val=0, max_val=100000)
    total = women + men
    if not is_open_now or total < 6:
        return {"pred_60m_wpct": None, "pred_60m_women": women, "pred_60m_men": men}

    cur_wpct = int(round((women / float(total)) * 100))
    hr = now_jst.hour
    is_wknd = _is_weekend_or_preholiday(now_jst)
    drift_tbl = _WEEKEND_DRIFT_BY_HOUR if is_wknd else _WEEKDAY_DRIFT_BY_HOUR
    base_dw, base_dm, _ = drift_tbl.get(hr, (0.0, 0.0, 12))
    p99_cap = max(40, int(STORE_P99_CAPACITY.get(canon_name, 120)))
    scale = max(0.38, min(1.45, p99_cap / 165.0))
    mid_ret = int(STORE_MIDNIGHT_RETENTION.get(canon_name, 85))
    dw = base_dw * scale
    dm = base_dm * scale
    if hr in (22, 23):
        if mid_ret < 70:
            dw -= (75 - mid_ret) * 0.14 * scale
        elif mid_ret >= 115:
            dw += (mid_ret - 105) * 0.10 * scale
    w_d15 = _safe_int(women_diff, default=0, min_val=-100000, max_val=100000)
    m_d15 = _safe_int(men_diff, default=0, min_val=-100000, max_val=100000)
    dw += max(-8.0, min(10.0, w_d15 * 0.85))
    dm += max(-8.0, min(12.0, m_d15 * 1.05))
    if women >= 20 and cur_wpct >= 70:
        dw = max(dw, 9.0 * scale)
        dm = min(dm, 14.0 * scale)
    elif women >= 12 and men <= 3:
        dw -= 4.5
    elif cur_wpct <= 40 and men >= 15:
        dm = max(dm, -2.0)
    ck = city_key_for_store(canon_name)
    city_w = _safe_dict(weather_map).get(ck) if (ck and isinstance(weather_map, dict)) else None
    city_w_dict = _safe_dict(city_w)
    if bool(city_w_dict.get("is_rainy") and canon_name in UNDERGROUND_DIRECT_STORES):
        dw += 3.2 * scale
        dm -= 1.2 * scale
    if isinstance(arena_signal, dict) and bool(arena_signal.get("is_surge_now", True)) and 19 <= hr <= 22:
        dw += 3.5 * scale
    pred_w = max(1, min(int(p99_cap * 0.72), _safe_round_int(women + dw, default=women, min_val=1)))
    pred_m = max(1, min(int(p99_cap * 0.72), _safe_round_int(men + dm, default=men, min_val=1)))
    pred_tot = max(2, pred_w + pred_m)
    return {
        "pred_60m_wpct": _safe_round_int((pred_w / float(pred_tot)) * 100, default=50, min_val=0, max_val=100),
        "pred_60m_women": pred_w,
        "pred_60m_men": pred_m,
    }


def calculate_turnover_momentum_index(
    store_name: str = "",
    women: int = 0,
    men: int = 0,
    is_open_now: bool = True,
    women_diff: int = 0,
    men_diff: int = 0,
    lag_context: dict = None,
    pos_telemetry: dict = None,
    p99_cap: int = 120,
    now_jst: datetime.datetime = None,
    store_id: str = None,
    **kwargs,
) -> dict:
    """
    時系列差分ボラティリティ＋多段ラグフラックス＋待ち消化速度＋P99規模正規化から
    「今の席回転スピードモメンタム指数（10〜95）」を算出する。
    """
    canon_name = _resolve_internal_store_key(store_name or store_id or "")
    women = _safe_int(women, default=0, min_val=0, max_val=100000)
    men = _safe_int(men, default=0, min_val=0, max_val=100000)
    total = women + men

    # 1. 営業時間外および少人数ガード
    if not is_open_now:
        return {
            "score": None,
            "label": "判定対象外",
            "tone": "neutral",
            "pill_class": "moat-pill tone-neutral hidden",
            "badge_class": "tone-neutral",
            "reason": "営業時間外のため判定対象外",
            "desc": "営業時間外のため判定対象外",
            "volatility": 0.0,
            "flux_rate": 0.0,
            "wait_clear_speed": "対象外",
            "turnover_momentum_index": None,
            "turnover_label": "判定対象外",
            "turnover_tone": "neutral",
        }
    if total < 6:
        return {
            "score": None,
            "label": "判定対象外",
            "tone": "neutral",
            "pill_class": "moat-pill tone-neutral hidden",
            "badge_class": "tone-neutral",
            "reason": "客数が6名未満のため判定対象外",
            "desc": "客数が6名未満のため判定対象外",
            "volatility": 0.0,
            "flux_rate": 0.0,
            "wait_clear_speed": "対象外",
            "turnover_momentum_index": None,
            "turnover_label": "判定対象外",
            "turnover_tone": "neutral",
        }

    has_explicit_now = now_jst is not None
    now_jst = _normalize_jst(now_jst)
    open_m, close_m, cur_m = _store_shift_bounds_minutes(canon_name, now_jst)
    mtc = int(close_m - cur_m)
    mso = int(cur_m - open_m)

    # 2. 定額スタンディング業態（ASOBIBAR・ヤタコイ）の特例判定
    c_upper = canon_name.upper()
    if "ASOBIBAR" in c_upper or "YATAKOI" in c_upper or "ヤタコイ" in c_upper or "アソビバー" in c_upper:
        w_d = _safe_int(women_diff, default=0, min_val=0, max_val=1000000)
        m_d = _safe_int(men_diff, default=0, min_val=0, max_val=1000000)
        tot_diff = min(1000000, w_d + m_d)
        return {
            "score": 85,
            "label": "自由回遊（出入自由）",
            "tone": "standing",
            "pill_class": "moat-pill tone-standing",
            "badge_class": "tone-standing",
            "reason": "定額スタンディング業態のため自由回遊・高頻度に入退店が発生",
            "desc": "定額スタンディング業態のため自由回遊・高頻度に入退店が発生",
            "volatility": round(float(tot_diff), 1),
            "flux_rate": 70.0,
            "wait_clear_speed": "出入自由",
            "turnover_momentum_index": 85,
            "turnover_label": "自由回遊（出入自由）",
            "turnover_tone": "standing",
        }

    # 3. 直近差分の取得とフォールバック
    w_d15 = _safe_int(women_diff, default=0, min_val=-1000000, max_val=1000000)
    m_d15 = _safe_int(men_diff, default=0, min_val=-1000000, max_val=1000000)

    f15 = min(1000000, abs(w_d15) + abs(m_d15))
    f30 = min(2000000.0, f15 * 1.8)

    if isinstance(lag_context, dict) and lag_context:
        l15 = lag_context.get("lag_15m")
        if isinstance(l15, (tuple, list)) and len(l15) >= 2:
            lw = _safe_int(l15[0], default=0, min_val=0, max_val=100000)
            lm = _safe_int(l15[1], default=0, min_val=0, max_val=100000)
            f15 = min(1000000, abs(women - lw) + abs(men - lm))
        l30 = lag_context.get("lag_30m")
        if isinstance(l30, (tuple, list)) and len(l30) >= 2:
            lw30 = _safe_int(l30[0], default=0, min_val=0, max_val=100000)
            lm30 = _safe_int(l30[1], default=0, min_val=0, max_val=100000)
            f30 = min(2000000.0, abs(women - lw30) + abs(men - lm30))

    # 開店初期ジャンプの抑制
    if has_explicit_now and (-15 <= mso <= 30):
        f15 = _safe_round_int(f15 * 0.35, default=0, min_val=0)
        f30 = _safe_round_int(f30 * 0.35, default=0, min_val=0)

    # 1時間換算フラックス量
    f_hourly = f15 * 2.4 + max(0.0, f30 - f15) * 1.2
    c_cap = _safe_int(p99_cap, default=120, min_val=35, max_val=10000)
    r_flux = min(500.0, (f_hourly / float(c_cap)) * 100.0)

    # 4. 待ち消化速度・POSテレメトリ補正
    bonus = 0
    wait_speed = "普通"

    if isinstance(pos_telemetry, dict) and pos_telemetry:
        is_reverse = bool(pos_telemetry.get("is_reverse_waiting"))
        w_wait = _safe_int(pos_telemetry.get("women_waiting_groups"), default=0, min_val=0, max_val=10000)
        m_wait = _safe_int(pos_telemetry.get("men_waiting_groups"), default=0, min_val=0, max_val=10000)
        q_tot = w_wait + m_wait
        if is_reverse:
            bonus -= 12
            wait_speed = "女性滞留・待ち逆転"
        elif q_tot > 0:
            if f15 >= 6:
                bonus += 12
                wait_speed = "良好"
            elif f15 <= 2:
                bonus -= 10
                wait_speed = "停滞"
    else:
        # 一般店舗（客数比率とフラックスから判定）
        occ = min(10.0, float(total) / float(c_cap))
        if occ >= 0.85 and f15 <= 1:
            bonus -= 15
            wait_speed = "満席滞留"
        elif f15 >= 8:
            bonus += 8
            wait_speed = "良好"

    # 5. スコア計算
    raw_score = 50.0 + (r_flux - 20.0) * 1.0 + bonus
    is_closing = has_explicit_now and ((0 <= mtc <= 30) or (mtc <= 30 and mtc >= -60))

    if is_closing:
        score = min(60, _safe_round_int(raw_score, default=50, min_val=10, max_val=95))
        reason = "閉店時刻が近いため一斉退店が進行中（席回転指数は終電抑制中）"
    else:
        score = _safe_round_int(raw_score, default=50, min_val=10, max_val=95)
        if score >= 68:
            reason = f"直近30分で活発な入退店（1h換算約{_safe_round_int(f_hourly, default=0, min_val=0, max_val=100000)}名）を検知。席の動きが非常にスピーディーです。"
        elif score >= 45:
            reason = "適度なペースで席の入れ替えが進行しています。"
        else:
            reason = "長時間の着席が多く席の動きは緩やかです。待ち時間の発生にご注意ください。"

    # 6. トーンとラベル判定
    if score >= 68:
        label = "⚡️高回転"
        tone = "fast"
    elif score >= 45:
        label = "🔄標準ペース"
        tone = "normal"
    else:
        label = "☕️滞留ぎみ"
        tone = "slow"

    return {
        "score": score,
        "label": label,
        "tone": tone,
        "pill_class": f"moat-pill tone-{tone}",
        "badge_class": f"tone-{tone}",
        "reason": reason,
        "desc": reason,
        "volatility": _safe_float(f15, 1, 0.0),
        "flux_rate": _safe_float(r_flux, 1, 0.0),
        "wait_clear_speed": wait_speed,
        "turnover_momentum_index": score,
        "turnover_label": label,
        "turnover_tone": tone,
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
    lag_context: dict = None,
    peer_context: dict = None,
    usual_pair_override: tuple = None,
    lead_intent_heat: dict = None,
) -> dict:
    """
    状態空間カルマン平滑化＋同都市スピルオーバーモデル（v3.0）に基づき、当該店舗の
    「60分後の予測女性比・予測男女数・男余り転落リスク・アラートバッジ」を返す。
    1) 連続時刻ノット補間（h:30基準・階層Huberロバスト推定）により正時の階段状ジャンプと外れ値過適合を排除
    2) 多段ラグ（t-30m / t-60m）カルマン状態アンカーで一時的な離席・会計ノイズを平滑化
    3) 同一都市内の他店舗リアルタイム女性比（city peer spillover）から当夜のエリア需給と店舗間移動を反映
    4) 階層Empirical-Bayes店舗×フェーズ事前分布＋連続補間30日普段カーブ縮小＋オンライン残差自己補正を統合
    """
    now_jst = _normalize_jst(now_jst)
    canon_name = _resolve_internal_store_key(store_name)
    women = _safe_int(women, default=0, min_val=0, max_val=100000)
    men = _safe_int(men, default=0, min_val=0, max_val=100000)
    total = women + men

    if not is_open_now or total < 6:
        return {
            "model_version": FORECAST_MODEL_VERSION,
            "pred_60m_wpct": None,
            "raw_pred_60m_wpct": None,
            "pred_60m_women": women,
            "pred_60m_men": men,
            "pred_women": women,  # E2E test_12 必須互換キー補完
            "pred_men": men,      # E2E test_12 必須互換キー補完
            "pred_delta_pt": 0,
            "flip_risk_pct": 0,
            "alert_code": "quiet",
            "badge_men": "",
            "badge_women": "",
            "badge_tone": "neutral",
            "is_rain_shelter": False,
            "arena_signal_label": "",
            "arena_phase": "",
            "arena_event_title": "",
            "turnover_momentum": None,
            "online_bias_adj_pt": 0.0,
            "lead_intent_bias_pt": 0.0,
            "lead_intent_heat": lead_intent_heat if isinstance(lead_intent_heat, dict) else None,
            "reason_short": "",
        }

    tot_f = float(total)
    cur_wp_float = (women / tot_f) * 100.0
    cur_wpct = _safe_round_int(cur_wp_float, default=50, min_val=0, max_val=100)
    hr = now_jst.hour
    is_wknd = _is_weekend_or_preholiday(now_jst)
    wk_key = 1 if is_wknd else 0

    p99_cap = max(36, int(STORE_P99_CAPACITY.get(canon_name, 120)))
    p99_f = float(p99_cap)
    scale = max(0.38, min(1.45, p99_f / 165.0))
    mid_ret = int(STORE_MIDNIGHT_RETENTION.get(canon_name, 88))
    mid_r = (float(mid_ret) - 90.0) / 100.0

    open_m, close_m, cur_m = _store_shift_bounds_minutes(canon_name, now_jst)
    mtc = int(close_m - cur_m)
    mso = int(cur_m - open_m)

    w_d15 = _safe_int(women_diff, default=0, min_val=-100000, max_val=100000)
    m_d15 = _safe_int(men_diff, default=0, min_val=-100000, max_val=100000)

    # 単体呼び出し時も非ブロッキングRAMリングバッファから t-15m/t-30m/t-60m ラグを補完
    if lag_context is None and canon_name:
        try:
            import local_db
            lag_context = local_db.get_cached_store_recent_lags(canon_name, now_jst)
        except Exception:
            lag_context = None

    # 開店直後に閉店中 0:0 からの立ち上がり差分がそのまま入っている場合はモメンタム二重計上を防ぐ
    if -15 <= mso <= 30 and w_d15 == women and m_d15 == men:
        w_d15 = _safe_round_int(w_d15 * 0.25, default=0)
        m_d15 = _safe_round_int(m_d15 * 0.25, default=0)

    if isinstance(lag_context, dict) and w_d15 == 0 and m_d15 == 0:
        l15 = lag_context.get("lag_15m")
        if isinstance(l15, (tuple, list)) and len(l15) >= 2:
            try:
                lw15 = _safe_int(l15[0], default=0, min_val=0, max_val=100000)
                lm15 = _safe_int(l15[1], default=0, min_val=0, max_val=100000)
                # 15分前が閉店・空室(0:0)からの営業開始ジャンプは急増モメンタムとして外挿しない
                if (lw15 + lm15) >= 2:
                    w_d15 = women - lw15
                    m_d15 = men - lm15
                    if -15 <= mso <= 25 and (lw15 + lm15) <= 4:
                        w_d15 = int(round(w_d15 * 0.4))
                        m_d15 = int(round(m_d15 * 0.4))
            except (TypeError, ValueError):
                pass

    wd_c = max(-16.0, min(18.0, float(w_d15)))
    md_c = max(-16.0, min(18.0, float(m_d15)))

    fhr = _night_fractional_hour(now_jst)
    if fhr <= 17.5:
        ef = 1.0
    elif fhr >= 21.5:
        ef = 0.0
    else:
        ef = max(0.0, min(1.0, (21.5 - fhr) / 4.0))

    closing = max(0.0, min(1.5, (90.0 - float(mtc)) / 60.0)) if mtc < 90 else 0.0
    occ = min(1.2, tot_f / p99_f)
    is_standing = _is_standing_bar_store(canon_name)
    ck = city_key_for_store(canon_name)
    city_w = _safe_dict(weather_map).get(ck) if (ck and isinstance(weather_map, dict)) else None
    city_w_dict = _safe_dict(city_w)
    is_rain_shelter = bool(city_w_dict.get("is_rainy") and canon_name in UNDERGROUND_DIRECT_STORES)

    usual_pair = usual_pair_override
    if usual_pair is None and mtc >= 65 and canon_name:
        try:
            import local_db
            usual_pair = local_db.get_cached_store_usual_pair(canon_name, now_jst)
        except Exception:
            usual_pair = None

    cap_f = p99_f * 0.75
    k_gain = tot_f / (tot_f + 22.0)

    if is_standing:
        # --- 定額スタンディングバー専用・強定常カルマン人数モデル（v3.1） ---
        # 着席型ラウンジ特有の53%平均回帰（_V2_KNOTS_WP / m_reb）を適用せず、
        # 男女別の滞在持続人数＋多段ラグ平滑化＋店舗固有30日普段カーブ＋Beta疑似カウント事前分布で予測する。
        base_w = float(women)
        base_m = float(men)
        if isinstance(lag_context, dict):
            l30 = lag_context.get("lag_30m")
            if isinstance(l30, (tuple, list)) and len(l30) >= 2:
                try:
                    w30 = _safe_int(l30[0], default=0, min_val=0, max_val=100000)
                    m30 = _safe_int(l30[1], default=0, min_val=0, max_val=100000)
                    t30 = w30 + m30
                    if t30 >= 6:
                        w_lag30 = 0.16 * (t30 / (t30 + 18.0))
                        base_w = (1.0 - w_lag30) * base_w + w_lag30 * float(w30)
                        base_m = (1.0 - w_lag30) * base_m + w_lag30 * float(m30)
                except (TypeError, ValueError, OverflowError):
                    pass
            l60 = lag_context.get("lag_60m")
            if isinstance(l60, (tuple, list)) and len(l60) >= 2:
                try:
                    w60_p = _safe_int(l60[0], default=0, min_val=0, max_val=100000)
                    m60_p = _safe_int(l60[1], default=0, min_val=0, max_val=100000)
                    t60_p = w60_p + m60_p
                    if t60_p >= 6:
                        w_lag60 = 0.06 * (t60_p / (t60_p + 20.0))
                        base_w = (1.0 - w_lag60) * base_w + w_lag60 * float(w60_p)
                        base_m = (1.0 - w_lag60) * base_m + w_lag60 * float(m60_p)
                except (TypeError, ValueError, OverflowError):
                    pass

        ramp_fac = 1.0 + 0.14 * ef
        if fhr >= 26.0:
            ramp_fac *= max(0.82, 1.0 - 0.06 * (fhr - 26.0))
        pw_raw = base_w * ramp_fac + 0.20 * wd_c
        pm_raw = base_m * ramp_fac + 0.20 * md_c

        if is_rain_shelter:
            pw_raw += 1.6 * scale
            pm_raw -= 0.4 * scale
        if isinstance(arena_signal, dict) and bool(arena_signal.get("is_surge_now", True)) and 19 <= hr <= 22:
            pw_raw += 2.0 * scale

        cal_dw, cal_dm, _ = _calendar_demand_adjustment(now_jst)
        if cal_dw or cal_dm:
            pw_raw += 0.6 * cal_dw * scale
            pm_raw += 0.6 * cal_dm * scale

        if mtc <= 45:
            fac = max(0.22, min(1.0, float(max(0, mtc)) / 45.0))
            pw_raw = min(pw_raw, max(0.5, float(women) * fac))
            pm_raw = min(pm_raw, max(0.5, float(men) * fac))

        pw = max(0.5, min(cap_f, pw_raw))
        pm = max(0.5, min(cap_f, pm_raw))

        if mtc >= 65 and isinstance(usual_pair, (tuple, list)) and len(usual_pair) >= 5:
            try:
                uw_t = max(0.0, _safe_float(usual_pair[0], default=0.0) or 0.0)
                um_t = max(0.0, _safe_float(usual_pair[1], default=0.0) or 0.0)
                uw_60 = max(0.0, _safe_float(usual_pair[2], default=0.0) or 0.0)
                um_60 = max(0.0, _safe_float(usual_pair[3], default=0.0) or 0.0)
                cnt_60 = max(0.0, _safe_float(usual_pair[4], default=0.0) or 0.0)
                if (uw_t + um_t) >= 5.0 and (uw_60 + um_60) >= 4.0 and cnt_60 >= 5.0:
                    u_w = min(0.22, 0.28 * (cnt_60 / (cnt_60 + 10.0)))
                    tgt_w = max(0.5, float(women) + 0.75 * (uw_60 - uw_t) + 0.25 * (uw_60 - float(women)))
                    tgt_m = max(0.5, float(men) + 0.75 * (um_60 - um_t) + 0.25 * (um_60 - float(men)))
                    pw = max(0.5, min(cap_f, (1.0 - u_w) * pw + u_w * tgt_w))
                    pm = max(0.5, min(cap_f, (1.0 - u_w) * pm + u_w * tgt_m))
            except (TypeError, ValueError, OverflowError):
                pass

        # Beta(0.76, 1.14) 疑似カウント事前分布（事前平均40.0%・計1.9人分）で少人数時の極端値を抑えつつ持続比率を算出
        pred_tot_f = pw + pm
        wp_b = ((pw + 0.76) / (pred_tot_f + 1.90)) * 100.0

        if isinstance(peer_context, dict):
            try:
                pw_c = _safe_int(peer_context.get("peer_women"), default=0, min_val=0, max_val=100000)
                pm_c = _safe_int(peer_context.get("peer_men"), default=0, min_val=0, max_val=100000)
                pn_c = _safe_int(peer_context.get("peer_stores"), default=0, min_val=0, max_val=1000)
                ptot = pw_c + pm_c
                if ptot >= 15 and pn_c >= 1:
                    peer_wp = (pw_c / float(ptot)) * 100.0
                    peer_rel = ptot / (ptot + 60.0)
                    wp_b += 0.07 * peer_rel * max(-20.0, min(20.0, peer_wp - wp_b))
            except (TypeError, ValueError, OverflowError):
                pass
    else:
        feat_wm = (
            ef * (p99_f / 100.0),
            float(women),
            float(men),
            wd_c,
            md_c,
            mid_r * float(women),
            closing * float(women),
            closing * float(men),
            occ * tot_f,
        )

        inv_tot = 100.0 / max(16.0, tot_f)
        j_buf = (max(0.0, cur_wp_float - 66.0) * min(1.0, float(women) / 24.0)) if women >= 16 else 0.0
        m_reb = max(0.0, 42.0 - cur_wp_float)
        feat_wp = (
            1.0,
            cur_wp_float,
            wd_c * inv_tot,
            md_c * inv_tot,
            mid_r * 10.0,
            k_gain * (cur_wp_float - 53.0),
            j_buf,
            m_reb,
            occ * 10.0,
        )

        kw = _interp_v2_knot(_V2_KNOTS_W, wk_key, now_jst)
        km = _interp_v2_knot(_V2_KNOTS_M, wk_key, now_jst)
        kwp = _interp_v2_knot(_V2_KNOTS_WP, wk_key, now_jst)

        pw_raw = sum(a * b for a, b in zip(kw, feat_wm))
        pm_raw = sum(a * b for a, b in zip(km, feat_wm))
        pwp_dir = sum(a * b for a, b in zip(kwp, feat_wp))

        # 早朝 05:00 以降（fhr > 29.0）での 05時ノット定数下方シフト外挿を抑制し、現在比率へ滑らかにアンカー
        if fhr > 29.0:
            late_w = min(0.60, (fhr - 29.0) * 0.40)
            pwp_dir = (1.0 - late_w) * pwp_dir + late_w * cur_wp_float

        # 1) 普段比異常スパイクの平均回帰（usual_w_diff が与えられている場合、過剰上振れの減衰をわずかに織り込む）
        if usual_w_diff is not None:
            try:
                uwd = float(usual_w_diff)
                if abs(uwd) >= 10.0:
                    pwp_dir -= max(-2.2, min(2.2, uwd * 0.05))
            except Exception:
                pass

        # 2) 雨天×地下街・駅直結ブースト ＆ ドーム・アリーナ終演後流入ブースト
        if is_rain_shelter:
            pw_raw += 2.4 * scale
            pm_raw -= 0.8 * scale
            pwp_dir += 1.4 if (m_d15 < w_d15 + 4) else 0.6

        if isinstance(arena_signal, dict) and bool(arena_signal.get("is_surge_now", True)) and 19 <= hr <= 22:
            pw_raw += 2.8 * scale
            pwp_dir += 1.5

        # 2b) カレンダー外生需要微調整（給料日・月末金曜二次会／連休中日の深夜維持）
        cal_dw, cal_dm, cal_dwp = _calendar_demand_adjustment(now_jst)
        if cal_dw or cal_dm or cal_dwp:
            pw_raw += cal_dw * scale
            pm_raw += cal_dm * scale
            pwp_dir += cal_dwp

        # 4) 閉店直前ハザード上限（mtc=45 で fac=1.0 となる境界連続リニア減衰キャップ）
        if mtc <= 45:
            fac = max(0.22, min(1.0, float(max(0, mtc)) / 45.0))
            pw_raw = min(pw_raw, max(0.5, float(women) * fac))
            pm_raw = min(pm_raw, max(0.5, float(men) * fac))

        pw = max(0.5, min(cap_f, pw_raw))
        pm = max(0.5, min(cap_f, pm_raw))

        # 5) 普段カーブによる男女カウントのEmpirical-Bayes補正（wp_from_wm 算出前に反映して整合化）
        usual_diff = 0.0
        if mtc >= 65 and isinstance(usual_pair, (tuple, list)) and len(usual_pair) >= 5:
            try:
                uw_t = max(0.0, _safe_float(usual_pair[0], default=0.0) or 0.0)
                um_t = max(0.0, _safe_float(usual_pair[1], default=0.0) or 0.0)
                uw_60 = max(0.0, _safe_float(usual_pair[2], default=0.0) or 0.0)
                um_60 = max(0.0, _safe_float(usual_pair[3], default=0.0) or 0.0)
                cnt_60 = max(0.0, _safe_float(usual_pair[4], default=0.0) or 0.0)
                ut1 = uw_60 + um_60
                if ut1 >= 6.0 and cnt_60 >= 5.0:
                    uwp1 = (uw_60 / ut1) * 100.0
                    cur_anom = 0.0
                    if (uw_t + um_t) >= 6.0:
                        cur_anom = cur_wp_float - ((uw_t / (uw_t + um_t)) * 100.0)
                    emp_tgt = max(15.0, min(85.0, uwp1 + 0.45 * cur_anom))
                    urel = (cnt_60 / (cnt_60 + 12.0)) * (1.0 - 0.35 * (tot_f / (tot_f + 30.0)))
                    usual_diff = urel * max(-28.0, min(28.0, emp_tgt - cur_wp_float))
                if (uw_t + um_t) >= 5.0 and ut1 >= 4.0 and cnt_60 >= 5.0:
                    u_w = min(0.16, 0.20 * (cnt_60 / (cnt_60 + 12.0)))
                    pw = max(0.5, min(cap_f, (1.0 - u_w) * pw + u_w * max(0.5, women + 0.70 * (uw_60 - uw_t) + 0.30 * (uw_60 - women))))
                    pm = max(0.5, min(cap_f, (1.0 - u_w) * pm + u_w * max(0.5, men + 0.70 * (um_60 - um_t) + 0.30 * (um_60 - men))))
            except (TypeError, ValueError, OverflowError):
                usual_diff = 0.0

        wp_from_wm = (pw / (pw + pm)) * 100.0
        store_prior_adj = _interp_store_phase_prior(canon_name, now_jst) * (1.0 - 0.22 * k_gain)

        # 6) v3.1 状態空間カルマン分離ラグ（ノイズ回帰 vs 大規模店モメンタム）・同都市同業態ピアスピルオーバー統合層
        noise_rev30 = 0.0
        mom30 = 0.0
        dw30_rate = 0.0
        dm30_rate = 0.0
        lag60_diff = 0.0
        if isinstance(lag_context, dict):
            l30 = lag_context.get("lag_30m")
            if isinstance(l30, (tuple, list)) and len(l30) >= 2:
                try:
                    w30 = _safe_int(l30[0], default=0, min_val=0, max_val=100000)
                    m30 = _safe_int(l30[1], default=0, min_val=0, max_val=100000)
                    t30 = w30 + m30
                    if t30 >= 6:
                        wp30 = (w30 / float(t30)) * 100.0
                        d30 = max(-30.0, min(30.0, wp30 - cur_wp_float))
                        rel30 = t30 / (t30 + 18.0)
                        noise_rev30 = (1.0 - k_gain) * rel30 * d30
                        mom30 = k_gain * rel30 * (-d30)
                        dw30_rate = max(-30.0, min(30.0, float(women - w30))) / (tot_f + 12.0)
                        dm30_rate = max(-30.0, min(30.0, float(men - m30))) / (tot_f + 12.0)
                except (TypeError, ValueError, OverflowError):
                    pass
            l60 = lag_context.get("lag_60m")
            if isinstance(l60, (tuple, list)) and len(l60) >= 2:
                try:
                    w60_p = _safe_int(l60[0], default=0, min_val=0, max_val=100000)
                    m60_p = _safe_int(l60[1], default=0, min_val=0, max_val=100000)
                    t60_p = w60_p + m60_p
                    if t60_p >= 6:
                        wp60_p = (w60_p / float(t60_p)) * 100.0
                        rel60 = t60_p / (t60_p + 20.0)
                        lag60_diff = rel60 * max(-32.0, min(32.0, wp60_p - cur_wp_float))
                except (TypeError, ValueError, OverflowError):
                    pass

        peer_gap = 0.0
        peer_regime = 0.0
        if isinstance(peer_context, dict):
            try:
                pw_c = _safe_int(peer_context.get("peer_women"), default=0, min_val=0, max_val=100000)
                pm_c = _safe_int(peer_context.get("peer_men"), default=0, min_val=0, max_val=100000)
                pn_c = _safe_int(peer_context.get("peer_stores"), default=0, min_val=0, max_val=1000)
                ptot = pw_c + pm_c
                if ptot >= 15 and pn_c >= 1:
                    peer_wp = (pw_c / float(ptot)) * 100.0
                    peer_rel = ptot / (ptot + 60.0)
                    peer_gap = peer_rel * max(-30.0, min(30.0, peer_wp - cur_wp_float))
                    peer_regime = peer_rel * max(-20.0, min(20.0, peer_wp - 53.0))
            except (TypeError, ValueError, OverflowError):
                peer_gap = 0.0
                peer_regime = 0.0

        jis_pull = (cur_wp_float - 54.0) if canon_name.startswith("JIS") else 0.0

        sf = (
            wp_from_wm - pwp_dir,
            noise_rev30,
            mom30,
            lag60_diff,
            dw30_rate,
            dm30_rate,
            peer_gap,
            peer_regime,
            jis_pull,
            usual_diff,
        )
        wp_b = pwp_dir + store_prior_adj + sum(a * x for a, x in zip(_V3_STRUCT_WEIGHTS, sf))
        pred_tot_f = pw + pm
        if women > 0 and men > 0 and pred_tot_f < 10.0:
            wp_b = 52.0 + (pred_tot_f / (pred_tot_f + 1.6)) * (wp_b - 52.0)

    wp_blend = max(8.0, min(92.0, wp_b))
    raw_pred_wpct = _safe_round_int(wp_blend, default=50, min_val=8, max_val=92)

    # 7) 蓄積された「未補正ベース予測 vs 60分後実測」残差ログからのオンラインEmpirical-Bayes自己補正
    online_bias_adj = 0.0
    if apply_online_bias and canon_name:
        try:
            import local_db
            online_bias_adj = float(local_db.get_cached_forecast_residual_adjustment(canon_name, hr) or 0.0)
            if online_bias_adj == 0.0 and store_name != canon_name:
                online_bias_adj = float(local_db.get_cached_forecast_residual_adjustment(store_name, hr) or 0.0)
            if online_bias_adj != 0.0:
                wp_blend = max(8.0, min(92.0, wp_blend + online_bias_adj))
        except Exception:
            online_bias_adj = 0.0

    # 7b) 先行需要インテント（3秒ハシゴ診断）からのリードインジケーター補正
    lead_intent_bias_applied = 0.0
    tot_intents = _safe_int(lead_intent_heat.get("total_intents") if isinstance(lead_intent_heat, dict) else 0, default=0, min_val=0, max_val=1000000)
    if isinstance(lead_intent_heat, dict) and tot_intents >= 3:
        sig_type = str(lead_intent_heat.get("lead_signal") or "normal")
        bias_pt = _safe_float(lead_intent_heat.get("lead_bias_pt"), ndigits=2, default=0.0) or 0.0
        if sig_type == "female_surge" or bias_pt > 0:
            lead_intent_bias_applied = max(1.5, min(3.0, bias_pt if bias_pt > 0 else 2.0))
            wp_blend = max(8.0, min(92.0, wp_blend + lead_intent_bias_applied))
        elif sig_type == "male_surge" or bias_pt < 0:
            lead_intent_bias_applied = min(-1.5, max(-3.0, bias_pt if bias_pt < 0 else -2.0))
            wp_blend = max(8.0, min(92.0, wp_blend + lead_intent_bias_applied))

    cap_side = _safe_int(cap_f, default=120, min_val=10)
    tot_int = max(2, min(cap_side * 2, _safe_round_int(max(2.0, pw + pm), default=2, min_val=2, max_val=cap_side * 2)))
    raw_w = _safe_round_int(tot_int * (wp_blend / 100.0), default=1, min_val=0, max_val=cap_side * 2)
    if men == 0 and wp_blend >= 75.0:
        pred_w = max(2, min(cap_side, max(raw_w, tot_int)))
        pred_m = 0
    elif women == 0 and wp_blend <= 25.0:
        pred_w = 0
        pred_m = max(2, min(cap_side, tot_int))
    else:
        pred_w = max(1, min(cap_side, min(tot_int - 1, raw_w)))
        pred_m = max(1, min(cap_side, tot_int - pred_w))
    pred_wpct = _safe_round_int(wp_blend, default=50, min_val=8, max_val=92)
    delta_pt = pred_wpct - cur_wpct

    # 男余り転落リスク（60分後に女性比50%未満になる校正済み確率%）の算出
    # 予測総人数 pred_tot_f の二項標本分散＋過程ノイズを考慮してSNRを正規化（少人数での過信防止・上限1.0で大規模店の校正維持）
    inv_sqrt_n = 1.0 / math.sqrt(max(6.0, tot_f))
    is_m_now = 1.0 if cur_wp_float < 50.0 else 0.0
    j_safe = 1.0 if (women >= 18 and cur_wp_float >= 68.0) else 0.0
    snr_scale = min(1.0, 10.3 / math.sqrt((2500.0 / (max(4.0, pred_tot_f) + 6.0)) + 52.0))
    rush_z = max(-0.35, min(0.55, 0.25 * float(m_d15 - w_d15) / max(4.0, math.sqrt(max(6.0, pred_tot_f)))))
    z_flip = (
        _V2_FLIP_LOGISTIC_W[0]
        + _V2_FLIP_LOGISTIC_W[1] * snr_scale * ((50.0 - wp_blend) / 10.0)
        + _V2_FLIP_LOGISTIC_W[2] * ((50.0 - cur_wp_float) / 10.0)
        + _V2_FLIP_LOGISTIC_W[3] * inv_sqrt_n
        + _V2_FLIP_LOGISTIC_W[4] * is_m_now
        + _V2_FLIP_LOGISTIC_W[5] * j_safe
        + rush_z
    )
    if isinstance(lead_intent_heat, dict):
        if lead_intent_heat.get("lead_signal") == "male_surge":
            z_flip += 0.45  # 男余り転落リスク加算
        elif lead_intent_heat.get("lead_signal") == "female_surge":
            z_flip -= 0.35  # 男余り転落リスク抑制
    z_flip = max(-12.0, min(12.0, z_flip))
    try:
        raw_flip = 100.0 / (1.0 + math.exp(-z_flip))
    except (ArithmeticError, OverflowError, ValueError):
        raw_flip = 50.0
    flip_risk = _safe_round_int(raw_flip, default=50, min_val=3, max_val=94)

    # --- アラート分類とユーザー向けバッジ生成 ---
    if cur_wpct <= 44 and men >= 12 and pred_wpct <= 48:
        alert_code = "male_glut_lock"
        badge_men = f"60分後も男余り予測 ({pred_wpct}%)"
        badge_women = "1時間後も女子待ち0分圏"
        badge_tone = "danger"
        if is_standing:
            reason = f"定額スタンディング業態では男女比の持続性が高く、60分後も女性比{pred_wpct}%前後で推移する見込みです（自由回遊可・近隣の女性優位店との比較も有効）。"
        else:
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
        badge_men = f"殺到注意 · 60分後 {pred_wpct}%"
        badge_women = "これから男性増 · 即相席圏"
        badge_tone = "warn"
        reason = f"現在は女性優位（{cur_wpct}%）ですが、この時間帯は画面を見て向かう男性が集中しやすく、到着頃には女性比{pred_wpct}%前後へ低下する『群衆殺到リスク（逆転確率{flip_risk}%）』があります。"
    elif hr in (22, 23) and mid_ret <= 62 and delta_pt <= -5:
        alert_code = "last_train_cliff"
        badge_men = f"終電減注意 · 60分後 {pred_wpct}%"
        badge_women = "落ち着いた会話狙い目"
        badge_tone = "warn"
        reason = f"終電前の帰宅波が出やすい1回転目中心店舗（深夜維持率{mid_ret}%）のため、23時台後半にかけて女性が減少する予測です。"
    elif is_rain_shelter and pred_wpct >= 55 and women >= 10 and flip_risk < 22:
        alert_code = "rain_shelter_boost"
        badge_men = f"雨天駅直結 · 60分後 {pred_wpct}%"
        badge_women = "駅直結 · 濡れずに来店可"
        badge_tone = "rain"
        reason = f"現在{city_w_dict.get('city_label', '')}エリアは{city_w_dict.get('condition_ja', '雨')}のため、駅・地下街直結の本店舗に女性客が集中しやすく60分後も女性比{pred_wpct}%前後を維持する予測です。"
    elif women >= 18 and cur_wpct >= 68 and pred_wpct >= 58 and flip_risk <= 15:
        alert_code = "jackpot_hold"
        badge_men = f"好調持続 · 60分後 {pred_wpct}%予測"
        badge_women = "女性多数 · 相席待ち注意"
        badge_tone = "safe"
        reason = f"女性{women}名＆女性比{cur_wpct}%の『70%安全バッファ』に入っており、これから男性が増えても60分後に女性優位を維持する確率は{100 - flip_risk}%と極めて安定しています。"
    elif hr in (2, 3, 4) and women >= 10 and pred_wpct >= 54:
        alert_code = "first_train_rebound"
        badge_men = f"始発待ち女子優位 ({pred_wpct}%予測)"
        badge_women = "始発までゆったり滞在可"
        badge_tone = "safe"
        reason = f"深夜2時以降は男性が先に退店し始発待ちの女性ペアが残るため、朝方まで女性比{pred_wpct}%前後の好バランスが続く予測です。"
    else:
        alert_code = "steady"
        badge_men = f"60分後 {pred_wpct}%予測" if total >= 14 else ""
        badge_women = "1時間後も待ち少なめ" if men >= women - 2 and total >= 12 else ""
        badge_tone = "safe" if pred_wpct >= 56 and women >= 12 else "neutral"
        if 0 <= mtc <= 40:
            reason = f"公式閉店時刻が近いためこれから退店が進む時間帯です（残存見込み：女性約{pred_w}名・男性約{pred_m}名／女性比{pred_wpct}%前後）。"
        else:
            reason = f"直近の流入ペースと時間帯波形から、60分後は女性約{pred_w}名・男性約{pred_m}名（女性比{pred_wpct}%前後）で推移する見込みです。"

    if isinstance(arena_signal, dict) and alert_code in ("steady", "jackpot_hold", "rain_shelter_boost"):
        lbl_short = str(arena_signal.get("label_short") or "").strip()
        if lbl_short:
            reason = f"【{lbl_short}】" + reason

    return {
        "model_version": FORECAST_MODEL_VERSION,
        "pred_60m_wpct": pred_wpct,
        "raw_pred_60m_wpct": raw_pred_wpct,
        "pred_60m_women": pred_w,
        "pred_60m_men": pred_m,
        "pred_women": pred_w,  # E2E test_12 必須互換キー
        "pred_men": pred_m,    # E2E test_12 必須互換キー
        "pred_delta_pt": delta_pt,
        "flip_risk_pct": flip_risk,
        "alert_code": alert_code,
        "badge_men": badge_men,
        "badge_women": badge_women,
        "badge_tone": badge_tone,
        "is_rain_shelter": is_rain_shelter,
        "arena_signal_label": str(_safe_dict(arena_signal).get("label_short") or "") if isinstance(arena_signal, dict) else "",
        "arena_phase": str(_safe_dict(arena_signal).get("phase") or "") if isinstance(arena_signal, dict) else "",
        "arena_event_title": str(_safe_dict(arena_signal).get("event_title") or "") if isinstance(arena_signal, dict) else "",
        "turnover_momentum": None,
        "online_bias_adj_pt": round(online_bias_adj, 1),
        "lead_intent_bias_pt": round(lead_intent_bias_applied, 1),
        "lead_intent_heat": lead_intent_heat if isinstance(lead_intent_heat, dict) else None,
        "reason_short": reason,
    }


def get_chayamachi_shift_analysis(
    now_jst: datetime.datetime = None,
    yatakoi_cust: int = 0,
    asobibar_cust: int = 0,
    yatakoi_seats: int = 70,
    asobibar_seats: int = 10,
    pos_telemetry: Optional[dict] = None,
    **kwargs
) -> dict:
    """
    梅田・茶屋町ビル内の「ASOBIBARフロア vs ヤタコイフロア」2フロア回遊シフト分析 (R4 / Feature 13)。
    
    同ビル内の着席相席ラウンジ（ヤタコイ）から自由回遊スタンディング（ASOBIBAR）への
    時間帯別回遊ダイナミクスを数理モデリング（序盤ヤタコイ優位 → 深夜ASOBIBAR優位へのシフト推移）。
    
    ゼロ除算・欠損・型異常・負数・極端値をすべて防御し、ゼロ例外保証（Zero-Exception Guarantee）を提供する。
    """
    now_jst = _normalize_jst(now_jst)

    # 0. 引数の安全な初期サニタイズ（型異常・文字列・負数・ゼロ除算の事前防御）
    if yatakoi_cust is not None:
        yatakoi_cust = _safe_int(yatakoi_cust, default=0, min_val=0, max_val=10000)
    if asobibar_cust is not None:
        asobibar_cust = _safe_int(asobibar_cust, default=0, min_val=0, max_val=10000)
    yatakoi_seats = _safe_int(yatakoi_seats, default=70, min_val=1, max_val=10000)
    asobibar_seats = _safe_int(asobibar_seats, default=10, min_val=1, max_val=10000)

    # 1. pos_telemetry からの自動展開
    if pos_telemetry and isinstance(pos_telemetry, dict):
        yk_m = _safe_int(pos_telemetry.get("yatakoi_men"), default=0, min_val=0, max_val=10000)
        yk_w = _safe_int(pos_telemetry.get("yatakoi_women"), default=0, min_val=0, max_val=10000)
        as_m = _safe_int(pos_telemetry.get("asobibar_men"), default=0, min_val=0, max_val=10000)
        as_w = _safe_int(pos_telemetry.get("asobibar_women"), default=0, min_val=0, max_val=10000)
        if yatakoi_cust is None or (yatakoi_cust == 0 and (yk_m + yk_w > 0)):
            yatakoi_cust = yk_m + yk_w
        if asobibar_cust is None or (asobibar_cust == 0 and (as_m + as_w > 0)):
            asobibar_cust = as_m + as_w
        tel_y_seats = pos_telemetry.get("yatakoi_seats")
        if tel_y_seats is None:
            tel_y_seats = pos_telemetry.get("ykSeats")
        if tel_y_seats is not None:
            yatakoi_seats = _safe_int(tel_y_seats, default=70, min_val=1, max_val=10000)
        elif not yatakoi_seats or yatakoi_seats <= 0:
            yatakoi_seats = 70

        tel_a_seats = pos_telemetry.get("asobibar_seats")
        if tel_a_seats is None:
            tel_a_seats = pos_telemetry.get("seats")
        if tel_a_seats is not None:
            asobibar_seats = _safe_int(tel_a_seats, default=10, min_val=1, max_val=10000)
        elif not asobibar_seats or asobibar_seats <= 0:
            asobibar_seats = 10

    # 2. 安全な型キャストと境界クランプ (ゼロ除算防止)
    y_cust = _safe_int(yatakoi_cust, default=0, min_val=0, max_val=10000)
    a_cust = _safe_int(asobibar_cust, default=0, min_val=0, max_val=10000)
    y_seats = _safe_int(yatakoi_seats, default=70, min_val=1, max_val=10000)
    a_seats = _safe_int(asobibar_seats, default=10, min_val=1, max_val=10000)
    total_cust = y_cust + a_cust

    # 3. 占有率の算出 (ゼロ除算防御)
    y_occ = round((float(y_cust) / float(y_seats)) * 100.0, 1)
    a_occ = round((float(a_cust) / float(a_seats)) * 100.0, 1)

    hour = now_jst.hour
    is_open = (18 <= hour < 24) or (0 <= hour < 5)

    # 4. 回遊フェーズ判定
    if not is_open:
        phase = "closed"
        phase_label_ja = "営業時間外"
        phase_tone = "zinc"
        flow_direction = "idle"
        flow_label_ja = "営業時間外（18:00オープン）"
        advice = "営業開始後にリアルタイム回遊データを集計します。"
        recommended_action = "18:00の営業開始をお待ちください"
    elif 18 <= hour < 22:
        phase = "yatakoi_dominant"
        phase_label_ja = "ヤタコイ優位（1軒目・着席相席フェーズ）"
        phase_tone = "emerald"
        flow_direction = "yatakoi_inflow"
        flow_label_ja = "1軒目着席：仕事・買い物帰りの女性先行来店中"
        advice = "1軒目の時間帯は3Fヤタコイで相席成立が活発です。まずはヤタコイからのスタートが最もスムーズです。"
        recommended_action = "まずはヤタコイ（着席相席）からのスタートが最もスムーズです"
    elif 22 <= hour < 24:
        phase = "transition"
        phase_label_ja = "回遊シフト中（着席→スタンディング移行期）"
        phase_tone = "amber"
        flow_direction = "yatakoi_to_asobibar"
        flow_label_ja = "フロア間回遊：ヤタコイからASOBIBARへのハシゴ進行中"
        advice = "ヤタコイからASOBIBARへのフロア間回遊が始まります。両フロアともに活気があります。"
        recommended_action = "ヤタコイで相席を楽しんだ後、同ビルASOBIBARへのスムーズなハシゴが可能です"
    else:  # 0 <= hour < 5
        phase = "asobibar_shift"
        phase_label_ja = "深夜ASOBIBARバーシフトフェーズ"
        phase_tone = "purple"
        flow_direction = "asobibar_inflow"
        flow_label_ja = "深夜ハシゴ：2軒目・終電後の自由回遊へ集中"
        advice = "深夜は2F ASOBIBAR（スタンディングバー）が中心になります。メーター課金を気にせず楽しめます。"
        recommended_action = "2軒目・終電後は10分課金のないASOBIBARへのハシゴが快適です"

    # 5. 実測・推定シェアの算出
    if total_cust > 0:
        y_ratio = round((float(y_cust) / float(total_cust)) * 100.0, 1)
        a_ratio = round((float(a_cust) / float(total_cust)) * 100.0, 1)
    else:
        if 18 <= hour < 22:
            y_ratio, a_ratio = 80.0, 20.0
        elif 22 <= hour < 24:
            y_ratio, a_ratio = 50.0, 50.0
        elif 0 <= hour < 5:
            y_ratio, a_ratio = 20.0, 80.0
        else:
            y_ratio, a_ratio = 50.0, 50.0

    return {
        "phase": phase,
        "phase_label": phase_label_ja,
        "phase_label_ja": phase_label_ja,
        "phase_tone": phase_tone,
        "flow_direction": flow_direction,
        "flow_label_ja": flow_label_ja,
        "yatakoi_cust": y_cust,
        "asobibar_cust": a_cust,
        "total_cust": total_cust,
        "yatakoi_seats": y_seats,
        "asobibar_seats": a_seats,
        "yatakoi_occ_pct": y_occ,
        "asobibar_occ_pct": a_occ,
        "yatakoi_ratio": round(y_ratio),
        "asobibar_ratio": round(a_ratio),
        "yatakoi_ratio_pct": y_ratio,
        "asobibar_ratio_pct": a_ratio,
        "shift_score": a_ratio,
        "advice": advice,
        "recommended_action": recommended_action,
        "recommendation": recommended_action,
        "description": advice,
        "is_open_now": is_open,
    }


def enrich_stores_with_moat_signals(
    stores: list,
    now_jst: datetime.datetime = None,
    allow_weather_blocking: bool = False,
    context_stores: list = None,
) -> list:
    """
    店舗リスト（ranking または full_data）に対して一括で：
      1) JIS POS実測データからのオンライン学習キャリブレーション
      2) Open-Meteo 都市別気象キャッシュ参照（非ブロッキング）
      3) ドーム・アリーナ終演流入シグナル検知
      4) 同都市リアルタイム他店舗需給コンテキスト（peer_context）＆非ブロッキング多段ラグ（lag_context）のO(N)構築
      5) 全店舗の「リアル待ち時間推論（wait_info）」＆「60分後男女比予測バッジ（forecast_60m）」＋予測精度実績の付与
    を実行して返す（全63店舗合計で約0.5ms以内の超高速インメモリ処理）。
    """
    if not stores:
        return stores
    now_jst = _normalize_jst(now_jst)
    ctx_source = context_stores if context_stores is not None else stores
    calibrate_from_jis_pos(ctx_source)
    weather_map = get_city_weather_map(allow_blocking=allow_weather_blocking)
    arena_map = detect_arena_event_surges(ctx_source, now_jst=now_jst)
    peer_map = build_city_peer_context(ctx_source)

    acc_map = {}
    lags_map = {}
    try:
        import local_db
        acc_map = local_db.get_cached_store_accuracy_map() or {}
        lags_map = local_db.get_cached_recent_lags_map(now_jst) or {}
    except Exception:
        acc_map = {}
        lags_map = {}

    lead_heat_cache = {}

    for s in stores:
        if not isinstance(s, dict):
            continue
        sname = str(s.get("name") or "").strip()
        canon_name = _resolve_internal_store_key(sname)
        w = _safe_int(s.get("women"), default=0, min_val=0, max_val=100000)
        m = _safe_int(s.get("men"), default=0, min_val=0, max_val=100000)
        if "is_open_now" in s and s.get("is_open_now") is not None:
            is_open = bool(s.get("is_open_now"))
        else:
            try:
                import store_hours
                is_open = bool(
                    store_hours.get_business_hours_status(
                        canon_name or sname, total_guests=w + m, now_jst=now_jst
                    ).get("is_open_now", True)
                )
            except Exception:
                is_open = True
            s["is_open_now"] = is_open
        w_diff = _safe_int(s.get("women_diff"), default=0, min_val=-100000, max_val=100000)
        m_diff = _safe_int(s.get("men_diff"), default=0, min_val=-100000, max_val=100000)
        uw_diff = _safe_float(s.get("usual_w_diff"))
        s["women"] = w
        s["men"] = m
        s["women_diff"] = w_diff
        s["men_diff"] = m_diff
        if "usual_w_diff" in s:
            s["usual_w_diff"] = uw_diff
        if "usual_m_diff" in s:
            s["usual_m_diff"] = _safe_float(s.get("usual_m_diff"))
        pos_tel = s.get("pos_telemetry")

        st_area = None
        try:
            import concierge_matcher
            st_area = concierge_matcher.normalize_concierge_area(canon_name or sname)
        except Exception:
            st_area = None

        lead_heat = None
        if st_area:
            if st_area in lead_heat_cache:
                lead_heat = lead_heat_cache[st_area]
            else:
                try:
                    import analytics
                    lead_heat = analytics.get_lead_demand_heat(area=st_area, hours=2)
                except Exception:
                    lead_heat = None
                lead_heat_cache[st_area] = lead_heat

        wait_info = estimate_store_wait_and_pos(
            store_name=canon_name or sname,
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
            arena_signal=arena_map.get(sname) or arena_map.get(canon_name),
            lag_context=lags_map.get(canon_name) or lags_map.get(sname) or {},
            peer_context=peer_map.get(canon_name) or peer_map.get(sname),
            lead_intent_heat=lead_heat,
        )
        st_acc = acc_map.get(canon_name) or acc_map.get(sname)
        if st_acc:
            fc60["accuracy"] = st_acc
        s["wait_estimate"] = wait_info
        s["forecast_60m"] = fc60
        s["lead_demand_heat"] = lead_heat

        # F8: 席回転スピード（回転モメンタム指数）の付与
        tm_info = calculate_turnover_momentum_index(
            store_name=canon_name or sname,
            women=w,
            men=m,
            is_open_now=is_open,
            women_diff=w_diff,
            men_diff=m_diff,
            lag_context=lags_map.get(canon_name) or lags_map.get(sname) or {},
            pos_telemetry=pos_tel,
            p99_cap=max(35, int(STORE_P99_CAPACITY.get(canon_name, 120))),
            now_jst=now_jst,
        )
        tm_d = _safe_dict(tm_info)
        s["turnover_momentum"] = tm_info
        s["turnover_momentum_index"] = tm_d.get("score")
        s["turnover_label"] = tm_d.get("label")
        s["turnover_tone"] = tm_d.get("tone")

        # 気象ブリーフの安全付与（キャッシュ空時・破損時は安全デフォルトへ完全フォールバック）
        s["weather_brief"] = get_store_weather_brief(canon_name or sname, weather_map)

        # F13: 梅田・茶屋町 ASOBIBAR vs ヤタコイ 回遊シフト分析の付与
        is_chayamachi = (
            sname in ("YATAKOI UMEDA", "ASOBIBAR 茶屋町")
            or canon_name in ("YATAKOI UMEDA", "ASOBIBAR 茶屋町")
            or s.get("slug") in ("yatakoi-umeda", "asobibar-chayamachi")
            or ("茶屋町" in (sname or "") and ("ASOBIBAR" in (sname or "") or "ヤタコイ" in (sname or "")))
        )
        if is_chayamachi:
            ch_pos = pos_tel if (isinstance(pos_tel, dict) and pos_tel.get("telemetry_type") == "yatakoi_pos") else None
            if not ch_pos and ctx_source:
                for cs in ctx_source:
                    if isinstance(cs, dict):
                        cs_p = cs.get("pos_telemetry")
                        if isinstance(cs_p, dict) and cs_p.get("telemetry_type") == "yatakoi_pos":
                            ch_pos = cs_p
                            break
            if ch_pos and not pos_tel:
                s["pos_telemetry"] = ch_pos
            s["chayamachi_shift"] = get_chayamachi_shift_analysis(
                now_jst=now_jst,
                pos_telemetry=ch_pos,
            )
    return stores
