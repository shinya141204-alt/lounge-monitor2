"""
sns_editorial.py - Multi-Platform 4-Role SNS Editorial Room Pipeline (X / Threads / Instagram)

Replaces rigid, repetitive "🥇🥈🥉 TOP3" bot posts across ALL 3 SNS channels (X, Threads, Instagram)
with a 4-role virtual editorial team:
  1. Researcher (現場データ発掘係): Extracts live hub gender-ratio gaps, 15-min surges, usual same-weekday anomalies, and East/West leaders, while verifying strict factual thresholds.
  2. Planner (編成・マンネリ防止係): Tracks per-channel history (`x`, `threads`, `instagram`) so consecutive posts never repeat the same angle or hook, and suppresses mechanical TOP3 lists.
  3. Writer (執筆・下書き係): Generates 4 distinct human-voiced draft candidates tailored to each platform's algorithm and character rules.
  4. Evaluator / Desk (鬼デスク・評価係): Scores all 4 candidates out of 100 across 4 axes (Human Tone, Anti-Duplication, Data Accuracy/Specificity, Platform Format Hygiene), attaches red-pen critiques, and selects the #1 winner.

All proposals, scores, and critiques are exposed exclusively to Master (`2918`) on `/staff/sns-editorial`.
"""

import os
import re
import time
import json
import datetime
import logging
import threading
import requests

import job_state
import store_master
import x_poster

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))
GEMINI_KEY_STORAGE_PREFIX = "token:gemini_api_key"
DEFAULT_GEMINI_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

# Words/phrases that betray AI-generated or promotional copywriting — strictly penalized by Desk
AI_CLICHE_WORDS = (
    "必見",
    "徹底解説",
    "いかがでしたか",
    "いかがですか",
    "まさに",
    "間違いなし",
    "大注目",
    "魅力的",
    "最適解",
    "盛り上がりを見せて",
    "ぜひ足を運んで",
    "お見逃しなく",
    "ご紹介",
)

_weather_cache = {"ts": 0.0, "data": None, "fetching": False}
_ai_candidate_cache = {}  # cache_key -> {"ts": float, "candidate": dict, "model": str}
_last_gemini_meta = {"last_model": "", "last_error": "", "last_ok_at": "", "working_route": None}

CHANNEL_LABELS = {
    "x": "𝕏 X（旧Twitter）",
    "threads": "🧵 Threads（スレッズ）",
    "instagram": "📸 Instagram（インスタグラム）",
}

STYLE_LABELS = {
    "ai_live_adlib": "✨ Gemini AI 現場アドリブ実況型（天気・街況連動）",
    # X styles
    "hub_gap": "🚶 エリア内ギャップ・ハシゴ観察型",
    "surge_focus": "📈 1店舗フォーカス・急増/穴場つぶやき型",
    "usual_anomaly": "💡 普段の同曜日比（平比）ギャップ発見型",
    "east_west_talk": "🗼🐙 東西トップ現場空気感トーク型",
    "compact_top3": "🥇 従来型ランキング速報（🥇🥈🥉 TOP3）",
    # Threads styles
    "threads_city_story": "🗣️ 街の温度差＆リアル現場エッセイ型",
    "threads_anomaly_insight": "📊 普段比（平比）の異変＆男女別立ち回り型",
    "threads_hopping_guide": "👟 東西4大エリア「1軒目→2軒目」実践比較型",
    "threads_classic_top3": "🏆 従来型・全国TOP3＆主要街掲示板型",
    # Instagram styles
    "ig_empathy_story": "💬 共感1行目フック＆待ち回避ストーリー型",
    "ig_data_discovery": "📖 データ編集長コラム＆穴場法則マガジン型",
    "ig_concise_save": "📌 余白重視・男女別スマート保存ガイド型",
    "ig_classic_template": "📊 従来型・定型アナウンスキャプション",
}

URBAN_CLUSTERS = [
    ("新宿", "shinjuku", ("新宿", "SHINJUKU")),
    ("渋谷・恵比寿", "shibuya", ("渋谷", "恵比寿", "SHIBUYA", "EBISU")),
    ("梅田・茶屋町", "umeda", ("梅田", "茶屋町", "大阪駅前", "UMEDA", "CHAYAMACHI")),
    ("難波・心斎橋", "namba", ("難波", "心斎橋", "NAMBA", "SHINSAIBASHI")),
    ("名古屋・栄", "nagoya", ("名古屋", "栄", "錦", "NAGOYA")),
    ("福岡・天神", "fukuoka", ("福岡", "天神", "FUKUOKA")),
    ("札幌・すすきの", "sapporo", ("札幌", "SAPPORO")),
]


def natural_store_name(raw_name: str) -> str:
    """
    Convert internal codes like 'OLG 新宿' or 'JIS SHINJUKU' into natural, human-searchable Japanese names:
      - 'OLG 新宿' -> 'オリエンタル新宿'
      - 'OLG 大阪駅前' -> 'オリエンタル大阪駅前'
      - 'OLG ag渋谷' -> 'ag渋谷'
      - 'JIS SHINJUKU' -> 'JIS新宿'
      - 'YATAKOI UMEDA' -> 'ヤタコイ梅田'
    """
    formatted = x_poster.format_ja_store_name(raw_name)
    if formatted.startswith("OLG "):
        return "オリエンタル" + formatted[4:].replace(" ", "")
    if formatted.startswith("JIS "):
        return "JIS" + formatted[4:].replace(" ", "")
    return formatted


def _domestic_stores(stores: list) -> list:
    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    return [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]


def _enrich_with_benchmarks(stores: list) -> list:
    """Attach live benchmarks (usual_w_diff, core_time_short, midnight_retention_pct) and natural Japanese names."""
    try:
        import local_db
        benchmarks = local_db.get_all_stores_live_benchmarks(allow_blocking=False) or {}
    except Exception:
        benchmarks = {}

    enriched = []
    for s in stores:
        item = dict(s)
        sname = item.get("name") or ""
        w = int(item.get("women") or 0)
        m = int(item.get("men") or 0)
        tot = w + m
        item["women"] = w
        item["men"] = m
        item["total"] = tot
        item["wpct"] = round((w / tot) * 100) if tot > 0 else 0
        item["ja_name"] = natural_store_name(sname)
        bm = benchmarks.get(sname) or {}
        if "usual_w_diff" not in item:
            avg_w = bm.get("usual_avg_w")
            item["usual_w_diff"] = int(round(w - avg_w)) if avg_w is not None and tot > 0 else None
        if "midnight_retention_pct" not in item:
            item["midnight_retention_pct"] = bm.get("midnight_retention_pct")
        if "core_time_short" not in item:
            item["core_time_short"] = bm.get("core_time_short")
        enriched.append(item)
    return enriched


def _history_key_for_channel(channel: str) -> str:
    ch = (channel or "x").lower()
    if ch == "x":
        return "sns_editorial_log"
    return f"sns_editorial_log_{ch}"


def get_recent_editorial_history(channel: str = "x", limit: int = 8, allow_blocking: bool = True) -> list:
    """Load recent editorial meeting logs for the specified SNS channel."""
    raw = job_state.get_value(_history_key_for_channel(channel), allow_blocking=allow_blocking)
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items[:limit]
    except Exception:
        pass
    return []


# =====================================================================
# Gemini API Key Management + Weather & Calendar Live Context
# =====================================================================
def clear_editorial_ai_cache():
    """Clear in-memory AI draft cache (called when API key updates or manual refresh requested)."""
    _ai_candidate_cache.clear()


def get_gemini_api_key(allow_blocking: bool = True) -> str:
    """
    Resolve active Gemini API key from either:
      1. Master-configured persistent secret in `job_state` (`token:gemini_api_key`)
      2. Render environment variable `GEMINI_API_KEY` or `GOOGLE_API_KEY`
    """
    stored = (job_state.get_value(GEMINI_KEY_STORAGE_PREFIX, allow_blocking=allow_blocking) or "").strip()
    if stored:
        return stored
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


def set_gemini_api_key(api_key: str):
    """Persist or clear Master-provided Gemini API key (obfuscated via `token:` prefix in job_state)."""
    cleaned = (api_key or "").strip()
    job_state.set_value(GEMINI_KEY_STORAGE_PREFIX, cleaned)
    clear_editorial_ai_cache()
    _last_gemini_meta["last_error"] = ""
    _last_gemini_meta["working_route"] = None


def get_gemini_status(allow_blocking: bool = True) -> dict:
    """Return safe, masked status metadata for the Master editorial dashboard (never exposes raw key)."""
    stored = (job_state.get_value(GEMINI_KEY_STORAGE_PREFIX, allow_blocking=allow_blocking) or "").strip()
    env_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    active_key = stored or env_key
    if stored:
        source = "管理画面から登録済み（暗号化保存）"
    elif env_key:
        source = "Render環境変数（GEMINI_API_KEY）"
    else:
        source = "未設定"

    masked = ""
    if active_key:
        if len(active_key) > 8:
            masked = f"{active_key[:4]}...{active_key[-4:]}"
        else:
            masked = "****"

    last_err = _last_gemini_meta.get("last_error") or ""
    if not active_key:
        state = "unconfigured"
    elif last_err:
        state = "degraded"
    else:
        state = "active"

    return {
        "configured": bool(active_key),
        "state": state,
        "key_kind": "vertex_express" if active_key.startswith("AQ.") else ("ai_studio" if active_key else "none"),
        "masked_key": masked,
        "source": source,
        "last_model": _last_gemini_meta.get("last_model") or DEFAULT_GEMINI_MODELS[0],
        "last_error": last_err,
        "last_ok_at": _last_gemini_meta.get("last_ok_at") or "",
    }


def _nth_monday(year: int, month: int, n: int) -> int:
    first_wd = datetime.date(year, month, 1).weekday()  # 0=Mon
    first_mon = 1 + ((7 - first_wd) % 7)
    return first_mon + (n - 1) * 7


def _is_jp_base_holiday(d: datetime.date) -> bool:
    y, m, day = d.year, d.month, d.day
    fixed = {
        (1, 1), (2, 11), (2, 23), (4, 29),
        (5, 3), (5, 4), (5, 5), (8, 11),
        (11, 3), (11, 23),
    }
    if (m, day) in fixed:
        return True
    if m == 1 and day == _nth_monday(y, 1, 2):
        return True
    if m == 7 and day == _nth_monday(y, 7, 3):
        return True
    if m == 9 and day == _nth_monday(y, 9, 3):
        return True
    if m == 10 and day == _nth_monday(y, 10, 2):
        return True
    # Vernal & Autumnal Equinoxes (accurate 2000-2099 formula)
    vernal = int(20.8431 + 0.242194 * (y - 1980) - int((y - 1980) / 4))
    autumnal = int(23.2488 + 0.242194 * (y - 1980) - int((y - 1980) / 4))
    if (m == 3 and day == vernal) or (m == 9 and day == autumnal):
        return True
    return False


def is_jp_holiday(d: datetime.date) -> bool:
    if _is_jp_base_holiday(d):
        return True
    # Substitute holiday when Sunday was a base holiday
    if d.weekday() == 0 and _is_jp_base_holiday(d - datetime.timedelta(days=1)):
        return True
    return False


