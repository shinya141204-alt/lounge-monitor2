"""
sns_insights_loop.py - 3-Layer Closed-Loop SNS Insights & Retention Bandit Engine for Aiseki Navi.

Closes the feedback loop across:
  1. Loop 1 (Hook & Copy Evolution):
     - Official Meta Threads Graph API insights (`views`, `likes`, `replies`, `reposts`, `quotes`)
     - Multimodal Screenshot / JSON Ingestion via `gemini-4-carbon` (Threads, note, X, IG Reels)
     - Opening Hook Archetype classification (`store_gap_shock`, `city_battle`, `ladies_zero_wait`, `time_wave`, `poetic_scene`)
     - Empirical Desk score boosts (`hook_archetype_boosts`) & real winner/loser prompt feedback (`sns_prompt_feedback`)
  2. Loop 2 (Instagram Reels 2-Second Skip Cliff & Multi-Armed Bandit):
     - Official Instagram Graph API Reel/Media insights (`views`, `reach`, `saved`, `shares`, `ig_reels_avg_watch_time`)
     - Screenshot ingestion for UI-only Reel metrics (`skip_rate_pct`, `typical_skip_rate_pct`, `avg_watch_sec`)
     - UCB1 retention-weighted bandit template ranking (`reel_template_boosts`) fed into `reel_generator.select_scheduled_reel_type`
  3. Loop 3 (note Title CTR & Cross-Funnel Synergy):
     - Empirical note title PV/CTR ranking (`note_ctr_feedback`) fed into `pdca_engine` & `column_editorial`.

Strict Fact Guarantee (`AGENTS.md §1`):
  - Zero fabricated or placeholder metrics. Missing metrics remain `None` and `data_status="insufficient"` until real data exists.
  - Bounded `job_state` storage (`< 36,000 chars`) to stay well within Google Sheets 45,000-char cell limit (`AGENTS.md §8`).
"""

import base64
import collections
import csv
import datetime
import io
import json
import logging
import math
import os
import re
import statistics
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import analytics
import job_state

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))
SNS_INSIGHTS_STORE_KEY = "sns_insights_store_v1"
GSC_STORE_KEY = "gsc_insights_store_v1"

MAX_STORED_POSTS = 85
MAX_STORED_NOTE_ARTICLES = 30
MAX_STORE_JSON_CHARS = 35000
MAX_GSC_QUERIES = 45
MAX_GSC_PAGES = 25
MAX_GSC_STORE_JSON_CHARS = 22000
MAX_SCREENSHOT_BYTES = 6 * 1024 * 1024  # 6 MB safety ceiling

HOOK_ARCHETYPE_LABELS: Dict[str, str] = {
    "store_gap_shock": "具体店名×極端数値フック（例: 〇〇店、1時間前は女性比…）",
    "city_battle": "同駅・都市間ギャップ対比フック",
    "ladies_zero_wait": "女子組・待ちゼロ男性先行フック",
    "time_wave": "時間帯・終電波形フック",
    "poetic_scene": "情景・天気ポエム導入（離脱注意）",
}

REEL_TEMPLATE_KEYS = (
    "live_top5",
    "area_battle",
    "tiktok_gap_shock",
    "ladies_zero_wait",
    "tiktok_ladies_real",
    "tiktok_midnight_cliff",
    "moshimo_3d",
)

REEL_TEMPLATE_LABELS: Dict[str, str] = {
    "live_top5": "全国リアルタイムTOP5 (live_top5)",
    "area_battle": "3大エリア徹底対決 (area_battle)",
    "tiktok_gap_shock": "同駅徒歩4分ギャップ実証 (tiktok_gap_shock)",
    "ladies_zero_wait": "女子得・受付待ちゼロ速報 (ladies_zero_wait)",
    "tiktok_ladies_real": "女子組の2軒目リアル男性先行 (tiktok_ladies_real)",
    "tiktok_midnight_cliff": "23時20分・終電後の真実 (tiktok_midnight_cliff)",
    "moshimo_3d": "3Dボクセル『もしもシリーズ』 (moshimo_3d)",
}

CARBON_VISION_MODELS = (
    "gemini-4-carbon",
    "gemini-4-argon",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
)

_POETIC_OPENER_WORDS = (
    "白い息",
    "コート",
    "上着",
    "マフラー",
    "夜風",
    "風が冷た",
    "風が少し",
    "東京は曇り",
    "東京は晴れ",
    "大阪は曇り",
    "大阪は晴れ",
    "出るにはまだ少し早い",
    "ネオンが",
    "街灯",
    "傘を",
    "小雨の",
)

_CITY_STORE_TOKENS = (
    "新宿",
    "渋谷",
    "恵比寿",
    "銀座",
    "上野",
    "池袋",
    "町田",
    "大宮",
    "横浜",
    "梅田",
    "茶屋町",
    "大阪駅前",
    "難波",
    "心斎橋",
    "天満",
    "京都",
    "神戸",
    "三宮",
    "名古屋",
    "栄",
    "錦",
    "福岡",
    "天神",
    "中洲",
    "小倉",
    "札幌",
    "すすきの",
    "仙台",
    "広島",
    "岡山",
    "松山",
    "高松",
    "熊本",
    "鹿児島",
    "長崎",
    "沖縄",
    "オリエンタルラウンジ",
    "JIS",
    "ag",
    "ASOBIBAR",
    "ヤタコイ",
    "XIX",
    "CLOVERS",
    "ALFA",
)

_store_lock = threading.Lock()
_mem_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_gsc_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_feedback_cache: Dict[str, Any] = {"ts": 0.0, "days": 28, "data": None}


def reset_insights_loop_cache_for_tests() -> None:
    """Clear in-memory caches for deterministic unit testing."""
    with _store_lock:
        _mem_cache["ts"] = 0.0
        _mem_cache["data"] = None
        _gsc_cache["ts"] = 0.0
        _gsc_cache["data"] = None
        _feedback_cache["ts"] = 0.0
        _feedback_cache["data"] = None


def _now_jst_str(now_jst: Optional[datetime.datetime] = None) -> str:
    dt = now_jst or datetime.datetime.now(JST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    else:
        dt = dt.astimezone(JST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _sanitize_text(raw: str, max_len: int = 140) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    try:
        import column_editorial
        txt = column_editorial._sanitize_brand_names(txt)
    except Exception:
        txt = re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_len]


def _extract_opener(raw: str, max_len: int = 92) -> str:
    if not raw:
        return ""
    first_line = str(raw).strip().splitlines()[0].strip() if str(raw).strip() else ""
    return _sanitize_text(first_line, max_len=max_len)


def _extract_s_param_from_text(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"[?&]s=([A-Za-z0-9_\-]{1,48})", str(raw))
    if not m:
        return ""
    return analytics.normalize_inflow_source(m.group(1))


def classify_opening_hook_archetype(text: str) -> str:
    """
    Classify the first line / first 56 chars of a post into one of the 5 empirical hook archetypes:
      - `poetic_scene`: weather / temperature / coat / abstract mood opener without immediate store+number
      - `ladies_zero_wait`: women-perspective zero-wait / male-lead opener
      - `city_battle`: multi-city / intra-station comparison (`vs`, `徒歩4分`, `同じ駅`, 2+ city names)
      - `store_gap_shock`: concrete city/store name + concrete number (`%`, `名`, `pt`, `1時間前`) in opening 56 chars
      - `time_wave`: time-band wave or general observation opener
    """
    raw = str(text or "").strip()
    if not raw:
        return "time_wave"
    first_line = raw.splitlines()[0].strip()
    head = first_line[:56]
    head_early = first_line[:28]

    has_early_store_and_num = (
        any(tok in head_early for tok in _CITY_STORE_TOKENS)
        and bool(re.search(r"\d+\s*(?:%|％|名|人|pt|倍)", head_early))
    )
    is_poetic = (
        any(pw in first_line for pw in _POETIC_OPENER_WORDS)
        or bool(re.search(r"\d+(?:\.\d+)?\s*℃|気温\s*\d+\s*度", first_line))
    )
    if is_poetic and not has_early_store_and_num:
        return "poetic_scene"

    if any(w in head for w in ("女子", "女性ペア", "女性2人", "待ち0", "待ちゼロ", "男性先行", "即乾杯", "女子会")):
        return "ladies_zero_wait"

    city_hits = sum(1 for tok in _CITY_STORE_TOKENS[:36] if tok in head)
    if (
        re.search(r"(?i)\bvs\b", head)
        or any(w in head for w in ("対決", "徒歩4分", "徒歩3分", "同じ駅", "温度差", "割れて"))
        or city_hits >= 2
    ):
        return "city_battle"

    has_store_or_city = any(tok in head for tok in _CITY_STORE_TOKENS)
    has_concrete_num = bool(re.search(r"\d+\s*(?:%|％|名|人|pt|倍|時間前|分前)", head))
    if has_store_or_city and has_concrete_num:
        return "store_gap_shock"

    return "time_wave"


def infer_reel_type_from_caption(caption: str) -> str:
    """Infer canonical `reel_type` from an Instagram Reel / YouTube Short caption when not explicitly tagged."""
    c = str(caption or "")
    if not c:
        return "live_top5"
    if any(w in c for w in ("#もしもシリーズ", "もしもシリーズ", "もし日本から", "もし相席", "もし金曜", "もし全員が", "もし給料日前", "もし5万人", "もし日曜", "もし夜の街", "moshimo_")):
        return "moshimo_3d"
    if any(w in c for w in ("同じ駅", "徒歩4分", "ドア1枚", "男女比ギャップ")):
        return "tiktok_gap_shock"
    if any(w in c for w in ("終電崖", "23時20分", "深夜維持", "終電後")):
        return "tiktok_midnight_cliff"
    if any(w in c for w in ("新宿 vs 梅田", "徹底対決", "激戦区", "3大激戦区")):
        return "area_battle"
    if any(w in c for w in ("女性70%超え", "女子2人組がいま受付待ちゼロ", "夜カフェ代わり", "女子会の2軒目メモ")):
        return "tiktok_ladies_real"
    if any(w in c for w in ("女子ペア", "待ち0", "待ちほぼ0", "男性先行", "女子得")):
        return "ladies_zero_wait"
    return "live_top5"


def _empty_store() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "last_sync_at": "",
        "last_sync_summary": {},
        "posts": [],
        "note_articles": [],
    }


def _load_store(allow_blocking: bool = True) -> Dict[str, Any]:
    now_m = time.monotonic()
    with _store_lock:
        if _mem_cache["data"] is not None and (now_m - _mem_cache["ts"] < 15.0):
            return json.loads(json.dumps(_mem_cache["data"]))

    raw = job_state.get_value(SNS_INSIGHTS_STORE_KEY, allow_blocking=allow_blocking)
    store = _empty_store()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                store["updated_at"] = str(parsed.get("updated_at") or "")
                store["last_sync_at"] = str(parsed.get("last_sync_at") or "")
                if isinstance(parsed.get("last_sync_summary"), dict):
                    store["last_sync_summary"] = dict(parsed["last_sync_summary"])
                if isinstance(parsed.get("posts"), list):
                    store["posts"] = [p for p in parsed["posts"] if isinstance(p, dict)][:MAX_STORED_POSTS]
                if isinstance(parsed.get("note_articles"), list):
                    store["note_articles"] = [n for n in parsed["note_articles"] if isinstance(n, dict)][:MAX_STORED_NOTE_ARTICLES]
        except Exception as e:
            logger.warning(f"[sns_insights_loop] Failed to parse stored insights JSON: {e}")

    with _store_lock:
        _mem_cache["ts"] = now_m
        _mem_cache["data"] = json.loads(json.dumps(store))
    return store


def _save_store(store: Dict[str, Any], now_jst: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    store["version"] = 1
    store["updated_at"] = _now_jst_str(now_jst)
    posts = [p for p in (store.get("posts") or []) if isinstance(p, dict)]
    posts.sort(key=lambda x: str(x.get("published_at") or x.get("updated_at") or ""), reverse=True)
    store["posts"] = posts[:MAX_STORED_POSTS]

    notes = [n for n in (store.get("note_articles") or []) if isinstance(n, dict)]
    notes.sort(key=lambda x: (int(x.get("views") or 0), str(x.get("updated_at") or "")), reverse=True)
    store["note_articles"] = notes[:MAX_STORED_NOTE_ARTICLES]

    serialized = json.dumps(store, ensure_ascii=False)
    while len(serialized) > MAX_STORE_JSON_CHARS and len(store["posts"]) > 10:
        store["posts"] = store["posts"][:-5]
        serialized = json.dumps(store, ensure_ascii=False)

    job_state.set_value(SNS_INSIGHTS_STORE_KEY, serialized)
    with _store_lock:
        _mem_cache["ts"] = time.monotonic()
        _mem_cache["data"] = json.loads(serialized)
        _feedback_cache["ts"] = 0.0
        _feedback_cache["data"] = None
    return store


def _safe_nonneg_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool) or val == "":
        return None
    try:
        cleaned = str(val).replace(",", "").strip()
        if not cleaned:
            return None
        iv = int(round(float(cleaned)))
        return iv if iv >= 0 else None
    except (TypeError, ValueError):
        return None


