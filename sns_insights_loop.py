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
import datetime
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

MAX_STORED_POSTS = 85
MAX_STORED_NOTE_ARTICLES = 30
MAX_STORE_JSON_CHARS = 35000
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
)

REEL_TEMPLATE_LABELS: Dict[str, str] = {
    "live_top5": "全国リアルタイムTOP5 (live_top5)",
    "area_battle": "3大エリア徹底対決 (area_battle)",
    "tiktok_gap_shock": "同駅徒歩4分ギャップ実証 (tiktok_gap_shock)",
    "ladies_zero_wait": "女子得・受付待ちゼロ速報 (ladies_zero_wait)",
    "tiktok_ladies_real": "女子組の2軒目リアル男性先行 (tiktok_ladies_real)",
    "tiktok_midnight_cliff": "23時20分・終電後の真実 (tiktok_midnight_cliff)",
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
_feedback_cache: Dict[str, Any] = {"ts": 0.0, "days": 28, "data": None}


def reset_insights_loop_cache_for_tests() -> None:
    """Clear in-memory caches for deterministic unit testing."""
    with _store_lock:
        _mem_cache["ts"] = 0.0
        _mem_cache["data"] = None
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
    """Infer canonical `reel_type` from an Instagram Reel caption when not explicitly tagged."""
    c = str(caption or "")
    if not c:
        return "live_top5"
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
    for int_k in ("views", "reach", "likes", "replies", "reposts", "quotes", "saved", "shares", "comments", "bookmarks", "link_clicks", "impressions"):
        iv = _safe_nonneg_int(raw_m.get(int_k))
        if iv is not None:
            out[int_k] = iv
    for flt_k, cap in (
        ("avg_watch_sec", 600.0),
        ("total_watch_sec", 10000000.0),
        ("skip_rate_pct", 100.0),
        ("typical_skip_rate_pct", 100.0),
        ("share_rate_pct", 100.0),
    ):
        fv = _safe_nonneg_float(raw_m.get(flt_k), max_val=cap)
        if fv is not None:
            out[flt_k] = fv
    return out


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
    if ch not in ("threads", "instagram", "reels", "x", "tiktok"):
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
    if ch == "reels" and not norm_rt:
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

    if existing is not None:
        if pid:
            existing["post_id"] = pid
        if ch == "reels":
            existing["channel"] = "reels"
        if opener and not existing.get("opener"):
            existing["opener"] = opener
        if snippet and len(snippet) > len(str(existing.get("text_snippet") or "")):
            existing["text_snippet"] = snippet
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


def sync_all_sns_insights(limit: int = 15, now_jst: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Run automated official API insights sync across Threads + Instagram/Reels and refresh loop summary."""
    th_res = collect_threads_insights(limit=limit, now_jst=now_jst)
    ig_res = collect_instagram_insights(limit=limit, now_jst=now_jst)

    store = _load_store(allow_blocking=True)
    sync_ts = _now_jst_str(now_jst)
    th_cnt = int(th_res.get("synced") or 0)
    ig_cnt = int(ig_res.get("synced") or 0)
    summary = {
        "synced_at": sync_ts,
        "threads": th_res,
        "instagram": ig_res,
        "threads_updated": th_cnt,
        "instagram_updated": ig_cnt,
        "total_synced": th_cnt + ig_cnt,
    }
    store["last_sync_at"] = sync_ts
    store["last_sync_summary"] = summary
    _save_store(store, now_jst=now_jst)

    feedback = compute_loop_feedback(days=28, allow_blocking=True)
    return {
        "ok": bool(th_res.get("ok") or ig_res.get("ok")),
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


def ingest_manual_insight_records(
    payload: Dict[str, Any],
    source: str = "manual_json",
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Ingest validated post / Reel / note insight items from structured JSON or Carbon Vision output.
    Accepts `{"posts": [...], "note_articles": [...]}` or a single flat metric dict.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "不正なデータ形式です", "ingested_posts": 0, "ingested_notes": 0}

    store = _load_store(allow_blocking=True)
    posts_in = payload.get("posts")
    notes_in = payload.get("note_articles")

    # Convenience: allow single-record submission without wrapping in `posts` / `note_articles`
    if posts_in is None and notes_in is None:
        ch_cand = str(payload.get("platform") or payload.get("channel") or "").strip().lower()
        if ch_cand == "note" or ("title" in payload and not payload.get("text") and not payload.get("opener")):
            notes_in = [payload]
            posts_in = []
        else:
            posts_in = [payload]
            notes_in = []

    ingested_posts = 0
    ingested_notes = 0

    for p in (posts_in or [])[:30]:
        if not isinstance(p, dict):
            continue
        ch = str(p.get("channel") or p.get("platform") or "threads").strip().lower()
        if ch in ("ig_reel", "ig_reels", "reel", "instagram_reel"):
            ch = "reels"
        elif ch == "note":
            n_rec = _upsert_note_article(store, p, source=source, now_jst=now_jst)
            if n_rec:
                ingested_notes += 1
            continue
        elif ch not in ("threads", "instagram", "reels", "x", "tiktok"):
            ch = "threads"

        raw_txt = str(p.get("text") or p.get("opener") or p.get("caption") or "").strip()
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
            "avg_watch_sec",
            "total_watch_sec",
            "skip_rate_pct",
            "typical_skip_rate_pct",
            "share_rate_pct",
        ):
            if flat_k in p and p.get(flat_k) is not None and flat_k not in metrics_in:
                metrics_in[flat_k] = p.get(flat_k)

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

    if ingested_posts == 0 and ingested_notes == 0:
        return {
            "ok": False,
            "error": "有効なインサイト数値（再生数・スキップ率・ビュー数など）が見つかりませんでした",
            "ingested_posts": 0,
            "ingested_notes": 0,
            "posts_ingested": 0,
            "notes_ingested": 0,
        }

    _save_store(store, now_jst=now_jst)
    feedback = compute_loop_feedback(days=28, allow_blocking=True)
    return {
        "ok": True,
        "source": source,
        "ingested_posts": ingested_posts,
        "ingested_notes": ingested_notes,
        "posts_ingested": ingested_posts,
        "notes_ingested": ingested_notes,
        "feedback": feedback,
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
    Extract verified SNS / note insight numbers from an uploaded screenshot using `gemini-4-carbon` Vision,
    persist them into `sns_insights_store_v1`, and recompute the 3-layer closed-loop directives.
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
        "あなたは『相席ナビ』のSNSインサイト数値抽出エンジンです。添付されたスクリーンショット（Instagramリールインサイト / Threads投稿インサイト / noteアクセス状況 / Xアナリティクスのいずれか）から、"
        "画面上に**明確に表示されている実測数値のみ**を正確にJSONで抽出してください。\n"
        f"プラットフォームヒント: {hint_clean}\n\n"
        "【絶対厳守ルール（推測値の完全禁止）】\n"
        "1. 画像内に表示されていない項目は必ず `null` にしてください。推測・補完・仮数値の生成は厳禁です。\n"
        "2. Instagramリールのインサイト画面の場合：`channel` を `\"reels\"` とし、`views`（閲覧数/再生数）、`reach`（リーチしたアカウント数）、`avg_watch_sec`（平均再生時間を秒数に変換。例: `2秒`→`2.0`、`0:04`→`4.0`）、"
        "`skip_rate_pct`（このリールのスキップ率%。例: `78.6%`→`78.6`）、`typical_skip_rate_pct`（通常のスキップ率%があれば）、`saved`（保存数）、`shares`（シェア数）、画面内のタイトルや冒頭文字があれば `opener` に記載してください。\n"
        "3. ThreadsやXの投稿一覧・インサイト画面の場合：`channel` を `\"threads\"` または `\"x\"` とし、表示されている各投稿の冒頭1〜2行を `opener`、`views`（表示回数/閲覧数）、`likes`（いいね）、`replies`（返信）、`reposts`（再投稿）を抽出してください。\n"
        "4. noteのアクセス状況画面の場合：`note_articles` 配列に各記事の `title`（記事タイトル）、`views`（全体ビュー）、`impressions`（表示回数がある場合のみ）、`likes`（スキ数）を抽出してください。\n\n"
        "以下のJSON形式のみを返してください：\n"
        "{\n"
        '  "detected_platform": "reels | threads | note | x | instagram",\n'
        '  "posts": [\n'
        '    {"channel": "reels", "opener": "", "reel_type": "", "views": null, "reach": null, "avg_watch_sec": null, "skip_rate_pct": null, "typical_skip_rate_pct": null, "saved": null, "shares": null, "likes": null, "replies": null, "reposts": null}\n'
        "  ],\n"
        '  "note_articles": [\n'
        '    {"title": "", "views": null, "impressions": null, "likes": null, "comments": null}\n'
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


def _compute_reel_quality_score(metrics: Dict[str, Any], median_views: float) -> Optional[float]:
    """
    Compute empirical 0..100 Retention Quality Score for a single Reel using ONLY measured fields:
      - Watch time vs 8.5s full length (`avg_watch_sec`)
      - Survival past 3s (`100 - skip_rate_pct`)
      - Save/Share viral signal (`saved`, `shares`)
      - View velocity vs account Reel median (`views`)
    """
    if not metrics:
        return None
    weighted_sum = 0.0
    total_weight = 0.0

    watch_s = metrics.get("avg_watch_sec")
    if watch_s is not None:
        # 2.0s = 23.5 pts, 4.25s = 50 pts, 6.8s = 80 pts, 8.5s+ = 100 pts
        watch_pts = min(100.0, max(0.0, (float(watch_s) / 8.5) * 100.0))
        weighted_sum += watch_pts * 0.40
        total_weight += 0.40

    skip_pct = metrics.get("skip_rate_pct")
    if skip_pct is not None:
        # 78.5% skip -> 21.5 survival -> ~33 pts; 50% skip -> 50 survival -> ~77 pts; 35% skip -> 100 pts
        survival = max(0.0, 100.0 - float(skip_pct))
        skip_pts = min(100.0, (survival / 65.0) * 100.0)
        weighted_sum += skip_pts * 0.35
        total_weight += 0.35

    views = metrics.get("views")
    if views is not None and views > 0:
        saved = int(metrics.get("saved") or 0)
        shares = int(metrics.get("shares") or 0)
        viral_rate = (saved * 2.0 + shares * 3.0) / float(max(1, views))
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
    Compute empirical closed-loop feedback from real SNS & note insights:
      1. `hook_archetype_boosts` (`dict[str, int]`, -4 .. +5) + `sns_prompt_feedback` (str)
      2. `sns_style_boosts_delta` (`dict[str, int]`, -2 .. +3)
      3. `reel_template_boosts` (`dict[str, float]`) via UCB1 Multi-Armed Bandit + `reel_diagnostics`
      4. `note_ctr_feedback` (dict & prompt guidance)
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
        if (p.get("channel") == "reels" or p.get("reel_type"))
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
        q_score = _compute_reel_quality_score(rp["metrics"], median_reel_views)
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
            # UCB1 exploration bonus encourages testing under-sampled templates while favoring high-retention winners
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

    has_any_data = bool(text_posts_with_views or reel_posts or note_with_views)
    result = {
        "data_status": "ok" if has_any_data else "insufficient",
        "days": safe_days,
        "last_sync_at": store.get("last_sync_at") or "",
        "updated_at": store.get("updated_at") or "",
        "sample_counts": {
            "total_posts_stored": len(store.get("posts") or []),
            "text_posts_with_views": len(text_posts_with_views),
            "reels_with_metrics": len(reel_posts),
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
    store = _load_store(allow_blocking=allow_blocking)
    fb = compute_loop_feedback(days=days, allow_blocking=allow_blocking)
    return {
        "ok": True,
        "last_sync_at": store.get("last_sync_at") or "",
        "last_sync_summary": store.get("last_sync_summary") or {},
        "feedback": fb,
    }