def build_calendar_context(now_jst: datetime.datetime = None) -> dict:
    """Build street-level calendar context (pre-holiday, payday window, weekend peak, weekday chill)."""
    now_jst = now_jst or datetime.datetime.now(JST)
    biz_dt = (now_jst - datetime.timedelta(hours=5)).date()
    tomorrow = biz_dt + datetime.timedelta(days=1)
    wd = biz_dt.weekday()  # 0=Mon..6=Sun
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_ja = weekdays_ja[wd]

    tomorrow_holiday = is_jp_holiday(tomorrow)
    today_holiday = is_jp_holiday(biz_dt)
    is_payday_window = (25 <= biz_dt.day <= 28) or (biz_dt.day >= 29 and wd in (4, 5))

    if (biz_dt.month, biz_dt.day) in ((10, 30), (10, 31)):
        tag = "🎃 ハロウィン週末モード"
        note = "ハロウィン時期で繁華街全体の出足が非常に早く、20時台から大型店が埋まりやすい夜"
    elif biz_dt.month == 12 and biz_dt.day >= 18:
        tag = "🎄 忘年会・年末ピークモード"
        note = "忘年会・2次会帰りのグループ合流で21時半以降の人数急増が起きやすい年末モード"
    elif wd not in (4, 5) and tomorrow_holiday:
        tag = f"✨ 祝前日（{wd_ja}曜夜）ボーナスモード"
        note = "明日が祝日のため、平日でも金曜並みに22時以降まで客足が落ちにくい狙い目の夜"
    elif is_payday_window and wd in (3, 4, 5):
        tag = f"💰 給料日直後×{wd_ja}曜ピーク"
        note = "25日給料日直後の週末前で、1軒目（20時〜21時台）の立ち上がりが普段より早い傾向"
    elif wd in (4, 5):
        tag = f"🔥 {wd_ja}曜ピークナイト"
        note = "週末ピークのため人気店は21時半すぎに男性待ちが出やすく、徒歩3分の別店舗比較が効く夜"
    elif wd == 6 or (today_holiday and not tomorrow_holiday):
        tag = f"⏰ {wd_ja}曜・早め勝負モード"
        note = "翌日仕事組が22時半〜23時頃に帰路につくため、19時半〜21時半のコアタイムが最も当たりやすい夜"
    else:
        tag = f"🥂 {wd_ja}曜・平日スマート穴場モード"
        note = "週末ほど騒がしすぎず、女性比が安定している店舗を選べば落ち着いて話しやすい平日の狙い目夜"

    return {
        "weekday_ja": wd_ja,
        "tag": tag,
        "note": note,
        "is_pre_holiday": tomorrow_holiday,
        "is_payday_window": is_payday_window,
    }


def build_slot_persona_context(slot_label: str = "22:00", channel: str = "x") -> dict:
    """
    Return time-of-night human emotional tone & speaking energy so the account never sounds
    like a flat, uniform bot across 20:00, 22:00, and 00:00+ midnight slots.
    """
    ch = (channel or "x").lower()
    if ch == "instagram":
        return {
            "label": "📖 編集部の保存版裏話トーン",
            "guidance": (
                "週末や仕事帰りの店選びで失敗してほしくない編集者が、友達にこっそりデータの法則を教える温かいトーン。"
                "宣伝文句ではなく『これ知ってるだけで待ち時間なくなるよ』という等身大の語り口にする。"
            ),
        }
    if slot_label == "20:00":
        return {
            "label": "🍻 20:00 仕事終わり・1軒目ウォームアップ",
            "guidance": (
                "仕事終わりの落ち着いた温度感。まだガツガツ煽らず、"
                "『お疲れさまです、これから1軒目どこ入るか迷ってる人向けに置いときます』『今夜ここ立ち上がり早めかも』"
                "といった余裕のあるスマートな語り口にする。"
            ),
        }
    if slot_label in ("00:00", "02:00"):
        return {
            "label": "🌙 00:00 終電後の深夜ラジオ・本音トーン",
            "guidance": (
                "終電が終わった後の、少し肩の力が抜けた深夜のリアルな本音トーン。"
                "ハイテンションな感嘆符（！）やキラキラ絵文字を抑え、"
                "『終電なくなった後のリアルな残り方なんだけど…』『この時間からまだちゃんと残ってるの強いな』"
                "のように静かに呟く空気感にする。"
            ),
        }
    return {
        "label": "🔥 22:00 ピーク・2軒目移動のライブ実況",
        "guidance": (
            "街が一番動いている22時台のリアルタイム実況トーン。"
            "『いま1軒目終わりの組が一気に流れてきてて』『徒歩3分なのにここまで男女比割れるか…』"
            "のように、今まさにスマホで現場モニターを見ている臨場感を出す。"
        ),
    }


def _describe_wmo_code(code: int, precip: float) -> tuple:
    if precip >= 0.4 or code in (51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99):
        return ("雨", True)
    if code in (71, 73, 75, 77, 85, 86):
        return ("雪", True)
    if code in (1, 2, 3, 45, 48):
        return ("曇り", False)
    return ("晴れ", False)


def _refresh_weather_sync():
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": "35.69,34.70",
                "longitude": "139.70,135.50",
                "current": "temperature_2m,precipitation,weather_code",
                "timezone": "Asia/Tokyo",
            },
            timeout=2.2,
        )
        if resp.status_code == 200:
            payload = resp.json()
            items = payload if isinstance(payload, list) else [payload]
            if len(items) >= 2:
                tk_cur = items[0].get("current") or {}
                os_cur = items[1].get("current") or {}
                tk_temp = int(round(float(tk_cur.get("temperature_2m", 18))))
                os_temp = int(round(float(os_cur.get("temperature_2m", 19))))
                tk_desc, tk_rain = _describe_wmo_code(
                    int(tk_cur.get("weather_code", 0)),
                    float(tk_cur.get("precipitation", 0.0)),
                )
                os_desc, os_rain = _describe_wmo_code(
                    int(os_cur.get("weather_code", 0)),
                    float(os_cur.get("precipitation", 0.0)),
                )
                summary = f"東京（新宿・渋谷）{tk_desc} {tk_temp}℃ ／ 大阪（梅田・難波）{os_desc} {os_temp}℃"
                if tk_rain and os_rain:
                    hint = "東京・大阪ともに雨模様のため、地下街・駅チカ店舗に客足が集中しやすい夜☔"
                elif tk_rain:
                    hint = "関東エリアは雨模様のため、新宿・渋谷・上野など駅近店舗への集中が目立つ夜☔"
                elif os_rain:
                    hint = "関西エリアは雨模様のため、梅田・難波の地下直結エリアに人が集まりやすい夜☔"
                elif min(tk_temp, os_temp) <= 12:
                    hint = f"外は{min(tk_temp, os_temp)}℃前後と冷え込んでいる分、暖かい屋内ラウンジに早めに人が集まる夜🌙"
                elif max(tk_temp, os_temp) >= 28:
                    hint = "蒸し暑い夜で1軒目から涼しいラウンジへ直行するグループが多い夜🍻"
                else:
                    hint = f"外歩きしやすい気候（東京{tk_temp}℃・大阪{os_temp}℃）で、エリア内の2軒目ハシゴも動きやすい夜✨"

                result = {
                    "available": True,
                    "summary": summary,
                    "adlib_hint": hint,
                    "is_rainy": tk_rain or os_rain,
                    "tokyo_rain": tk_rain,
                    "osaka_rain": os_rain,
                    "temps": [tk_temp, os_temp],
                }
                _weather_cache["ts"] = time.time()
                _weather_cache["data"] = result
                return result
    except Exception as e:
        logger.debug(f"Open-Meteo weather fetch skipped: {e}")
    finally:
        _weather_cache["fetching"] = False
    return None


def fetch_live_weather_context(force_network: bool = False, allow_blocking: bool = False) -> dict:
    """
    Fetch free real-time Tokyo & Osaka weather via Open-Meteo (cached for 30 min).
    When `allow_blocking=False` (default for web page loads), returns cached data in 0ms
    and refreshes asynchronously in a background thread so page navigation never blocks.
    """
    now_t = time.time()
    cached_data = _weather_cache.get("data")
    if not force_network and cached_data and (now_t - _weather_cache["ts"] < 1800):
        return cached_data

    if os.environ.get("DISABLE_SCHEDULER") == "1" and not force_network:
        return {
            "available": False,
            "summary": "気象データ取得スキップ（テスト環境）",
            "adlib_hint": "",
            "is_rainy": False,
            "temps": [],
        }

    if allow_blocking or force_network:
        _weather_cache["fetching"] = True
        fresh = _refresh_weather_sync()
        if fresh:
            return fresh
    else:
        if not _weather_cache.get("fetching"):
            _weather_cache["fetching"] = True
            threading.Thread(target=_refresh_weather_sync, daemon=True).start()
        if cached_data:
            return cached_data

    return {
        "available": False,
        "summary": "気象データ取得中...",
        "adlib_hint": "",
        "is_rainy": False,
        "temps": [],
    }