def _safe_nonneg_float(val: Any, max_val: float = 100000.0) -> Optional[float]:
    if val is None or isinstance(val, bool) or val == "":
        return None
    try:
        cleaned = str(val).replace(",", "").replace("%", "").replace("秒", "").strip()
        if not cleaned:
            return None
        fv = float(cleaned)
        if math.isnan(fv) or math.isinf(fv) or fv < 0.0 or fv > max_val:
            return None
        return round(fv, 2)
    except (TypeError, ValueError):
        return None


def _clean_metrics_dict(raw_m: Dict[str, Any]) -> Dict[str, Any]:
    """Keep ONLY verified, non-null numeric metrics (zero fabricated values)."""
    if not isinstance(raw_m, dict):
        return {}
    out: Dict[str, Any] = {}
    for int_k in (
        "views",
        "reach",
        "likes",
        "replies",
        "reposts",
        "quotes",
        "saved",
        "shares",
        "comments",
        "bookmarks",
        "link_clicks",
        "impressions",
        "subscribers_gained",
    ):
        iv = _safe_nonneg_int(raw_m.get(int_k))
        if iv is not None:
            out[int_k] = iv
    for flt_k, cap in (
        ("avg_watch_sec", 600.0),
        ("total_watch_sec", 10000000.0),
        ("skip_rate_pct", 100.0),
        ("typical_skip_rate_pct", 100.0),
        ("share_rate_pct", 100.0),
        ("stayed_to_watch_pct", 100.0),
        ("avg_pct_viewed", 300.0),
    ):
        fv = _safe_nonneg_float(raw_m.get(flt_k), max_val=cap)
        if fv is not None:
            out[flt_k] = fv
    return out


_YT_VIDEO_PID_RE = re.compile(r"^yt_[A-Za-z0-9_-]{11}$")


def _normalize_yt_title_core(raw_title: str) -> str:
    """Strip series tags (`【...】`), `#shorts` hashtags, spaces, and punctuation for manual->API post deduplication."""
    s = re.sub(r"【[^】]{0,24}】", "", str(raw_title or ""))
    s = re.sub(r"#\S+", "", s)
    s = re.sub(r"[\s\|\｜・:：!！\?？「」『』（）\(\)\-\—_〜~]+", "", s)
    return s.lower()