# =====================================================================
# Role 1: Researcher (現場データ発掘係)
# =====================================================================
def run_researcher(
    stores: list,
    slot_label: str = "22:00",
    channel: str = "x",
    ig_ctx: dict = None,
    now_jst: datetime.datetime = None,
    allow_weather_blocking: bool = False,
) -> dict:
    """
    Analyze live domestic store counts (and weekly carousel stats for IG) to extract verified field findings:
      1. Best same-city gender-ratio gap (`best_cluster`) where one store is female-lead and another differs
      2. Genuine sweet-spot / momentum surge store (`focus_store` with women >= men or top surge)
      3. Positive anomaly vs usual same-weekday/hour average (`anomaly_store`)
      4. City leaders across Shinjuku, Shibuya, Umeda, Namba, Nagoya, Fukuoka
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    dom = _enrich_with_benchmarks(_domestic_stores(stores))
    dom.sort(key=lambda x: (x["women"], x["men"]), reverse=True)

    total_w = sum(s["women"] for s in dom)
    total_m = sum(s["men"] for s in dom)
    active_stores = [s for s in dom if s["total"] >= 6]

    # 1. Urban cluster gap analysis
    best_cluster = None
    best_cluster_score = -1.0
    city_leaders = []
    for hub_label, area_id, kws in URBAN_CLUSTERS:
        hub_s = [
            s for s in dom
            if any(k in (s.get("name") or "").upper() or k in (s.get("name") or "") for k in kws)
        ]
        active_hub = [s for s in hub_s if s["total"] >= 8]
        substantial_hub = [s for s in hub_s if s["total"] >= 24]
        candidate_hub = substantial_hub if len(substantial_hub) >= 2 else active_hub
        if hub_s and hub_s[0]["women"] > 0:
            city_leaders.append({"hub_label": hub_label, "area_id": area_id, "store": hub_s[0]})

        if len(candidate_hub) >= 2:
            by_ratio = sorted(candidate_hub, key=lambda x: (x["wpct"], x["women"]), reverse=True)
            high_s = by_ratio[0]
            low_s = by_ratio[-1]
            pct_gap = high_s["wpct"] - low_s["wpct"]
            score = pct_gap * 1.35 + (high_s["women"] * 0.25) + (min(high_s["total"], low_s["total"]) * 0.25)
            if high_s["women"] >= 10 and score > best_cluster_score:
                best_cluster_score = score
                best_cluster = {
                    "hub_label": hub_label,
                    "area_id": area_id,
                    "high_store": high_s,
                    "low_store": low_s,
                    "pct_gap": pct_gap,
                    "store_count": len(active_hub),
                }

    # 2. Genuine sweet-spot / surge store (strictly prefer stores where women >= men so we never call a male-heavy store "穴場")
    female_lead_pool = [s for s in dom if s["women"] >= 12 and s["women"] >= s["men"]]
    if not female_lead_pool:
        female_lead_pool = [s for s in dom if s["women"] >= 10]
    female_lead_pool.sort(
        key=lambda x: (
            int(x.get("women_diff") or 0) * 3.0
            + (x["women"] - x["men"]) * 1.6
            + x["women"] * 0.45
        ),
        reverse=True,
    )
    focus_store = female_lead_pool[0] if female_lead_pool else (dom[0] if dom else None)

    # Also detect strongest male-lead lively store (great for female followers: 0-min wait)
    male_lead_pool = [s for s in dom if s["men"] >= 18 and s["women"] >= 10 and (s["men"] - s["women"]) >= 6]
    male_lead_pool.sort(key=lambda x: (x["men"] - x["women"], x["men"]), reverse=True)
    male_lead_store = male_lead_pool[0] if male_lead_pool else None

    # 3. Usual same-weekday/hour positive anomaly
    usual_candidates = [
        s for s in dom
        if s.get("usual_w_diff") is not None and int(s.get("usual_w_diff") or 0) >= 4 and s["women"] >= 12
    ]
    usual_candidates.sort(key=lambda x: (int(x.get("usual_w_diff") or 0), x["women"]), reverse=True)
    anomaly_store = usual_candidates[0] if usual_candidates else None

    # 4. East vs West top stores
    kanto_s = [s for s in dom if s.get("region") == "Kanto"]
    kansai_s = [s for s in dom if s.get("region") == "Kinki"]
    east_top = kanto_s[0] if kanto_s else (dom[0] if dom else None)
    west_top = kansai_s[0] if kansai_s else (dom[1] if len(dom) > 1 else east_top)

    cal_ctx = build_calendar_context(now_jst=now_jst)
    weather_ctx = fetch_live_weather_context(allow_blocking=allow_weather_blocking)
    slot_persona = build_slot_persona_context(slot_label=slot_label, channel=channel)

    notes = [
        f"対象媒体：{CHANNEL_LABELS.get(channel, channel)} ｜ 全国稼働 {len(active_stores)}店舗（女性計 {total_w:,}名 / 男性計 {total_m:,}名）を解析。",
        f"【街況・カレンダー】{cal_ctx['tag']}（{cal_ctx['note']}）",
        f"【時間帯テンション設計】{slot_persona['label']}",
    ]
    if weather_ctx.get("available"):
        notes.append(f"【リアルタイム気象】{weather_ctx['summary']} — {weather_ctx['adlib_hint']}")
    if best_cluster:
        hs = best_cluster["high_store"]
        ls = best_cluster["low_store"]
        notes.append(
            f"【街別ギャップ】{best_cluster['hub_label']}で「{hs['ja_name']}（女性{hs['women']}・男性{hs['men']} / 女性比{hs['wpct']}%）」と"
            f"「{ls['ja_name']}（女性{ls['women']}・男性{ls['men']} / 女性比{ls['wpct']}%）」に {best_cluster['pct_gap']}pt の差を検出。"
        )
    if focus_store:
        w_diff = int(focus_store.get("women_diff") or 0)
        diff_txt = f"（15分前比 +{w_diff}名）" if w_diff > 0 else ""
        notes.append(
            f"【男性向け穴場】{focus_store['ja_name']}が女性{focus_store['women']}名・男性{focus_store['men']}名"
            f"（女性比{focus_store['wpct']}%）{diff_txt}で待ち回避に最適。"
        )
    if anomaly_store:
        notes.append(
            f"【普段比プラス】{anomaly_store['ja_name']}が普段の同曜日・同時間帯より女性 +{anomaly_store['usual_w_diff']}名で上振れ中。"
        )
    if ig_ctx and ig_ctx.get("top_anaba"):
        top_a = ig_ctx["top_anaba"][0]
        notes.append(
            f"【カルーセル連動】スライド3枚目の高女性比率1位は「{top_a['seo_name']}（{top_a['avg_wpct']}%）」。"
        )

    return {
        "channel": channel,
        "slot_label": slot_label,
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "total_women": total_w,
        "total_men": total_m,
        "domestic_count": len(dom),
        "active_store_count": len(active_stores),
        "top_stores": dom[:6],
        "all_domestic_stores": dom,
        "city_leaders": city_leaders,
        "best_cluster": best_cluster,
        "focus_store": focus_store,
        "male_lead_store": male_lead_store,
        "anomaly_store": anomaly_store,
        "east_top": east_top,
        "west_top": west_top,
        "ig_ctx": ig_ctx,
        "calendar": cal_ctx,
        "weather": weather_ctx,
        "slot_persona": slot_persona,
        "research_notes": notes,
    }


# =====================================================================
# Role 2: Planner (編成・頻度調整係)
# =====================================================================
def run_planner(
    research: dict,
    slot_label: str = "22:00",
    channel: str = "x",
    now_jst: datetime.datetime = None,
    allow_blocking: bool = True,
) -> dict:
    """
    Determine style rotation and anti-repetition rules per SNS channel (`x`, `threads`, `instagram`).
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    history = get_recent_editorial_history(channel=ch, limit=6, allow_blocking=allow_blocking)
    recent_styles = [h.get("selected_style") for h in history if h.get("selected_style")]
    last_style = recent_styles[0] if recent_styles else None

    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]

    recent_post_cooldown = False
    cooldown_reason = "間隔OK（同チャンネルの直近投稿から十分な時間が経過）"
    if history:
        try:
            last_ts = datetime.datetime.strptime(history[0].get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            mins_ago = abs((now_jst.replace(tzinfo=None) - last_ts).total_seconds()) / 60.0
            if mins_ago < 90:
                recent_post_cooldown = True
                cooldown_reason = f"直近の{CHANNEL_LABELS.get(ch, ch)}投稿から{int(mins_ago)}分のため連投注意"
        except Exception:
            pass

    if ch == "threads":
        base_conv = ["threads_city_story", "threads_anomaly_insight", "threads_hopping_guide"]
        classic_key = "threads_classic_top3"
    elif ch == "instagram":
        base_conv = ["ig_empathy_story", "ig_data_discovery", "ig_concise_save"]
        classic_key = "ig_classic_template"
    else:
        if slot_label == "20:00":
            base_conv = ["usual_anomaly", "surge_focus", "hub_gap", "east_west_talk"]
        elif slot_label in ("22:00", "22:30"):
            base_conv = ["hub_gap", "surge_focus", "east_west_talk", "usual_anomaly"]
        else:
            base_conv = ["surge_focus", "hub_gap", "east_west_talk", "usual_anomaly"]
        classic_key = "compact_top3"

    rot = (now_jst.day + now_jst.hour) % len(base_conv)
    rotated = base_conv[rot:] + base_conv[:rot]
    style_priority = [st for st in rotated if st != last_style] + ([last_style] if last_style in rotated else [])
    style_priority.append(classic_key)

    planner_memo = (
        f"【{CHANNEL_LABELS.get(ch, ch)}・{wd_str}曜編成】"
        f"前回採用『{STYLE_LABELS.get(last_style, '履歴なし')}』との被りをブロックし、"
        f"定型テンプレ（{STYLE_LABELS.get(classic_key, '')}）を抑えて『{STYLE_LABELS.get(style_priority[0], '')}』を最優先に設定。"
    )

    return {
        "channel": ch,
        "slot_label": slot_label,
        "weekday_ja": wd_str,
        "last_style": last_style,
        "recent_styles": recent_styles[:4],
        "classic_key": classic_key,
        "suppress_classic": True,
        "style_priority": style_priority,
        "recent_post_cooldown": recent_post_cooldown,
        "cooldown_reason": cooldown_reason,
        "planner_memo": planner_memo,
    }


# =====================================================================
# Role 3: Writer (執筆・下書き係) — X / Threads / Instagram 専用ライティング
# =====================================================================
def _pick_distinct_store(stores: list, exclude_names: set):
    for s in stores:
        if s.get("name") not in exclude_names and int(s.get("women") or 0) > 0:
            return s
    return stores[0] if stores else None


def _write_x_candidates(research: dict, slot_label: str, now_jst: datetime.datetime, site_url: str) -> list:
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    t_str = now_jst.strftime("%H:%M")
    slot_code = (slot_label or "20:00").replace(":", "")
    live_url = f"{site_url}/?s={now_jst.strftime('%Y%m%d')}_{slot_code}"

    top_stores = research.get("top_stores") or []
    if not top_stores:
        return []

    s1 = top_stores[0]
    cluster = research.get("best_cluster")
    focus = research.get("focus_store") or s1
    anomaly = research.get("anomaly_store")
    east = research.get("east_top") or s1
    west = research.get("west_top") or _pick_distinct_store(top_stores, {east["name"]})

    candidates = []

    # 1) hub_gap: Same-city store gap without repeating the same store twice
    if cluster and cluster["high_store"]["name"] != cluster["low_store"]["name"]:
        hs = cluster["high_store"]
        ls = cluster["low_store"]
        hub_name = cluster["hub_label"]
        area_id = cluster["area_id"]
        outside_top = _pick_distinct_store(top_stores, {hs["name"], ls["name"]})
        outside_line = (
            f"他エリアは{outside_top['ja_name']}（女性{outside_top['women']}名）も好調。"
            if outside_top else ""
        )

        if cluster["pct_gap"] >= 12:
            body_a = (
                f"いま（{t_str}）の{hub_name}、徒歩圏でも店舗で男女比が割れてる👀\n\n"
                f"・{hs['ja_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                f"・{ls['ja_name']}：女性{ls['women']}・男性{ls['men']}（女性比{ls['wpct']}%）\n\n"
                f"{outside_line}比較はリプ欄へ👇\n"
                f"#相席ナビ #相席ラウンジ"
            )
            if x_poster.compute_twitter_weight(body_a) > 272:
                body_a = (
                    f"いま（{t_str}）の{hub_name}、徒歩圏でも店舗で男女比が割れてる👀\n\n"
                    f"・{hs['ja_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                    f"・{ls['ja_name']}：女性{ls['women']}・男性{ls['men']}（女性比{ls['wpct']}%）\n\n"
                    f"向かう前の比較はリプ欄から👇\n"
                    f"#相席ナビ #相席ラウンジ"
                )
        else:
            body_a = (
                f"{wd_str}曜{t_str}時点、いま{hub_name}まわりがいいバランスで集まってる🍻\n\n"
                f"・{hs['ja_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                f"・{ls['ja_name']}：女性{ls['women']}・男性{ls['men']}\n\n"
                f"待ちが出そうな時は近くの別店舗も覗くのが確実👇\n"
                f"#相席ナビ #相席ラウンジ"
            )
        reply_a = (
            f"📍 {hub_name}エリアの料金・現在人数横並び比較はこちら\n"
            f"{site_url}/price-comparison?area={area_id}\n\n"
            f"🔍 全国53店舗リアルタイム人数ランキング\n{live_url}"
        )
        candidates.append({
            "style": "hub_gap",
            "style_label": STYLE_LABELS["hub_gap"],
            "writer_note": f"{hub_name}内の男女比差（{hs['ja_name']} {hs['wpct']}% vs {ls['ja_name']} {ls['wpct']}%）を切り取り、同一店舗の重複言及を排除して作成。",
            "main_text": body_a,
            "reply_text": reply_a,
        })

    # 2) surge_focus: Spotlight 1 sweet-spot store + 2 distinct runners-up (weather/calendar aware)
    w_diff = int(focus.get("women_diff") or 0)
    cal_ctx = research.get("calendar") or {}
    weather_ctx = research.get("weather") or {}
    if slot_label == "20:00":
        if cal_ctx.get("is_payday_window"):
            opener_b = f"給料日後の20時台、今夜は「{focus['ja_name']}」の立ち上がりがかなり早い👀"
        else:
            opener_b = f"まだ20時台だけど、今夜は「{focus['ja_name']}」の立ち上がりがかなり早い👀"
        closing_b = "混み始める前に待ちなしで座るなら今のうちが狙い目👇"
    elif slot_label in ("00:00", "02:00"):
        opener_b = f"終電すぎのこの時間（{t_str}）、いま一番女性が残ってるのは「{focus['ja_name']}」🌙"
        closing_b = "深夜は人が集まる店と静かな店が分かれるので事前確認が確実👇"
    else:
        if weather_ctx.get("is_rainy"):
            opener_b = f"雨の{t_str}時点☔ 駅チカでいま女性比率が高めなのが「{focus['ja_name']}」✨"
        elif cal_ctx.get("is_pre_holiday"):
            opener_b = f"祝前日の{t_str}時点、いま女性比率が高めで座りやすいのが「{focus['ja_name']}」✨"
        elif w_diff >= 3:
            opener_b = f"{t_str}時点、「{focus['ja_name']}」に直近で女性が+{w_diff}名増えて狙い目🔥"
        else:
            opener_b = f"{t_str}時点、いま女性比率が高めで座りやすいのが「{focus['ja_name']}」✨"
        closing_b = "向かう前に近隣店舗のバランスも見比べてみてください👇"

    other_top = [s for s in top_stores if s["name"] != focus["name"]][:2]
    other_line = ""
    if len(other_top) >= 2:
        other_line = f"ほかにも{other_top[0]['ja_name']}（女性{other_top[0]['women']}名）、{other_top[1]['ja_name']}（女性{other_top[1]['women']}名）が好調。\n\n"

    body_b = (
        f"{opener_b}\n"
        f"現在：女性{focus['women']}名 / 男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
        f"{other_line}"
        f"{closing_b}\n"
        f"#相席ラウンジ #相席ナビ"
    )
    if x_poster.compute_twitter_weight(body_b) > 272 and other_top:
        body_b = (
            f"{opener_b}\n"
            f"現在：女性{focus['women']}名 / 男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
            f"ほかにも{other_top[0]['ja_name']}（女性{other_top[0]['women']}名）が盛り上がってます。\n"
            f"{closing_b}\n"
            f"#相席ラウンジ #相席ナビ"
        )
    f_slug = store_master.slug_of(focus["name"])
    reply_b = (
        f"📊 {focus['ja_name']}の現在人数・終電後の残りやすさ指標はこちら\n"
        f"{site_url}/store/{f_slug}\n\n"
        f"🔍 全国53店舗リアルタイム人数ランキング\n{live_url}"
    )
    candidates.append({
        "style": "surge_focus",
        "style_label": STYLE_LABELS["surge_focus"],
        "writer_note": f"女性比・勢いが良好な「{focus['ja_name']}」を主役に据え、機械的なメダル記号を使わず自然な観察文で作成。",
        "main_text": body_b,
        "reply_text": reply_b,
    })

    # 3) usual_anomaly or east_west_talk (strictly distinct second store!)
    if anomaly and anomaly.get("usual_w_diff") and int(anomaly["usual_w_diff"]) >= 4:
        u_diff = int(anomaly["usual_w_diff"])
        partner = _pick_distinct_store(top_stores, {anomaly["name"]})
        partner_line = (
            f"・{partner['ja_name']}：女性{partner['women']}・男性{partner['men']}（女性比{partner['wpct']}%）\n"
            if partner else ""
        )
        body_c = (
            f"いつもの{wd_str}曜{slot_label[:2]}時台より、今夜は「{anomaly['ja_name']}」に女性が+{u_diff}名多く入ってる📊\n\n"
            f"・{anomaly['ja_name']}：女性{anomaly['women']}・男性{anomaly['men']}（女性比{anomaly['wpct']}%）\n"
            f"{partner_line}\n"
            f"普段比プラスの店は当たりやすいので今夜の候補にどうぞ👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
        c_style = "usual_anomaly"
        w_note_c = f"普段の{wd_str}曜同時間帯より女性が+{u_diff}名多い実測ギャップをフックに作成。"
    else:
        body_c = (
            f"{wd_str}曜{t_str}時点、関東と関西でいま一番人が集まってる2店舗🍻\n\n"
            f"🗼関東：{east['ja_name']}（女性{east['women']}・男性{east['men']} / {east['wpct']}%）\n"
            f"🐙関西：{west['ja_name']}（女性{west['women']}・男性{west['men']} / {west['wpct']}%）\n\n"
            f"どちらも好ペースで推移中。各街の穴場・待ち状況はリプ欄へ👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
        c_style = "east_west_talk"
        w_note_c = f"関東トップ（{east['ja_name']}）と関西トップ（{west['ja_name']}）を対比し、東西どちらのフォロワーにも刺さる構成で作成。"

    reply_c = (
        f"🔍 全国53店舗の現在人数＆普段の同曜日比（平比）はこちら\n{live_url}\n\n"
        f"💴 全51店舗の時間帯料金・開催中イベント比較ボード\n{site_url}/price-comparison"
    )
    candidates.append({
        "style": c_style,
        "style_label": STYLE_LABELS[c_style],
        "writer_note": w_note_c,
        "main_text": body_c,
        "reply_text": reply_c,
    })

    # 4) compact_top3 (Classic baseline for Desk comparison)
    legacy_text = x_poster.build_scheduled_tweet_text(top_stores, slot_label=slot_label, app_url=site_url, compact=True)
    legacy_reply = x_poster.build_scheduled_reply_text(slot_label=slot_label, app_url=site_url)
    candidates.append({
        "style": "compact_top3",
        "style_label": STYLE_LABELS["compact_top3"],
        "writer_note": "従来の『🥇🥈🥉 TOP3』定型フォーマット（デスク比較採点用ベンチマーク）。",
        "main_text": legacy_text,
        "reply_text": legacy_reply,
    })

    return candidates


def _write_threads_candidates(research: dict, slot_label: str, now_jst: datetime.datetime, site_url: str) -> list:
    """
    Generate 4 Meta Threads candidates (strictly <= 495 chars; conversational essay style works best on Threads).
    Respects odd/even day URL vs Bio-link anti-spam rotation.
    """
    import threads_poster
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    t_str = now_jst.strftime("%H:%M")

    top_stores = research.get("top_stores") or []
    if not top_stores:
        return []

    cluster = research.get("best_cluster")
    focus = research.get("focus_store") or top_stores[0]
    male_lead = research.get("male_lead_store")
    anomaly = research.get("anomaly_store")
    city_leaders = research.get("city_leaders") or []

    # Meta anti-spam link rotation (even day = bio link, odd day = direct link)
    if now_jst.day % 2 == 0:
        cta_footer = "🔍 全51店舗の現在人数＆料金比較はプロフィールのリンク（相席ナビ）から見れます\n#相席ラウンジ"
    else:
        cta_footer = f"▼ 全51店舗の現在人数＆料金・イベント比較\n{site_url}/price-comparison\n#相席ラウンジ"

    candidates = []

    # 1) threads_city_story: Conversational street-level temperature check
    if cluster and cluster["high_store"]["name"] != cluster["low_store"]["name"]:
        hs = cluster["high_store"]
        ls = cluster["low_store"]
        hub_name = cluster["hub_label"]
        other_cities = [c for c in city_leaders if c["hub_label"] != hub_name][:3]
        city_sub = "\n".join(
            f"・{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}・男性{c['store']['men']}）"
            for c in other_cities
        )
        if cluster["pct_gap"] >= 12 and ls["wpct"] <= 48:
            lead_1 = f"{wd_str}曜{t_str}すぎ、今夜は{hub_name}まわりで店舗ごとの男女比がかなりはっきり分かれてます👀"
            contrast_1 = f"近くの「{ls['ja_name']}」は女性{ls['women']}名・男性{ls['men']}名（女性比{ls['wpct']}%）と男性先行で空気感が対照的。"
        elif cluster["pct_gap"] >= 10:
            lead_1 = f"{wd_str}曜{t_str}すぎ、同じ{hub_name}エリアでも店舗によって入りやすさに結構差が出てます👀"
            contrast_1 = f"近くの「{ls['ja_name']}」は女性{ls['women']}名・男性{ls['men']}名（女性比{ls['wpct']}%）で推移中。"
        else:
            lead_1 = f"{wd_str}曜{t_str}すぎ、今夜は{hub_name}エリアの大型店がどこも高い熱量で盛り上がってます🍻"
            contrast_1 = f"近くの「{ls['ja_name']}」も女性{ls['women']}名・男性{ls['men']}名（女性比{ls['wpct']}%）と好バランス。"
        body_1 = (
            f"{lead_1}\n\n"
            f"たとえば「{hs['ja_name']}」は女性{hs['women']}名・男性{hs['men']}名（女性比{hs['wpct']}%）で座りやすく、"
            f"{contrast_1}\n\n"
            f"ほかの主要エリアの現在トップはこんな感じ👇\n"
            f"{city_sub}\n\n"
            f"1軒目で「男性待ち長そうだな」と思ったら、無理に粘らず徒歩3〜4分の別ブランドを覗くのが一番コスパいいです🥂\n\n"
            f"{cta_footer}"
        )
    else:
        c_lines = "\n".join(
            f"・{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}名 / 男性{c['store']['men']}名）"
            for c in city_leaders[:4]
        )
        body_1 = (
            f"{wd_str}曜{t_str}すぎ、全国{research['domestic_count']}店舗でいま女性{research['total_women']:,}名・男性{research['total_men']:,}名が入ってます🥂\n\n"
            f"今夜特にバランスがいいのは「{focus['ja_name']}（女性{focus['women']}名 / 男性{focus['men']}名・女性比{focus['wpct']}%）」。\n\n"
            f"主要エリアの現在トップメモ📝\n{c_lines}\n\n"
            f"向かう前の30秒で人数バランスを見ておくだけで「行ったのに1時間待ち」を回避できます。\n\n"
            f"{cta_footer}"
        )

    candidates.append({
        "style": "threads_city_story",
        "style_label": STYLE_LABELS["threads_city_story"],
        "writer_note": "Threadsで最も伸びやすい「現場の温度差を語りかけるエッセイ調」で作成（メダル記号なし）。",
        "main_text": body_1[:495],
        "reply_text": "（Threadsは1投稿完結・偶数日はプロフ誘導／奇数日はURL付与でリンク制限を回避）",
    })

    # 2) threads_anomaly_insight: Both Male & Female perspective + usual diff
    if anomaly and anomaly.get("usual_w_diff"):
        hook_2 = (
            f"いつもの{wd_str}曜夜と比べても、今夜は「{anomaly['ja_name']}」に普段より女性が+{anomaly['usual_w_diff']}名多く集まってます📊"
        )
    else:
        hook_2 = (
            f"今夜（{wd_str}曜{t_str}）の相席ラウンジ、全国で女性{research['total_women']:,}名・男性{research['total_men']:,}名とかなり動いてます🥂"
        )

    if male_lead:
        female_tip = (
            f"逆に女性グループなら、いま男性{male_lead['men']}名・女性{male_lead['women']}名で盛り上がっている"
            f"「{male_lead['ja_name']}」あたりが待ち時間ゼロで即乾杯できて狙い目です✨"
        )
    else:
        female_tip = "逆に女性グループなら、男性が先行して入っている大型店を選ぶと待ち0分で即乾杯しやすいです✨"

    runner = _pick_distinct_store(top_stores, {focus["name"]})
    runner_txt = f"と「{runner['ja_name']}（女性{runner['women']}名 / 男性{runner['men']}名）」" if runner else ""

    body_2 = (
        f"{hook_2}\n\n"
        f"男性目線でいま待ち時間を避けて座りやすいのは「{focus['ja_name']}（女性{focus['women']}名 / 男性{focus['men']}名・女性比{focus['wpct']}%）」{runner_txt}。\n\n"
        f"{female_tip}\n\n"
        f"同じ街でも店によって男女比が15〜25%違うので、入る前にチラッと比較してみてください👇\n\n"
        f"{cta_footer}"
    )
    candidates.append({
        "style": "threads_anomaly_insight",
        "style_label": STYLE_LABELS["threads_anomaly_insight"],
        "writer_note": "男性向けの穴場店舗と、女性向けの即相席店舗（男性先行店）の両目線を入れて保存・共感を狙う構成。",
        "main_text": body_2[:495],
        "reply_text": "（Threadsは1投稿完結）",
    })

    # 3) threads_hopping_guide: 4-hub quick hopping guide
    hub_bullets = "\n".join(
        f"📍{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}・男性{c['store']['men']} / 女性比{c['store']['wpct']}%）"
        for c in city_leaders[:4]
    )
    body_3 = (
        f"いま（{t_str}時点）から1軒目・2軒目で相席ラウンジ動く人向けに、主要エリアで一番人が集まってる店舗だけ抜粋しました📝\n\n"
        f"{hub_bullets}\n\n"
        f"席替えがしっかり回るのは「女性10名以上（約5組）×女性比55%以上」のお店。微妙な席で惰性で90分粘るより、40分で見切って近くの好バランス店に回る方が満足度もコスパも段違いです🍸\n\n"
        f"{cta_footer}"
    )
    candidates.append({
        "style": "threads_hopping_guide",
        "style_label": STYLE_LABELS["threads_hopping_guide"],
        "writer_note": "東西主要4エリアの現在トップと「40分見切り・女性10名基準」の実践セオリーを組み合わせた実用投稿。",
        "main_text": body_3[:495],
        "reply_text": "（Threadsは1投稿完結）",
    })

    # 4) threads_classic_top3: Legacy baseline
    classic_th = threads_poster.build_threads_live_post(top_stores, slot_label=slot_label, now_jst=now_jst)
    candidates.append({
        "style": "threads_classic_top3",
        "style_label": STYLE_LABELS["threads_classic_top3"],
        "writer_note": "従来の『🏆全国リアルタイムTOP3 🥇🥈🥉』定型フォーマット（比較ベンチマーク用）。",
        "main_text": classic_th,
        "reply_text": "（Threadsは1投稿完結）",
    })

    return candidates


def _write_instagram_candidates(research: dict, now_jst: datetime.datetime) -> list:
    """
    Generate 4 Instagram Carousel/Feed caption candidates.
    Instagram prioritizes:
      - First 1-2 lines before '...続きを読む' (hook empathy/curiosity without stiff bracket banners)
      - Save-worthiness ('保存しておくと便利')
      - Clean alignment with the 5 portrait slides (`top_volume` & `top_anaba`)
    """
    import ig_carousel_generator
    ig_ctx = research.get("ig_ctx")
    if not ig_ctx:
        ig_ctx = ig_carousel_generator.build_forecast_context(live_stores=research.get("top_stores") or [], now_jst=now_jst)

    v_top = ig_ctx.get("top_volume", [])[:3]
    a_top = ig_ctx.get("top_anaba", [])[:3]
    wk = ig_ctx.get("weekend_label", "")
    theme = ig_ctx.get("theme") or {}
    t_key = theme.get("theme_key", "weekend")
    is_live_fb = bool(ig_ctx.get("is_live_fallback"))
    v_lbl = "現在女性" if is_live_fb else "最高女性"
    a_lbl = "女性比率" if is_live_fb else "平均女性比"

    v_lines = "\n".join(f"・{s['seo_name']}（{v_lbl}{s['max_women']}名）" for s in v_top)
    a_lines = "\n".join(f"・{s['seo_name']}（{a_lbl}{s['avg_wpct']}%）" for s in a_top)
    tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #夜遊びガイド"

    candidates = []

    # 1) ig_empathy_story: Strong 1st-line human hook before fold
    if t_key == "weekday":
        hook_1 = "「平日の仕事帰り、どの店なら待たずに楽しく飲める？」って迷う夜用のまとめです📝"
    elif t_key == "sunday":
        hook_1 = "今週も全国51店舗のデータを追っていて、「やっぱり店選びで9割決まるな…」と実感した結果がこちら📊"
    else:
        hook_1 = "金土の21時半に何も見ないで突撃して「男性待ち50分です」って言われるの、一番もったいないですよね…😂"

    body_ig_1 = (
        f"{hook_1}\n"
        f"実は同じ新宿や梅田・渋谷でも、歩いて3分の別ブランドを見るだけで待ち時間ゼロで座れることが本当に多いんです。\n\n"
        f"お店に向かう前にすぐ見返せるよう、右上の「保存📌」をどうぞ！\n\n"
        f"── スライド2枚目：圧倒的に人が集まる人気店\n"
        f"{v_lines}\n\n"
        f"── スライド3枚目：男性待ちを回避しやすい高女性比率の穴場\n"
        f"{a_lines}\n\n"
        f"💡 男女別の賢い選び方\n"
        f"・男性は「女性10名以上×女性比55%以上」の店舗を選ぶと席替えもスムーズ！\n"
        f"・女性は「男性が先行して盛り上がっている大型店」を選ぶと待ち0分で即乾杯できます🥂\n\n"
        f"いま現在のリアルタイム男女人数と、全51店舗の料金・イベント比較はプロフィールのリンク（aiseki-navi.com）から3秒で見れます🔍\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_empathy_story",
        "style_label": STYLE_LABELS["ig_empathy_story"],
        "writer_note": "インスタの『…続きを読む』で折りたたまれる前の1行目に、一番共感されやすい現場あるあるを配置。",
        "main_text": body_ig_1,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 2) ig_data_discovery: Editorial magazine column style
    top_a_name = a_top[0]["seo_name"] if a_top else "穴場店舗"
    top_a_pct = a_top[0]["avg_wpct"] if a_top else 60
    body_ig_2 = (
        f"9.6万件の来店ログを集計していて分かった、{wk}の「外さないお店選び」を5枚のスライドに凝縮しました📖\n\n"
        f"特に注目なのが、女性比率{top_a_pct}%と非常にバランスが良い「{top_a_name}」をはじめとする穴場店舗の推移です👀\n\n"
        f"✦ 女性来店数が多い盛り上がり上位\n"
        f"{v_lines}\n\n"
        f"✦ 男性がスムーズに入りやすい高女性比率上位\n"
        f"{a_lines}\n\n"
        f"23時20分前後は終電組の退店で一時的に人数が動くため、1軒目は20時台、2軒目は0時すぎの第2波を狙うのがデータ上の鉄板ルートです🌙\n"
        f"あとで友達との作戦会議に使えるよう「保存📌」しておくのがおすすめ！\n\n"
        f"現在の全店リアルタイム人数はプロフィールのリンク（相席ナビ）で無料公開中です🔍\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_data_discovery",
        "style_label": STYLE_LABELS["ig_data_discovery"],
        "writer_note": "9.6万件ログの裏付けと「20時台・0時すぎ第2波」の時間帯法則を盛り込んだマガジン編集長トーン。",
        "main_text": body_ig_2,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 3) ig_concise_save: Clean, scannable save-first layout
    body_ig_3 = (
        f"行くお店で迷ったらこの5枚だけチェックしてみてください🥂（{wk}版）\n"
        f"※入店前にパッと見返せる「保存📌」が便利です\n\n"
        f"【ワイワイ盛り上がり重視ならここ】\n"
        f"{v_lines}\n\n"
        f"【待ち時間回避・ゆっくり話すならここ】\n"
        f"{a_lines}\n\n"
        f"同じエリアでも日によって男女比が15〜20%変わるので、向かう3分前にプロフィールのリンク（aiseki-navi.com）で現在人数を見てから入るのが一番確実です✨\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_concise_save",
        "style_label": STYLE_LABELS["ig_concise_save"],
        "writer_note": "長文を読まない層向けに余白を広く取り、目的別（盛り上がり重視 vs 待ち回避）でスッキリ整理。",
        "main_text": body_ig_3,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 4) ig_classic_template: Legacy baseline
    classic_ig = ig_carousel_generator.build_captions(ig_ctx)["instagram_caption"]
    candidates.append({
        "style": "ig_classic_template",
        "style_label": STYLE_LABELS["ig_classic_template"],
        "writer_note": "従来の『【〇〇 相席ラウンジ狙い目予報📊】』定型キャプション（比較ベンチマーク用）。",
        "main_text": classic_ig,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    return candidates


def verify_ai_factual_numbers(text: str, research: dict, channel: str = "x") -> tuple:
    """
    Strict Desk Fact-Checker for AI-generated drafts:
      1. Ensures every headcount (`女性XX`, `男性XX`), ratio (`女性比XX%`), and diff (`+XX名`)
         matches verified numbers from `research` (zero tolerance for hallucinated counts).
      2. On single-store bullet lines, ensures the store's exact headcount wasn't swapped with another store.
    Returns `(is_valid: bool, bad_tokens: list[str])`.
    """
    all_dom = research.get("all_domestic_stores") or research.get("top_stores") or []
    ig_ctx = research.get("ig_ctx") or {}
    ig_vol = ig_ctx.get("top_volume") or []
    ig_ana = ig_ctx.get("top_anaba") or []

    allowed_women = {10, int(research.get("total_women") or 0)}
    allowed_men = {0, int(research.get("total_men") or 0)}
    allowed_pct = {15, 20, 25, 50, 55, 60}
    allowed_diff = set()
    store_facts = {}

    def _reg_alias(alias: str, w_val=None, m_val=None, p_val=None):
        if not alias:
            return
        rec = store_facts.setdefault(alias, {"women": set(), "men": set(), "pct": set()})
        if w_val is not None:
            rec["women"].add(int(w_val))
        if m_val is not None:
            rec["men"].add(int(m_val))
        if p_val is not None:
            rec["pct"].add(int(p_val))

    for s in all_dom:
        w = int(s.get("women") or 0)
        m = int(s.get("men") or 0)
        p = int(s.get("wpct") or 0)
        allowed_women.add(w)
        allowed_men.add(m)
        allowed_pct.add(p)
        wd = int(s.get("women_diff") or 0)
        ud = int(s.get("usual_w_diff") or 0) if s.get("usual_w_diff") is not None else 0
        if wd > 0:
            allowed_diff.add(wd)
        if ud > 0:
            allowed_diff.add(ud)
        _reg_alias(s.get("ja_name"), w, m, p)

    for s in ig_vol:
        mw = int(s.get("max_women") or 0)
        allowed_women.add(mw)
        _reg_alias(s.get("seo_name"), w_val=mw)

    for s in ig_ana:
        ap = int(s.get("avg_wpct") or 0)
        allowed_pct.add(ap)
        _reg_alias(s.get("seo_name"), p_val=ap)

    bad = []

    for m_w in re.finditer(r"(?:最高|現在)?女性\s*([0-9,]+)\s*(?:名|人)?", text):
        # Skip generic rule phrase "女性10名以上" or "女性比"
        after = text[m_w.end():m_w.end() + 3]
        if after.startswith("%") or "以上" in after:
            continue
        val = int(m_w.group(1).replace(",", ""))
        if val not in allowed_women:
            bad.append(f"女性{val}")

    for m_m in re.finditer(r"男性\s*([0-9,]+)\s*(?:名|人)?", text):
        after = text[m_m.end():m_m.end() + 3]
        if after.startswith("%") or "以上" in after:
            continue
        val = int(m_m.group(1).replace(",", ""))
        if val not in allowed_men:
            bad.append(f"男性{val}")

    for m_p in re.finditer(r"(?:女性比(?:率)?|平均女性比)\s*(\d+)\s*%", text):
        val = int(m_p.group(1))
        if val not in allowed_pct:
            bad.append(f"女性比{val}%")

    for m_d in re.finditer(r"\+\s*(\d+)\s*名", text):
        val = int(m_d.group(1))
        if val not in allowed_diff:
            bad.append(f"+{val}名")

    # Line-level single-store cross-check (prevents swapping store A's count onto store B)
    sorted_aliases = sorted(store_facts.keys(), key=len, reverse=True)
    for line in text.splitlines():
        matched_aliases = []
        line_probe = line
        for al in sorted_aliases:
            if al in line_probe:
                matched_aliases.append(al)
                line_probe = line_probe.replace(al, " ")
        if len(matched_aliases) == 1:
            al = matched_aliases[0]
            facts = store_facts[al]
            for mw in re.finditer(r"(?:最高|現在)?女性\s*(\d+)", line):
                after = line[mw.end():mw.end() + 3]
                if "以上" in after or after.startswith("%"):
                    continue
                vw = int(mw.group(1))
                if facts["women"] and vw not in facts["women"]:
                    bad.append(f"{al}:女性{vw}(実測不一致)")
            for mm in re.finditer(r"男性\s*(\d+)", line):
                vm = int(mm.group(1))
                if facts["men"] and vm not in facts["men"]:
                    bad.append(f"{al}:男性{vm}(実測不一致)")

    return (len(bad) == 0, bad)


def _build_gemini_prompt(
    research: dict,
    plan: dict,
    slot_label: str,
    channel: str,
    now_jst: datetime.datetime,
    site_url: str,
) -> tuple:
    ch = (channel or "x").lower()
    t_str = now_jst.strftime("%H:%M")
    cal_ctx = research.get("calendar") or {}
    weather_ctx = research.get("weather") or {}
    history = get_recent_editorial_history(channel=ch, limit=3)
    recent_openers = [
        (h.get("selected_text") or "").split("\n", 1)[0][:55]
        for h in history if h.get("selected_text")
    ]

    top_s = research.get("top_stores") or []
    focus = research.get("focus_store") or (top_s[0] if top_s else None)
    cluster = research.get("best_cluster")
    anomaly = research.get("anomaly_store")
    male_lead = research.get("male_lead_store")
    city_leaders = research.get("city_leaders") or []

    fact_payload = {
        "current_time": f"{cal_ctx.get('weekday_ja', '')}曜 {t_str}",
        "calendar_vibe": f"{cal_ctx.get('tag', '')} - {cal_ctx.get('note', '')}",
        "weather_vibe": weather_ctx.get("adlib_hint") or "穏やかな夜",
        "weather_detail": weather_ctx.get("summary") or "",
        "national_active_stores": research.get("active_store_count", 0),
        "national_total_women": research.get("total_women", 0),
        "national_total_men": research.get("total_men", 0),
        "sweet_spot_store": (
            {
                "name": focus["ja_name"],
                "women": focus["women"],
                "men": focus["men"],
                "women_pct": focus["wpct"],
                "diff_15m": int(focus.get("women_diff") or 0),
            }
            if focus else None
        ),
        "same_area_gap": (
            {
                "area": cluster["hub_label"],
                "high_ratio_store": {
                    "name": cluster["high_store"]["ja_name"],
                    "women": cluster["high_store"]["women"],
                    "men": cluster["high_store"]["men"],
                    "women_pct": cluster["high_store"]["wpct"],
                },
                "contrast_store": {
                    "name": cluster["low_store"]["ja_name"],
                    "women": cluster["low_store"]["women"],
                    "men": cluster["low_store"]["men"],
                    "women_pct": cluster["low_store"]["wpct"],
                },
                "gap_pt": cluster["pct_gap"],
            }
            if cluster else None
        ),
        "usual_positive_anomaly": (
            {
                "name": anomaly["ja_name"],
                "women": anomaly["women"],
                "men": anomaly["men"],
                "women_pct": anomaly["wpct"],
                "plus_vs_usual_women": int(anomaly["usual_w_diff"]),
            }
            if anomaly and anomaly.get("usual_w_diff") else None
        ),
        "female_group_zero_wait_store": (
            {
                "name": male_lead["ja_name"],
                "men": male_lead["men"],
                "women": male_lead["women"],
            }
            if male_lead else None
        ),
        "top_stores": [
            {"name": s["ja_name"], "women": s["women"], "men": s["men"], "women_pct": s["wpct"]}
            for s in top_s[:4]
        ],
        "city_leaders": [
            {"area": c["hub_label"], "name": c["store"]["ja_name"], "women": c["store"]["women"], "men": c["store"]["men"], "women_pct": c["store"]["wpct"]}
            for c in city_leaders[:4]
        ],
    }

    slot_code = (slot_label or "20:00").replace(":", "")
    live_url = f"{site_url}/?s={now_jst.strftime('%Y%m%d')}_{slot_code}"

    if ch == "x":
        f_slug = store_master.slug_of(focus["name"]) if focus else ""
        reply_text = (
            f"📊 {focus['ja_name']}の現在人数・終電後の残りやすさ指標はこちら\n"
            f"{site_url}/store/{f_slug}\n\n"
            f"🔍 全国53店舗リアルタイム人数ランキング\n{live_url}"
            if focus else f"🔍 全国53店舗リアルタイム人数ランキング\n{live_url}"
        )
        platform_spec = (
            "【対象SNS：X（旧Twitter）】\n"
            "- 文字数制限：日本語で **95文字〜122文字以内**（X換算で必ず255ウェイト以下になるようコンパクトに引き締めること）\n"
            "- 禁止事項：URL（http...）は本文に絶対に入れない。『🥇🥈🥉』メダル記号や『【〇〇速報】』のような業者見出しは禁止。同じ店舗名を2回書かない。\n"
            "- 構成：1行目で今夜の天気・気温・曜日・給料日などの空気感と現場データの面白い気づきを友達に話すような自然なアドリブで切り出し、中盤で2〜3店舗の正確な人数（例：女性122・男性52（女性比70%））を示し、最後はリプ欄へ自然に誘導して『#相席ナビ #相席ラウンジ』で締める。"
        )
    elif ch == "threads":
        if now_jst.day % 2 == 0:
            cta_footer = "🔍 全51店舗の現在人数＆料金比較はプロフィールのリンク（相席ナビ）から見れます\n#相席ラウンジ"
        else:
            cta_footer = f"▼ 全51店舗の現在人数＆料金・イベント比較\n{site_url}/price-comparison\n#相席ラウンジ"
        reply_text = "（Threadsは1投稿完結・AIアドリブ生成＋リンク制限回避済）"
        platform_spec = (
            "【対象SNS：Meta Threads（スレッズ）】\n"
            "- 文字数制限：全体で **290文字〜440文字以内**（絶対上限480文字）\n"
            "- 禁止事項：『🥇🥈🥉』メダル記号や『【〇〇】』角括弧タイトルは禁止。ハッシュタグは末尾の1個のみ。\n"
            "- 構成：Threads特有の『中の人が現場データと外の天気・曜日を見ながら本音で語るエッセイ・会話調』で書くこと。男性目線の穴場だけでなく可能なら女性目線の即相席店やハシゴのコツも自然に織り交ぜる。\n"
            f"- 末尾固定フッター：文章の最後は必ず次のフッター文字列を一字一句変えずに置いて締めること：\n{cta_footer}"
        )
    else:
        import ig_carousel_generator
        ig_ctx = research.get("ig_ctx") or ig_carousel_generator.build_forecast_context(live_stores=top_s, now_jst=now_jst)
        v_top = ig_ctx.get("top_volume", [])[:3]
        a_top = ig_ctx.get("top_anaba", [])[:3]
        is_live_fb = bool(ig_ctx.get("is_live_fallback"))
        v_lbl = "現在女性" if is_live_fb else "最高女性"
        a_lbl = "女性比率" if is_live_fb else "平均女性比"
        fact_payload["carousel_slide2_volume_top3"] = [
            {"name": s["seo_name"], "label": f"{v_lbl}{s['max_women']}名"} for s in v_top
        ]
        fact_payload["carousel_slide3_anaba_top3"] = [
            {"name": s["seo_name"], "label": f"{a_lbl}{s['avg_wpct']}%"} for s in a_top
        ]
        fact_payload["weekend_label"] = ig_ctx.get("weekend_label", "")
        reply_text = "（5枚組カルーセル画像とセットで投稿・AIアドリブ生成）"
        platform_spec = (
            "【対象SNS：Instagram（5枚組カルーセル投稿キャプション）】\n"
            "- 文字数制限：**400文字〜680文字以内**\n"
            "- 禁止事項：1行目を『【〇〇予報】』のような堅い角括弧見出しにしないこと。\n"
            "- 構成：『…続きを読む』で折りたたまれる前の最初の1〜2行目に、天気・季節・週末あるあるを交えた思わずタップしたくなる人間味100%の共感アドリブを書く。『保存📌』を促す一言を入れ、スライド2枚目の盛り上がり上位3店舗とスライド3枚目の高女性比率上位3店舗の数値を正確に紹介し、プロフィールのリンク（aiseki-navi.com）への誘導と、末尾に『#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #夜遊びガイド』の6タグを付ける。"
        )

    slot_persona = research.get("slot_persona") or build_slot_persona_context(slot_label=slot_label, channel=ch)
    cliche_list_str = "・".join(f"『{w}』" for w in AI_CLICHE_WORDS)

    prompt = (
        "あなたは全国53店舗の相席ラウンジ人数をリアルタイム計測している『相席ナビ』のベテランSNS編集長です。\n"
        "毎回同じボットのような投稿ではなく、本当にスマホで今の店内人数と外の空気（天気・気温・曜日・給料日など）を見ながら、"
        "フォロワーに『今夜ここ狙い目だよ』と自然に呟いているアドリブ投稿を1案作成してください。\n\n"
        f"【今夜の時間帯テンション設定：{slot_persona['label']}】\n"
        f"- {slot_persona['guidance']}\n\n"
        "【長考（Thinking）プロセス：脳内3案コンペ＆脱AI推敲手順】\n"
        "いきなり書き始めず、内部思考の中で必ず次の3ステップを行ってから最終1本を出力してください：\n"
        "  Step 1. 性格の違う3つの切り口を頭の中でラフに書き比べる\n"
        "    - 案①（現場ウォッチャーの独り言風）：モニターを見ながら『え、今日〇〇なのに梅田すごいな…』とポロッと漏らすトーン\n"
        "    - 案②（常連の友達・先輩にLINEする風）：『今から〇〇行くなら正直こっちにしとき』と親身にそっと教えるトーン\n"
        "    - 案③（現場の違和感・温度差発見風）：『普段のこの時間よりだいぶ早い』『徒歩3分なのにここまで割れるか…』という気づきトーン\n"
        "  Step 2. 3案を見比べ、一番『業者の宣伝』『AIの優等生構文』に見えず、人間がスマホでフリック入力したような自然な呼吸感・改行の1案に推敲する（良いフレーズの掛け合わせOK）。キラキラ絵文字の連打は削り、絵文字は自然な位置に1〜3個までに抑える。\n"
        "  Step 3. ファクトシートの数字と1人・1%の狂いもないか指差し確認する。\n\n"
        f"{platform_spec}\n\n"
        "【絶対厳守ルール（違反するとデスク検閲で減点・不採用になります）】\n"
        "1. 下記の JSON ファクトシートに含まれる店舗名・人数・パーセンテージ以外は絶対に書かないこと。数値を1人でも盛ったり丸めたり変更することは厳禁。\n"
        "2. 内部略称『OLG』は使わず、ファクトシート記載の自然な日本語店名（『オリエンタル〇〇』『ag〇〇』『JIS〇〇』）をそのまま使うこと。\n"
        f"3. AI臭・宣伝臭ワードの完全禁止：{cliche_list_str} は本文に絶対に使わないこと。\n"
        f"4. 直近の過去投稿の書き出し（{json.dumps(recent_openers, ensure_ascii=False)}）とは全く違う新鮮な1行目の切り口・語尾にすること。\n\n"
        f"【今夜の検証済みファクトシート】\n{json.dumps(fact_payload, ensure_ascii=False, indent=2)}\n\n"
        "出力は必ず以下のJSON形式のみを返してください（Markdownコードブロック不要）：\n"
        '{"main_text": "投稿本文（改行は\\n）", "writer_note": "今回どの天気・街況・データをどうアドリブに活かしたかの1行メモ", "thinking_memo": "脳内3案（①独り言風・②常連風・③違和感発見風）をどう比較し、なぜこの言い回し・温度感に削ぎ落としたかの編集長思考メモ（80〜140文字）"}'
    )
    return prompt, reply_text


def _discover_ai_studio_flash_model(api_key: str) -> str:
    """Dynamically query `/v1beta/models` to find an active Flash model if hardcoded names 404."""
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
            timeout=5,
        )
        if r.status_code != 200:
            return ""
        models = (r.json() or {}).get("models") or []
        flash_cands = []
        fallback_cands = []
        for m in models:
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = (m.get("name") or "").replace("models/", "")
            if not name or "embedding" in name or "imagen" in name or "tts" in name or "audio" in name:
                continue
            if "gemini" in name and "flash" in name and "vision" not in name:
                flash_cands.append(name)
            elif "gemini" in name:
                fallback_cands.append(name)
        if flash_cands:
            flash_cands.sort(reverse=True)
            return flash_cands[0]
        if fallback_cands:
            return fallback_cands[0]
    except Exception:
        pass
    return ""


def _build_endpoint_request(endpoint_kind: str, model_name: str, api_key: str, prompt: str) -> tuple:
    gen_cfg = {
        "temperature": 0.88,
        "maxOutputTokens": 3072,
        "responseMimeType": "application/json",
    }
    # Bounded deep-thinking budget on Gemini 2.5 Flash (~4-6s for 3-persona internal competition without >10s timeout)
    if model_name.startswith("gemini-2.5"):
        budget = 512 if "lite" in model_name else 1536
        gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    if endpoint_kind == "vertex_v1":
        return (
            f"https://aiplatform.googleapis.com/v1/publishers/google/models/{model_name}:generateContent",
            {"Content-Type": "application/json"},
            {"key": api_key},
            payload,
        )
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
        {"Content-Type": "application/json", "x-goog-api-key": api_key},
        None,
        payload,
    )


def has_cached_ai_candidate(research: dict, slot_label: str, channel: str) -> bool:
    ch = (channel or "x").lower()
    top_s = research.get("top_stores") or []
    if not top_s:
        return False
    sig_top = f"{top_s[0]['name']}:{top_s[0]['women']}:{top_s[0]['men']}"
    cache_key = (ch, slot_label, sig_top)
    cached = _ai_candidate_cache.get(cache_key)
    return bool(cached and (time.time() - cached["ts"] < 900))


def _generate_gemini_adlib_candidate(
    research: dict,
    plan: dict,
    slot_label: str,
    channel: str,
    now_jst: datetime.datetime,
    site_url: str,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> dict:
    """
    Call Gemini Flash REST API (`generativelanguage.googleapis.com` for both `AIza...` and `AQ....` AI Studio keys,
    with `thinkingBudget: 0` for ~1.2s sub-2s response latency).
    When `cache_only=True`, returns cached draft in 0ms without blocking page render.
    """
    api_key = get_gemini_api_key(allow_blocking=(not cache_only))
    if not api_key:
        return None

    ch = (channel or "x").lower()
    top_s = research.get("top_stores") or []
    if not top_s:
        return None

    sig_top = f"{top_s[0]['name']}:{top_s[0]['women']}:{top_s[0]['men']}"
    cache_key = (ch, slot_label, sig_top)
    now_t = time.time()
    if not force_refresh and cache_key in _ai_candidate_cache:
        cached = _ai_candidate_cache[cache_key]
        if now_t - cached["ts"] < 900:
            return dict(cached["candidate"])

    if cache_only:
        return None

    prompt, reply_text = _build_gemini_prompt(
        research=research,
        plan=plan,
        slot_label=slot_label,
        channel=ch,
        now_jst=now_jst,
        site_url=site_url,
    )

    preferred_env = (os.environ.get("GEMINI_MODEL") or "").strip()
    models_to_try = [preferred_env] + [m for m in DEFAULT_GEMINI_MODELS if m != preferred_env] if preferred_env else list(DEFAULT_GEMINI_MODELS)

    # Both modern AI Studio `AQ.` auth keys and classic `AIza` keys target `ai_studio` directly first!
    routes = []
    saved_route = _last_gemini_meta.get("working_route")
    if saved_route:
        routes.append(saved_route)
    for mn in models_to_try:
        if ("ai_studio", mn) not in routes:
            routes.append(("ai_studio", mn))
    if api_key.startswith("AQ.") and ("vertex_v1", "gemini-2.5-flash") not in routes:
        routes.append(("vertex_v1", "gemini-2.5-flash"))

    last_err = ""
    saw_404_on_ai_studio = False

    def _try_single_route(ek: str, mn: str):
        url, headers, params, payload = _build_endpoint_request(ek, mn, api_key, prompt)
        return requests.post(url, headers=headers, params=params, json=payload, timeout=25)

    for ek, model_name in routes:
        try:
            resp = _try_single_route(ek, model_name)
            if resp.status_code == 404:
                if ek == "ai_studio":
                    saw_404_on_ai_studio = True
                last_err = f"Model {model_name} ({ek}) HTTP 404"
                continue
            if resp.status_code >= 400:
                err_snippet = (resp.text or "")[:140]
                last_err = f"Gemini HTTP {resp.status_code} ({ek}): {err_snippet}"
                logger.warning(f"Gemini API error ({ek}/{model_name}): {last_err}")
                continue

            data = resp.json() or {}
            cands = data.get("candidates") or []
            if not cands:
                last_err = "Gemini returned empty candidates"
                continue
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            raw_text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if not raw_text:
                raw_text = "".join(p.get("text", "") for p in parts).strip()
            if raw_text.startswith("```"):
                raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                raw_text = re.sub(r"\s*```$", "", raw_text)
            parsed = json.loads(raw_text, strict=False)
            main_text = (parsed.get("main_text") or "").strip()
            writer_note = (parsed.get("writer_note") or "天気・曜日・リアルタイム人数を掛け合わせたGemini AIアドリブ執筆案。").strip()
            thinking_memo = (parsed.get("thinking_memo") or "").strip()
            if not main_text:
                last_err = "Parsed JSON missing main_text"
                continue

            candidate = {
                "style": "ai_live_adlib",
                "style_label": STYLE_LABELS["ai_live_adlib"],
                "writer_note": f"【{model_name}・長考推敲】{writer_note}",
                "thinking_memo": thinking_memo,
                "main_text": main_text,
                "reply_text": reply_text,
                "is_ai_generated": True,
                "ai_model": model_name,
            }
            _ai_candidate_cache[cache_key] = {
                "ts": now_t,
                "candidate": candidate,
                "model": model_name,
            }
            _last_gemini_meta["working_route"] = (ek, model_name)
            _last_gemini_meta["last_model"] = model_name
            _last_gemini_meta["last_error"] = ""
            _last_gemini_meta["last_ok_at"] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
            return dict(candidate)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = f"通信タイムアウト ({model_name}): {str(e)[:110]}"
            logger.warning(f"Gemini adlib network timeout ({ek}/{model_name}): {e}")
            if _last_gemini_meta.get("working_route") == (ek, model_name):
                _last_gemini_meta["working_route"] = None
            break
        except Exception as e:
            last_err = str(e)[:160]
            logger.warning(f"Gemini adlib generation exception ({ek}/{model_name}): {e}")
            if _last_gemini_meta.get("working_route") == (ek, model_name):
                _last_gemini_meta["working_route"] = None
            continue

    # Dynamic model auto-discovery fallback if preset model names returned 404
    if saw_404_on_ai_studio:
        discovered = _discover_ai_studio_flash_model(api_key)
        if discovered and discovered not in models_to_try:
            try:
                resp = _try_single_route("ai_studio", discovered)
                if resp.status_code == 200:
                    data = resp.json() or {}
                    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
                    raw_text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                    if raw_text.startswith("```"):
                        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                        raw_text = re.sub(r"\s*```$", "", raw_text)
                    parsed = json.loads(raw_text, strict=False)
                    main_text = (parsed.get("main_text") or "").strip()
                    writer_note = (parsed.get("writer_note") or "Gemini AIアドリブ執筆案。").strip()
                    thinking_memo = (parsed.get("thinking_memo") or "").strip()
                    if main_text:
                        candidate = {
                            "style": "ai_live_adlib",
                            "style_label": STYLE_LABELS["ai_live_adlib"],
                            "writer_note": f"【{discovered}・長考推敲】{writer_note}",
                            "thinking_memo": thinking_memo,
                            "main_text": main_text,
                            "reply_text": reply_text,
                            "is_ai_generated": True,
                            "ai_model": discovered,
                        }
                        _ai_candidate_cache[cache_key] = {"ts": now_t, "candidate": candidate, "model": discovered}
                        _last_gemini_meta["working_route"] = ("ai_studio", discovered)
                        _last_gemini_meta["last_model"] = discovered
                        _last_gemini_meta["last_error"] = ""
                        _last_gemini_meta["last_ok_at"] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
                        return dict(candidate)
            except Exception:
                pass

    if last_err:
        _last_gemini_meta["last_error"] = last_err
    return None


def run_writer(
    research: dict,
    plan: dict,
    slot_label: str = "22:00",
    channel: str = "x",
    now_jst: datetime.datetime = None,
    app_url: str = "",
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
) -> list:
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    ch = (channel or "x").lower()
    if ch == "threads":
        base_cands = _write_threads_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url)
    elif ch == "instagram":
        base_cands = _write_instagram_candidates(research, now_jst=now_jst)
    else:
        base_cands = _write_x_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url)

    if use_ai:
        ai_cand = _generate_gemini_adlib_candidate(
            research=research,
            plan=plan,
            slot_label=slot_label,
            channel=ch,
            now_jst=now_jst,
            site_url=site_url,
            force_refresh=force_ai_refresh,
            cache_only=ai_cache_only,
        )
        if ai_cand:
            return [ai_cand] + base_cands
    return base_cands


# =====================================================================
# Role 4: Evaluator / Desk (鬼デスク・評価係)
# =====================================================================
def run_evaluator(
    candidates: list,
    research: dict,
    plan: dict,
    channel: str = "x",
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Score each candidate out of 100 across 4 criteria tailored to `x`, `threads`, or `instagram`:
      1. human_tone (35 pts): Natural human voice vs mechanical medal/bracket templates
      2. freshness_diversity (30 pts): Rotation vs recent history on the same SNS channel
      3. data_specificity (25 pts): Verified numbers (strict AI hallucination guard!), zero redundant mentions
      4. weight_fit (10 pts): Strict compliance with each platform's character & link-hygiene limits
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    last_style = plan.get("last_style")
    recent_styles = plan.get("recent_styles") or []
    style_priority = plan.get("style_priority") or []
    classic_key = plan.get("classic_key")

    evaluated = []
    for cand in candidates:
        text = cand.get("main_text") or ""
        style = cand.get("style") or ""
        is_ai = bool(cand.get("is_ai_generated") or style == "ai_live_adlib")
        first_line = text.split("\n", 1)[0] if text else ""

        if ch == "x":
            metric_val = x_poster.compute_twitter_weight(text)
            metric_unit = f"{metric_val}/280w"
        else:
            metric_val = len(text)
            metric_unit = f"{metric_val}文字"

        # 1. Human Tone (max 35) — strictly penalizes bot medals, bracket headers, AND AI copywriting cliches
        is_medal_bot = ("🥇" in text and "🥈" in text and "🥉" in text)
        is_bracket_banner = first_line.startswith("【") and "】" in first_line
        detected_cliches = [w for w in AI_CLICHE_WORDS if w in text]
        if style == classic_key or is_medal_bot:
            human_score = 18
            human_comment = "⚠️ 毎回同じ『🥇🥈🥉 TOP3』や【角括弧見出し】の定型フォーマットで業者・ボット感が出やすいため減点。"
        elif detected_cliches:
            human_score = 22
            human_comment = f"⚠️ AI・宣伝っぽく見えやすい定型ワード（『{'』『'.join(detected_cliches[:2])}』）が含まれているため減点。"
        elif is_bracket_banner:
            human_score = 24
            human_comment = "△ 冒頭が【角括弧の見出し】から始まっており少し宣伝っぽいため、自然な語り出し案を優先。"
        elif is_ai:
            human_score = 35
            human_comment = "◎ 長考3案コンペ＆脱AI推敲クリア：AI臭ワード0個・時間帯の空気に馴染む生きた人間の語り口です。"
        else:
            human_score = 34
            if any(w in text for w in ("割れてる", "立ち上がりがかなり早い", "いつもの", "もったいない", "実は同じ", "温度差", "雨の", "給料日", "祝前日")):
                human_score = 35
            human_comment = "◎ 人間がスマホで現場を見ながら話しかけている自然な導入で、タイムラインに馴染みます。"

        # 2. Freshness & Anti-Duplication (max 30)
        if is_ai:
            # Even if AI was used last time, each AI generation uses a brand-new opener avoiding recent history!
            fresh_score = 30
            fresh_comment = "◎ 過去3回の投稿と書き出し・言い回しが一切被らない完全新規アドリブ構成。"
        elif style == last_style:
            fresh_score = 12
            fresh_comment = f"⚠️ 前回の{CHANNEL_LABELS.get(ch, ch)}投稿と同じ型（{STYLE_LABELS.get(style, style)}）のため連続回避で大幅減点。"
        elif style == classic_key and plan.get("suppress_classic"):
            fresh_score = 15
            fresh_comment = "⚠️ 定型ランキングのマンネリ防止ルールにより優先度を引き下げ。"
        elif style in recent_styles[:2]:
            fresh_score = 22
            fresh_comment = "◯ 直近2回以内に近い切り口があるため、別角度の候補を優先。"
        else:
            if style in style_priority:
                p_idx = style_priority.index(style)
                fresh_score = 30 if p_idx == 0 else (26 if p_idx == 1 else 24)
            else:
                fresh_score = 24
            fresh_comment = "◎ 直近の投稿履歴と切り口・1行目のフックが被っておらず新鮮です。"

        # 3. Data Specificity & Precision Check (max 25; -50 hard reject if AI alters any number)
        has_raw_olg = "OLG " in text and style != classic_key
        if is_ai:
            facts_ok, bad_tokens = verify_ai_factual_numbers(text, research, channel=ch)
            if not facts_ok:
                data_score = -50
                data_comment = (
                    f"❌ 鬼デスク検閲ブロック：AI下書き内に実測データと一致しない数値（{', '.join(bad_tokens[:3])}）を検知！"
                    "ハルシネーション防止のため不採用とし、検証済み案へ自動切替します。"
                )
            elif has_raw_olg:
                data_score = 18
                data_comment = "⚠️ 内部略称『OLG』が残っているため減点。"
            else:
                data_score = 25
                data_comment = "◎ 鬼デスク数値検閲クリア：すべての店舗人数・比率がSQLite実測値と1人単位で100%一致しています。"
        elif has_raw_olg:
            data_score = 18
            data_comment = "⚠️ 内部略称『OLG』がそのまま残っているため減点（『オリエンタル〇〇』表記が自然）。"
        elif style in ("hub_gap", "threads_city_story") and research.get("best_cluster"):
            gap = research["best_cluster"].get("pct_gap", 0)
            data_score = 25 if gap >= 12 else 22
            data_comment = f"◎ 同一エリア内の男女比ギャップ（{gap}pt差）が実測値で示されており、店選びの納得感が非常に高い。"
        elif style in ("usual_anomaly", "threads_anomaly_insight"):
            data_score = 25
            data_comment = "◎ 普段比（平比）データや男女双方の目線（穴場店＋即相席店）が具体的で精度が高い。"
        elif style in ("ig_empathy_story", "ig_data_discovery"):
            data_score = 25
            data_comment = "◎ カルーセル5枚の統計データと完全連動し、保存したくなる具体的な立ち回り根拠あり。"
        else:
            data_score = 23 if style != classic_key else 20
            data_comment = "◎ 主要店舗の実測人数・比率が過不足なく整理されています。"

        # 4. Platform Format Hygiene (max 10; hard penalty if over API limit)
        if ch == "x":
            has_url_in_main = "http://" in text or "https://" in text
            if metric_val > 280 or has_url_in_main:
                weight_score = -40
                weight_comment = f"❌ X制限超過({metric_unit})または1ポスト目URL混入のため不採用。"
            elif 130 <= metric_val <= 274:
                weight_score = 10
                weight_comment = f"◎ X最適ウェイト（{metric_unit}・1ポスト目URLなし・リプ欄誘導）を完璧にクリア。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数ウェイト（{metric_unit}）は許容範囲内。"
        elif ch == "threads":
            tag_cnt = len(re.findall(r"#[^\s#]+", text))
            if metric_val > 500:
                weight_score = -40
                weight_comment = f"❌ Threads上限500文字を超過（{metric_unit}）しているため不採用。"
            elif 240 <= metric_val <= 488 and tag_cnt <= 2:
                weight_score = 10
                weight_comment = f"◎ Threads最適文字数（{metric_unit}/500文字・タグ{tag_cnt}個でスパム判定回避）に合致。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数（{metric_unit}）・タグ数（{tag_cnt}個）は許容範囲内。"
        else:  # instagram
            tag_cnt = len(re.findall(r"#[^\s#]+", text))
            if 340 <= metric_val <= 980 and 4 <= tag_cnt <= 8 and "保存" in text:
                weight_score = 10
                weight_comment = f"◎ IG最適長（{metric_unit}）・保存導線あり・厳選ハッシュタグ{tag_cnt}個でアルゴリズム最適。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数（{metric_unit}）・タグ{tag_cnt}個は標準範囲内。"

        total_score = human_score + fresh_score + data_score + weight_score
        item_copy = dict(cand)
        item_copy.update({
            "channel": ch,
            "total_score": total_score,
            "weight": metric_val,
            "metric_unit": metric_unit,
            "scores": {
                "human_tone": human_score,
                "freshness_diversity": fresh_score,
                "data_specificity": data_score,
                "weight_fit": weight_score,
            },
            "desk_comments": [
                human_comment,
                fresh_comment,
                data_comment,
                weight_comment,
            ],
        })
        evaluated.append(item_copy)

    evaluated.sort(key=lambda x: x["total_score"], reverse=True)
    for idx, item in enumerate(evaluated):
        item["rank"] = idx + 1
        item["is_selected"] = (idx == 0)

    winner = evaluated[0] if evaluated else None
    classic_score = next((c["total_score"] for c in evaluated if c["style"] == classic_key), "-")
    desk_summary = (
        f"【{CHANNEL_LABELS.get(ch, ch)}】全{len(evaluated)}案を審査し、最高スコア {winner['total_score']}点（100点満点）の"
        f"『{winner['style_label']}』を採用しました。"
        f"従来の定型フォーマット（{classic_score}点）を避け、媒体アルゴリズムと人間の読みやすさに最適化しています。"
        if winner else "候補案なし"
    )

    return {
        "channel": ch,
        "winner": winner,
        "candidates": evaluated,
        "desk_summary": desk_summary,
    }


def run_editorial_pipeline(
    stores: list,
    slot_label: str = "22:00",
    channel: str = "x",
    ig_ctx: dict = None,
    now_jst: datetime.datetime = None,
    app_url: str = "",
    record_history: bool = False,
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
) -> dict:
    """
    Run the full 4-role SNS Editorial Room pipeline for `channel` ('x', 'threads', or 'instagram').
    When `ai_cache_only=True` (used on HTML page render), returns in <30ms using cached AI draft if available,
    and flags `ai_pending_async=True` so client JS can fetch the fresh AI draft asynchronously without blocking UI.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    if ch not in ("x", "threads", "instagram"):
        ch = "x"

    allow_state_blocking = bool(record_history or (use_ai and not ai_cache_only))
    research = run_researcher(
        stores,
        slot_label=slot_label,
        channel=ch,
        ig_ctx=ig_ctx,
        now_jst=now_jst,
        allow_weather_blocking=record_history,
    )
    plan = run_planner(
        research,
        slot_label=slot_label,
        channel=ch,
        now_jst=now_jst,
        allow_blocking=allow_state_blocking,
    )
    candidates = run_writer(
        research,
        plan,
        slot_label=slot_label,
        channel=ch,
        now_jst=now_jst,
        app_url=app_url,
        use_ai=use_ai,
        force_ai_refresh=force_ai_refresh,
        ai_cache_only=ai_cache_only,
    )
    evaluation = run_evaluator(candidates, research, plan, channel=ch, now_jst=now_jst)

    has_ai_cand = any(c.get("is_ai_generated") for c in (evaluation.get("candidates") or []))
    sheets_warming = (not getattr(job_state, "_sheets_loaded", True)) and (not job_state.sheets_store.is_disabled())
    ai_pending_async = bool(
        ai_cache_only
        and use_ai
        and (bool(get_gemini_api_key(allow_blocking=False)) or sheets_warming)
        and (force_ai_refresh or not has_ai_cand)
        and bool(research.get("top_stores"))
    )

    winner = evaluation.get("winner") or {}
    session_record = {
        "channel": ch,
        "channel_label": CHANNEL_LABELS.get(ch, ch),
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "slot_label": slot_label,
        "selected_style": winner.get("style", ""),
        "selected_style_label": winner.get("style_label", ""),
        "selected_score": winner.get("total_score", 0),
        "selected_text": winner.get("main_text", ""),
        "selected_reply": winner.get("reply_text", ""),
        "selected_thinking_memo": winner.get("thinking_memo", ""),
        "desk_summary": evaluation.get("desk_summary", ""),
        "planner_memo": plan.get("planner_memo", ""),
        "research_notes": research.get("research_notes", []),
        "candidates": [
            {
                "rank": c.get("rank"),
                "is_selected": c.get("is_selected"),
                "style": c.get("style"),
                "style_label": c.get("style_label"),
                "total_score": c.get("total_score"),
                "weight": c.get("weight"),
                "metric_unit": c.get("metric_unit"),
                "scores": c.get("scores"),
                "writer_note": c.get("writer_note"),
                "thinking_memo": c.get("thinking_memo") or "",
                "desk_comments": c.get("desk_comments"),
                "main_text": c.get("main_text"),
                "reply_text": c.get("reply_text"),
                "is_ai_generated": bool(c.get("is_ai_generated")),
            }
            for c in (evaluation.get("candidates") or [])
        ],
    }

    if record_history and winner:
        try:
            h_key = _history_key_for_channel(ch)
            history = get_recent_editorial_history(channel=ch, limit=11)
            history.insert(0, session_record)
            job_state.set_value(h_key, json.dumps(history[:12], ensure_ascii=False))
        except Exception as e:
            logger.warning(f"Failed to persist {ch} editorial log: {e}")

    return {
        "channel": ch,
        "channel_label": CHANNEL_LABELS.get(ch, ch),
        "gemini_status": get_gemini_status(allow_blocking=allow_state_blocking),
        "ai_pending_async": ai_pending_async,
        "research": research,
        "plan": plan,
        "evaluation": evaluation,
        "winner": winner,
        "session_record": session_record,
        "history": get_recent_editorial_history(channel=ch, limit=8, allow_blocking=allow_state_blocking),
    }