def _upsert_post_record(
    store: Dict[str, Any],
    channel: str,
    post_id: str = "",
    text: str = "",
    style: str = "",
    reel_type: str = "",
    slot_label: str = "",
    s_param: str = "",
    published_at: str = "",
    permalink: str = "",
    source: str = "publish",
    metrics: Optional[Dict[str, Any]] = None,
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    ch = (channel or "threads").strip().lower()
    if ch not in ("threads", "instagram", "reels", "x", "tiktok", "youtube_shorts"):
        ch = "threads"
    pid = str(post_id or "").strip()[:80]
    opener = _extract_opener(text, max_len=92)
    snippet = _sanitize_text(text, max_len=150)
    clean_m = _clean_metrics_dict(metrics or {})
    ts_now = _now_jst_str(now_jst)
    pub_ts = str(published_at or "").strip()[:25] or ts_now

    norm_rt = str(reel_type or "").strip().lower()
    if norm_rt and norm_rt not in REEL_TEMPLATE_KEYS:
        norm_rt = infer_reel_type_from_caption(text)
    if ch in ("reels", "youtube_shorts") and not norm_rt:
        norm_rt = infer_reel_type_from_caption(text)

    s_tag = analytics.normalize_inflow_source(s_param) if s_param else _extract_s_param_from_text(text)
    hook_arch = classify_opening_hook_archetype(text) if text else ""

    posts = store.setdefault("posts", [])
    existing: Optional[Dict[str, Any]] = None
    for item in posts:
        if pid and item.get("post_id") == pid:
            existing = item
            break
    if existing is None and opener and len(opener) >= 8:
        op_prefix = opener[:24]
        for item in posts:
            if item.get("channel") == ch and str(item.get("opener") or "")[:24] == op_prefix:
                existing = item
                break
    if existing is None and ch == "youtube_shorts" and _YT_VIDEO_PID_RE.match(pid):
        in_core = _normalize_yt_title_core(opener)
        if len(in_core) >= 8:
            for item in posts:
                if item.get("channel") != "youtube_shorts":
                    continue
                cand_pid = str(item.get("post_id") or "")
                if _YT_VIDEO_PID_RE.match(cand_pid):
                    continue
                cand_core = _normalize_yt_title_core(str(item.get("opener") or item.get("text_snippet") or ""))
                if len(cand_core) >= 8 and (in_core[:12] in cand_core or cand_core[:12] in in_core):
                    existing = item
                    break

    if existing is not None:
        prev_style = str(existing.get("style") or "").strip()
        prev_rt = str(existing.get("reel_type") or "").strip()
        preserve_moshimo = (
            source in ("ig_graph_api", "youtube_api")
            and (prev_style.startswith("moshimo_") or prev_rt == "moshimo_3d")
        )
        if pid:
            existing["post_id"] = pid
        if ch in ("reels", "youtube_shorts"):
            existing["channel"] = ch
        if opener and (not existing.get("opener") or source == "youtube_api"):
            existing["opener"] = opener
        if snippet and len(snippet) > len(str(existing.get("text_snippet") or "")):
            existing["text_snippet"] = snippet
        if preserve_moshimo:
            if prev_style.startswith("moshimo_"):
                existing["style"] = prev_style
            existing["reel_type"] = "moshimo_3d"
        else:
            if style:
                existing["style"] = str(style).strip()[:48]
            if norm_rt:
                existing["reel_type"] = norm_rt
        if slot_label:
            existing["slot_label"] = str(slot_label).strip()[:24]
        if s_tag:
            existing["s_param"] = s_tag
        if hook_arch and (not existing.get("hook_archetype") or text):
            existing["hook_archetype"] = hook_arch
        if permalink:
            existing["permalink"] = str(permalink).strip()[:180]
        if published_at:
            existing["published_at"] = pub_ts
        merged_m = dict(existing.get("metrics") or {})
        merged_m.update(clean_m)
        existing["metrics"] = merged_m
        existing["source"] = source or existing.get("source") or "publish"
        existing["updated_at"] = ts_now
        return existing

    rec = {
        "channel": ch,
        "post_id": pid,
        "published_at": pub_ts,
        "updated_at": ts_now,
        "slot_label": str(slot_label or "").strip()[:24],
        "style": str(style or "").strip()[:48],
        "reel_type": norm_rt,
        "hook_archetype": hook_arch or "time_wave",
        "opener": opener,
        "text_snippet": snippet,
        "s_param": s_tag,
        "permalink": str(permalink or "").strip()[:180],
        "source": source,
        "metrics": clean_m,
    }
    posts.insert(0, rec)
    return rec


def register_published_post(
    channel: str,
    post_id: str = "",
    text: str = "",
    style: str = "",
    reel_type: str = "",
    slot_label: str = "",
    s_param: str = "",
    now_jst: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """
    Register a newly published SNS post (Threads / Instagram Reel / Instagram Feed / X)
    so later Graph API syncs or screenshot ingestions map `post_id` / `opener` back to
    the exact `style`, `reel_type`, `slot_label`, and `hook_archetype`.
    """
    try:
        store = _load_store(allow_blocking=True)
        rec = _upsert_post_record(
            store=store,
            channel=channel,
            post_id=post_id,
            text=text,
            style=style,
            reel_type=reel_type,
            slot_label=slot_label,
            s_param=s_param,
            published_at=_now_jst_str(now_jst),
            source="publish",
            now_jst=now_jst,
        )
        _save_store(store, now_jst=now_jst)
        return rec
    except Exception as e:
        logger.warning(f"[sns_insights_loop] register_published_post skipped ({channel}): {e}")
        return None


def _normalize_meta_timestamp(raw_ts: str) -> str:
    s = str(raw_ts or "").strip()
    if not s:
        return _now_jst_str()
    try:
        iso_s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s.replace("Z", "+00:00"))
        dt = datetime.datetime.fromisoformat(iso_s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s[:19]


def _parse_meta_insights_data(data_list: Any) -> Dict[str, float]:
    """Parse Meta Graph API `insights` payload (`data: [{name, values: [{value}]} | {name, total_value: {value}}]`)."""
    parsed: Dict[str, float] = {}
    if not isinstance(data_list, list):
        return parsed
    for entry in data_list:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        val = None
        tot_box = entry.get("total_value")
        if isinstance(tot_box, dict) and "value" in tot_box:
            val = tot_box.get("value")
        elif isinstance(entry.get("values"), list) and entry["values"]:
            v0 = entry["values"][0]
            if isinstance(v0, dict) and "value" in v0:
                val = v0.get("value")
        elif "value" in entry:
            val = entry.get("value")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            parsed[name] = float(val)
    return parsed


def collect_threads_insights(limit: int = 15, now_jst: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """
    Pull recent Threads posts & post-level metrics via Official Meta Threads Graph API:
      1. `GET /v1.0/{user_id}/threads?fields=id,text,timestamp,media_type,permalink&limit={limit}`
      2. `GET /v1.0/{media_id}/insights?metric=views,likes,replies,reposts,quotes`
    Never logs raw tokens; handles missing `threads_manage_insights` scope gracefully.
    """
    import threads_poster

    token = threads_poster._get_active_threads_token()
    if not token:
        return {
            "ok": False,
            "channel": "threads",
            "status": "unconfigured",
            "synced": 0,
            "detail": "THREADS_ACCESS_TOKEN 未設定",
        }

    safe_limit = max(1, min(25, int(limit or 15)))
    user_id = threads_poster._resolve_threads_user_id(token)
    try:
        r_list = requests.get(
            f"{threads_poster.THREADS_API_BASE}/{user_id}/threads",
            params={
                "fields": "id,text,timestamp,media_type,permalink",
                "limit": safe_limit,
                "access_token": token,
            },
            timeout=14,
        )
        if r_list.status_code >= 400:
            return {
                "ok": False,
                "channel": "threads",
                "status": "api_error",
                "synced": 0,
                "detail": f"Threads一覧取得 HTTP {r_list.status_code}",
            }
        items = (r_list.json() or {}).get("data") or []
    except Exception as e:
        return {
            "ok": False,
            "channel": "threads",
            "status": "network_error",
            "synced": 0,
            "detail": f"Threads通信エラー: {type(e).__name__}",
        }

    store = _load_store(allow_blocking=True)
    # Cross-link recent editorial history by opener if post_id wasn't saved yet
    try:
        import sns_editorial
        ed_hist = sns_editorial.get_recent_editorial_history("threads", limit=12, allow_blocking=False) or []
    except Exception:
        ed_hist = []

    synced_count = 0
    insights_ok_count = 0
    scope_warning = ""

    for media in items[:safe_limit]:
        if not isinstance(media, dict):
            continue
        mid = str(media.get("id") or "").strip()
        if not mid:
            continue
        txt = str(media.get("text") or "")
        pub_at = _normalize_meta_timestamp(str(media.get("timestamp") or ""))
        permalink = str(media.get("permalink") or "")

        matched_style = ""
        matched_slot = ""
        op_head = _extract_opener(txt, max_len=36)[:20]
        for h in ed_hist:
            if not isinstance(h, dict):
                continue
            if mid and str(h.get("post_id") or "") == mid:
                matched_style = str(h.get("selected_style") or h.get("style") or "")
                matched_slot = str(h.get("slot_label") or "")
                break
            h_txt = str(h.get("selected_text") or h.get("opener") or "")
            if op_head and len(op_head) >= 8 and _extract_opener(h_txt, max_len=36)[:20] == op_head:
                matched_style = str(h.get("selected_style") or h.get("style") or "")
                matched_slot = str(h.get("slot_label") or "")
                break

        metrics_map: Dict[str, Any] = {}
        try:
            r_ins = requests.get(
                f"{threads_poster.THREADS_API_BASE}/{mid}/insights",
                params={
                    "metric": "views,likes,replies,reposts,quotes",
                    "access_token": token,
                },
                timeout=10,
            )
            if r_ins.status_code == 200:
                raw_vals = _parse_meta_insights_data((r_ins.json() or {}).get("data"))
                for k in ("views", "likes", "replies", "reposts", "quotes"):
                    if k in raw_vals:
                        metrics_map[k] = int(round(raw_vals[k]))
                if metrics_map:
                    insights_ok_count += 1
            elif r_ins.status_code in (400, 403) and not scope_warning:
                scope_warning = f"Threads Insights権限不足または対象外 (HTTP {r_ins.status_code})"
        except Exception:
            pass

        _upsert_post_record(
            store=store,
            channel="threads",
            post_id=mid,
            text=txt,
            style=matched_style,
            slot_label=matched_slot,
            published_at=pub_at,
            permalink=permalink,
            source="threads_graph_api",
            metrics=metrics_map,
            now_jst=now_jst,
        )
        synced_count += 1

    _save_store(store, now_jst=now_jst)
    return {
        "ok": True,
        "channel": "threads",
        "status": "ok" if insights_ok_count > 0 or synced_count == 0 else "partial_no_insights_scope",
        "synced": synced_count,
        "updated": synced_count,
        "insights_fetched": insights_ok_count,
        "detail": scope_warning or f"Threads投稿 {synced_count}件（インサイト数値 {insights_ok_count}件）を同期",
    }


def collect_instagram_insights(limit: int = 15, now_jst: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """
    Pull recent Instagram Reels & Feed media + insights via Official Meta Instagram Graph API:
      1. `GET /{ig_user_id}/media?fields=id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count`
      2. For Reels (`media_product_type == 'REELS'`):
         `GET /{media_id}/insights?metric=views,reach,saved,shares,ig_reels_avg_watch_time,ig_reels_video_view_total_time`
         (with automatic fallback to `views,reach,saved,shares` if watch-time metrics are unavailable on low-view clips).
    """
    import instagram_poster

    token = instagram_poster._get_active_ig_token()
    if not token:
        return {
            "ok": False,
            "channel": "instagram",
            "status": "unconfigured",
            "synced": 0,
            "detail": "INSTAGRAM_ACCESS_TOKEN 未設定",
        }

    safe_limit = max(1, min(25, int(limit or 15)))
    base = instagram_poster._graph_base_for_token(token)
    ig_uid = instagram_poster._resolve_ig_user_id(token)

    try:
        r_list = requests.get(
            f"{base}/{ig_uid}/media",
            params={
                "fields": "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
                "limit": safe_limit,
                "access_token": token,
            },
            timeout=14,
        )
        if r_list.status_code >= 400:
            return {
                "ok": False,
                "channel": "instagram",
                "status": "api_error",
                "synced": 0,
                "detail": f"Instagramメディア一覧 HTTP {r_list.status_code}",
            }
        items = (r_list.json() or {}).get("data") or []
    except Exception as e:
        return {
            "ok": False,
            "channel": "instagram",
            "status": "network_error",
            "synced": 0,
            "detail": f"Instagram通信エラー: {type(e).__name__}",
        }

    store = _load_store(allow_blocking=True)
    synced_count = 0
    reels_insights_count = 0
    scope_warning = ""

    for media in items[:safe_limit]:
        if not isinstance(media, dict):
            continue
        mid = str(media.get("id") or "").strip()
        if not mid:
            continue
        caption = str(media.get("caption") or "")
        m_prod = str(media.get("media_product_type") or "").upper()
        m_type = str(media.get("media_type") or "").upper()
        is_reel = (m_prod == "REELS") or (m_type == "VIDEO" and m_prod != "FEED")
        ch_key = "reels" if is_reel else "instagram"
        pub_at = _normalize_meta_timestamp(str(media.get("timestamp") or ""))
        permalink = str(media.get("permalink") or "")

        metrics_map: Dict[str, Any] = {}
        if media.get("like_count") is not None:
            metrics_map["likes"] = _safe_nonneg_int(media.get("like_count"))
        if media.get("comments_count") is not None:
            metrics_map["comments"] = _safe_nonneg_int(media.get("comments_count"))

        metric_queries = (
            (
                "views,reach,saved,shares,ig_reels_avg_watch_time,ig_reels_video_view_total_time",
                "views,reach,saved,shares",
            )
            if is_reel
            else ("views,reach,saved,shares",)
        )
        for mq in metric_queries:
            try:
                r_ins = requests.get(
                    f"{base}/{mid}/insights",
                    params={"metric": mq, "access_token": token},
                    timeout=10,
                )
                if r_ins.status_code == 200:
                    raw_vals = _parse_meta_insights_data((r_ins.json() or {}).get("data"))
                    for k in ("views", "reach", "saved", "shares"):
                        if k in raw_vals:
                            metrics_map[k] = int(round(raw_vals[k]))
                    if "ig_reels_avg_watch_time" in raw_vals:
                        raw_watch = float(raw_vals["ig_reels_avg_watch_time"])
                        # Meta Graph API returns `ig_reels_avg_watch_time` in milliseconds (e.g., 2150 -> 2.15s)
                        watch_sec = (raw_watch / 1000.0) if raw_watch >= 100.0 else raw_watch
                        if 0.0 < watch_sec <= 600.0:
                            metrics_map["avg_watch_sec"] = round(watch_sec, 2)
                    if "ig_reels_video_view_total_time" in raw_vals:
                        raw_tot = float(raw_vals["ig_reels_video_view_total_time"])
                        tot_sec = (raw_tot / 1000.0) if raw_tot >= 1000.0 else raw_tot
                        if tot_sec >= 0.0:
                            metrics_map["total_watch_sec"] = round(tot_sec, 1)
                    if is_reel and ("views" in metrics_map or "avg_watch_sec" in metrics_map):
                        reels_insights_count += 1
                    break
                elif r_ins.status_code in (400, 403) and mq == metric_queries[-1] and not scope_warning:
                    scope_warning = f"IG Insights一部未取得 (HTTP {r_ins.status_code})"
            except Exception:
                break

        inferred_rt = infer_reel_type_from_caption(caption) if is_reel else ""
        _upsert_post_record(
            store=store,
            channel=ch_key,
            post_id=mid,
            text=caption,
            style=f"reel_{inferred_rt}" if is_reel else "",
            reel_type=inferred_rt,
            published_at=pub_at,
            permalink=permalink,
            source="ig_graph_api",
            metrics=metrics_map,
            now_jst=now_jst,
        )
        synced_count += 1

    _save_store(store, now_jst=now_jst)
    return {
        "ok": True,
        "channel": "instagram",
        "status": "ok",
        "synced": synced_count,
        "updated": synced_count,
        "reels_insights_fetched": reels_insights_count,
        "detail": scope_warning or f"Instagram/Reels {synced_count}件（リール維持率データ {reels_insights_count}件）を同期",
    }


def collect_youtube_shorts_insights(limit: int = 20, now_jst: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """
    Pull recent YouTube Shorts + retention metrics via Official YouTube Data API v3 & Analytics API v2
    (`youtube_analytics_sync.fetch_youtube_shorts_metrics`).
    Skips cleanly without error when OAuth credentials are not yet connected.
    """
    import youtube_analytics_sync

    res = youtube_analytics_sync.fetch_youtube_shorts_metrics(limit=limit, days=90, now_jst=now_jst)
    if not res.get("ok"):
        return {
            "ok": False,
            "channel": "youtube_shorts",
            "status": str(res.get("status") or "unconfigured"),
            "synced": 0,
            "updated": 0,
            "detail": str(res.get("detail") or "YouTube OAuth未設定"),
        }

    store = _load_store(allow_blocking=True)
    synced_count = 0
    for post in res.get("posts") or []:
        if not isinstance(post, dict):
            continue
        pid = str(post.get("post_id") or "").strip()
        title = str(post.get("title") or "").strip()
        desc = str(post.get("description") or "").strip()
        if not pid or not title:
            continue
        combined_txt = f"{title}\n{desc}".strip() if desc else title
        inferred_rt = infer_reel_type_from_caption(combined_txt)
        pub_at = _normalize_meta_timestamp(str(post.get("published_at") or ""))
        _upsert_post_record(
            store=store,
            channel="youtube_shorts",
            post_id=pid,
            text=title,
            style=f"reel_{inferred_rt}",
            reel_type=inferred_rt,
            published_at=pub_at,
            permalink=str(post.get("permalink") or ""),
            source="youtube_api",
            metrics=dict(post.get("metrics") or {}),
            now_jst=now_jst,
        )
        synced_count += 1

    if synced_count > 0:
        _save_store(store, now_jst=now_jst)

    return {
        "ok": True,
        "channel": "youtube_shorts",
        "status": str(res.get("status") or "ok"),
        "synced": synced_count,
        "updated": synced_count,
        "analytics_fetched": int(res.get("analytics_fetched") or 0),
        "detail": str(res.get("detail") or f"YouTubeショート {synced_count}本を同期"),
    }


def sync_all_sns_insights(limit: int = 15, now_jst: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Run automated official API insights sync across Threads + Instagram/Reels + YouTube Shorts and refresh loop summary."""
    th_res = collect_threads_insights(limit=limit, now_jst=now_jst)
    ig_res = collect_instagram_insights(limit=limit, now_jst=now_jst)
    yt_res = collect_youtube_shorts_insights(limit=max(15, int(limit or 15)), now_jst=now_jst)

    store = _load_store(allow_blocking=True)
    sync_ts = _now_jst_str(now_jst)
    th_cnt = int(th_res.get("synced") or 0)
    ig_cnt = int(ig_res.get("synced") or 0)
    yt_cnt = int(yt_res.get("synced") or 0)
    summary = {
        "synced_at": sync_ts,
        "threads": th_res,
        "instagram": ig_res,
        "youtube_shorts": yt_res,
        "threads_updated": th_cnt,
        "instagram_updated": ig_cnt,
        "youtube_updated": yt_cnt,
        "total_synced": th_cnt + ig_cnt + yt_cnt,
    }
    store["last_sync_at"] = sync_ts
    store["last_sync_summary"] = summary
    _save_store(store, now_jst=now_jst)

    feedback = compute_loop_feedback(days=28, allow_blocking=True)
    return {
        "ok": bool(th_res.get("ok") or ig_res.get("ok") or yt_res.get("ok")),
        "sync": summary,
        "summary": summary,
        "feedback": feedback,
    }


def run_scheduled_sync_safe(limit: int = 15) -> None:
    """Exception-safe wrapper for APScheduler cron (`sns_insights_sync`)."""
    try:
        sync_all_sns_insights(limit=limit)
    except Exception as err:
        logger.warning(f"[sns_insights_loop] Scheduled SNS insights sync warning: {err}")


def _upsert_note_article(store: Dict[str, Any], item: Dict[str, Any], source: str, now_jst: Optional[datetime.datetime] = None) -> Optional[Dict[str, Any]]:
    title = _sanitize_text(str(item.get("title") or ""), max_len=95)
    if not title or len(title) < 3:
        return None
    views = _safe_nonneg_int(item.get("views") if item.get("views") is not None else item.get("pv"))
    imps = _safe_nonneg_int(item.get("impressions"))
    likes = _safe_nonneg_int(item.get("likes") if item.get("likes") is not None else item.get("ski"))
    comments = _safe_nonneg_int(item.get("comments"))
    if views is None and imps is None and likes is None:
        return None

    ctr_pct: Optional[float] = None
    if views is not None and imps is not None and imps > 0 and views <= imps:
        ctr_pct = round((views / imps) * 100.0, 2)

    has_concrete_num = bool(re.search(r"\d+|2026|最新|比較|何分|何時|待ち|料金|男女比", title))
    has_bracket_or_question = ("【" in title and "】" in title) or ("？" in title or "?" in title or "「" in title)
    title_archetype = (
        "concrete_utility"
        if (has_concrete_num and has_bracket_or_question)
        else ("numeric_insight" if has_concrete_num else "abstract_essay")
    )

    ts_now = _now_jst_str(now_jst)
    notes = store.setdefault("note_articles", [])
    for existing in notes:
        if str(existing.get("title") or "")[:36] == title[:36]:
            if views is not None:
                existing["views"] = views
            if imps is not None:
                existing["impressions"] = imps
            if likes is not None:
                existing["likes"] = likes
            if comments is not None:
                existing["comments"] = comments
            if ctr_pct is not None:
                existing["ctr_pct"] = ctr_pct
            existing["title"] = title
            existing["title_archetype"] = title_archetype
            existing["source"] = source
            existing["updated_at"] = ts_now
            return existing

    rec: Dict[str, Any] = {
        "title": title,
        "title_archetype": title_archetype,
        "views": views,
        "impressions": imps,
        "likes": likes,
        "comments": comments,
        "ctr_pct": ctr_pct,
        "source": source,
        "updated_at": ts_now,
    }
    notes.insert(0, rec)
    return rec


def _empty_gsc_store() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "source": "",
        "summary": {},
        "queries": [],
        "pages": [],
    }


def _load_gsc_store(allow_blocking: bool = True) -> Dict[str, Any]:
    now_m = time.monotonic()
    with _store_lock:
        if _gsc_cache["data"] is not None and (now_m - _gsc_cache["ts"] < 15.0):
            return json.loads(json.dumps(_gsc_cache["data"]))

    raw = job_state.get_value(GSC_STORE_KEY, allow_blocking=allow_blocking)
    gsc = _empty_gsc_store()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                gsc["updated_at"] = str(parsed.get("updated_at") or "")
                gsc["source"] = str(parsed.get("source") or "")
                if isinstance(parsed.get("summary"), dict):
                    gsc["summary"] = dict(parsed["summary"])
                if isinstance(parsed.get("queries"), list):
                    gsc["queries"] = [q for q in parsed["queries"] if isinstance(q, dict)][:MAX_GSC_QUERIES]
                if isinstance(parsed.get("pages"), list):
                    gsc["pages"] = [p for p in parsed["pages"] if isinstance(p, dict)][:MAX_GSC_PAGES]
        except Exception as e:
            logger.warning(f"[sns_insights_loop] Failed to parse GSC store JSON: {e}")

    with _store_lock:
        _gsc_cache["ts"] = now_m
        _gsc_cache["data"] = json.loads(json.dumps(gsc))
    return gsc


def _save_gsc_store(gsc: Dict[str, Any], now_jst: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    gsc["version"] = 1
    gsc["updated_at"] = _now_jst_str(now_jst)
    queries = [q for q in (gsc.get("queries") or []) if isinstance(q, dict) and q.get("query")]
    queries.sort(
        key=lambda x: (int(x.get("impressions") or 0), int(x.get("clicks") or 0)),
        reverse=True,
    )
    gsc["queries"] = queries[:MAX_GSC_QUERIES]

    pages = [p for p in (gsc.get("pages") or []) if isinstance(p, dict) and p.get("page")]
    pages.sort(
        key=lambda x: (int(x.get("impressions") or 0), int(x.get("clicks") or 0)),
        reverse=True,
    )
    gsc["pages"] = pages[:MAX_GSC_PAGES]

    serialized = json.dumps(gsc, ensure_ascii=False)
    while len(serialized) > MAX_GSC_STORE_JSON_CHARS and len(gsc["queries"]) > 10:
        gsc["queries"] = gsc["queries"][:-5]
        serialized = json.dumps(gsc, ensure_ascii=False)

    job_state.set_value(GSC_STORE_KEY, serialized)
    with _store_lock:
        _gsc_cache["ts"] = time.monotonic()
        _gsc_cache["data"] = json.loads(serialized)
        _feedback_cache["ts"] = 0.0
        _feedback_cache["data"] = None
    return gsc


def _upsert_gsc_payload(
    gsc_summary_in: Optional[Dict[str, Any]] = None,
    gsc_queries_in: Optional[List[Dict[str, Any]]] = None,
    gsc_pages_in: Optional[List[Dict[str, Any]]] = None,
    source: str = "manual_gsc",
    replace_lists: bool = False,
    now_jst: Optional[datetime.datetime] = None,
) -> int:
    """Validate and persist real Google Search Console metrics (`summary`, `queries`, `pages`)."""
    gsc = _load_gsc_store(allow_blocking=True)
    touched = 0

    summary_cur = dict(gsc.get("summary") or {})
    if isinstance(gsc_summary_in, dict):
        tc = _safe_nonneg_int(gsc_summary_in.get("total_clicks") if gsc_summary_in.get("total_clicks") is not None else gsc_summary_in.get("clicks"))
        ti = _safe_nonneg_int(gsc_summary_in.get("total_impressions") if gsc_summary_in.get("total_impressions") is not None else gsc_summary_in.get("impressions"))
        ctr = _safe_nonneg_float(gsc_summary_in.get("avg_ctr_pct") if gsc_summary_in.get("avg_ctr_pct") is not None else gsc_summary_in.get("ctr_pct"), max_val=100.0)
        pos = _safe_nonneg_float(gsc_summary_in.get("avg_position") if gsc_summary_in.get("avg_position") is not None else gsc_summary_in.get("position"), max_val=500.0)
        if tc is not None:
            summary_cur["total_clicks"] = tc
            touched += 1
        if ti is not None:
            summary_cur["total_impressions"] = ti
            touched += 1
        if ctr is not None:
            summary_cur["avg_ctr_pct"] = ctr
            touched += 1
        elif tc is not None and ti is not None and ti > 0:
            summary_cur["avg_ctr_pct"] = round((tc / float(ti)) * 100.0, 2)
        if pos is not None and pos > 0.0:
            summary_cur["avg_position"] = pos
            touched += 1

    q_map: Dict[str, Dict[str, Any]] = (
        {} if replace_lists else {str(q.get("query") or ""): dict(q) for q in (gsc.get("queries") or []) if isinstance(q, dict) and q.get("query")}
    )
    for raw_q in (gsc_queries_in or [])[:MAX_GSC_QUERIES]:
        if not isinstance(raw_q, dict):
            continue
        q_txt = _sanitize_text(str(raw_q.get("query") or raw_q.get("keyword") or ""), max_len=80)
        if not q_txt:
            continue
        clicks = _safe_nonneg_int(raw_q.get("clicks"))
        imps = _safe_nonneg_int(raw_q.get("impressions"))
        ctr = _safe_nonneg_float(raw_q.get("ctr_pct") if raw_q.get("ctr_pct") is not None else raw_q.get("ctr"), max_val=100.0)
        pos = _safe_nonneg_float(raw_q.get("position"), max_val=500.0)
        if clicks is None and imps is None:
            continue
        if ctr is None and clicks is not None and imps is not None and imps > 0:
            ctr = round((clicks / float(imps)) * 100.0, 2)
        q_map[q_txt] = {
            "query": q_txt,
            "clicks": clicks if clicks is not None else 0,
            "impressions": imps if imps is not None else 0,
            "ctr_pct": ctr,
            "position": pos,
        }
        touched += 1

    p_map: Dict[str, Dict[str, Any]] = (
        {} if replace_lists else {str(p.get("page") or ""): dict(p) for p in (gsc.get("pages") or []) if isinstance(p, dict) and p.get("page")}
    )
    for raw_p in (gsc_pages_in or [])[:MAX_GSC_PAGES]:
        if not isinstance(raw_p, dict):
            continue
        raw_url = str(raw_p.get("page") or raw_p.get("url") or raw_p.get("path") or "").strip()
        if not raw_url:
            continue
        # Normalize full URL to clean site path when possible
        clean_path = re.sub(r"^https?://[^/]+", "", raw_url).strip() or "/"
        clean_path = clean_path.split("#", 1)[0][:120]
        clicks = _safe_nonneg_int(raw_p.get("clicks"))
        imps = _safe_nonneg_int(raw_p.get("impressions"))
        ctr = _safe_nonneg_float(raw_p.get("ctr_pct") if raw_p.get("ctr_pct") is not None else raw_p.get("ctr"), max_val=100.0)
        pos = _safe_nonneg_float(raw_p.get("position"), max_val=500.0)
        if clicks is None and imps is None:
            continue
        if ctr is None and clicks is not None and imps is not None and imps > 0:
            ctr = round((clicks / float(imps)) * 100.0, 2)
        p_map[clean_path] = {
            "page": clean_path,
            "clicks": clicks if clicks is not None else 0,
            "impressions": imps if imps is not None else 0,
            "ctr_pct": ctr,
            "position": pos,
        }
        touched += 1

    if touched == 0:
        return 0

    # Derive summary totals from queries list if summary was not explicitly provided
    q_vals = list(q_map.values())
    if q_vals and (replace_lists or "total_impressions" not in summary_cur):
        if not isinstance(gsc_summary_in, dict) or gsc_summary_in.get("total_impressions") is None:
            tot_c = sum(int(x.get("clicks") or 0) for x in q_vals)
            tot_i = sum(int(x.get("impressions") or 0) for x in q_vals)
            summary_cur["total_clicks"] = tot_c
            summary_cur["total_impressions"] = tot_i
            if tot_i > 0:
                summary_cur["avg_ctr_pct"] = round((tot_c / float(tot_i)) * 100.0, 2)
            weighted_pos_num = sum(
                float(x["position"]) * max(1, int(x.get("impressions") or 1))
                for x in q_vals
                if x.get("position") is not None
            )
            weighted_pos_den = sum(
                max(1, int(x.get("impressions") or 1))
                for x in q_vals
                if x.get("position") is not None
            )
            if weighted_pos_den > 0:
                summary_cur["avg_position"] = round(weighted_pos_num / float(weighted_pos_den), 1)

    gsc["summary"] = summary_cur
    gsc["queries"] = q_vals
    gsc["pages"] = list(p_map.values())
    gsc["source"] = str(source or "manual_gsc")[:48]
    _save_gsc_store(gsc, now_jst=now_jst)
    return touched


def _parse_duration_to_seconds(raw_val: Any) -> Optional[float]:
    """Parse `0:00:24`, `0:24`, `24.5`, or `24秒` into seconds (`24.0`)."""
    if raw_val is None or isinstance(raw_val, bool):
        return None
    s = str(raw_val).strip()
    if not s:
        return None
    if ":" in s:
        parts = [p.strip() for p in s.split(":")]
        try:
            if len(parts) == 3:
                sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                sec = int(parts[0]) * 60 + float(parts[1])
            else:
                return None
            return round(sec, 2) if 0.0 < sec <= 600.0 else None
        except (TypeError, ValueError):
            return None
    return _safe_nonneg_float(s, max_val=600.0)


def ingest_manual_insight_records(
    payload: Dict[str, Any],
    source: str = "manual_json",
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Ingest validated post / Reel / YouTube Shorts / note / Google Search Console insight items
    from structured JSON or Carbon Vision output.
    Accepts `{"posts": [...], "note_articles": [...], "gsc_summary": {...}, "gsc_queries": [...]}`
    or a single flat metric dict.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "不正なデータ形式です", "ingested_posts": 0, "ingested_notes": 0}

    store = _load_store(allow_blocking=True)
    posts_in = payload.get("posts")
    notes_in = payload.get("note_articles")
    gsc_summary_in = payload.get("gsc_summary") if isinstance(payload.get("gsc_summary"), dict) else None
    gsc_queries_in = payload.get("gsc_queries") if isinstance(payload.get("gsc_queries"), list) else None
    gsc_pages_in = payload.get("gsc_pages") if isinstance(payload.get("gsc_pages"), list) else None

    det_plat = str(payload.get("detected_platform") or payload.get("platform") or payload.get("channel") or "").strip().lower()
    if det_plat in ("gsc", "search_console", "google_search_console") and gsc_summary_in is None:
        if any(k in payload for k in ("total_clicks", "total_impressions", "clicks", "impressions")):
            gsc_summary_in = payload

    # Convenience: allow single-record submission without wrapping in `posts` / `note_articles`
    if posts_in is None and notes_in is None and gsc_summary_in is None and not gsc_queries_in and not gsc_pages_in:
        if det_plat == "note" or ("title" in payload and not payload.get("text") and not payload.get("opener") and det_plat not in ("youtube_shorts", "yt_shorts", "shorts", "youtube")):
            notes_in = [payload]
            posts_in = []
        else:
            posts_in = [payload]
            notes_in = []

    ingested_posts = 0
    ingested_notes = 0
    ingested_gsc = 0

    if gsc_summary_in is not None or gsc_queries_in or gsc_pages_in:
        ingested_gsc = _upsert_gsc_payload(
            gsc_summary_in=gsc_summary_in,
            gsc_queries_in=gsc_queries_in,
            gsc_pages_in=gsc_pages_in,
            source=source,
            now_jst=now_jst,
        )

    for p in (posts_in or [])[:30]:
        if not isinstance(p, dict):
            continue
        ch = str(p.get("channel") or p.get("platform") or det_plat or "threads").strip().lower()
        if ch in ("ig_reel", "ig_reels", "reel", "instagram_reel"):
            ch = "reels"
        elif ch in ("youtube_shorts", "yt_shorts", "shorts", "youtube", "yt"):
            ch = "youtube_shorts"
        elif ch == "note":
            n_rec = _upsert_note_article(store, p, source=source, now_jst=now_jst)
            if n_rec:
                ingested_notes += 1
            continue
        elif ch in ("gsc", "search_console"):
            continue
        elif ch not in ("threads", "instagram", "reels", "x", "tiktok", "youtube_shorts"):
            ch = "threads"

        raw_txt = str(p.get("text") or p.get("opener") or p.get("title") or p.get("caption") or "").strip()
        metrics_in = dict(p.get("metrics") or {})
        for flat_k in (
            "views",
            "reach",
            "likes",
            "replies",
            "reposts",
            "quotes",
            "saved",
            "shares",
            "comments",
            "bookmarks",
            "link_clicks",
            "impressions",
            "subscribers_gained",
            "avg_watch_sec",
            "total_watch_sec",
            "skip_rate_pct",
            "typical_skip_rate_pct",
            "share_rate_pct",
            "stayed_to_watch_pct",
            "avg_pct_viewed",
        ):
            if flat_k in p and p.get(flat_k) is not None and flat_k not in metrics_in:
                metrics_in[flat_k] = p.get(flat_k)

        if isinstance(metrics_in.get("avg_watch_sec"), str) and ":" in str(metrics_in.get("avg_watch_sec")):
            metrics_in["avg_watch_sec"] = _parse_duration_to_seconds(metrics_in.get("avg_watch_sec"))

        clean_m = _clean_metrics_dict(metrics_in)
        if not clean_m and not raw_txt:
            continue

        _upsert_post_record(
            store=store,
            channel=ch,
            post_id=str(p.get("post_id") or p.get("id") or ""),
            text=raw_txt,
            style=str(p.get("style") or ""),
            reel_type=str(p.get("reel_type") or ""),
            slot_label=str(p.get("slot_label") or ""),
            s_param=str(p.get("s_param") or ""),
            published_at=str(p.get("published_at") or ""),
            source=source,
            metrics=clean_m,
            now_jst=now_jst,
        )
        ingested_posts += 1

    for n in (notes_in or [])[:30]:
        if not isinstance(n, dict):
            continue
        n_rec = _upsert_note_article(store, n, source=source, now_jst=now_jst)
        if n_rec:
            ingested_notes += 1

    if ingested_posts == 0 and ingested_notes == 0 and ingested_gsc == 0:
        return {
            "ok": False,
            "error": "有効なインサイト数値（再生数・スキップ率・ビュー数・検索表示回数など）が見つかりませんでした",
            "ingested_posts": 0,
            "ingested_notes": 0,
            "ingested_gsc": 0,
            "posts_ingested": 0,
            "notes_ingested": 0,
        }

    if ingested_posts > 0 or ingested_notes > 0:
        _save_store(store, now_jst=now_jst)
    feedback = compute_loop_feedback(days=28, allow_blocking=True)
    return {
        "ok": True,
        "source": source,
        "ingested_posts": ingested_posts,
        "ingested_notes": ingested_notes,
        "ingested_gsc": ingested_gsc,
        "posts_ingested": ingested_posts,
        "notes_ingested": ingested_notes,
        "feedback": feedback,
    }


def _decode_csv_bytes_best_effort(raw_bytes: bytes) -> str:
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def ingest_zip_insights(
    zip_bytes: bytes,
    platform_hint: str = "auto",
    filename: str = "",
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Directly parse exported ZIP archives from Google Search Console (`Performance-on-Search-*.zip`)
    or YouTube Studio (`*.zip` containing `表データ.csv` / `Table data.csv`) with strict zip-bomb bounds.
    """
    import zipfile

    if not zip_bytes:
        return {"ok": False, "error": "ZIPファイルが空です"}
    if len(zip_bytes) > 4 * 1024 * 1024:
        return {"ok": False, "error": "ZIPファイルサイズは4MB以内にしてください"}
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception:
        return {"ok": False, "error": "有効なZIPアーカイブとして読み取れませんでした"}

    with zf:
        infos = [inf for inf in zf.infolist() if not inf.is_dir() and inf.filename.lower().endswith(".csv")]
        if not infos:
            return {"ok": False, "error": "ZIP内にCSVファイルが見つかりませんでした"}
        if len(infos) > 20:
            return {"ok": False, "error": "ZIP内のファイル数が上限（20件）を超えています"}

        csv_map: Dict[str, List[List[str]]] = {}
        raw_text_map: Dict[str, str] = {}
        for inf in infos:
            if inf.file_size > 2 * 1024 * 1024:
                continue
            base_name = inf.filename.rsplit("/", 1)[-1].strip()
            txt = _decode_csv_bytes_best_effort(zf.read(inf)).lstrip("\ufeff").strip()
            if not txt:
                continue
            raw_text_map[base_name] = txt
            parsed_rows = [r for r in csv.reader(io.StringIO(txt)) if r and any(str(c).strip() for c in r)]
            if len(parsed_rows) >= 2:
                csv_map[base_name] = parsed_rows

    if not csv_map:
        return {"ok": False, "error": "ZIP内のCSVから有効なデータ行を抽出できませんでした"}

    # Check if this is a YouTube Studio ZIP export (`表データ.csv` / `Table data.csv`)
    for bname, btxt in raw_text_map.items():
        blow = bname.lower()
        if "table data" in blow or "表データ" in bname:
            return ingest_csv_insights(btxt, platform_hint="youtube_shorts", filename=bname, now_jst=now_jst)

    # Parse Google Search Console multi-CSV ZIP (`クエリ.csv` + `ページ.csv` + `デバイス.csv` / `国.csv` / `チャート.csv`)
    queries_parsed: List[Dict[str, Any]] = []
    pages_parsed: List[Dict[str, Any]] = []
    summary_totals: Dict[str, Any] = {}

    def _col_idx(hdrs_low: List[str], cands: Tuple[str, ...]) -> int:
        for idx, h in enumerate(hdrs_low):
            for c in cands:
                if c in h:
                    return idx
        return -1

    for bname, rows in csv_map.items():
        blow = bname.lower()
        hdrs_low = [str(x or "").strip().lower() for x in rows[0]]
        c_col = _col_idx(hdrs_low, ("クリック数", "clicks"))
        i_col = _col_idx(hdrs_low, ("表示回数", "impressions"))
        ctr_col = _col_idx(hdrs_low, ("ctr",))
        pos_col = _col_idx(hdrs_low, ("掲載順位", "position"))
        if c_col < 0 or i_col < 0:
            continue

        if any(k in blow for k in ("クエリ", "queries", "query")):
            for r in rows[1:200]:
                label = str(r[0] or "").strip() if r else ""
                if not label or label.lower() in ("合計", "total", "totals"):
                    continue
                queries_parsed.append({
                    "query": label,
                    "clicks": (_safe_nonneg_int(r[c_col]) if c_col < len(r) else 0) or 0,
                    "impressions": (_safe_nonneg_int(r[i_col]) if i_col < len(r) else 0) or 0,
                    "ctr_pct": _safe_nonneg_float(r[ctr_col], max_val=100.0) if 0 <= ctr_col < len(r) else None,
                    "position": _safe_nonneg_float(r[pos_col], max_val=500.0) if 0 <= pos_col < len(r) else None,
                })
        elif any(k in blow for k in ("ページ", "pages", "page")):
            for r in rows[1:200]:
                label = str(r[0] or "").strip() if r else ""
                if not label or label.lower() in ("合計", "total", "totals"):
                    continue
                pages_parsed.append({
                    "page": label,
                    "clicks": (_safe_nonneg_int(r[c_col]) if c_col < len(r) else 0) or 0,
                    "impressions": (_safe_nonneg_int(r[i_col]) if i_col < len(r) else 0) or 0,
                    "ctr_pct": _safe_nonneg_float(r[ctr_col], max_val=100.0) if 0 <= ctr_col < len(r) else None,
                    "position": _safe_nonneg_float(r[pos_col], max_val=500.0) if 0 <= pos_col < len(r) else None,
                })
        elif any(k in blow for k in ("デバイス", "devices", "国", "countries", "日付", "dates", "チャート", "chart")):
            # Devices / Countries / Chart CSVs carry exact un-thresholded site totals
            tot_c = 0
            tot_i = 0
            w_pos_sum = 0.0
            w_pos_imps = 0
            for r in rows[1:400]:
                c_v = (_safe_nonneg_int(r[c_col]) if c_col < len(r) else 0) or 0
                i_v = (_safe_nonneg_int(r[i_col]) if i_col < len(r) else 0) or 0
                p_v = _safe_nonneg_float(r[pos_col], max_val=500.0) if 0 <= pos_col < len(r) else None
                tot_c += c_v
                tot_i += i_v
                if p_v and p_v > 0 and i_v > 0:
                    w_pos_sum += p_v * i_v
                    w_pos_imps += i_v
            prev_i = int(summary_totals.get("total_impressions") or -1)
            is_device_file = any(k in blow for k in ("デバイス", "devices"))
            if tot_i > prev_i or (is_device_file and tot_i >= prev_i and tot_i > 0):
                summary_totals = {
                    "total_clicks": tot_c,
                    "total_impressions": tot_i,
                    "avg_ctr_pct": round((tot_c / float(tot_i)) * 100.0, 2) if tot_i > 0 else 0.0,
                    "avg_position": round(w_pos_sum / float(w_pos_imps), 2) if w_pos_imps > 0 else None,
                }

    touched = _upsert_gsc_payload(
        gsc_summary_in=summary_totals or None,
        gsc_queries_in=queries_parsed or None,
        gsc_pages_in=pages_parsed or None,
        source="gsc_zip",
        replace_lists=True,
        now_jst=now_jst,
    )
    if touched == 0:
        return {"ok": False, "error": "Search Console ZIPから有効な検索パフォーマンスデータを抽出できませんでした"}
    feedback = compute_loop_feedback(days=28, allow_blocking=True)
    return {
        "ok": True,
        "detected_platform": "gsc",
        "source": "gsc_zip",
        "ingested_gsc": touched,
        "queries_ingested": len(queries_parsed),
        "pages_ingested": len(pages_parsed),
        "ingested_posts": 0,
        "ingested_notes": 0,
        "feedback": feedback,
    }


def ingest_csv_insights(
    csv_text: str,
    platform_hint: str = "auto",
    filename: str = "",
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Directly parse exported CSV files (zero API token required, zero hallucination):
      1. Google Search Console CSV (`クエリ.csv` / `Queries.csv` / `ページ.csv` / `Pages.csv`)
      2. YouTube Studio Analytics CSV (`表データ.csv` / `Table data.csv` with Shorts retention metrics)
    """
    raw = str(csv_text or "").lstrip("\ufeff").strip()
    if not raw:
        return {"ok": False, "error": "CSVデータが空です"}

    reader = csv.reader(io.StringIO(raw))
    rows = [r for r in reader if r and any(str(c).strip() for c in r)]
    if len(rows) < 2:
        return {"ok": False, "error": "CSVに見出し行とデータ行が含まれていません"}

    headers = [str(h or "").strip() for h in rows[0]]
    headers_low = [h.lower() for h in headers]
    hint = str(platform_hint or "auto").strip().lower()
    fname_low = str(filename or "").strip().lower()

    def _find_col(candidates: Tuple[str, ...]) -> int:
        for idx, h_low in enumerate(headers_low):
            for cand in candidates:
                if cand in h_low:
                    return idx
        return -1

    # Detect Google Search Console CSV vs YouTube Studio CSV
    gsc_query_col = _find_col(("上位のクエリ", "クエリ", "top queries", "query", "queries"))
    gsc_page_col = _find_col(("上位のページ", "ページ", "top pages", "page", "pages"))
    gsc_clicks_col = _find_col(("クリック数", "clicks"))
    gsc_imps_col = _find_col(("表示回数", "impressions"))
    gsc_ctr_col = _find_col(("ctr",))
    gsc_pos_col = _find_col(("掲載順位", "position"))

    yt_title_col = _find_col(("動画のタイトル", "video title", "タイトル"))
    yt_content_col = _find_col(("コンテンツ", "content", "動画 id", "video id"))
    yt_views_col = _find_col(("視聴回数", "views", "エンゲージ ビュー"))
    yt_imps_col = _find_col(("インプレッション数", "impressions"))
    yt_stayed_col = _find_col(("視聴を選択", "viewed (%)", "stayed to watch", "フィードで表示"))
    yt_pct_col = _find_col(("平均再生率", "average percentage viewed", "平均視聴率"))
    yt_dur_col = _find_col(("平均視聴時間", "average view duration"))
    yt_likes_col = _find_col(("高評価数", "likes"))
    yt_subs_col = _find_col(("チャンネル登録者", "subscribers"))
    yt_pub_col = _find_col(("動画公開時刻", "video publish time", "公開日"))

    is_gsc = (
        hint in ("gsc", "search_console", "google_search_console")
        or (gsc_clicks_col >= 0 and gsc_imps_col >= 0 and (gsc_query_col >= 0 or gsc_page_col >= 0 or gsc_pos_col >= 0))
    )
    is_yt = (
        hint in ("youtube_shorts", "yt_shorts", "shorts", "youtube")
        or (yt_views_col >= 0 and (yt_title_col >= 0 or yt_stayed_col >= 0 or yt_pct_col >= 0 or "youtube" in fname_low))
    )

    if is_gsc and not hint.startswith("yt") and hint != "youtube_shorts":
        queries_parsed: List[Dict[str, Any]] = []
        pages_parsed: List[Dict[str, Any]] = []
        key_col = gsc_query_col if gsc_query_col >= 0 else (gsc_page_col if gsc_page_col >= 0 else 0)
        is_page_mode = gsc_page_col >= 0 and gsc_query_col < 0

        for r in rows[1:200]:
            if key_col >= len(r):
                continue
            label = str(r[key_col] or "").strip()
            if not label or label.lower() in ("合計", "total", "totals"):
                continue
            clicks = _safe_nonneg_int(r[gsc_clicks_col]) if 0 <= gsc_clicks_col < len(r) else 0
            imps = _safe_nonneg_int(r[gsc_imps_col]) if 0 <= gsc_imps_col < len(r) else 0
            ctr = _safe_nonneg_float(r[gsc_ctr_col], max_val=100.0) if 0 <= gsc_ctr_col < len(r) else None
            pos = _safe_nonneg_float(r[gsc_pos_col], max_val=500.0) if 0 <= gsc_pos_col < len(r) else None
            if clicks is None and imps is None:
                continue
            if is_page_mode or label.startswith(("http://", "https://", "/")):
                pages_parsed.append({
                    "page": label,
                    "clicks": clicks or 0,
                    "impressions": imps or 0,
                    "ctr_pct": ctr,
                    "position": pos,
                })
            else:
                queries_parsed.append({
                    "query": label,
                    "clicks": clicks or 0,
                    "impressions": imps or 0,
                    "ctr_pct": ctr,
                    "position": pos,
                })

        touched = _upsert_gsc_payload(
            gsc_queries_in=queries_parsed or None,
            gsc_pages_in=pages_parsed or None,
            source="gsc_csv",
            replace_lists=bool(queries_parsed and not pages_parsed),
            now_jst=now_jst,
        )
        if touched == 0:
            return {"ok": False, "error": "Search Console CSVから有効なクエリまたはページ行を抽出できませんでした"}
        feedback = compute_loop_feedback(days=28, allow_blocking=True)
        return {
            "ok": True,
            "detected_platform": "gsc",
            "source": "gsc_csv",
            "ingested_gsc": touched,
            "queries_ingested": len(queries_parsed),
            "pages_ingested": len(pages_parsed),
            "ingested_posts": 0,
            "ingested_notes": 0,
            "feedback": feedback,
        }

    if is_yt:
        yt_posts: List[Dict[str, Any]] = []
        for r in rows[1:80]:
            cid = str(r[yt_content_col] or "").strip() if 0 <= yt_content_col < len(r) else ""
            title = str(r[yt_title_col] or "").strip() if 0 <= yt_title_col < len(r) else cid
            if not title or title.lower() in ("合計", "total", "totals") or cid.lower() in ("合計", "total", "totals"):
                continue
            views = _safe_nonneg_int(r[yt_views_col]) if 0 <= yt_views_col < len(r) else None
            imps = _safe_nonneg_int(r[yt_imps_col]) if 0 <= yt_imps_col < len(r) else None
            stayed = _safe_nonneg_float(r[yt_stayed_col], max_val=100.0) if 0 <= yt_stayed_col < len(r) else None
            pct_v = _safe_nonneg_float(r[yt_pct_col], max_val=300.0) if 0 <= yt_pct_col < len(r) else None
            watch_s = _parse_duration_to_seconds(r[yt_dur_col]) if 0 <= yt_dur_col < len(r) else None
            likes = _safe_nonneg_int(r[yt_likes_col]) if 0 <= yt_likes_col < len(r) else None
            subs = _safe_nonneg_int(r[yt_subs_col]) if 0 <= yt_subs_col < len(r) else None
            pub_raw = str(r[yt_pub_col] or "").strip() if 0 <= yt_pub_col < len(r) else ""
            if views is None and stayed is None and pct_v is None and imps is None:
                continue
            yt_posts.append({
                "channel": "youtube_shorts",
                "post_id": f"yt_{cid}" if cid and cid != title else "",
                "text": title,
                "reel_type": infer_reel_type_from_caption(title),
                "published_at": pub_raw[:19] if pub_raw else "",
                "views": views,
                "impressions": imps,
                "stayed_to_watch_pct": stayed,
                "avg_pct_viewed": pct_v,
                "avg_watch_sec": watch_s,
                "likes": likes,
                "subscribers_gained": subs,
            })

        if not yt_posts:
            return {"ok": False, "error": "YouTube Studio CSVから有効な動画アナリティクス行を抽出できませんでした"}
        res = ingest_manual_insight_records({"posts": yt_posts}, source="youtube_studio_csv", now_jst=now_jst)
        res["detected_platform"] = "youtube_shorts"
        return res

    return {
        "ok": False,
        "error": "未対応のCSV見出し形式です（Google Search Consoleのクエリ/ページCSV、またはYouTube Studioの表データCSVを指定してください）",
    }


def _parse_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def ingest_insight_screenshot_via_carbon(
    image_bytes: bytes,
    mime_type: str = "image/png",
    platform_hint: str = "auto",
    timeout_sec: float = 24.0,
    api_key: Optional[str] = None,
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Extract verified SNS / YouTube Shorts / Google Search Console / note insight numbers
    from an uploaded screenshot using `gemini-4-carbon` Vision, persist them, and recompute
    closed-loop directives.
    """
    if not image_bytes or not isinstance(image_bytes, (bytes, bytearray)):
        return {"ok": False, "error": "画像データが空です"}
    if len(image_bytes) > MAX_SCREENSHOT_BYTES:
        return {"ok": False, "error": "画像サイズが大きすぎます（上限 6MB 以内のスクリーンショットを指定してください）"}

    clean_mime = str(mime_type or "image/png").split(";", 1)[0].strip().lower()
    if clean_mime == "image/jpg":
        clean_mime = "image/jpeg"
    if clean_mime not in ("image/png", "image/jpeg", "image/webp"):
        return {"ok": False, "error": f"未対応の画像形式です（{clean_mime}｜PNG/JPEG/WebPに対応）"}

    import sns_editorial
    eff_key = (api_key if api_key is not None else sns_editorial.get_gemini_api_key(allow_blocking=True)).strip()
    if not eff_key:
        return {"ok": False, "error": "GEMINI_API_KEY が未設定のためスクリーンショット自動読取を実行できません"}

    hint_clean = str(platform_hint or "auto").strip().lower()[:24]
    b64_img = base64.b64encode(bytes(image_bytes)).decode("ascii")

    prompt = (
        "あなたは『相席ナビ』のマルチチャネル分析抽出エンジンです。添付されたスクリーンショット（Instagramリール / YouTube Studioショート分析 / Google Search Console検索パフォーマンス / Threads / noteアクセス状況 / Xアナリティクスのいずれか）から、"
        "画面上に**明確に表示されている実測数値のみ**を正確にJSONで抽出してください。\n"
        f"プラットフォームヒント: {hint_clean}\n\n"
        "【絶対厳守ルール（推測値の完全禁止）】\n"
        "1. 画像内に表示されていない項目は必ず `null` にしてください。推測・補完・仮数値の生成は厳禁です。\n"
        "2. Instagramリールのインサイト画面の場合：`channel` を `\"reels\"` とし、`views`（閲覧数/再生数）、`reach`、`avg_watch_sec`（平均再生秒数）、"
        "`skip_rate_pct`（スキップ率%）、`typical_skip_rate_pct`、`saved`、`shares`、動画タイトルや冒頭文字を `opener` に記載してください。\n"
        "3. YouTube Studio（ショート動画アナリティクス）画面の場合：`detected_platform` を `\"youtube_shorts\"`、`posts` 配列の `channel` を `\"youtube_shorts\"` とし、"
        "`opener`（動画タイトル）、`views`（視聴回数/エンゲージビュー）、`impressions`（フィードでの表示回数）、`stayed_to_watch_pct`（『視聴を選択』%／スワイプせず残った割合%）、"
        "`avg_pct_viewed`（平均視聴率%／平均再生率%）、`avg_watch_sec`（平均視聴秒数）、`likes`（高評価数）、`subscribers_gained`（登録者増減のプラス値）を抽出してください。\n"
        "4. Google Search Console（サーチコンソール検索パフォーマンス）画面の場合：`detected_platform` を `\"gsc\"` とし、`gsc_summary` に `total_clicks`（合計クリック数）、`total_impressions`（合計表示回数）、`avg_ctr_pct`（平均CTR%）、`avg_position`（平均掲載順位）、"
        "さらに画面内にクエリ一覧が見えていれば `gsc_queries` 配列に `{\"query\": \"...\", \"clicks\": null, \"impressions\": null, \"ctr_pct\": null, \"position\": null}` を抽出してください。\n"
        "5. ThreadsやXの場合：`channel` を `\"threads\"` または `\"x\"` とし、`opener`、`views`、`likes`、`replies`、`reposts` を抽出してください。\n"
        "6. noteのアクセス状況画面の場合：`note_articles` 配列に各記事の `title`、`views`、`impressions`、`likes` を抽出してください。\n\n"
        "以下のJSON形式のみを返してください：\n"
        "{\n"
        '  "detected_platform": "reels | youtube_shorts | gsc | threads | note | x | instagram",\n'
        '  "posts": [\n'
        '    {"channel": "reels", "opener": "", "reel_type": "", "views": null, "impressions": null, "reach": null, "avg_watch_sec": null, "skip_rate_pct": null, "typical_skip_rate_pct": null, "stayed_to_watch_pct": null, "avg_pct_viewed": null, "saved": null, "shares": null, "likes": null, "replies": null, "reposts": null, "subscribers_gained": null}\n'
        "  ],\n"
        '  "note_articles": [\n'
        '    {"title": "", "views": null, "impressions": null, "likes": null, "comments": null}\n'
        "  ],\n"
        '  "gsc_summary": {"total_clicks": null, "total_impressions": null, "avg_ctr_pct": null, "avg_position": null},\n'
        '  "gsc_queries": [\n'
        '    {"query": "", "clicks": null, "impressions": null, "ctr_pct": null, "position": null}\n'
        "  ]\n"
        "}"
    )

    env_model = (os.environ.get("GEMINI_EDITORIAL_MODEL") or "").strip()
    configured = [env_model] if env_model else []
    models_to_try: List[str] = []
    for m in configured + list(CARBON_VISION_MODELS):
        if m and m not in models_to_try:
            models_to_try.append(m)

    t0 = time.monotonic()
    last_err = ""
    for m_name in models_to_try:
        rem = timeout_sec - (time.monotonic() - t0)
        if rem <= 1.5:
            break
        gen_cfg: Dict[str, Any] = {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 2048,
        }
        low_m = m_name.lower()
        if "carbon" in low_m or "argon" in low_m or low_m.startswith("gemini-4"):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "LOW"}

        req_body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": clean_mime, "data": b64_img}},
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": gen_cfg,
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": eff_key}
        try:
            resp = requests.post(url, headers=headers, json=req_body, timeout=min(rem, 18.0))
            if resp.status_code == 200:
                cands = ((resp.json() or {}).get("candidates")) or []
                if cands:
                    parts = ((cands[0].get("content") or {}).get("parts")) or []
                    raw_txt = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                    parsed = _parse_json_object(raw_txt)
                    if parsed and isinstance(parsed, dict):
                        ingest_res = ingest_manual_insight_records(
                            parsed,
                            source=f"carbon_vision:{m_name}",
                            now_jst=now_jst,
                        )
                        ingest_res["model_used"] = m_name
                        ingest_res["detected_platform"] = str(parsed.get("detected_platform") or hint_clean)
                        ingest_res["extracted_raw"] = {
                            "posts_count": len(parsed.get("posts") or []),
                            "note_articles_count": len(parsed.get("note_articles") or []),
                            "gsc_queries_count": len(parsed.get("gsc_queries") or []),
                        }
                        return ingest_res
                    last_err = f"JSON解析失敗 ({m_name})"
                else:
                    last_err = f"空の応答 ({m_name})"
            else:
                last_err = f"HTTP {resp.status_code} ({m_name})"
        except requests.exceptions.Timeout:
            last_err = f"タイムアウト ({m_name})"
        except Exception as e:
            last_err = f"{type(e).__name__} ({m_name})"

    return {
        "ok": False,
        "error": f"スクリーンショットからの数値抽出に失敗しました（{last_err or 'timeout'}）",
    }


def _compute_reel_quality_score(metrics: Dict[str, Any], median_views: float, reel_type: str = "") -> Optional[float]:
    """
    Compute empirical 0..100 Retention Quality Score for a single Reel / Short using ONLY measured fields:
      - Watch completion (`avg_pct_viewed` if available, else `avg_watch_sec` vs template target:
        `22.0s` for 30s+ `moshimo_3d` narrative Reels vs `8.5s` for 8.5s live data Reels)
      - Survival past swipe cliff (`100 - skip_rate_pct` or `stayed_to_watch_pct`)
      - Save/Share/Like viral signal (`saved`, `shares`, `likes`)
      - View velocity vs account median (`views`)
    """
    if not metrics:
        return None
    weighted_sum = 0.0
    total_weight = 0.0

    pct_viewed = metrics.get("avg_pct_viewed")
    watch_s = metrics.get("avg_watch_sec")
    if pct_viewed is not None:
        watch_pts = min(100.0, max(0.0, float(pct_viewed)))
        weighted_sum += watch_pts * 0.40
        total_weight += 0.40
    elif watch_s is not None:
        target_sec = 22.0 if str(reel_type or "") == "moshimo_3d" else 8.5
        watch_pts = min(100.0, max(0.0, (float(watch_s) / target_sec) * 100.0))
        weighted_sum += watch_pts * 0.40
        total_weight += 0.40

    skip_pct = metrics.get("skip_rate_pct")
    stayed_pct = metrics.get("stayed_to_watch_pct")
    if skip_pct is not None:
        survival = max(0.0, 100.0 - float(skip_pct))
        skip_pts = min(100.0, (survival / 65.0) * 100.0)
        weighted_sum += skip_pts * 0.35
        total_weight += 0.35
    elif stayed_pct is not None:
        survival = max(0.0, min(100.0, float(stayed_pct)))
        skip_pts = min(100.0, (survival / 65.0) * 100.0)
        weighted_sum += skip_pts * 0.35
        total_weight += 0.35

    views = metrics.get("views")
    if views is not None and views > 0:
        saved = int(metrics.get("saved") or 0)
        shares = int(metrics.get("shares") or 0)
        likes = int(metrics.get("likes") or 0)
        subs = int(metrics.get("subscribers_gained") or 0)
        viral_rate = (saved * 2.0 + shares * 3.0 + likes * 0.6 + subs * 4.0) / float(max(1, views))
        viral_pts = min(100.0, viral_rate * 1200.0)
        weighted_sum += viral_pts * 0.15
        total_weight += 0.15

        if median_views > 0:
            rel_views = float(views) / float(median_views)
            view_pts = min(100.0, max(10.0, rel_views * 50.0))
            weighted_sum += view_pts * 0.10
            total_weight += 0.10

    if total_weight <= 0.0:
        return None
    return round(weighted_sum / total_weight, 1)


def compute_loop_feedback(days: int = 28, allow_blocking: bool = False) -> Dict[str, Any]:
    """
    Compute empirical closed-loop feedback from real SNS, YouTube Shorts, Search Console & note insights:
      1. `hook_archetype_boosts` (`dict[str, int]`, -4 .. +5) + `sns_prompt_feedback` (str)
      2. `sns_style_boosts_delta` (`dict[str, int]`, -2 .. +3)
      3. `reel_template_boosts` (`dict[str, float]`) via UCB1 Multi-Armed Bandit + `reel_diagnostics`
      4. `youtube_shorts_diagnostics` (YouTube Studio retention & views)
      5. `gsc_diagnostics` (Google Search Console clicks, impressions, CTR, opportunity queries)
      6. `note_ctr_feedback` (dict & prompt guidance)
    Never fabricates numbers when samples are missing (`data_status="insufficient"`).
    """
    safe_days = max(1, min(90, int(days or 28)))
    now_m = time.monotonic()
    with _store_lock:
        if (
            _feedback_cache["data"] is not None
            and _feedback_cache["days"] == safe_days
            and (now_m - _feedback_cache["ts"] < 15.0)
        ):
            return json.loads(json.dumps(_feedback_cache["data"]))

    store = _load_store(allow_blocking=allow_blocking)
    gsc_store = _load_gsc_store(allow_blocking=allow_blocking)
    cutoff_dt = datetime.datetime.now(JST) - datetime.timedelta(days=safe_days)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d")

    posts = [
        p for p in (store.get("posts") or [])
        if isinstance(p, dict) and str(p.get("published_at") or p.get("updated_at") or "9999")[:10] >= cutoff_str
    ]
    note_articles = [n for n in (store.get("note_articles") or []) if isinstance(n, dict)]

    # ------------------------------------------------------------------
    # Loop 1: Threads / X Opening Hook Archetype Lift & Prompt Feedback
    # ------------------------------------------------------------------
    text_posts_with_views = [
        p for p in posts
        if p.get("channel") in ("threads", "x")
        and isinstance(p.get("metrics"), dict)
        and p["metrics"].get("views") is not None
    ]

    hook_stats: Dict[str, Dict[str, Any]] = {}
    hook_archetype_boosts: Dict[str, int] = {}
    sns_style_boosts_delta: Dict[str, int] = {}
    sns_prompt_feedback = ""

    if text_posts_with_views:
        all_views = [int(p["metrics"]["views"]) for p in text_posts_with_views]
        overall_avg_views = sum(all_views) / float(len(all_views))
        overall_med_views = float(statistics.median(all_views))
        baseline_views = max(1.0, overall_avg_views)

        by_arch: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
        by_style: Dict[str, List[int]] = collections.defaultdict(list)
        for p in text_posts_with_views:
            arch = str(p.get("hook_archetype") or classify_opening_hook_archetype(p.get("opener") or ""))
            by_arch[arch].append(p)
            st = str(p.get("style") or "").strip()
            if st:
                by_style[st].append(int(p["metrics"]["views"]))

        for arch, plist in by_arch.items():
            v_list = [int(x["metrics"]["views"]) for x in plist]
            avg_v = round(sum(v_list) / float(len(v_list)), 1)
            lift = round(avg_v / baseline_views, 2)
            boost = 0
            if len(text_posts_with_views) >= 3:
                if lift >= 1.55 and avg_v >= 25:
                    boost = 5 if lift >= 2.0 else 4
                elif lift >= 1.15 and avg_v >= 15:
                    boost = 2
                elif lift <= 0.50:
                    boost = -4
                elif lift <= 0.78:
                    boost = -2
            if boost != 0:
                hook_archetype_boosts[arch] = boost
            hook_stats[arch] = {
                "archetype": arch,
                "label": HOOK_ARCHETYPE_LABELS.get(arch, arch),
                "sample_count": len(v_list),
                "avg_views": avg_v,
                "max_views": max(v_list),
                "lift_ratio": lift,
                "score_boost": boost,
            }

        if len(text_posts_with_views) >= 3:
            for st_k, sv_list in by_style.items():
                st_avg = sum(sv_list) / float(len(sv_list))
                if st_avg >= baseline_views * 1.35 and st_avg >= 20:
                    sns_style_boosts_delta[st_k] = 3
                elif st_avg <= baseline_views * 0.55 and len(sv_list) >= 2:
                    sns_style_boosts_delta[st_k] = -2

            ranked_text = sorted(
                text_posts_with_views,
                key=lambda x: (int(x["metrics"].get("views") or 0), int(x["metrics"].get("likes") or 0)),
                reverse=True,
            )
            winners = [
                p for p in ranked_text[:2]
                if p.get("opener") and int(p["metrics"].get("views") or 0) >= max(20, overall_med_views * 1.15)
            ]
            losers = [
                p for p in reversed(ranked_text)
                if p.get("opener") and int(p["metrics"].get("views") or 0) <= max(18, overall_med_views * 0.65)
            ][:2]
            fb_lines: List[str] = []
            if winners:
                w_examples = " ／ ".join(
                    f"『{w['opener'][:44]}』（実測 {w['metrics']['views']}表示）"
                    for w in winners
                )
                fb_lines.append(f"- 直近アカウント実測で伸びた1行目フック: {w_examples} → このように冒頭25文字以内で『具体店名/都市名＋極端な比率・時間変化』を即座に提示する構造を優先すること。")
            if losers:
                l_examples = " ／ ".join(
                    f"『{l['opener'][:40]}』（実測 {l['metrics']['views']}表示）"
                    for l in losers
                )
                fb_lines.append(f"- 逆にスワイプ離脱された低反応フック: {l_examples} → 同じ入り方や抽象的な空気感・情景からの書き出しは避けること。")
            if fb_lines:
                sns_prompt_feedback = (
                    "【実測SNSインサイト自動学習フィードバック（直近アカウント実績）】\n"
                    + "\n".join(fb_lines)
                )

    # ------------------------------------------------------------------
    # Loop 2: Instagram Reels Retention & Skip-Rate Multi-Armed Bandit
    # ------------------------------------------------------------------
    reel_posts = [
        p for p in posts
        if p.get("channel") == "reels"
        and isinstance(p.get("metrics"), dict)
        and any(p["metrics"].get(k) is not None for k in ("views", "avg_watch_sec", "skip_rate_pct"))
    ]

    reel_views_list = [int(p["metrics"]["views"]) for p in reel_posts if p["metrics"].get("views") is not None]
    median_reel_views = float(statistics.median(reel_views_list)) if reel_views_list else 0.0
    watch_vals = [float(p["metrics"]["avg_watch_sec"]) for p in reel_posts if p["metrics"].get("avg_watch_sec") is not None]
    skip_vals = [float(p["metrics"]["skip_rate_pct"]) for p in reel_posts if p["metrics"].get("skip_rate_pct") is not None]

    avg_watch_sec = round(sum(watch_vals) / float(len(watch_vals)), 2) if watch_vals else None
    avg_skip_rate_pct = round(sum(skip_vals) / float(len(skip_vals)), 1) if skip_vals else None
    avg_reel_views = round(sum(reel_views_list) / float(len(reel_views_list)), 1) if reel_views_list else None

    tpl_scores_raw: Dict[str, List[float]] = collections.defaultdict(list)
    tpl_watch_raw: Dict[str, List[float]] = collections.defaultdict(list)
    tpl_skip_raw: Dict[str, List[float]] = collections.defaultdict(list)

    for rp in reel_posts:
        rt = str(rp.get("reel_type") or "").strip().lower()
        if rt not in REEL_TEMPLATE_KEYS:
            rt = infer_reel_type_from_caption(rp.get("text_snippet") or rp.get("opener") or "")
        q_score = _compute_reel_quality_score(rp["metrics"], median_reel_views, reel_type=rt)
        if q_score is not None:
            tpl_scores_raw[rt].append(q_score)
        if rp["metrics"].get("avg_watch_sec") is not None:
            tpl_watch_raw[rt].append(float(rp["metrics"]["avg_watch_sec"]))
        if rp["metrics"].get("skip_rate_pct") is not None:
            tpl_skip_raw[rt].append(float(rp["metrics"]["skip_rate_pct"]))

    total_scored_reels = sum(len(v) for v in tpl_scores_raw.values())
    reel_bandit_table: List[Dict[str, Any]] = []
    reel_template_boosts: Dict[str, float] = {}

    if total_scored_reels >= 6:
        all_q = [s for vals in tpl_scores_raw.values() for s in vals]
        prior_mean = sum(all_q) / float(len(all_q))
        ucb_by_tpl: Dict[str, float] = {}
        for rt in REEL_TEMPLATE_KEYS:
            obs = tpl_scores_raw.get(rt) or []
            n_k = len(obs)
            emp_mean = (sum(obs) / float(n_k)) if n_k > 0 else prior_mean
            explore_bonus = 12.0 * math.sqrt(math.log(total_scored_reels + 1.0) / float(n_k + 1))
            ucb_val = round(emp_mean + explore_bonus, 2)
            ucb_by_tpl[rt] = ucb_val

        mean_ucb = sum(ucb_by_tpl.values()) / float(len(ucb_by_tpl))
        for rt in REEL_TEMPLATE_KEYS:
            obs = tpl_scores_raw.get(rt) or []
            n_k = len(obs)
            delta = round(ucb_by_tpl[rt] - mean_ucb, 2)
            reel_template_boosts[rt] = delta
            w_list = tpl_watch_raw.get(rt) or []
            sk_list = tpl_skip_raw.get(rt) or []
            reel_bandit_table.append({
                "reel_type": rt,
                "label": REEL_TEMPLATE_LABELS.get(rt, rt),
                "sample_count": n_k,
                "mean_retention_score": round(sum(obs) / float(n_k), 1) if n_k > 0 else None,
                "avg_watch_sec": round(sum(w_list) / float(len(w_list)), 2) if w_list else None,
                "avg_skip_rate_pct": round(sum(sk_list) / float(len(sk_list)), 1) if sk_list else None,
                "ucb_score": ucb_by_tpl[rt],
                "bandit_boost": delta,
            })
        reel_bandit_table.sort(key=lambda x: x["ucb_score"], reverse=True)

    # ------------------------------------------------------------------
    # Loop 2B: YouTube Shorts Retention & Feed Survival Diagnostics
    # ------------------------------------------------------------------
    yt_posts = [
        p for p in posts
        if p.get("channel") == "youtube_shorts"
        and isinstance(p.get("metrics"), dict)
        and any(
            p["metrics"].get(k) is not None
            for k in ("views", "impressions", "stayed_to_watch_pct", "avg_pct_viewed", "avg_watch_sec")
        )
    ]
    yt_views_vals = [int(p["metrics"]["views"]) for p in yt_posts if p["metrics"].get("views") is not None]
    yt_stayed_vals = [float(p["metrics"]["stayed_to_watch_pct"]) for p in yt_posts if p["metrics"].get("stayed_to_watch_pct") is not None]
    yt_pct_vals = [float(p["metrics"]["avg_pct_viewed"]) for p in yt_posts if p["metrics"].get("avg_pct_viewed") is not None]
    yt_watch_vals = [float(p["metrics"]["avg_watch_sec"]) for p in yt_posts if p["metrics"].get("avg_watch_sec") is not None]
    yt_subs_total = sum(int(p["metrics"].get("subscribers_gained") or 0) for p in yt_posts)

    yt_avg_stayed = round(sum(yt_stayed_vals) / float(len(yt_stayed_vals)), 1) if yt_stayed_vals else None
    yt_avg_pct = round(sum(yt_pct_vals) / float(len(yt_pct_vals)), 1) if yt_pct_vals else None
    yt_avg_watch = round(sum(yt_watch_vals) / float(len(yt_watch_vals)), 2) if yt_watch_vals else None
    yt_med_views = float(statistics.median(yt_views_vals)) if yt_views_vals else 0.0

    yt_ranked = sorted(
        yt_posts,
        key=lambda x: (
            int(x["metrics"].get("views") or 0),
            float(x["metrics"].get("stayed_to_watch_pct") or 0.0),
        ),
        reverse=True,
    )
    youtube_shorts_diagnostics: Dict[str, Any] = {
        "data_status": "ok" if yt_posts else "insufficient",
        "sample_count": len(yt_posts),
        "total_views": sum(yt_views_vals) if yt_views_vals else None,
        "avg_views": round(sum(yt_views_vals) / float(len(yt_views_vals)), 1) if yt_views_vals else None,
        "avg_stayed_to_watch_pct": yt_avg_stayed,
        "avg_pct_viewed": yt_avg_pct,
        "avg_watch_sec": yt_avg_watch,
        "subscribers_gained": yt_subs_total if yt_posts else None,
        "retention_strong": bool(
            (yt_avg_stayed is not None and yt_avg_stayed >= 60.0)
            or (yt_avg_pct is not None and yt_avg_pct >= 75.0)
        ),
        "top_shorts": [
            {
                "title": p.get("opener") or p.get("text_snippet") or "(タイトル未記録)",
                "reel_type": p.get("reel_type") or "moshimo_3d",
                "views": p["metrics"].get("views"),
                "impressions": p["metrics"].get("impressions"),
                "stayed_to_watch_pct": p["metrics"].get("stayed_to_watch_pct"),
                "avg_pct_viewed": p["metrics"].get("avg_pct_viewed"),
                "avg_watch_sec": p["metrics"].get("avg_watch_sec"),
                "likes": p["metrics"].get("likes"),
                "subscribers_gained": p["metrics"].get("subscribers_gained"),
                "quality_score": _compute_reel_quality_score(
                    p["metrics"], yt_med_views, reel_type=str(p.get("reel_type") or "moshimo_3d")
                ),
            }
            for p in yt_ranked[:6]
        ],
    }

    # ------------------------------------------------------------------
    # Loop 2C: Google Search Console Organic Demand & Opportunity Queries
    # ------------------------------------------------------------------
    gsc_summary = dict(gsc_store.get("summary") or {})
    gsc_queries = [q for q in (gsc_store.get("queries") or []) if isinstance(q, dict) and q.get("query")]
    gsc_pages = [pg for pg in (gsc_store.get("pages") or []) if isinstance(pg, dict) and pg.get("page")]
    gsc_has_data = bool(
        gsc_queries
        or gsc_pages
        or gsc_summary.get("total_impressions") is not None
        or gsc_summary.get("total_clicks") is not None
    )
    max_q_imps = max((int(q.get("impressions") or 0) for q in gsc_queries), default=0)
    if max_q_imps >= 20:
        opp_queries = [
            q for q in gsc_queries
            if int(q.get("impressions") or 0) >= 20
            and (q.get("ctr_pct") is None or float(q.get("ctr_pct") or 0.0) <= 4.0)
        ]
        opp_queries.sort(key=lambda x: int(x.get("impressions") or 0), reverse=True)
    else:
        # Short-window (24h/7d) export: surface Page 1-2 zero-click striking-distance queries (pos 1..20)
        opp_queries = [
            q for q in gsc_queries
            if int(q.get("clicks") or 0) == 0
            and int(q.get("impressions") or 0) >= 1
            and q.get("position") is not None
            and 1.0 <= float(q.get("position") or 999.0) <= 20.0
        ]
        opp_queries.sort(
            key=lambda x: (-int(x.get("impressions") or 0), float(x.get("position") or 999.0))
        )

    seo_prompt_directive = ""
    if gsc_queries:
        top_q_txt = " ／ ".join(
            f"『{q['query']}』（表示{q.get('impressions', 0)}・クリック{q.get('clicks', 0)}）"
            for q in gsc_queries[:3]
        )
        seo_prompt_directive = f"Search Console実測上位クエリ: {top_q_txt}。"
        if opp_queries:
            opp_txt = " ／ ".join(
                f"『{q['query']}』（{q.get('position', '-')}位・表示{q.get('impressions', 0)}・CTR {q.get('ctr_pct', 0)}%）"
                for q in opp_queries[:3]
            )
            seo_prompt_directive += f" 1〜2ページ目圏内の未回収お宝クエリ: {opp_txt} → 見出し・FAQ・新規コラムの切り口で優先回収すること。"

    gsc_diagnostics: Dict[str, Any] = {
        "data_status": "ok" if gsc_has_data else "insufficient",
        "updated_at": gsc_store.get("updated_at") or "",
        "source": gsc_store.get("source") or "",
        "total_clicks": gsc_summary.get("total_clicks"),
        "total_impressions": gsc_summary.get("total_impressions"),
        "avg_ctr_pct": gsc_summary.get("avg_ctr_pct"),
        "avg_position": gsc_summary.get("avg_position"),
        "top_queries": gsc_queries[:10],
        "opportunity_queries": opp_queries[:6],
        "top_pages": gsc_pages[:8],
        "seo_prompt_directive": seo_prompt_directive,
    }

    # ------------------------------------------------------------------
    # Loop 3: note Article Title CTR / PV Feedback
    # ------------------------------------------------------------------
    note_with_views = [n for n in note_articles if n.get("views") is not None]
    note_ctr_feedback: Dict[str, Any] = {
        "data_status": "ok" if len(note_with_views) >= 2 else "insufficient",
        "sample_count": len(note_with_views),
        "top_titles": [],
        "low_titles": [],
        "prompt_directive": "",
    }
    if len(note_with_views) >= 2:
        ranked_notes = sorted(
            note_with_views,
            key=lambda x: (
                float(x.get("ctr_pct") or 0.0),
                int(x.get("views") or 0),
                int(x.get("likes") or 0),
            ),
            reverse=True,
        )
        top_n = ranked_notes[:2]
        low_n = [x for x in reversed(ranked_notes) if x not in top_n][:2]
        note_ctr_feedback["top_titles"] = [
            {
                "title": x["title"],
                "views": x.get("views"),
                "impressions": x.get("impressions"),
                "ctr_pct": x.get("ctr_pct"),
                "likes": x.get("likes"),
            }
            for x in top_n
        ]
        note_ctr_feedback["low_titles"] = [
            {
                "title": x["title"],
                "views": x.get("views"),
                "impressions": x.get("impressions"),
                "ctr_pct": x.get("ctr_pct"),
            }
            for x in low_n
        ]
        top_desc = " ／ ".join(
            f"『{x['title'][:36]}』（{x['views']}ビュー"
            + (f"・CTR {x['ctr_pct']}%" if x.get("ctr_pct") is not None else "")
            + "）"
            for x in top_n
        )
        note_ctr_feedback["prompt_directive"] = (
            f"note実測で読まれたタイトル傾向: {top_desc}。抽象的な時間帯ポエム題名を避け、"
            "具体的な数字・年齢層・比較軸・読者の疑問文（『〜は本当か？』『【2026年最新】〜』）を見出しに入れること。"
        )

    has_any_data = bool(text_posts_with_views or reel_posts or yt_posts or gsc_has_data or note_with_views)
    result = {
        "data_status": "ok" if has_any_data else "insufficient",
        "days": safe_days,
        "last_sync_at": store.get("last_sync_at") or "",
        "updated_at": store.get("updated_at") or gsc_store.get("updated_at") or "",
        "sample_counts": {
            "total_posts_stored": len(store.get("posts") or []),
            "text_posts_with_views": len(text_posts_with_views),
            "reels_with_metrics": len(reel_posts),
            "youtube_shorts_with_metrics": len(yt_posts),
            "gsc_queries": len(gsc_queries),
            "note_articles": len(note_with_views),
        },
        "hook_archetype_boosts": hook_archetype_boosts,
        "hook_archetype_stats": sorted(
            hook_stats.values(),
            key=lambda x: (x["avg_views"], x["sample_count"]),
            reverse=True,
        ),
        "sns_style_boosts_delta": sns_style_boosts_delta,
        "sns_prompt_feedback": sns_prompt_feedback,
        "reel_diagnostics": {
            "sample_count": len(reel_posts),
            "avg_watch_sec": avg_watch_sec,
            "avg_skip_rate_pct": avg_skip_rate_pct,
            "avg_views": avg_reel_views,
            "skip_cliff_alert": bool(
                (avg_skip_rate_pct is not None and avg_skip_rate_pct >= 65.0)
                or (avg_watch_sec is not None and avg_watch_sec <= 3.0)
            ),
            "bandit_table": reel_bandit_table,
        },
        "youtube_shorts_diagnostics": youtube_shorts_diagnostics,
        "gsc_diagnostics": gsc_diagnostics,
        "reel_template_boosts": reel_template_boosts,
        "note_ctr_feedback": note_ctr_feedback,
        "recent_posts": (store.get("posts") or [])[:12],
        "recent_note_articles": (store.get("note_articles") or [])[:8],
    }

    with _store_lock:
        _feedback_cache["ts"] = time.monotonic()
        _feedback_cache["days"] = safe_days
        _feedback_cache["data"] = json.loads(json.dumps(result))
    return result


def get_insights_dashboard_payload(days: int = 28, allow_blocking: bool = False) -> Dict[str, Any]:
    """Return full loop state & empirical feedback for `/staff/pdca` and `/api/pdca/sns-insights`."""
    import youtube_analytics_sync

    store = _load_store(allow_blocking=allow_blocking)
    gsc_store = _load_gsc_store(allow_blocking=allow_blocking)
    fb = compute_loop_feedback(days=days, allow_blocking=allow_blocking)
    yt_oauth = youtube_analytics_sync.get_youtube_sync_status(allow_blocking=allow_blocking)
    return {
        "ok": True,
        "last_sync_at": store.get("last_sync_at") or "",
        "last_sync_summary": store.get("last_sync_summary") or {},
        "gsc_updated_at": gsc_store.get("updated_at") or "",
        "youtube_oauth": yt_oauth,
        "feedback": fb,
    }
