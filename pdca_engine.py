"""
pdca_engine.py - Autonomous Closed-Loop PDCA Strategist ("作戦参謀・グロース統括係")

Continuously evaluates what is working vs. what is underperforming across Aiseki Navi
(Web Traffic, Affiliate CTR by Article, Store Demand vs. Column Supply, SNS Style Inflow `?s=`,
and Scheduled Column Queue Inventory), and automatically feeds actionable directives back into:
  1. `sns_editorial.py`  -> Boosts Desk scores (+1 to +5 pts) for high-performing SNS angles & penalizes stale ones.
  2. `tips_poster.py`    -> Prioritizes high-CTR "winner" columns & newly released columns in X thread replies.
  3. `column_editorial.py` -> Sets the next high-opportunity "Special Order Topic" based on real store demand gaps,
                              and optionally auto-schedules a fresh column if upcoming inventory runs low.
"""

import os
import re
import time
import json
import datetime
import logging
import threading
import collections
import urllib.parse
import requests

import analytics
import job_state
import store_master
import sns_editorial
import column_editorial
import scheduled_articles
import turbo_store

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

PDCA_SETTINGS_KEY = "pdca_settings"
PDCA_DIRECTIVES_KEY = "pdca_active_directives"
PDCA_HISTORY_KEY = "pdca_cycle_history"

_directives_mem_cache = {"ts": 0.0, "data": None, "verified": False}
_briefing_ai_cache = collections.OrderedDict()
_pdca_lock = threading.Lock()
_BRIEFING_TTL_SEC = 900


def reset_pdca_cache_for_tests():
    with _pdca_lock:
        _directives_mem_cache["ts"] = 0.0
        _directives_mem_cache["data"] = None
        _directives_mem_cache["verified"] = False
        _briefing_ai_cache.clear()
    turbo_store.clear_ai_cache("pdca_briefing")


VALID_GROWTH_PHASE_MODES = (
    "auto",
    "phase1_inflow",
    "phase2_engagement",
    "phase3_monetize",
)

GROWTH_PHASE_SPECS = {
    "phase1_inflow": {
        "phase_number": 1,
        "label": "Phase 1｜流入拡大最優先",
        "short_label": "流入拡大最優先",
        "step1_target_daily_pv": 100,
        "target_daily_pv": 300,
        "target_label": "第1目標: 100〜300 PV/日（月間3,000〜1万PV）",
        "min_pv_for_funnel_action": 50,
    },
    "phase2_engagement": {
        "phase_number": 2,
        "label": "Phase 2｜回遊定着＆2ステップ強化",
        "short_label": "回遊定着＆初期収益化",
        "step1_target_daily_pv": 500,
        "target_daily_pv": 1000,
        "target_label": "第2目標: 300〜1,000 PV/日（月間1万〜3万PV）",
        "min_pv_for_funnel_action": 20,
    },
    "phase3_monetize": {
        "phase_number": 3,
        "label": "Phase 3｜収益CVR・EPC最大化",
        "short_label": "収益CVR・EPC最大化",
        "step1_target_daily_pv": 1500,
        "target_daily_pv": 3000,
        "target_label": "第3目標: 1,000〜3,000 PV/日（高EPC収益運用）",
        "min_pv_for_funnel_action": 5,
    },
}


def _normalize_growth_phase_mode(raw_mode: str) -> str:
    m = str(raw_mode or "auto").strip().lower()
    return m if m in VALID_GROWTH_PHASE_MODES else "auto"


def resolve_growth_phase(
    daily_pv: float,
    mode: str = "auto",
    pv_basis: str = "raw_pv_fallback",
    raw_daily_pv: float = None,
) -> dict:
    """Resolve active Growth Phase (auto by daily PV milestone or explicit manual override)."""
    norm_mode = _normalize_growth_phase_mode(mode)
    dpv = max(0.0, float(daily_pv or 0.0))
    raw_dpv = max(0.0, float(raw_daily_pv)) if raw_daily_pv is not None else dpv
    basis_tag = "実ブラウザPV基準" if pv_basis == "human_pv" else "生PV基準"
    is_auto = norm_mode == "auto"
    if is_auto:
        if dpv < 300.0:
            active_phase = "phase1_inflow"
        elif dpv < 1000.0:
            active_phase = "phase2_engagement"
        else:
            active_phase = "phase3_monetize"
    else:
        active_phase = norm_mode

    spec = GROWTH_PHASE_SPECS[active_phase]
    target_pv = float(spec["target_daily_pv"])
    step1_pv = float(spec["step1_target_daily_pv"])
    milestone_pct = min(100, max(0, int(round((dpv / target_pv) * 100)))) if target_pv > 0 else 0
    step1_pct = min(100, max(0, int(round((dpv / step1_pv) * 100)))) if step1_pv > 0 else 0

    if active_phase == "phase1_inflow":
        if dpv < 100.0:
            rem = max(1, int(round(100.0 - dpv)))
            hint = f"まずは初期突破ライン 100 PV/日（{basis_tag}・月間3,000PV）まであと {rem} PV/日"
        elif dpv < 300.0:
            rem = max(1, int(round(300.0 - dpv)))
            hint = f"100 PV/日達成（{basis_tag}）！Phase 2移行ライン（300 PV/日・月間1万PV）まであと {rem} PV/日"
        else:
            hint = f"300 PV/日を達成済み（{basis_tag}・自動判定またはPhase 2へ移行可能）"
    elif active_phase == "phase2_engagement":
        if dpv < 1000.0:
            rem = max(1, int(round(1000.0 - dpv)))
            hint = f"Phase 3（収益最大化・1,000 PV/日）移行まであと {rem} PV/日"
        else:
            hint = "1,000 PV/日を達成済み（Phase 3へ移行可能）"
    else:
        hint = "高トラフィック帯：検索・ショート動画流入を維持しつつ2ステップCVR・EPCを最適化"

    return {
        "mode": norm_mode,
        "active_phase": active_phase,
        "phase_number": spec["phase_number"],
        "label": spec["label"],
        "short_label": spec["short_label"],
        "is_auto": is_auto,
        "mode_badge": "自動判定" if is_auto else "手動固定",
        "pv_basis": pv_basis,
        "pv_basis_label": basis_tag,
        "daily_pv": round(dpv, 1),
        "raw_daily_pv": round(raw_dpv, 1),
        "monthly_pv_pace": int(round(dpv * 30.0)),
        "step1_target_daily_pv": int(step1_pv),
        "target_daily_pv": int(target_pv),
        "step1_pct": step1_pct,
        "milestone_pct": milestone_pct,
        "target_label": spec["target_label"],
        "min_pv_for_funnel_action": spec["min_pv_for_funnel_action"],
        "next_phase_hint": hint,
    }


def get_pdca_settings(allow_blocking: bool = True) -> dict:
    default_replenish = os.environ.get("PDCA_AUTO_REPLENISH_DEFAULT", "0").strip().lower() in ("1", "true", "yes", "on")
    defaults = {
        "auto_optimize_sns": True,
        "auto_replenish_columns": default_replenish,
        "growth_phase_mode": "auto",
        "updated_at": "",
    }
    raw = job_state.get_value(PDCA_SETTINGS_KEY, allow_blocking=allow_blocking)
    if not raw:
        return defaults
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            defaults.update({
                "auto_optimize_sns": bool(parsed.get("auto_optimize_sns", True)),
                "auto_replenish_columns": bool(parsed.get("auto_replenish_columns", default_replenish)),
                "growth_phase_mode": _normalize_growth_phase_mode(parsed.get("growth_phase_mode", "auto")),
                "updated_at": str(parsed.get("updated_at") or ""),
            })
    except Exception:
        pass
    return defaults


def update_pdca_settings(
    auto_optimize_sns: bool = None,
    auto_replenish_columns: bool = None,
    growth_phase_mode: str = None,
) -> dict:
    cur = get_pdca_settings(allow_blocking=True)
    if auto_optimize_sns is not None:
        cur["auto_optimize_sns"] = bool(auto_optimize_sns)
    if auto_replenish_columns is not None:
        cur["auto_replenish_columns"] = bool(auto_replenish_columns)
    if growth_phase_mode is not None:
        cur["growth_phase_mode"] = _normalize_growth_phase_mode(growth_phase_mode)
    cur["updated_at"] = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    job_state.set_value(PDCA_SETTINGS_KEY, json.dumps(cur, ensure_ascii=False))
    with _pdca_lock:
        _directives_mem_cache["ts"] = 0.0
        _directives_mem_cache["data"] = None
        _directives_mem_cache["verified"] = False
    try:
        with _site_perf_cache_lock:
            _site_perf_cache.clear()
    except Exception:
        pass
    return cur


def get_active_directives(allow_blocking: bool = False) -> dict:
    """Fast cached read of current PDCA directives consumed by SNS & Column editorial pipelines."""
    now_t = time.monotonic()
    with _pdca_lock:
        if (
            _directives_mem_cache["data"] is not None
            and (now_t - _directives_mem_cache["ts"] < 30.0)
            and (_directives_mem_cache["verified"] or not allow_blocking)
        ):
            return dict(_directives_mem_cache["data"])

    settings = get_pdca_settings(allow_blocking=allow_blocking)
    raw = job_state.get_value(PDCA_DIRECTIVES_KEY, allow_blocking=allow_blocking)
    is_verified = bool(allow_blocking or getattr(job_state, "_sheets_loaded", False) or raw is not None)
    data = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = dict(parsed)
        except Exception:
            data = {}

    if data and not settings.get("auto_optimize_sns", True):
        data["sns_style_boosts"] = {}
        data["priority_column_paths"] = []
        data["hook_archetype_boosts"] = {}
        data["sns_prompt_feedback"] = ""
        data["reel_template_boosts"] = {}

    if is_verified:
        with _pdca_lock:
            _directives_mem_cache["ts"] = now_t
            _directives_mem_cache["data"] = dict(data)
            _directives_mem_cache["verified"] = True
    return dict(data)


def get_pdca_history(limit: int = 8, allow_blocking: bool = False) -> list:
    raw = job_state.get_value(PDCA_HISTORY_KEY, allow_blocking=allow_blocking)
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items[:limit]
    except Exception:
        pass
    return []


def _record_pdca_history(entry: dict):
    hist = get_pdca_history(limit=12, allow_blocking=True)
    updated = ([entry] + hist)[:10]
    job_state.set_value(PDCA_HISTORY_KEY, json.dumps(updated, ensure_ascii=False))


# =====================================================================
# Phase 1: CHECK — Multi-Dimensional Site, SNS & Revenue Diagnostics
# =====================================================================
OFFER_CATEGORIES = [
    {
        "category_key": "dating",
        "pattern": "dating",
        "label": "💝 マッチング・審査制デートアプリ",
        "short_label": "マッチングアプリ (dating)",
        "audience": "men",
        "epc_yen": 110,
        "primary_link": "/link/dating_instant",
        "default_priority": 5,
        "winning_pitch": "男余り・入場待ちの夜に代替選択肢として提示する混雑回避文脈の検証枠です（ASP確定成果は蓄積・検証中）。",
    },
    {
        "category_key": "orbis",
        "pattern": "mens_skincare_clinic",
        "label": "🧴 メンズスキンケア（ORBIS Mr.等）",
        "short_label": "メンズスキンケア (mens_skincare_clinic)",
        "audience": "men",
        "epc_yen": 95,
        "primary_link": "/link/mens_skincare_clinic",
        "default_priority": 4,
        "winning_pitch": "間接照明・ソファ席距離70cmの身だしなみ解説パートと相性が高い想定の2ステップ検証枠です（ASP確定成果は蓄積・検証中）。",
    },
    {
        "category_key": "hair_removal",
        "pattern": "mens_hair_removal",
        "label": "🪒 メンズ医療脱毛・青ヒゲ対策クリニック",
        "short_label": "医療脱毛 (mens_hair_removal)",
        "audience": "men",
        "epc_yen": 160,
        "primary_link": "/link/mens_hair_removal",
        "default_priority": 3,
        "winning_pitch": "仕事帰りの夕方の青ヒゲ対策と絡め、/grooming 内の初回体験比較枠として反応を検証中です（ASP確定成果は蓄積・検証中）。",
    },
    {
        "category_key": "spu",
        "pattern": "spu_styleup",
        "label": "👔 メンズファッション（SPUスタイルアップ便）",
        "short_label": "メンズファッション (spu_styleup)",
        "audience": "men",
        "epc_yen": 120,
        "primary_link": "/link/spu_styleup",
        "default_priority": 2,
        "winning_pitch": "入店時のドレスコード・服装チェック文脈で /grooming 服装セクション直下に配置している検証枠です（ASP確定成果は蓄積・検証中）。",
    },
    {
        "category_key": "women",
        "pattern": "women_*",
        "label": "💄 女性向け特典（無料ハイクラスデート・美容医療・サロン）",
        "short_label": "女性向け特典 (women_*)",
        "audience": "women",
        "epc_yen": 105,
        "primary_link": "/link/women_second_venue",
        "default_priority": 1,
        "winning_pitch": "女性向け待ちゼロ店舗ページ（/for-women）内で、0次会・女子会レストラン予約や無料デート枠の需要を検証中です。",
    },
    {
        "category_key": "supplement",
        "pattern": "night_care_supplement",
        "label": "🥂 乾杯サポート・ナイトケアサプリ",
        "short_label": "ナイトケアサプリ (supplement)",
        "audience": "common",
        "epc_yen": 90,
        "primary_link": "/link/night_care_supplement",
        "default_priority": 3,
        "winning_pitch": "飲み放題・2軒目バーの乾杯前ルーティン文脈として /grooming・/for-women で男女双方のクリック反応を検証中です。",
    },
    {
        "category_key": "second_venue",
        "pattern": "second_venue_reservation",
        "label": "🍽️ 2軒目バー・ディナー予約（一休.comレストラン）",
        "short_label": "2軒目・ディナー予約 (second_venue)",
        "audience": "common",
        "epc_yen": 95,
        "primary_link": "/link/second_venue_reservation",
        "default_priority": 3,
        "winning_pitch": "退店後の2軒目移動や後日デート店選び文脈で、全店舗2軒目セクション・/grooming・/for-women の送客反応を検証中です。",
    },
]
_OFFER_META_BY_KEY = {c["category_key"]: c for c in OFFER_CATEGORIES}


def _canonicalize_aff_slot_id(raw_item_id: str) -> str:
    it = str(raw_item_id or "").strip().lower()
    if it.startswith("aff_click:"):
        it = it.split(":", 1)[1].strip()
    if not it:
        return ""
    try:
        import affiliate_manager
        canon = affiliate_manager.canonical_slot_id(it)
        if canon:
            return canon
    except Exception:
        pass
    return it


def _classify_offer_category(raw_item_id: str) -> str:
    """Map raw `/link/<id>` or `aff_click:<id>` identifier into one of the canonical offer categories."""
    it = str(raw_item_id or "").strip().lower()
    if it.startswith("aff_click:"):
        it = it.split(":", 1)[1].strip()
    if not it:
        return "dating"
    canon = _canonicalize_aff_slot_id(it)
    for cand in (it, canon):
        if "second_venue" in cand or "ikyu" in cand or "restaurant" in cand:
            return "second_venue"
        if "supplement" in cand or "hangover" in cand:
            return "supplement"
    target = canon or it
    if target.startswith("women_") or target == "women":
        return "women"
    if target in ("spu", "spu_styleup", "spu_fashion") or "spu" in target or "fashion" in target or "styleup" in target:
        return "spu"
    if target in ("orbis", "orbis_mr", "mens_skincare_clinic", "mens_skincare") or "orbis" in target or "skincare" in target:
        return "orbis"
    if target in ("hair_removal", "mens_hair_removal", "hair_removal_trial") or "hair" in target or "datsumo" in target:
        return "hair_removal"
    if target in ("dating", "dating_instant", "dating_bachelor", "dating_casual") or "dating" in target or "bachelor" in target or "app" in target:
        return "dating"
    return "dating"


def _extract_aff_category_stats(summary_dict: dict) -> tuple:
    """
    Extract per-category click totals and referrer breakdown from `analytics.summary()`.
    Supports both `all_by_label['affiliate_click']` (`<item> @ <path>`) and granular `by_event['aff_click:*']`
    without double-counting if both are emitted simultaneously.
    """
    all_labels = (summary_dict or {}).get("all_by_label") or {}
    by_label = (summary_dict or {}).get("by_label") or {}
    by_event = (summary_dict or {}).get("by_event") or {}

    raw_aff = all_labels.get("affiliate_click")
    if not raw_aff and by_label.get("affiliate_click"):
        raw_pairs = by_label.get("affiliate_click")
        raw_aff = dict(raw_pairs) if not isinstance(raw_pairs, dict) else raw_pairs
    raw_aff = raw_aff or {}

    cat_clicks = collections.defaultdict(int)
    item_clicks = collections.defaultdict(int)
    cat_refs = collections.defaultdict(lambda: collections.defaultdict(int))
    label_refs_by_canon = collections.defaultdict(lambda: collections.defaultdict(int))

    for raw_lbl, cnt in raw_aff.items():
        c = int(cnt or 0)
        if c <= 0:
            continue
        if "@" in str(raw_lbl):
            item_part, ref_p = [p.strip() for p in str(raw_lbl).split("@", 1)]
        else:
            item_part, ref_p = str(raw_lbl).strip(), "-"
        clean_item = _canonicalize_aff_slot_id(item_part)
        cat_k = _classify_offer_category(clean_item)
        cat_clicks[cat_k] += c
        item_clicks[clean_item] += c
        if ref_p and ref_p != "-":
            cat_refs[cat_k][ref_p] += c
            label_refs_by_canon[clean_item][ref_p] += c

    # Also support `by_event['aff_click:<id>']` (and optional `all_by_label['aff_click:<id>']`) without double-counting
    ev_by_canon = collections.defaultdict(int)
    ev_refs_by_canon = collections.defaultdict(lambda: collections.defaultdict(int))
    for ev_k, ev_cnt in by_event.items():
        ev_str = str(ev_k or "").strip()
        if not ev_str.lower().startswith("aff_click:"):
            continue
        c = int(ev_cnt or 0)
        if c <= 0:
            continue
        sub_item = _canonicalize_aff_slot_id(ev_str)
        ev_by_canon[sub_item] += c
        ev_lbl_map = all_labels.get(ev_str) or {}
        if isinstance(ev_lbl_map, dict):
            for ref_p, ref_c in ev_lbl_map.items():
                rp = str(ref_p or "").strip()
                rc_int = int(ref_c or 0)
                if rp and rp != "-" and rc_int > 0:
                    ev_refs_by_canon[sub_item][rp] += rc_int

    for sub_item, ev_total in ev_by_canon.items():
        cat_k = _classify_offer_category(sub_item)
        already = item_clicks[sub_item]
        delta = max(0, ev_total - already)
        if delta > 0:
            cat_clicks[cat_k] += delta
            item_clicks[sub_item] += delta
            lbl_refs = label_refs_by_canon.get(sub_item, {})
            ref_deltas = {
                rp: d_ref
                for rp, ref_c in ev_refs_by_canon.get(sub_item, {}).items()
                if (d_ref := max(0, ref_c - int(lbl_refs.get(rp, 0)))) > 0
            }
            surplus_total = sum(ref_deltas.values())
            if surplus_total > 0:
                if surplus_total <= delta:
                    for rp, d_ref in ref_deltas.items():
                        cat_refs[cat_k][rp] += d_ref
                else:
                    remaining = delta
                    remainders = []
                    for rp, d_ref in ref_deltas.items():
                        exact = (d_ref * delta) / float(surplus_total)
                        base = int(exact)
                        if base > 0:
                            cat_refs[cat_k][rp] += base
                            remaining -= base
                        remainders.append((exact - base, d_ref, rp))
                    for _rem, _d_ref, rp in sorted(remainders, reverse=True):
                        if remaining <= 0:
                            break
                        cat_refs[cat_k][rp] += 1
                        remaining -= 1

    return cat_clicks, cat_refs


def _build_offer_breakdown(stats: dict, stats_7d: dict, days: int) -> list:
    """
    Build Google AI Ultra Affiliate Offer Category Performance Breakdown (`offer_breakdown`):
      - Clicks, share %, 7-day trend, bar chart %, top source page, and category winning pitch.
    """
    cat_clicks, cat_refs = _extract_aff_category_stats(stats)
    cat_clicks_7d, _ = _extract_aff_category_stats(stats_7d)

    total_clicks = sum(cat_clicks.values())
    max_clicks = max(cat_clicks.values(), default=0)

    rows = []
    for meta in OFFER_CATEGORIES:
        ck = meta["category_key"]
        clicks = int(cat_clicks.get(ck, 0))
        clicks_7d = int(cat_clicks_7d.get(ck, 0))
        share_pct = round((clicks / total_clicks) * 100, 1) if total_clicks > 0 else 0.0
        bar_pct = max(6, round((clicks / max_clicks) * 100)) if (clicks > 0 and max_clicks > 0) else 0

        # Compute 7-day trend vs prior window when days > 7, or 7-day active velocity when days <= 7
        if days > 7:
            prev_days = max(1, days - 7)
            prev_clicks = max(0, clicks - clicks_7d)
            pace_7d = clicks_7d / 7.0
            pace_prev = prev_clicks / float(prev_days)
            if clicks_7d == 0 and prev_clicks == 0:
                trend_dir = "zero"
                trend_pct = 0.0
                trend_label = "💤 直近7日 0件（導線強化余地）"
            elif prev_clicks == 0 and clicks_7d > 0:
                trend_dir = "up"
                trend_pct = 100.0
                trend_label = f"🔥 直近7日 +{clicks_7d}件（新規伸長）"
            else:
                trend_pct = round(((pace_7d - pace_prev) / pace_prev) * 100, 1)
                if trend_pct >= 15.0:
                    trend_dir = "up"
                    trend_label = f"📈 直近7日 {clicks_7d}件（+{trend_pct}%）"
                elif trend_pct <= -15.0:
                    trend_dir = "down"
                    trend_label = f"📉 直近7日 {clicks_7d}件（{trend_pct}%）"
                else:
                    trend_dir = "flat"
                    trend_label = f"➡️ 直近7日 {clicks_7d}件（安定推移）"
        else:
            if clicks_7d > 0:
                trend_dir = "up"
                trend_pct = 100.0
                trend_label = f"📈 直近7日 {clicks_7d}件稼働"
            else:
                trend_dir = "zero"
                trend_pct = 0.0
                trend_label = "💤 直近7日 0件（導線強化余地）"

        refs_for_cat = cat_refs.get(ck) or {}
        if refs_for_cat:
            top_ref_path = max(refs_for_cat, key=refs_for_cat.get)
            top_ref_title = column_editorial._sanitize_brand_names(
                analytics.describe_label("page_view", top_ref_path).get("title") or top_ref_path
            )
        elif clicks > 0:
            top_ref_path = "-"
            top_ref_title = "ページ別計測へ移行中（旧リファラ省略分）"
        else:
            top_ref_path = "-"
            top_ref_title = "未発生（これからの導線強化枠）"

        confirmed_epc = round(meta["epc_yen"] * 0.30)
        rows.append({
            "category_key": ck,
            "category": meta["pattern"],
            "label": meta["label"],
            "short_label": meta["short_label"],
            "audience": meta["audience"],
            "clicks": clicks,
            "clicks_7d": clicks_7d,
            "share_pct": share_pct,
            "bar_pct": bar_pct,
            "trend_7d": trend_dir,
            "trend_pct": trend_pct,
            "trend_7d_label": trend_label,
            "est_epc_yen": meta["epc_yen"],
            "est_revenue_yen": clicks * meta["epc_yen"],
            "confirmed_epc_yen": confirmed_epc,
            "est_confirmed_revenue_yen": clicks * confirmed_epc,
            "epc_sample_status": "sample_sufficient" if total_clicks >= 500 else "accumulating",
            "epc_basis": "assumed_fixed",
            "top_source_path": top_ref_path,
            "top_source_title": top_ref_title,
            "recommended_link": meta["primary_link"],
            "winning_pitch": meta["winning_pitch"],
            "_sort_key": (clicks, clicks_7d, meta["default_priority"]),
        })

    rows.sort(key=lambda r: r["_sort_key"], reverse=True)
    for idx, r in enumerate(rows, start=1):
        r["rank"] = idx
        r.pop("_sort_key", None)
    return rows


def _prescribe_page_funnel_upgrade(path: str, grooming_rate_low: bool = False) -> dict:
    """Return concrete funnel links and exact placement instructions tailored to a page's reader intent."""
    p = (path or "").strip()
    p_low = p.lower()

    if p == "/grooming":
        return {
            "recommended_funnel": "主要収益枠の冒頭クイックジャンプバー ＋ 清潔感3点セット即決CTA固定",
            "recommended_links": ["/link/mens_skincare_clinic", "/link/mens_hair_removal", "/link/spu_styleup"],
            "placement_position": "冒頭の「距離70cmチェックリスト」直下にカテゴリ別クイック比較チップ、各パート終端に即決CTA",
            "action_proposal": "せっかく /grooming に到達した顕在読者を途中離脱させないよう、ファーストビュー直下に『肌ケア（/link/mens_skincare_clinic）』『青ヒゲ医療脱毛（/link/mens_hair_removal）』『私服コーデ便（/link/spu_styleup）』『2軒目一休予約（/link/second_venue_reservation）』への即決導線を整理してください。",
        }

    if p == "/for-women" or "women" in p_low or "ladies" in p_low:
        return {
            "recommended_funnel": "女性完全無料・審査制デート枠 ＋ 美容医療＆一休女子会プラン予約枠",
            "recommended_links": ["/link/women_dating_bachelor", "/link/women_beauty_clinic", "/link/women_second_venue"],
            "placement_position": "「待ちゼロ店舗ボード」直下および女子向けメリット解説パートの直後",
            "action_proposal": "待ちゼロ店舗を探す女性読者向けに、店舗ボード直下へ『完全無料・顔出しなしの審査制カフェデート（/link/women_dating_bachelor）』『照明映え美容医療（/link/women_beauty_clinic）』『一休女子会プラン予約（/link/women_second_venue）』の導線を優先表示してください。",
        }

    if "lounge-vs-screened" in p_low or "screened-date" in p_low:
        return {
            "recommended_funnel": "男女別2ステップ比較ブリッジ（/grooming・/for-women の審査制1対1デート枠）",
            "recommended_links": ["/grooming", "/for-women", "/concierge"],
            "placement_position": "比較表直下の「向いている人」分岐ボックスおよび記事末尾の男女別CTAボタン",
            "action_proposal": "『ラウンジか1対1デートか』を比較中の成約直結層です。記事直接広告は避け2ステップ原則を守りつつ、比較表直下に男性向け『/grooming（dating_bachelor・即時アプリ比較）』と女性向け『/for-women（women_dating_bachelor無料枠）』の分岐導線を強化してください。",
        }

    if "dress-code" in p_low or "cleanliness" in p_low:
        grooming_note = "（現在サイト全体の /grooming 遷移率も低めのため最優先）" if grooming_rate_low else ""
        return {
            "recommended_funnel": f"服装・70cm清潔感2ステップ導線（/grooming の SPU私服・ORBIS・ヒゲ脱毛）{grooming_note}",
            "recommended_links": ["/grooming", "/concierge"],
            "placement_position": "「NG服装早見表」直下および「70cm暖色スポット照明」パート末尾の2ステップ内部リンク",
            "action_proposal": "入店直前に服装規定と清潔感を確認している本気層です。NG早見表と照明パート直後に『浮かない大人カジュアル一式＆口元・皮脂対策まとめ（/grooming → spu_styleup / mens_skincare_clinic / mens_hair_removal）』への2ステップ送客リンクを目立たせてください。",
        }

    if "second-venue" in p_low:
        return {
            "recommended_funnel": "2軒目バー空席予約（一休.comレストラン）＋ 乾杯ナイトケア2ステップ導線",
            "recommended_links": ["/grooming", "/for-women", "/areas"],
            "placement_position": "「着席45分の誘い出しフレーズ」直後（店舗詳細2軒目バー＆ /grooming・/for-women 予約枠への内部導線）",
            "action_proposal": "2軒目の店選びで満席難民になりたくない読者向けに、誘い出し会話例の直後で『駅近の隠れ家バー・ディナー空席予約（second_venue_reservation / women_second_venue 枠）』と『乾杯前後のナイトケア習慣（/grooming）』へ2ステップで自然にブリッジしてください。",
        }

    if "male-lead" in p_low:
        return {
            "recommended_funnel": "男性先行時の穴場診断（/concierge）＋ 女性向け待ちゼロ特設ボード（/for-women）",
            "recommended_links": ["/concierge", "/for-women", "/grooming"],
            "placement_position": "「男性先行が起きる時間帯メカニズム」図解直下および記事末尾の男女分岐CTA",
            "action_proposal": "男性読者には待機課金回避の『穴場＆徒歩ハシゴ診断（/concierge）＆今日飲める恋活アプリ比較（/grooming → dating_instant）』を、女性読者には『今すぐ待ちゼロで入れる店舗＆無料特典（/for-women）』を分岐提示してください。",
        }

    if any(k in p_low for k in ("first-10", "beauty-area", "talk", "impression", "clean", "table-change", "thirties")):
        grooming_note = "（現在サイト全体の /grooming 遷移率も低めのため最優先）" if grooming_rate_low else ""
        return {
            "recommended_funnel": f"会話前の清潔感ケア特集（/grooming）＋ スキンケア・医療脱毛2ステップ導線{grooming_note}",
            "recommended_links": ["/grooming", "/for-women", "/concierge"],
            "placement_position": "記事中盤「女性目線の減点サイン（会話ボックス直後）」に /grooming 内部リンク、末尾に男女別ガイド導線",
            "action_proposal": "会話術や席替え・年齢層を調べる読者は対面直前の本気層です。会話例の直後に『実は会話以前に距離70cmで見られている清潔感チェック（/grooming → mens_skincare_clinic / mens_hair_removal / spu_styleup）』を挟み2ステップ送客してください。",
        }

    if any(k in p_low for k in ("last-train", "weekend", "cost-break", "price", "local-vs-urban", "midnight", "hopping", "stats", "crowd-rush", "waveform", "solo-vs-pair", "osaka")):
        return {
            "recommended_funnel": "穴場＆徒歩ハシゴ診断（/concierge）＋ 混雑回避・審査制1対1デート比較2ステップ導線",
            "recommended_links": ["/concierge", "/guide/lounge-vs-screened-date", "/grooming"],
            "placement_position": "実測データ比較表の直下（「男性待ち・損益分岐ライン」解説横）および記事末尾CTA枠",
            "action_proposal": "待ち時間や料金コスパを検証している読者に刺さるよう、データ表直下に『徒歩圏の穴場診断（/concierge）』と『男余りの夜に切り替えられる恋活・審査制1対1デート比較（/grooming・/guide/lounge-vs-screened-date）』を配置してください。",
        }

    if p.startswith("/store/"):
        return {
            "recommended_funnel": "店舗別2軒目バー予約（一休.comレストラン）＋ 満席時の近隣ハシゴ診断（/concierge）",
            "recommended_links": ["/link/second_venue_reservation", "/concierge", "/grooming"],
            "placement_position": "店舗詳細「2軒目スポット提案」直下の一休予約バーおよび「男性先行」バッジ横の回遊リンク",
            "action_proposal": "個別店舗の現在人数を見ている来店直前層です。2軒目マップ検索バー直下の『一休.comレストラン空席予約（/link/second_venue_reservation）』と、混雑時の『穴場＆徒歩ハシゴ診断（/concierge）』への視認性を高めてください。",
        }

    if p == "/" or p.startswith(("/area/", "/areas", "/oriental-lounge", "/ag", "/jis", "/asobibar", "/concierge")):
        return {
            "recommended_funnel": "4位・10位直下の時間帯連動インフィード2枠（rank4_context / rank10_context）＋ 2軒目一休予約最適化",
            "recommended_links": ["/link/dating_bachelor", "/link/second_venue_reservation", "/link/dating_instant"],
            "placement_position": "トップ4位直下（出会い・1対1軸）＆ 10位直下（夜:一休2軒目予約／夕方:乾杯・肌ケア／昼:私服SPU）および店舗モーダル下部",
            "action_proposal": "人数確認だけで離脱する層を2段階で受け止めるため、4位直下の出会い系文脈枠（rank4_context）と10位直下の2軒目予約・身だしなみハイブリッド枠（rank10_context）のCTR推移を重点監視してください。",
        }

    return {
        "recommended_funnel": "清潔感ガイド（/grooming）経由の私服コーデ（SPU）＋ 1対1デート比較（2ステップ導線）",
        "recommended_links": ["/grooming", "/guide/lounge-vs-screened-date", "/for-women"],
        "placement_position": "中盤の会話ボックス直後に /grooming 文脈リンク、記事末尾に男女別ガイド導線",
        "action_proposal": "記事読了前の途中離脱を防ぐため、中盤の体験談直後に『浮かない私服＆清潔感の基準（/grooming）』への内部リンクを置き、末尾に『相席 vs 審査制1対1デート比較（/guide/lounge-vs-screened-date）』の受け皿を足してください。",
    }


def _detect_monetization_gaps(
    pv_by_path: dict,
    aff_by_ref_path: dict,
    known_cols: list,
    cushion_by_ref_path: dict = None,
    active_phase: str = "phase1_inflow",
    effective_daily_pv: float = None,
) -> list:
    """
    Auto-detect "High PV × Low CVR" opportunity-loss pages (`monetization_gaps`) and prescribe
    which funnel (`/grooming`,混雑回避アプリ, 女性特典 etc.) to add at which exact position.
    """
    cushion_map = cushion_by_ref_path or {}
    col_title_map = {c["path"]: c["title"] for c in (known_cols or [])}
    total_col_pv = sum(int(pv_by_path.get(c["path"], 0)) for c in (known_cols or []))
    grooming_pv = int(pv_by_path.get("/grooming", 0))
    grooming_rate_low = grooming_pv < max(2, int(total_col_pv * 0.08))

    non_monetized_exact = {
        "/privacy",
        "/terms",
        "/about",
        "/contact",
        "/editorial-policy",
        "/methodology",
        "/stats",
    }
    candidate_paths = []
    seen_p = set()
    for p in list(pv_by_path.keys()) + list(col_title_map.keys()):
        if (
            not p
            or p in seen_p
            or p in non_monetized_exact
            or p.startswith(("/staff", "/stats/", "/api/", "/ogp/"))
        ):
            continue
        seen_p.add(p)
        candidate_paths.append(p)

    gaps_primary = []
    gaps_watch = []

    for p in candidate_paths:
        pv = int(pv_by_path.get(p, 0))
        if pv <= 0:
            continue
        aff = int(aff_by_ref_path.get(p, 0))
        cush = int(cushion_map.get(p, 0))
        cvr_pct = round((aff / pv) * 100, 2)
        bridge_rate_pct = round((cush / pv) * 100, 2)

        if p in ("/grooming", "/for-women"):
            target_cvr = 4.0
        elif p.startswith("/guide"):
            target_cvr = 2.5
        elif p in ("/price-comparison", "/stats", "/columns"):
            target_cvr = 1.8
        else:
            target_cvr = 1.2

        # Pages already converting near or above target CVR (or columns with >=8 PV meeting >=8.0% 2-step cushion bridge rate) are winners, not gaps
        if aff > 0 and cvr_pct >= target_cvr * 0.8:
            continue
        if p.startswith("/guide") and pv >= 8 and cush > 0 and bridge_rate_pct >= 8.0:
            continue

        raw_title = col_title_map.get(p) or analytics.describe_label("page_view", p).get("title") or p
        clean_title = column_editorial._sanitize_brand_names(raw_title)
        rx = _prescribe_page_funnel_upgrade(p, grooming_rate_low=grooming_rate_low)

        est_missed = max(1, int(round(pv * (target_cvr / 100.0) - aff)))
        intent_weight = 1.45 if (p.startswith("/guide") or p in ("/price-comparison", "/for-women", "/grooming")) else 1.0
        effective_pct = max(cvr_pct, round(bridge_rate_pct * 0.3, 2)) if p.startswith("/guide") else cvr_pct
        gap_score = round(pv * max(0.4, target_cvr - effective_pct) * intent_weight, 2)

        defer_in_phase1 = (
            active_phase == "phase1_inflow"
            and effective_daily_pv is not None
            and float(effective_daily_pv) < 50.0
            and pv < 50
        )
        if defer_in_phase1:
            sev = "deferred_phase1"
            sev_badge = "⏸ Phase 1 保留（流入50PV/日到達後に着手）"
        elif pv >= 10 and aff == 0 and cush == 0:
            sev = "critical"
            sev_badge = "🔥 最優先（高PV・CVゼロ）"
        elif pv >= 8 and effective_pct < target_cvr * 0.5:
            sev = "high"
            sev_badge = "⚠️ 要改善（高PV・低CVR）"
        elif pv >= 3:
            sev = "medium"
            sev_badge = "🔍 導線チューニング推奨"
        else:
            sev = "watch"
            sev_badge = "📊 初期観測（CV未発火）"

        entry = {
            "path": p,
            "title": clean_title,
            "pv": pv,
            "aff_clicks": aff,
            "cushion_clicks": cush,
            "bridge_rate_pct": bridge_rate_pct,
            "cvr_pct": cvr_pct,
            "target_cvr_pct": target_cvr,
            "grooming_pv": grooming_pv,
            "est_missed_clicks": est_missed,
            "est_missed_yen": est_missed * 110,
            "severity": sev,
            "severity_badge": sev_badge,
            "recommended_funnel": rx["recommended_funnel"],
            "recommended_links": rx["recommended_links"],
            "placement_position": rx["placement_position"],
            "action_proposal": rx["action_proposal"],
            "_gap_score": gap_score,
        }
        if pv >= 3:
            gaps_primary.append(entry)
        else:
            gaps_watch.append(entry)

    gaps_primary.sort(key=lambda x: x["_gap_score"], reverse=True)
    gaps_watch.sort(key=lambda x: x["_gap_score"], reverse=True)
    merged = (gaps_primary + gaps_watch)[:5]

    # Cold-start fallback when analytics has 0 PV across all pages
    if not merged and sum(int(v or 0) for v in pv_by_path.values()) == 0:
        for seed_p in ("/guide/second-venue", "/guide/last-train-cliff", "/price-comparison"):
            raw_title = col_title_map.get(seed_p) or analytics.describe_label("page_view", seed_p).get("title") or seed_p
            rx = _prescribe_page_funnel_upgrade(seed_p, grooming_rate_low=True)
            merged.append({
                "path": seed_p,
                "title": column_editorial._sanitize_brand_names(raw_title),
                "pv": 0,
                "aff_clicks": 0,
                "cvr_pct": 0.0,
                "target_cvr_pct": 2.5 if seed_p.startswith("/guide") else 1.8,
                "grooming_pv": 0,
                "est_missed_clicks": 2,
                "est_missed_yen": 220,
                "severity": "cold_start",
                "severity_badge": "📌 初期重点導線チェック枠",
                "recommended_funnel": rx["recommended_funnel"],
                "recommended_links": rx["recommended_links"],
                "placement_position": rx["placement_position"],
                "action_proposal": rx["action_proposal"],
            })
        return merged

    for item in merged:
        item.pop("_gap_score", None)
    return merged


def _classify_sns_tag(tag: str) -> tuple:
    """Classify a normalized `?s=` or `ref_*` inflow tag into `(channel_key, hour_str, style_key)`."""
    t = (tag or "").strip().lower()
    if t.startswith("sns_inflow:"):
        t = t.split(":", 1)[1].strip()
    for pfx in ("cvslot:", "cush:", "cv:"):
        if t.startswith(pfx):
            t = t[len(pfx):].split("@", 1)[0].strip()
            break
    if not t:
        return "x", "", "live"

    if t in ("short_bio", "shorts_bio", "ref_youtube") or t.startswith(("yt_", "youtube")):
        return "youtube_shorts", "20:00", t

    if t in ("ref_google", "ref_yahoo_bing", "ref_ai_search"):
        return "search", "21:00", t

    if t.startswith("moshimo"):
        return "reels", "20:00", t

    if t == "ref_tiktok" or any(t.startswith(px) for px in ("reel", "ig_reel", "short", "tiktok")):
        st = re.sub(r"^(?:ig_)?reels?_?|^tiktok_?|^shorts?_?", "", t) or "short_hook"
        return "reels", "20:00" if "moshimo" in t else "21:30", st

    if t == "ref_x":
        return "x", "21:00", "ref_x"
    m_x = re.match(r"^x(\d{2})_(.+)$", t)
    if m_x:
        return "x", f"{m_x.group(1)}:00", m_x.group(2)
    if t == "x_rel":
        return "x", "19:00", "rel"
    if t == "x_wk":
        return "x", "20:00", "wk"
    if t == "x":
        return "x", "20:00", "live"

    if t == "ref_threads":
        return "threads", "20:30", "ref_threads"
    m_th = re.match(r"^th(\d{2})_(.+)$", t)
    if m_th:
        return "threads", f"{m_th.group(1)}:00", m_th.group(2)
    if t.startswith(("th_", "threads")):
        st = re.sub(r"^th(?:reads)?_?", "", t) or "threads_city_story"
        return "threads", "20:30", st

    if t == "ref_instagram":
        return "instagram", "20:00", "ref_instagram"
    m_ig = re.match(r"^ig(\d{2})_(.+)$", t)
    if m_ig:
        return "instagram", f"{m_ig.group(1)}:00", m_ig.group(2)
    if t.startswith(("ig", "insta")):
        st = re.sub(r"^insta(?:gram)?_?|^ig_?", "", t) or "ig_empathy_story"
        return "instagram", "19:30", st

    if t == "ref_note" or t.startswith("note"):
        return "threads", "12:30", t

    return "x", "22:00", t


def _build_sns_channel_efficiency(
    stats: dict,
    inflow_cohorts: list,
    x_hist: list,
    th_hist: list,
    ig_hist: list,
    demand_gap: dict,
    offer_breakdown: list,
    reels_logged_extra: int = 0,
    yt_shorts_logged: int = 0,
    active_phase: str = "phase1_inflow",
) -> dict:
    """
    Compare Search / Reels / YouTube Shorts / X / Threads / Instagram inflow efficiency,
    surface the #1 acquisition channel, best time slot & editorial style, and generate Reels video concepts.
    """
    is_inflow_first = active_phase in ("phase1_inflow", "phase2_engagement")
    ig_carousel_logged = sum(
        1 for h in (ig_hist or [])
        if not str(h.get("selected_style") or "").lower().startswith(("reel", "short", "tiktok", "moshimo"))
    )
    ig_reels_logged = sum(
        1 for h in (ig_hist or [])
        if str(h.get("selected_style") or "").lower().startswith(("reel", "short", "tiktok", "moshimo"))
    ) + int(reels_logged_extra or 0)

    channel_defs = [
        {
            "channel_key": "search",
            "label": "🔍 自然検索・AI検索 (Google / AI)",
            "default_slot": "19:00〜23:00（入店前リサーチ・エリア比較帯）",
            "default_style": "店舗・エリア比較SEO＆一次統計コラム (ref_google)",
            "posts_logged": 0,
            "priority": 6 if is_inflow_first else 4,
            "base_tactic": "Search Consoleのお宝クエリ（4〜20位・高表示）のタイトル最適化と、ドーム終演・エリア比較コラムの予約公開で検索・AI引用流入を複利拡大。",
        },
        {
            "channel_key": "reels",
            "label": "🎬 IGリール / TikTok（3Dもしも＆ライブ速報）",
            "default_slot": "20:00（平日『もしもシリーズ』自動配信＋土曜21:30速報）",
            "default_style": "3Dボクセル『もしもシリーズ』＆ギャップ実証型",
            "posts_logged": ig_reels_logged,
            "priority": 5,
            "base_tactic": "月〜金20:00は3D『もしもシリーズ』キューから自動投稿し、週末ピークは同駅徒歩5分ギャップ実証リールで非フォロワー層をプロフリンクへ誘導。",
        },
        {
            "channel_key": "youtube_shorts",
            "label": "📺 YouTubeショート (@aiseki_navi)",
            "default_slot": "20:00（平日『もしもシリーズ』同時展開）",
            "default_style": "3Dボクセル『もしもシリーズ』35秒検証 (short_bio)",
            "posts_logged": int(yt_shorts_logged or 0),
            "priority": 5,
            "base_tactic": "SNS編集部のYouTubeショート連携パックからタイトル・固定コメントを展開し、プロフ『?s=short_bio』経由の検索・関連動画流入を獲得。",
        },
        {
            "channel_key": "x",
            "label": "𝕏 X（旧Twitter）",
            "default_slot": "22:00（1軒目→2軒目移動前）",
            "default_style": "街別ギャップ・ハシゴ型 (hub_gap)",
            "posts_logged": len(x_hist or []),
            "priority": 4,
            "base_tactic": "20時・22時速報の1行目フックを『具体店名×普段比ギャップ』に寄せ、リプ欄に高流入コラム＋?s=識別リンクを徹底固定。",
        },
        {
            "channel_key": "threads",
            "label": "🧵 Threads（スレッズ）",
            "default_slot": "20:30（仕事帰り・女子組リサーチ帯）",
            "default_style": "街の温度差エッセイ型 (threads_city_story)",
            "posts_logged": len(th_hist or []),
            "priority": 3,
            "base_tactic": "宣伝感を消した『データ観察エッセイ・男女の本音』で共感保存を取り、奇数日リンク＆プロフ導線でトップと /for-women へ送客。",
        },
        {
            "channel_key": "instagram",
            "label": "📸 Instagram（カルーセル・保存枠）",
            "default_slot": "19:30（木〜土曜の予定決め・保存帯）",
            "default_style": "週末予測カルーセル＆女子得保存ガイド",
            "posts_logged": ig_carousel_logged,
            "priority": 2,
            "base_tactic": "SNS編集部の週末5枚組スライドで保存数を稼ぎ、ストーリーズのリンクスタンプ（?s=ig_story）からリアルタイム順位へ直結。",
        },
    ]

    ch_visits = collections.defaultdict(int)
    ch_cvs = collections.defaultdict(int)
    ch_cushions = collections.defaultdict(int)
    ch_rev_yen = collections.defaultdict(int)
    ch_hours = collections.defaultdict(lambda: collections.defaultdict(int))
    ch_styles = collections.defaultdict(lambda: collections.defaultdict(int))
    style_agg = collections.defaultdict(lambda: {"visits": 0, "conversions": 0, "est_revenue_yen": 0, "channel_key": "x"})
    seen_tag_visits = collections.defaultdict(int)
    seen_tag_cvs = collections.defaultdict(int)

    for c in (inflow_cohorts or []):
        tag = c.get("tag") or ""
        norm_t = analytics.normalize_inflow_source(tag) or tag.strip().lower()
        v = int(c.get("visits") or 0)
        cv = int(c.get("conversions") or 0)
        cush = int(c.get("cushion_clicks") or 0)
        rev_y = int(c.get("est_revenue_yen") or 0)
        ck, hr, st = _classify_sns_tag(tag)
        ch_visits[ck] += v
        ch_cvs[ck] += cv
        ch_cushions[ck] += cush
        ch_rev_yen[ck] += rev_y
        seen_tag_visits[norm_t] += v
        seen_tag_cvs[norm_t] += cv
        if is_inflow_first:
            score_w = v * 4 + cush * 2 + cv
        else:
            score_w = v + cush * 2 + cv * 5 + int(rev_y / 60)
        if hr:
            ch_hours[ck][hr] += score_w
        if st:
            ch_styles[ck][st] += score_w
            style_agg[st]["visits"] += v
            style_agg[st]["conversions"] += cv
            style_agg[st]["est_revenue_yen"] += rev_y
            style_agg[st]["channel_key"] = ck

    # Support optional `by_event['sns_inflow:<channel_or_tag>']` without double-counting or dropping sibling tags
    bare_channel_keys = {"x", "threads", "instagram", "reels", "tiktok", "note", "youtube_shorts", "search"}
    by_event = (stats or {}).get("by_event") or {}
    for ev_k, ev_cnt in by_event.items():
        ev_str = str(ev_k or "").strip()
        if not ev_str.lower().startswith("sns_inflow:"):
            continue
        sub = ev_str.split(":", 1)[1].strip()
        if sub.lower().startswith(("cush:", "cvslot:")):
            continue
        if sub.lower().startswith("cv:"):
            raw_cv_tag = sub[3:].strip()
            norm_cv_tag = analytics.normalize_inflow_source(raw_cv_tag) or raw_cv_tag.lower()
            ck_cv, _, st_cv = _classify_sns_tag(raw_cv_tag)
            already_cv = ch_cvs[ck_cv] if norm_cv_tag in bare_channel_keys else seen_tag_cvs[norm_cv_tag]
            delta_cv = max(0, int(ev_cnt or 0) - already_cv)
            if delta_cv > 0:
                ch_cvs[ck_cv] += delta_cv
                seen_tag_cvs[norm_cv_tag] += delta_cv
                if st_cv:
                    style_agg[st_cv]["conversions"] += delta_cv
        else:
            norm_v_tag = analytics.normalize_inflow_source(sub) or sub.lower()
            ck_v, hr_v, st_v = _classify_sns_tag(sub)
            already_v = ch_visits[ck_v] if norm_v_tag in bare_channel_keys else seen_tag_visits[norm_v_tag]
            delta_v = max(0, int(ev_cnt or 0) - already_v)
            if delta_v > 0:
                ch_visits[ck_v] += delta_v
                seen_tag_visits[norm_v_tag] += delta_v
                if hr_v:
                    ch_hours[ck_v][hr_v] += delta_v * (4 if is_inflow_first else 1)
                if st_v:
                    ch_styles[ck_v][st_v] += delta_v * (4 if is_inflow_first else 1)
                    style_agg[st_v]["visits"] += delta_v

    total_tracked_inflows = sum(ch_visits.values())
    total_search_visits = int(ch_visits.get("search", 0))
    total_short_video_visits = int(ch_visits.get("reels", 0)) + int(ch_visits.get("youtube_shorts", 0))
    total_sns_visits = max(0, total_tracked_inflows - total_search_visits)
    share_denom = total_tracked_inflows if total_tracked_inflows > 0 else 0
    channels_out = []
    for cdef in channel_defs:
        ck = cdef["channel_key"]
        v = int(ch_visits.get(ck, 0))
        cv = int(ch_cvs.get(ck, 0))
        cush_c = int(ch_cushions.get(ck, 0))
        rev_y = int(ch_rev_yen.get(ck, 0))
        if rev_y == 0 and cv > 0:
            rev_y = cv * 95
        epc_v = round(rev_y / float(v), 1) if v > 0 else 0.0
        cvr = round((cv / v) * 100, 1) if v > 0 else 0.0
        share_pct = round((v / share_denom) * 100, 1) if share_denom > 0 else 0.0
        if is_inflow_first:
            eff_score = round(v * 10.0 + cush_c * 2.5 + cv * 1.5, 1)
        else:
            eff_score = round(cv * 15.0 + cush_c * 3.0 + (rev_y / 35.0) + v * 2.5 + cvr * 0.3, 1)

        if ch_hours.get(ck):
            top_hr = max(ch_hours[ck], key=ch_hours[ck].get)
            best_slot = f"{top_hr}枠（実測トップ反応帯）"
        else:
            best_slot = cdef["default_slot"]

        if ch_styles.get(ck):
            top_st = max(ch_styles[ck], key=ch_styles[ck].get)
            st_ja = analytics._INFLOW_STYLE_JA.get(top_st) or sns_editorial.STYLE_LABELS.get(top_st) or top_st
            best_style = f"{st_ja} ({top_st})"
        else:
            best_style = cdef["default_style"]

        channels_out.append({
            "channel_key": ck,
            "label": cdef["label"],
            "visits": v,
            "cushion_clicks": cush_c,
            "conversions": cv,
            "est_revenue_yen": rev_y,
            "epc_yen": epc_v,
            "cvr_pct": cvr,
            "share_pct": share_pct,
            "posts_logged": cdef["posts_logged"],
            "efficiency_score": eff_score,
            "best_time_slot": best_slot,
            "best_style_label": best_style,
            "tactic": cdef["base_tactic"],
            "_sort_key": (v, eff_score, cdef["priority"]) if is_inflow_first else (eff_score, v, cdef["priority"]),
        })

    channels_out.sort(key=lambda x: x["_sort_key"], reverse=True)
    max_eff_or_v = max([max(1.0, c["efficiency_score"]) for c in channels_out if c["visits"] > 0 or c["conversions"] > 0], default=0.0)

    for idx, ch in enumerate(channels_out, start=1):
        ch["rank"] = idx
        ch.pop("_sort_key", None)
        if max_eff_or_v > 0 and (ch["visits"] > 0 or ch["conversions"] > 0):
            ch["bar_pct"] = max(8, round((max(1.0, ch["efficiency_score"]) / max_eff_or_v) * 100))
        else:
            ch["bar_pct"] = 0
        if idx == 1 and (ch["visits"] > 0 or ch["conversions"] > 0):
            ch["status_badge"] = "🥇 サイト流入牽引 No.1" if is_inflow_first else "🥇 サイト誘導効率 No.1"
        elif ch["channel_key"] in ("search", "reels", "youtube_shorts") and ch["visits"] == 0:
            ch["status_badge"] = "🚀 最優先グロース枠"
        elif ch["visits"] > 0:
            ch["status_badge"] = "📈 流入稼働中"
        else:
            ch["status_badge"] = "🌱 計測強化枠"

    style_ranking = []
    for st_k, sdata in style_agg.items():
        sv = sdata["visits"]
        scv = sdata["conversions"]
        scvr = round((scv / sv) * 100, 1) if sv > 0 else 0.0
        st_ja = analytics._INFLOW_STYLE_JA.get(st_k) or sns_editorial.STYLE_LABELS.get(st_k) or st_k
        style_ranking.append({
            "style_key": st_k,
            "style_label": st_ja,
            "channel_key": sdata["channel_key"],
            "visits": sv,
            "conversions": scv,
            "cvr_pct": scvr,
            "score": (sv * 4 + scv) if is_inflow_first else (sv + scv * 6),
        })
    style_ranking.sort(key=lambda x: x["score"], reverse=True)

    top_ch = channels_out[0]
    if style_ranking:
        rec_style_key = style_ranking[0]["style_key"]
        rec_style_label = style_ranking[0]["style_label"]
    else:
        rec_style_key = "hub_gap"
        rec_style_label = "街別ギャップ・ハシゴ観察型"

    hub_info = (demand_gap or {}).get("top_hub") or {}
    hub_name = hub_info.get("cluster_label") or "梅田・茶屋町"
    has_measured_hub = bool(hub_info.get("has_measured_gap"))
    gap_pt = max(12, int(hub_info.get("gap_pt") or 15))
    live_w = max(28, int(hub_info.get("live_women") or 48))
    top_offer_label = offer_breakdown[0]["short_label"] if offer_breakdown else "メンズスキンケア"

    if has_measured_hub:
        reel1_title = f"【15秒・街別格差】同じ{hub_name}・徒歩5分で男女比が{gap_pt}%割れる金曜22時のリアル"
        reel1_outline = f"スマホのリアルタイム画面を映しながら、徒歩5分圏内のオリエンタルラウンジ・ag・JISで女性{live_w}名規模でも男女比が{gap_pt}pt割れる実測ギャップを3カットで比較。"
    else:
        reel1_title = f"【15秒・街別格差】同じ{hub_name}・徒歩5分で店舗ごとに男女比が大きく割れる金曜22時のリアル"
        reel1_outline = "スマホのリアルタイム画面を映しながら、徒歩5分圏内のオリエンタルラウンジ・ag・JISで同じ時間帯でも男女比が大きく割れる現場構造を3カットで比較。"

    reels_recommendations = [
        {
            "idea_no": 1,
            "theme_tag": "reel_hub_gap",
            "title": reel1_title,
            "hook_0_3s": f"「正直、金曜の{hub_name}で看板だけ見て1軒目に並ぶ人、かなり損してます…」",
            "body_outline": reel1_outline,
            "cta_route": "プロフリンク『?s=reel_hub_gap』（YTショートは『?s=short_bio』）→ トップ現在人数ボード",
            "best_post_time": "金曜・土曜 21:15〜21:45",
            "data_status": "measured" if has_measured_hub else "prior_estimate",
        },
        {
            "idea_no": 2,
            "theme_tag": "reel_grooming_70cm",
            "title": "【20秒・女子の本音】乾杯して最初の10分、ソファ席70cmで密かに減点される3つの瞬間",
            "hook_0_3s": "「女子トイレの反省会で一番よく出る『惜しい男性』の共通点、実は会話じゃなくて…」",
            "body_outline": f"①間接照明で浮く夕方の肌テカリ ②19時の青ヒゲ ③サイズ感の合わない服をテンポよく文字入れし、入店前3分の対策（{top_offer_label}）へ自然に繋ぐ。",
            "cta_route": "プロフまたはハイライト『?s=reel_grooming』→ /grooming 身だしなみ特集",
            "best_post_time": "木曜・金曜 18:30〜19:30",
        },
        {
            "idea_no": 3,
            "theme_tag": "reel_40min_rule",
            "title": "【18秒・損益分岐】男余りの店で90分粘って課金する人 vs 40分で見切って勝つ人の差",
            "hook_0_3s": "「相席で一番お金と時間を溶かすパターン、40分で見切れないサンクコストです」",
            "body_outline": "滞在時間と課金メーターの損益分岐グラフを1枚見せ、渋い夜は40分で2軒目移動または近隣穴場へ切り替える常連の立ち回りを解説。",
            "cta_route": "プロフリンク『?s=reel_40min』→ /concierge 穴場診断 ＆ /guide/cost-break-even",
            "best_post_time": "水曜・金曜 20:30〜21:00",
        },
    ]

    if total_tracked_inflows > 0:
        summary_insight = (
            f"現在のサイト流入首位は『{top_ch['label']}』（純流入 {top_ch['visits']}件 ／ 検索 {total_search_visits}件・ショート動画 {total_short_video_visits}件）。"
            f"主力時間帯は {top_ch['best_time_slot']}、伸びている切り口は『{rec_style_label}』です。"
            f"Google/AI検索SEOと平日20:00の3D『もしもシリーズ』（IGリール＋YouTubeショート）の2本柱で流入母数を拡大します。"
        )
    else:
        summary_insight = (
            "外部純新規流入（検索『ref_google』・ショート動画『?s=short_bio / reel_*』・SNS『?s=』）の初期ブースト段階です。"
            "最優先2軸である『Google/AI検索SEO（店舗・エリア・ドーム終演特集）』と『縦型ショート動画（平日20:00 3Dもしも＋週末ギャップ実証）』から着地母数を作ります。"
        )

    return {
        "top_channel_key": top_ch["channel_key"],
        "top_channel_label": top_ch["label"],
        "recommended_time_slot": top_ch["best_time_slot"],
        "recommended_style_key": rec_style_key,
        "recommended_style_label": rec_style_label,
        "total_sns_visits": total_sns_visits,
        "total_search_visits": total_search_visits,
        "total_short_video_visits": total_short_video_visits,
        "total_tracked_inflows": total_tracked_inflows,
        "total_sns_conversions": sum(v for k, v in ch_cvs.items() if k != "search"),
        "total_search_conversions": int(ch_cvs.get("search", 0)),
        "summary_insight": summary_insight,
        "channels": channels_out,
        "style_ranking": style_ranking[:8],
        "reels_recommendations": reels_recommendations,
    }


def _build_flywheel_status(
    closed_loop_feedback: dict,
    sns_channel_efficiency: dict,
    upcoming_cols: list,
    settings: dict,
    demand_gap: dict,
    allow_blocking: bool = False,
) -> dict:
    """
    Build unified 4-Gear Growth Flywheel telemetry (`flywheel_status`) for `/staff/pdca`:
      Gear 1: 3D Moshimo Reel Queue (`ep1`..`ep10` + Evergreen Recycle)
      Gear 2: YouTube Shorts Retention & `?s=short_bio` / `ref_youtube` Inflows
      Gear 3: Google Search Console Organic Impressions & `ref_google` / `ref_ai_search` Inflows
      Gear 4: Autonomous Scheduled Column Pipeline (`upcoming_cols` + `auto_replenish_columns`)
    """
    try:
        import moshimo_reel_queue
        mq = moshimo_reel_queue.get_moshimo_queue_status(allow_blocking=allow_blocking)
    except Exception:
        mq = {
            "enabled": True,
            "evergreen_recycle": True,
            "total_episodes": 0,
            "posted_count": 0,
            "remaining_count": 0,
            "next_episode": None,
            "last_posted_at": "",
        }

    ch_map = {c["channel_key"]: c for c in (sns_channel_efficiency.get("channels") or [])}
    yt_ch = ch_map.get("youtube_shorts") or {}
    srch_ch = ch_map.get("search") or {}
    reels_ch = ch_map.get("reels") or {}

    yt_diag = dict((closed_loop_feedback or {}).get("youtube_shorts_diagnostics") or {})
    gsc_diag = dict((closed_loop_feedback or {}).get("gsc_diagnostics") or {})

    hub_name = ((demand_gap or {}).get("top_hub") or {}).get("cluster_label") or "梅田・新宿"
    opp_q = ((gsc_diag.get("opportunity_queries") or [{}])[0]).get("query") if gsc_diag.get("opportunity_queries") else ""

    next_moshimo_ideas = [
        f"もし『{hub_name}の徒歩4分先』の男女比が頭の上に数字で見えたら？（街別ギャップ検証）",
        "もし相席ラウンジの『19時ディナー帯』と『22時二次会帯』が別の扉だったら？（タダ飯論争の真実）",
        (
            f"もし検索急上昇『{opp_q}』の答えを実測混雑ログで30秒再現したら？"
            if opp_q
            else "もしマッチングアプリの『2週間メッセージ』が対面の乾杯直後に短縮されたら？（アプリ疲れ検証）"
        ),
    ]

    try:
        import youtube_analytics_sync
        yt_oauth_info = youtube_analytics_sync.get_youtube_sync_status(allow_blocking=False)
    except Exception:
        yt_oauth_info = {}

    next_col = upcoming_cols[0] if upcoming_cols else None
    return {
        "moshimo_queue": {
            "enabled": bool(mq.get("enabled", True)),
            "evergreen_recycle": bool(mq.get("evergreen_recycle", True)),
            "total_episodes": int(mq.get("total_episodes") or 0),
            "posted_count": int(mq.get("posted_count") or 0),
            "remaining_count": int(mq.get("remaining_count") or 0),
            "next_episode": mq.get("next_episode"),
            "last_posted_at": str(mq.get("last_posted_at") or ""),
            "ig_reels_visits": int(reels_ch.get("visits") or 0),
            "ig_reels_conversions": int(reels_ch.get("conversions") or 0),
            "next_moshimo_ideas": next_moshimo_ideas,
        },
        "youtube_shorts": {
            "data_status": yt_diag.get("data_status") or "insufficient",
            "api_connected": bool(yt_oauth_info.get("connected")),
            "channel_title": str(yt_oauth_info.get("channel_title") or ""),
            "custom_url": str(yt_oauth_info.get("custom_url") or ""),
            "last_sync_at": str(yt_oauth_info.get("last_sync_at") or ""),
            "sample_count": int(yt_diag.get("sample_count") or 0),
            "total_views": yt_diag.get("total_views"),
            "avg_stayed_to_watch_pct": yt_diag.get("avg_stayed_to_watch_pct"),
            "avg_pct_viewed": yt_diag.get("avg_pct_viewed"),
            "avg_watch_sec": yt_diag.get("avg_watch_sec"),
            "subscribers_gained": yt_diag.get("subscribers_gained"),
            "site_visits": int(yt_ch.get("visits") or 0),
            "site_cushion_clicks": int(yt_ch.get("cushion_clicks") or 0),
            "site_conversions": int(yt_ch.get("conversions") or 0),
            "bio_tracking_url": "https://aiseki-navi.com/?s=short_bio",
        },
        "search_console": {
            "data_status": gsc_diag.get("data_status") or "insufficient",
            "updated_at": str(gsc_diag.get("updated_at") or ""),
            "total_clicks": gsc_diag.get("total_clicks"),
            "total_impressions": gsc_diag.get("total_impressions"),
            "avg_ctr_pct": gsc_diag.get("avg_ctr_pct"),
            "avg_position": gsc_diag.get("avg_position"),
            "opportunity_count": len(gsc_diag.get("opportunity_queries") or []),
            "site_visits": int(srch_ch.get("visits") or 0),
            "site_cushion_clicks": int(srch_ch.get("cushion_clicks") or 0),
            "site_conversions": int(srch_ch.get("conversions") or 0),
        },
        "column_pipeline": {
            "upcoming_count": len(upcoming_cols or []),
            "auto_replenish_columns": bool((settings or {}).get("auto_replenish_columns", False)),
            "next_publish_at_ja": str(next_col.get("publish_at_ja") or next_col.get("publish_at") or "") if next_col else "",
            "next_title": str(next_col.get("short_title") or next_col.get("title") or "") if next_col else "",
            "upcoming_titles": [
                {
                    "slug": c.get("slug"),
                    "path": c.get("path"),
                    "title": c.get("short_title") or c.get("title"),
                    "publish_at_ja": c.get("publish_at_ja") or c.get("publish_at"),
                }
                for c in (upcoming_cols or [])[:4]
            ],
        },
    }


def _sum_external_inflow_visits(summary_dict: dict) -> dict:
    """Count pure external arrival visits (`sns_inflow` excluding `cv:` / `cush:` / `cvslot:`) grouped by acquisition pillar."""
    all_labels = (summary_dict or {}).get("all_by_label") or {}
    inflow_map = all_labels.get("sns_inflow") or {}
    by_event = (summary_dict or {}).get("by_event") or {}
    bare_channel_keys = {"x", "threads", "instagram", "reels", "tiktok", "note", "youtube_shorts", "search"}

    ch_counts = collections.defaultdict(int)
    seen_tags = collections.defaultdict(int)

    for raw_lbl, cnt in inflow_map.items():
        c_int = int(cnt or 0)
        if c_int <= 0:
            continue
        src_part = str(raw_lbl).split("@", 1)[0].strip() if "@" in str(raw_lbl) else str(raw_lbl).strip()
        if src_part.lower().startswith(("cv:", "cush:", "cvslot:")):
            continue
        norm_t = analytics.normalize_inflow_source(src_part) or src_part.lower()
        if not norm_t:
            continue
        ck, _, _ = _classify_sns_tag(norm_t)
        ch_counts[ck] += c_int
        seen_tags[norm_t] += c_int

    for ev_k, ev_cnt in by_event.items():
        ev_str = str(ev_k or "").strip()
        if not ev_str.lower().startswith("sns_inflow:"):
            continue
        sub = ev_str.split(":", 1)[1].strip()
        if sub.lower().startswith(("cv:", "cush:", "cvslot:")):
            continue
        norm_t = analytics.normalize_inflow_source(sub) or sub.lower()
        if not norm_t:
            continue
        ck, _, _ = _classify_sns_tag(norm_t)
        already = ch_counts[ck] if norm_t in bare_channel_keys else seen_tags[norm_t]
        delta = max(0, int(ev_cnt or 0) - already)
        if delta > 0:
            ch_counts[ck] += delta
            seen_tags[norm_t] += delta

    search_v = int(ch_counts.get("search", 0))
    short_v = int(ch_counts.get("reels", 0)) + int(ch_counts.get("youtube_shorts", 0))
    social_text_v = int(ch_counts.get("x", 0)) + int(ch_counts.get("threads", 0)) + int(ch_counts.get("instagram", 0))
    total_v = search_v + short_v + social_text_v
    return {
        "total": total_v,
        "search": search_v,
        "short_video": short_v,
        "social_text": social_text_v,
        "by_channel": dict(ch_counts),
    }


def _compute_wow_delta(curr_7d: int, prev_7d: int) -> dict:
    """Compute honest Week-over-Week (7d vs prior 7d) growth without fabricating percentages when denominator is 0."""
    c7 = max(0, int(curr_7d or 0))
    p7 = max(0, int(prev_7d or 0))
    if p7 > 0:
        pct = round(((c7 - p7) / float(p7)) * 100.0, 1)
        sign = "+" if pct > 0 else ""
        direction = "up" if pct >= 5.0 else ("down" if pct <= -5.0 else "flat")
        return {
            "curr_7d": c7,
            "prev_7d": p7,
            "wow_pct": pct,
            "status": "measured",
            "direction": direction,
            "short_label": f"前週比 {sign}{pct}%",
            "detail_label": f"前週 {p7:,} → 直近7日 {c7:,}（{sign}{pct}%）",
        }
    if c7 > 0:
        return {
            "curr_7d": c7,
            "prev_7d": 0,
            "wow_pct": None,
            "status": "new_growth",
            "direction": "up",
            "short_label": f"新規 +{c7:,}",
            "detail_label": f"前週 0 → 直近7日 {c7:,}（初速発生中）",
        }
    return {
        "curr_7d": 0,
        "prev_7d": 0,
        "wow_pct": None,
        "status": "accumulating",
        "direction": "zero",
        "short_label": "比較ログ蓄積中",
        "detail_label": "前週・直近7日とも 0（計測蓄積中）",
    }


def _compute_inflow_north_star(
    stats_window: dict,
    stats_7d: dict,
    stats_14d: dict,
    days: int,
    closed_loop_feedback: dict = None,
    effective_daily_pv: float = None,
    pv_basis: str = None,
) -> dict:
    """Build North Star external inflow & WoW growth telemetry for the PDCA hero & diagnostics."""
    win_inf = _sum_external_inflow_visits(stats_window)
    inf_7d = _sum_external_inflow_visits(stats_7d)
    inf_14d = _sum_external_inflow_visits(stats_14d)

    prev_7d_inflow = max(0, inf_14d["total"] - inf_7d["total"])
    prev_7d_search = max(0, inf_14d["search"] - inf_7d["search"])
    prev_7d_short = max(0, inf_14d["short_video"] - inf_7d["short_video"])

    f_win = (stats_window or {}).get("funnel") or {}
    f_7d = (stats_7d or {}).get("funnel") or {}
    f_14d = (stats_14d or {}).get("funnel") or {}

    pv_7d = int(f_7d.get("pv_total") or 0)
    pv_14d = int(f_14d.get("pv_total") or 0)
    prev_7d_pv = max(0, pv_14d - pv_7d)

    hpv_win = int(f_win.get("human_pv_total") or 0)
    hpv_days = int(f_win.get("human_pv_days") or 0)
    hpv_7d = int(f_7d.get("human_pv_total") or 0)
    hpv_14d = int(f_14d.get("human_pv_total") or 0)
    prev_7d_hpv = max(0, hpv_14d - hpv_7d)

    inflow_wow = _compute_wow_delta(inf_7d["total"], prev_7d_inflow)
    pv_wow = _compute_wow_delta(pv_7d, prev_7d_pv)
    human_pv_wow = _compute_wow_delta(hpv_7d, prev_7d_hpv)
    search_wow = _compute_wow_delta(inf_7d["search"], prev_7d_search)
    if search_wow.get("status") == "new_growth":
        search_wow["detail_label"] = f"前週 0（リファラ計測開始前含む）→ 直近7日 {inf_7d['search']:,}件"
    short_wow = _compute_wow_delta(inf_7d["short_video"], prev_7d_short)

    gsc_diag = dict(((closed_loop_feedback or {}).get("gsc_diagnostics")) or {})
    yt_diag = dict(((closed_loop_feedback or {}).get("youtube_shorts_diagnostics")) or {})

    gsc_w1 = gsc_diag.get("w1_clicks")
    gsc_w2 = gsc_diag.get("w2_clicks")
    gsc_wow = None
    if gsc_w1 is not None and gsc_w2 is not None:
        try:
            gsc_wow = _compute_wow_delta(int(round(float(gsc_w2))), int(round(float(gsc_w1))))
        except (TypeError, ValueError):
            gsc_wow = None

    days_int = max(1, int(days or 14))
    days_norm = float(days_int)
    pv_tot = int(f_win.get("pv_total") or 0)
    raw_daily_pv = round(pv_tot / days_norm, 1)
    hpv_span = int(f_win.get("human_pv_span_days") or 0)
    if hpv_win > 0:
        raw_hpv_span = max(1, min(days_int, hpv_span if hpv_span > 0 else max(1, hpv_days)))
        eff_hpv_span = max(min(days_int, 7), raw_hpv_span)
        human_daily_pv = round(hpv_win / float(eff_hpv_span), 1)
    else:
        eff_hpv_span = 0
        human_daily_pv = None

    resolved_pv_basis = pv_basis or ("human_pv" if hpv_win > 0 else "raw_pv_fallback")
    if effective_daily_pv is not None:
        headline_daily_pv = round(float(effective_daily_pv), 1)
    elif human_daily_pv is not None:
        headline_daily_pv = human_daily_pv
    else:
        headline_daily_pv = raw_daily_pv

    return {
        "window_days": days_int,
        "external_inflow_total": win_inf["total"],
        "daily_external_inflow": round(win_inf["total"] / days_norm, 1),
        "search_inflow_total": win_inf["search"],
        "short_video_inflow_total": win_inf["short_video"],
        "social_text_inflow_total": win_inf["social_text"],
        "daily_pv": headline_daily_pv,
        "human_daily_pv": human_daily_pv,
        "human_pv_span_days": eff_hpv_span,
        "raw_daily_pv": raw_daily_pv,
        "human_pv_total": hpv_win,
        "raw_pv_total": pv_tot,
        "pv_basis": resolved_pv_basis,
        "inflow_wow": inflow_wow,
        "pv_wow": pv_wow,
        "human_pv_wow": human_pv_wow,
        "search_wow": search_wow,
        "short_video_wow": short_wow,
        "gsc_wow": gsc_wow,
        "gsc_impressions": gsc_diag.get("total_impressions"),
        "gsc_clicks": gsc_diag.get("total_clicks"),
        "gsc_opportunity_count": len(gsc_diag.get("opportunity_queries") or []),
        "yt_shorts_views": yt_diag.get("total_views"),
    }


def _compute_phase_calibrated_scores(
    active_phase: str,
    pv_tot: int,
    daily_pv: float,
    eng_rate: float,
    aff_tot: int,
    daily_aff: float,
    aff_ctr_pct: float,
    attributed_aff: int,
    active_offer_cats: int,
    cushion_total: int,
    inflow_ns: dict,
    active_sns_channels: int,
    reels_active: bool,
    moshimo_enabled: bool,
    style_diversity_count: int,
    col_audit: dict,
    upcoming_count: int,
    sleeping_ratio: float,
) -> tuple:
    """
    Compute Phase-Calibrated 100-Point Score + 4 Score Axes + legacy `score_breakdown` mirror.
      - Phase 1 (`phase1_inflow`, Milestone 100〜300 PV/day):
          Axis 1: 純新規流入・前週比成長 (35pt)
          Axis 2: 検索SEO・AI検索基盤 (30pt)
          Axis 3: ショート動画・SNS誘導 (25pt)
          Axis 4: 有効回遊・定着率 (10pt)
      - Phase 2 (`phase2_engagement`, Milestone 300〜1,000 PV/day):
          30 / 25 / 25 / 20
      - Phase 3 (`phase3_monetize`, Milestone 1,000+ PV/day):
          25 / 20 / 20 / 35
    """
    days_norm = max(1.0, float(inflow_ns.get("window_days") or 14))
    ext_daily = float(inflow_ns.get("external_inflow_total") or 0) / days_norm
    srch_daily = float(inflow_ns.get("search_inflow_total") or 0) / days_norm
    short_daily = float(inflow_ns.get("short_video_inflow_total") or 0) / days_norm
    social_daily = float(inflow_ns.get("social_text_inflow_total") or 0) / days_norm
    gsc_impr = int(inflow_ns.get("gsc_impressions") or 0)
    gsc_clicks = int(inflow_ns.get("gsc_clicks") or 0)
    yt_views = int(inflow_ns.get("yt_shorts_views") or 0)

    inf_wow = inflow_ns.get("inflow_wow") or {}
    pv_wow = (
        (inflow_ns.get("human_pv_wow") or {})
        if inflow_ns.get("pv_basis") == "human_pv"
        else (inflow_ns.get("pv_wow") or {})
    )
    best_wow_pct = inf_wow.get("wow_pct") if inf_wow.get("wow_pct") is not None else pv_wow.get("wow_pct")
    has_new_growth = (inf_wow.get("status") == "new_growth") or (pv_wow.get("status") == "new_growth")

    if active_phase == "phase1_inflow":
        # 1. Pure New Inflow & WoW Growth (max 35pt — calibrated to 100〜300 PV/day milestone)
        pv_step1_pts = int(round(min(1.0, daily_pv / 100.0) * 14))
        pv_step2_pts = int(round(min(1.0, max(0.0, daily_pv - 100.0) / 200.0) * 8))
        ext_pts = int(round(min(1.0, ext_daily / 25.0) * 7))
        if best_wow_pct is not None:
            wow_pts = 6 if best_wow_pct >= 20.0 else (4 if best_wow_pct >= 5.0 else (2 if best_wow_pct >= -5.0 and pv_tot > 0 else 0))
        elif has_new_growth:
            wow_pts = 5 if (pv_tot >= 20 or inflow_ns.get("external_inflow_total", 0) >= 5) else 3
        else:
            wow_pts = 0
        ax_inflow = min(35, pv_step1_pts + pv_step2_pts + ext_pts + wow_pts)

        # 2. Search SEO & AI Search Foundation (max 30pt)
        srch_visit_pts = int(round(min(1.0, (srch_daily + (gsc_clicks / 28.0)) / 18.0) * 12))
        gsc_pts = 6 if gsc_impr >= 500 else (4 if gsc_impr >= 100 else (2 if gsc_impr >= 10 else 0))
        col_base_pts = (3 if col_audit.get("total_bare_oriental", 0) == 0 else 0) + min(3, int(col_audit.get("total_columns", 0) / 12))
        queue_pts = 4 if upcoming_count >= 2 else (2 if upcoming_count == 1 else 0)
        exp_pts = 2 if sleeping_ratio <= 0.35 else (1 if sleeping_ratio <= 0.65 else 0)
        ax_seo = min(30, srch_visit_pts + gsc_pts + col_base_pts + queue_pts + exp_pts)

        # 3. Short Video & SNS Profile Inflow (max 25pt)
        short_visit_pts = int(round(min(1.0, short_daily / 12.0) * 10))
        social_visit_pts = int(round(min(1.0, social_daily / 10.0) * 6))
        video_pipe_pts = (2 if moshimo_enabled else 0) + (2 if reels_active else 0) + (1 if yt_views > 0 else 0)
        ch_div_pts = min(4, active_sns_channels + (1 if style_diversity_count >= 2 else 0))
        ax_sns = min(25, short_visit_pts + social_visit_pts + video_pipe_pts + ch_div_pts)

        # 4. Engaged Site Circulation & Readiness (max 10pt — requires >=15 PV sample for full eng_pts)
        eng_pts = 5 if (pv_tot >= 15 and eng_rate >= 20.0) else (3 if (pv_tot >= 10 and eng_rate >= 10.0) else (1 if eng_rate > 0 else 0))
        cush_pts = 3 if cushion_total >= 5 else (2 if cushion_total >= 1 else 0)
        ready_pts = 2 if (aff_tot > 0 or pv_tot >= 30) else 1
        ax_ret = min(10, eng_pts + cush_pts + ready_pts)

        axes = [
            {"key": "inflow_growth", "label": "純新規流入・前週比成長", "score": ax_inflow, "max_score": 35, "pct": round(ax_inflow / 35.0 * 100)},
            {"key": "search_seo", "label": "検索SEO・AI検索基盤", "score": ax_seo, "max_score": 30, "pct": round(ax_seo / 30.0 * 100)},
            {"key": "short_video_sns", "label": "ショート動画・SNS誘導", "score": ax_sns, "max_score": 25, "pct": round(ax_sns / 25.0 * 100)},
            {"key": "engaged_retention", "label": "有効回遊・定着率", "score": ax_ret, "max_score": 10, "pct": round(ax_ret / 10.0 * 100)},
        ]
        overall = ax_inflow + ax_seo + ax_sns + ax_ret
        legacy_breakdown = {
            "traffic_engagement": ax_inflow,
            "monetization_efficiency": ax_ret,
            "sns_diversity_inflow": ax_sns,
            "content_pipeline_health": ax_seo,
        }
        return overall, legacy_breakdown, axes

    if active_phase == "phase2_engagement":
        ax_inflow = min(30, int(round(min(1.0, daily_pv / 600.0) * 20)) + int(round(min(1.0, ext_daily / 60.0) * 6)) + (4 if (best_wow_pct or 0) >= 5.0 or has_new_growth else 1))
        ax_seo = min(25, int(round(min(1.0, srch_daily / 40.0) * 12)) + (5 if gsc_impr >= 500 else 2) + (4 if col_audit.get("total_bare_oriental", 0) == 0 else 0) + min(4, upcoming_count * 2))
        ax_sns = min(25, int(round(min(1.0, (short_daily + social_daily) / 35.0) * 16)) + min(5, active_sns_channels * 2) + (4 if reels_active else 0))
        ax_mon = min(20, (6 if eng_rate >= 18.0 else 3) + min(6, cushion_total) + min(8, int(round(min(1.0, daily_aff / 3.0) * 8))))
        axes = [
            {"key": "inflow_growth", "label": "流入成長（目標1,000PV/日）", "score": ax_inflow, "max_score": 30, "pct": round(ax_inflow / 30.0 * 100)},
            {"key": "search_seo", "label": "検索SEO・記事資産", "score": ax_seo, "max_score": 25, "pct": round(ax_seo / 25.0 * 100)},
            {"key": "short_video_sns", "label": "ショート動画・SNS誘導", "score": ax_sns, "max_score": 25, "pct": round(ax_sns / 25.0 * 100)},
            {"key": "engaged_monetize", "label": "2ステップ回遊・初期収益", "score": ax_mon, "max_score": 20, "pct": round(ax_mon / 20.0 * 100)},
        ]
        overall = ax_inflow + ax_seo + ax_sns + ax_mon
        return overall, {
            "traffic_engagement": ax_inflow,
            "monetization_efficiency": ax_mon,
            "sns_diversity_inflow": ax_sns,
            "content_pipeline_health": ax_seo,
        }, axes

    # Phase 3 (`phase3_monetize`)
    ax_inflow = min(25, int(round(min(1.0, daily_pv / 1500.0) * 18)) + (4 if eng_rate >= 15.0 else 2) + (3 if (best_wow_pct or 0) >= 0 else 0))
    ax_seo = min(20, int(round(min(1.0, srch_daily / 80.0) * 10)) + (5 if col_audit.get("total_bare_oriental", 0) == 0 else 0) + min(5, upcoming_count * 2 + 1))
    ax_sns = min(20, int(round(min(1.0, (short_daily + social_daily) / 60.0) * 12)) + min(5, active_sns_channels * 2) + (3 if reels_active else 0))
    ax_mon = min(
        35,
        int(round(min(1.0, daily_aff / 10.0) * 18))
        + (8 if aff_ctr_pct >= 2.0 else (4 if aff_ctr_pct >= 1.0 else 0))
        + (5 if attributed_aff > 0 else 0)
        + (4 if active_offer_cats >= 3 else (2 if active_offer_cats >= 1 else 0)),
    )
    axes = [
        {"key": "inflow_growth", "label": "トラフィック規模維持", "score": ax_inflow, "max_score": 25, "pct": round(ax_inflow / 25.0 * 100)},
        {"key": "search_seo", "label": "検索SEO・一次統計資産", "score": ax_seo, "max_score": 20, "pct": round(ax_seo / 20.0 * 100)},
        {"key": "short_video_sns", "label": "ショート動画・SNS送客", "score": ax_sns, "max_score": 20, "pct": round(ax_sns / 20.0 * 100)},
        {"key": "monetize_epc", "label": "収益CVR・EPC効率", "score": ax_mon, "max_score": 35, "pct": round(ax_mon / 35.0 * 100)},
    ]
    overall = ax_inflow + ax_seo + ax_sns + ax_mon
    return overall, {
        "traffic_engagement": ax_inflow,
        "monetization_efficiency": ax_mon,
        "sns_diversity_inflow": ax_sns,
        "content_pipeline_health": ax_seo,
    }, axes


def _build_weekly_growth_actions(
    demand_gap: dict,
    offer_breakdown: list,
    monetization_gaps: list,
    sns_efficiency: dict,
    upcoming_count: int,
    growth_phase: dict = None,
    closed_loop_feedback: dict = None,
    flywheel_status: dict = None,
) -> list:
    """
    Build Phase-Adaptive TOP 3 Weekly Growth Actions (`weekly_growth_actions`) for the Master administrator.
    In Phase 1 (`phase1_inflow` — Inflow First):
      1. Search SEO & GSC CTR Optimization (`seo_ctr_boost`)
      2. Search & Crowd-Demand Special Column (`column_theme`)
      3. Short Video (3D Moshimo / 15s Gap Reels / YT Shorts) Hook & Bio Inflow (`reels_video`)
      * Monetization (`funnel_tuning`) is strictly gated behind `pv >= min_pv_for_funnel_action` (50 PV in Phase 1).
    """
    active_phase = (growth_phase or {}).get("active_phase") or "phase1_inflow"
    min_pv_gate = int((growth_phase or {}).get("min_pv_for_funnel_action") or (50 if active_phase == "phase1_inflow" else 20))

    top_gap = monetization_gaps[0] if monetization_gaps else None
    top_offer = offer_breakdown[0] if offer_breakdown else OFFER_CATEGORIES[0]
    hub_info = (demand_gap or {}).get("top_hub") or {}
    hub_label = hub_info.get("cluster_label") or "梅田・茶屋町"
    area_slug = hub_info.get("area_slug") or "umeda"
    rec_theme = (demand_gap or {}).get("recommended_theme") or "area_hopping"
    rec_topic = (demand_gap or {}).get("recommended_topic") or f"{hub_label}エリアの男女比ギャップと徒歩5分ハシゴ基準"
    reels_top = ((sns_efficiency or {}).get("reels_recommendations") or [{}])[0]

    gsc_diag = dict(((closed_loop_feedback or {}).get("gsc_diagnostics")) or {})
    opp_queries = gsc_diag.get("opportunity_queries") or []
    arena_ev = (demand_gap or {}).get("upcoming_arena_event")
    mq_info = ((flywheel_status or {}).get("moshimo_queue")) or {}

    # Pillar 1: Search SEO & AI Search CTR Boost (最優先チャネル①: Google/AI検索SEO)
    if opp_queries:
        q0 = opp_queries[0]
        q_txt = str(q0.get("query") or "")
        q_impr = int(float(q0.get("impressions") or 0))
        q_clk = int(float(q0.get("clicks") or 0))
        q_pos = round(float(q0.get("position") or 0), 1)
        q_path = str(q0.get("page") or q0.get("target_path") or "")
        if q_path.startswith("http"):
            try:
                q_path = urllib.parse.urlsplit(q_path).path or "/columns"
            except Exception:
                q_path = "/columns"
        seo_cta = q_path if q_path.startswith("/") else "/staff/pdca?tab=automation#pdca-flywheel"
        seo_action = {
            "action_type": "seo_ctr_boost",
            "category_badge": "🔍 検索SEO・CTR改善提案",
            "title": f"GSCお宝クエリ『{q_txt[:24]}』（平均{q_pos}位・表示{q_impr}回）のタイトル＆冒頭結論改善",
            "why": (
                f"Google検索で既に {q_impr}回 表示されていますがクリックは {q_clk}件。"
                "新記事をゼロから書くより、4〜20位圏内クエリの検索CTRを引き上げる方が最速で純新規流入が増えます。"
            ),
            "how_to": "対象ページのタイトル・メタ説明文・冒頭3行に検索キーワードと実測男女比の結論を明記し、比較表を上部へ配置する。",
            "expected_impact": "自然検索クリック率（CTR）改善による継続的な純新規オーガニック流入の獲得",
            "cta_label": "対象ページとGSC検索データを確認する →",
            "cta_url": seo_cta,
            "_urgency": 97 if active_phase == "phase1_inflow" else 88,
        }
    elif arena_ev and isinstance(arena_ev, dict):
        v_name = arena_ev.get("venue_name") or arena_ev.get("venue") or "主要ドーム"
        ev_title = str(arena_ev.get("title") or arena_ev.get("event_title") or "大型ライブ")[:22]
        ev_date = str(arena_ev.get("event_date") or arena_ev.get("date") or "")
        ar_topic = urllib.parse.quote(f"{v_name}終演21時台の周辺相席サージ検証と穴場店舗マップ", safe="")
        seo_action = {
            "action_type": "seo_ctr_boost",
            "category_badge": "🔍 検索SEO・イベント流入提案",
            "title": f"直近公演『{ev_date} {v_name}（{ev_title}）』終演サージ×周辺店舗SEOの先行仕込み",
            "why": (
                f"直近30日以内に {v_name} の大型公演が控えています。"
                "競合が持たない『終演後21時台の来店サージ×徒歩圏店舗比較』はGoogle検索とAI検索の双方で独占流入を狙えます。"
            ),
            "how_to": f"対象エリアページ（/area/{area_slug}）と終演サージ検証コラムの内部リンクを結び、Search Console連携で順位を追跡する。",
            "expected_impact": "公演日〜週末にかけてのイベント検索・エリア名検索からの純新規着地増",
            "cta_label": "コラム編集部で終演サージ企画を開く →",
            "cta_url": f"/staff/column-editorial?theme=area_hopping&topic={ar_topic}",
            "_urgency": 95 if active_phase == "phase1_inflow" else 86,
        }
    else:
        seo_action = {
            "action_type": "seo_ctr_boost",
            "category_badge": "🔍 検索SEO・クエリ発掘提案",
            "title": f"『{hub_label}』店舗・エリア比較SEO強化 ＆ Search Console 4〜20位クエリ同期",
            "why": (
                f"流入拡大フェーズ（目標100〜300 PV/日）では『店舗名＋混雑/男女比/料金』『{hub_label} 相席』の検索上位化が最大の複利資産になります。"
            ),
            "how_to": f"4歯車タブからGSC 28日クエリを同期して表示済みゼロクリック語を特定し、/area/{area_slug} と主要店舗ページの見出し・内部リンクを強化する。",
            "expected_impact": "Google検索・AI検索からの安定した毎日型の純新規流入ベース構築",
            "cta_label": "4歯車・Search Console連携を確認する →",
            "cta_url": "/staff/pdca?tab=automation#pdca-flywheel",
            "_urgency": 95 if active_phase == "phase1_inflow" else 85,
        }

    # Pillar 2: Demand-Gap Data Column Proposal (検索＆SNS両取りの需要ギャップ記事)
    col_urgency = (94 if upcoming_count <= 1 else 91) if active_phase == "phase1_inflow" else (92 if upcoming_count <= 1 else 84)
    encoded_topic = urllib.parse.quote(rec_topic, safe="")
    if hub_info.get("has_measured_gap"):
        headcount_prefix = "ピーク実測女性" if hub_info.get("is_off_hours_benchmark") else "稼働女性"
        col_why_stats = f"（{headcount_prefix} {hub_info.get('live_women', 0)}名 / 店舗間実測差 {hub_info.get('gap_pt', 0)}pt）"
    else:
        col_why_stats = "（夜間ピーク参考モデル・実測波形蓄積中）"
    watch_cnt = int((demand_gap or {}).get("watch_intent_total") or 0)
    watch_note = f"・空席ウォッチ登録 {watch_cnt}件" if watch_cnt > 0 else ""
    column_action = {
        "action_type": "column_theme",
        "category_badge": "📖 需要ギャップ特命コラム",
        "title": f"特命コラム『{rec_topic}』を執筆・予約（現在待機在庫 {upcoming_count}本）",
        "why": (
            f"現場データで『{hub_label}』{col_why_stats}{watch_note}の需要ギャップが最大。"
            "ロングテール検索SEOとX新着スレッド・noteエッセイの同時流入を狙えるテーマです。"
        ),
        "how_to": "オリエンタルラウンジ・ag（アグ）・JIS（ジス）の実測比較表と会話ボックスを入れ、/concierge やエリア比較へ回遊導線を張る。",
        "expected_impact": "検索ロングテール資産の積み上げ＋公開初週のSNS・note横断流入獲得",
        "cta_label": "コラム編集部でこのお題を開く →",
        "cta_url": f"/staff/column-editorial?theme={rec_theme}&topic={encoded_topic}",
        "_urgency": col_urgency,
    }

    # Pillar 3: Short Video (IG Reels / YouTube Shorts / TikTok) Bio Inflow Proposal (最優先チャネル②)
    rem_ep = int(mq_info.get("remaining_count") or 0)
    moshimo_note = f"（平日20:00『3Dもしも』残り{rem_ep}話稼働中）" if rem_ep > 0 else ""
    reels_action = {
        "action_type": "reels_video",
        "category_badge": "🎬 ショート動画・プロフ誘導",
        "title": reels_top.get("title") or f"【15秒ショート】{hub_label}の徒歩5分・男女比ギャップ実証動画",
        "why": (
            f"非フォロワー層へ一気にリーチできる縦型ショート動画（IGリール / YouTubeショート / TikTok）{moshimo_note}は、"
            "初期フェーズで純新規プロフ流入（?s=short_bio / ?s=reel_hub_gap）を作る最強エンジンです。"
        ),
        "how_to": (
            f"冒頭2秒フック：{reels_top.get('hook_0_3s', '')} ｜ "
            f"平日20:00は3DもしもをYTショートへ同時展開、週末 {reels_top.get('best_post_time', '金曜 21:30')} は実測ギャップ動画を配信。"
        ),
        "expected_impact": "非フォロワー層からのプロフ遷移＆『?s=short_bio / ?s=reel_*』経由の純新規訪問増",
        "cta_label": "9:16 ショート動画スタジオを開く →",
        "cta_url": "/staff/sns-editorial?channel=instagram#reel-studio-section",
        "_urgency": 92 if active_phase == "phase1_inflow" else 82,
    }

    candidates = [seo_action, column_action, reels_action]

    # High-PV Gate on Monetization Action:
    # In Phase 1, ONLY include `funnel_tuning` if a non-home content/article page (`path != "/"`) ALREADY has >= 50 PV.
    if active_phase == "phase1_inflow":
        gated_gap = next(
            (g for g in (monetization_gaps or []) if g.get("path") != "/" and int(g.get("pv") or 0) >= min_pv_gate),
            None,
        )
    else:
        gated_gap = top_gap if (top_gap and int(top_gap.get("pv") or 0) >= min_pv_gate) else None

    if gated_gap:
        funnel_urgency = (
            93 if active_phase == "phase1_inflow"
            else (98 if gated_gap.get("severity") in ("critical", "high") else 89)
        )
        candidates.append({
            "action_type": "funnel_tuning",
            "category_badge": "💰 高PV記事・2ステップ導線改善",
            "title": f"高流入『{gated_gap['title'][:26]}』（{gated_gap['pv']} PV）に {gated_gap['recommended_funnel'][:22]} を追加",
            "why": (
                f"既に {gated_gap['pv']} PV の集客に成功していますが、2ステップ送客（CV {gated_gap['aff_clicks']}件 / CVR {gated_gap['cvr_pct']}%）で離脱が発生中。"
                f"集客済みページの回遊・収益受け皿（{top_offer['short_label']}）を整える好機です。"
            ),
            "how_to": f"配置位置：{gated_gap['placement_position']}（推奨内部導線：{', '.join(gated_gap['recommended_links'][:2])}）",
            "expected_impact": f"既存流入を取りこぼさず期間あたり +{gated_gap['est_missed_clicks']}件以上の送客増",
            "cta_label": "対象ページと導線を確認する →",
            "cta_url": gated_gap["path"] if gated_gap["path"].startswith("/") else "/staff/analytics",
            "_urgency": funnel_urgency,
        })
    elif active_phase == "phase3_monetize":
        candidates.append({
            "action_type": "funnel_tuning",
            "category_badge": "💰 収益導線最適化",
            "title": f"勝ち枠『{top_offer['short_label']}』を /grooming ＆上位コラム中盤へ横展開",
            "why": f"クリック首位の『{top_offer['short_label']}』（{top_offer['clicks']}件）は対面直前読者との相性が抜群です。",
            "how_to": "会話ボックス直後に /grooming 内部リンク、クッションページ側で即決CTAを整理。",
            "expected_impact": "サイト全体のアフィリエイトCTR +0.8〜1.5pt 底上げ",
            "cta_label": "収益スロット設定を確認する →",
            "cta_url": "/staff/analytics?tab=affiliate",
            "_urgency": 96,
        })

    actions = sorted(candidates, key=lambda x: x["_urgency"], reverse=True)[:3]
    for idx, act_item in enumerate(actions, start=1):
        act_item["priority"] = idx
        act_item.pop("_urgency", None)
    return actions


def _collect_all_known_columns(allow_blocking: bool = False) -> list:
    cols = []
    seen = set()
    for item in scheduled_articles.get_published_articles(allow_blocking=allow_blocking):
        p = item.get("path")
        if p and p not in seen:
            seen.add(p)
            cols.append({
                "slug": item["slug"],
                "path": p,
                "title": item.get("short_title") or item.get("title"),
                "is_new_release": True,
            })
    for c in column_editorial.EXISTING_STATIC_COLUMNS:
        p = c["path"]
        if p not in seen:
            seen.add(p)
            cols.append({
                "slug": c["slug"],
                "path": p,
                "title": c["title"],
                "is_new_release": False,
            })
    return cols


_CLUSTER_PEAK_PRIORS = {
    "umeda": (94, 66, 21),
    "shinjuku": (90, 68, 19),
    "shibuya": (86, 60, 18),
    "namba": (82, 58, 17),
    "fukuoka": (74, 52, 16),
    "nagoya": (62, 45, 15),
    "sapporo": (58, 40, 15),
    "kyoto-kobe": (52, 36, 14),
    "yokohama-kanto": (56, 42, 14),
    "tokyo-east": (50, 38, 14),
}


def _detect_hot_cluster_and_topic(
    live_stores: list,
    store_opens: dict,
    store_pvs: dict,
    allow_blocking: bool = False,
) -> dict:
    """
    Identify which city hub has the strongest user interest + crowd asymmetry right now.
    During daytime / pre-open / post-close hours when live domestic women < 20, automatically
    falls back to 30-day peak-hour (22:00 / 20:00) SQLite waveforms so scheduled PDCA cycles
    (17:35 & 03:35 JST) never degenerate into 0-headcount / 0pt-gap diagnostics.
    """
    domestic = sns_editorial._domestic_stores(live_stores or [])
    enriched = sns_editorial._enrich_with_benchmarks(domestic)
    total_live_w = sum(int(s.get("women") or 0) for s in enriched)
    use_peak_benchmark = total_live_w < 20

    waveforms = {}
    if use_peak_benchmark:
        try:
            import local_db
            waveforms = local_db.get_all_stores_hourly_waveforms(allow_blocking=allow_blocking) or {}
        except Exception:
            waveforms = {}

    cluster_scores = []
    for cl_label, cl_slug, kws in sns_editorial.URBAN_CLUSTERS:
        members = [
            s for s in enriched
            if any(k in (s.get("name") or "").upper() or k in (s.get("ja_name") or "") for k in kws)
        ]
        raw_live_w = sum(int(s.get("women") or 0) for s in members)
        raw_live_m = sum(int(s.get("men") or 0) for s in members)
        eff_w = raw_live_w
        eff_m = raw_live_m
        gap_pt = 0
        has_measured_gap = False

        if not use_peak_benchmark:
            active_m = [m for m in members if m.get("total", 0) >= 6]
            if len(active_m) >= 2:
                wpcts = [int(round(m.get("wpct", 50))) for m in active_m]
                gap_pt = max(wpcts) - min(wpcts)
                has_measured_gap = True
        else:
            wf_pts = []
            for sname, prof in waveforms.items():
                if cl_slug == "nagoya" and "錦糸町" in sname:
                    continue
                if any(k in sname.upper() or k in sname for k in kws):
                    pt = (prof or {}).get("h22") or (prof or {}).get("h20")
                    if pt and float(pt.get("total") or 0) >= 6.0:
                        wf_pts.append(pt)
            if wf_pts:
                eff_w = int(round(sum(float(p.get("w") or 0) for p in wf_pts)))
                eff_m = int(round(sum(float(p.get("m") or 0) for p in wf_pts)))
                if len(wf_pts) >= 2:
                    wpcts = [float(p.get("wpct") or 50.0) for p in wf_pts]
                    gap_pt = max(8, int(round(max(wpcts) - min(wpcts))))
                    has_measured_gap = True
                else:
                    gap_pt = _CLUSTER_PEAK_PRIORS.get(cl_slug, (54, 40, 14))[2]
            else:
                eff_w, eff_m, gap_pt = _CLUSTER_PEAK_PRIORS.get(cl_slug, (54, 40, 14))

        opens = 0
        for raw_lbl, cnt in store_opens.items():
            sname = raw_lbl.split(":", 1)[0].strip()
            if cl_slug == "nagoya" and "錦糸町" in sname:
                continue
            if any(k in sname.upper() or k in sname for k in kws):
                opens += cnt
        pvs = sum(cnt for path, cnt in store_pvs.items() if cl_slug in path)

        demand_score = (opens * 4) + (pvs * 2) + eff_w + (gap_pt * 2)
        cluster_scores.append({
            "cluster_label": cl_label,
            "area_slug": cl_slug,
            "live_women": eff_w,
            "live_men": eff_m,
            "raw_live_women": raw_live_w,
            "is_off_hours_benchmark": use_peak_benchmark,
            "opens": opens,
            "gap_pt": gap_pt,
            "has_measured_gap": has_measured_gap,
            "demand_score": demand_score,
        })

    cluster_scores.sort(key=lambda x: x["demand_score"], reverse=True)
    top_hub = cluster_scores[0] if cluster_scores else {
        "cluster_label": "梅田・茶屋町",
        "area_slug": "umeda",
        "live_women": 94,
        "live_men": 66,
        "raw_live_women": 0,
        "is_off_hours_benchmark": True,
        "opens": 0,
        "gap_pt": 21,
        "has_measured_gap": False,
        "demand_score": 136,
    }

    hub = top_hub["cluster_label"]
    now_j = datetime.datetime.now(JST)
    wd = now_j.weekday()  # 0=Mon ... 6=Sun
    hr = now_j.hour
    total_live = top_hub["live_women"] + top_hub["live_men"]
    hub_wpct = round((top_hub["live_women"] / total_live) * 100) if total_live > 0 else 55

    # Rotate across all 8 deep-mechanism themes based on live gap, day-of-week, hour, and gender balance
    if top_hub["gap_pt"] >= 18:
        rec_theme = "area_hopping"
        if top_hub.get("has_measured_gap"):
            rec_topic = f"{hub}の店舗間男女比ギャップ（最大{top_hub['gap_pt']}pt差）を生む群集心理と徒歩5分アービトラージ基準"
        else:
            rec_topic = f"{hub}の店舗間男女比ギャップを生む群集心理と徒歩5分アービトラージ基準"
    elif hr >= 23 or hr <= 4:
        rec_theme = "midnight_wave"
        rec_topic = f"{hub}で終電後に客層コホートが入れ替わる「深夜第2波」の構造と2軒目ラウンジ選別法"
    elif wd in (0, 1, 2, 3):  # Mon-Thu: weekday structural advantage or solo/female lens
        weekday_pool = [
            ("weekday_anaba", f"{hub}の平日夜になぜ『真剣対話層』が残るのか——実測混雑ログで見る火〜木曜・雨天の需給優位性"),
            ("female_real_voice", f"{hub}の女性ペアが支払う『準備90分の機会費用』と、男女で真逆になる最適店舗のカラクリ"),
            ("solo_trip", f"出張・1人飲みの夜に{hub}で浮かない理由——ペア合意形成と1名入店規定の構造解剖"),
            ("first_talk_clean", f"{hub}の間接照明と距離70cmで最初の3分に起きている『非言語減点』の正体"),
        ]
        rec_theme, rec_topic = weekday_pool[(now_j.day + wd) % len(weekday_pool)]
    elif hub_wpct <= 46:
        rec_theme = "bitter_loser_habits"
        rec_topic = f"{hub}の男性過密タイムになぜサンクコスト延長が起きるのか——待機課金と損切りの行動経済学"
    else:
        weekend_pool = [
            ("second_venue_move", f"{hub}で『もう一軒』が通る席・断られる席——ピーク・エンドの法則と出口時刻明示の科学"),
            ("female_real_voice", f"週末の{hub}で女子トイレ反省会行きになる席の共通点——非対称コスト構造からの逆算"),
            ("area_hopping", f"週末ピークの{hub}で看板店集中を避ける『徒歩4分・需給ギャップ』活用プロトコル"),
            ("bitter_loser_habits", f"{hub}の週末レシートから解剖する『元を取ろうとして赤字になる夜』の構造的敗因"),
        ]
        rec_theme, rec_topic = weekend_pool[(now_j.day + hr) % len(weekend_pool)]

    watch_intent_total = 0
    upcoming_arena_event = None
    try:
        import local_db
        w_map = local_db.get_watch_intent_counts(hours=14 * 24) or {}
        if isinstance(w_map, dict):
            watch_intent_total = sum(int(v or 0) for v in w_map.values() if isinstance(v, (int, float)))
        arenas = local_db.get_upcoming_arena_events(days_ahead=30, limit=5) or []
        if arenas:
            upcoming_arena_event = arenas[0]
    except Exception:
        pass

    return {
        "top_hub": top_hub,
        "cluster_ranking": cluster_scores[:4],
        "recommended_theme": rec_theme,
        "recommended_topic": rec_topic,
        "watch_intent_total": watch_intent_total,
        "upcoming_arena_event": upcoming_arena_event,
    }


_SITE_PERF_TTL_SEC = 60.0
_site_perf_cache = {}
_site_perf_cache_lock = threading.Lock()


def _site_perf_cache_key(live_stores, days, allow_blocking):
    try:
        import local_db
        db_path = str(getattr(local_db, "DB_PATH", ""))
    except Exception:
        db_path = ""
    try:
        phase_mode = get_pdca_settings(allow_blocking=False).get("growth_phase_mode", "auto")
    except Exception:
        phase_mode = "auto"
    store_sig = tuple(sorted(
        (str(s.get("name") or ""), int(s.get("women") or 0), int(s.get("men") or 0), bool(s.get("is_open_now", True)))
        for s in (live_stores or []) if isinstance(s, dict)
    ))
    # id(analytics.summary) changes when tests patch it, so mocked runs never share entries
    return (db_path, int(days or 14), bool(allow_blocking), phase_mode, hash(store_sig), id(analytics.summary))


def analyze_site_performance(live_stores: list = None, days: int = 14, allow_blocking: bool = True) -> dict:
    """Run comprehensive Check diagnostics (60s TTL memo keyed by DB_PATH / days / phase_mode / live store signature)."""
    import copy
    import sys
    bypass = ("unittest" in sys.modules or "pytest" in sys.modules) and os.environ.get("PDCA_CACHE_IN_TESTS") != "1"
    if bypass:
        return _analyze_site_performance_uncached(live_stores=live_stores, days=days, allow_blocking=allow_blocking)
    try:
        key = _site_perf_cache_key(live_stores, days, allow_blocking)
    except Exception:
        key = None
    now_m = time.monotonic()
    if key is not None:
        with _site_perf_cache_lock:
            hit = _site_perf_cache.get(key)
            if hit and now_m - hit[0] < _SITE_PERF_TTL_SEC:
                return copy.deepcopy(hit[1])
    result = _analyze_site_performance_uncached(live_stores=live_stores, days=days, allow_blocking=allow_blocking)
    if key is not None and isinstance(result, dict):
        with _site_perf_cache_lock:
            for k in [k for k, v in _site_perf_cache.items() if now_m - v[0] >= _SITE_PERF_TTL_SEC]:
                _site_perf_cache.pop(k, None)
            if len(_site_perf_cache) >= 32:
                # 古いエントリから間引き、ホットなキャッシュを全消ししない
                _evict_n = max(1, len(_site_perf_cache) - 24)
                _oldest = sorted(_site_perf_cache.items(), key=lambda kv: kv[1][0])[:_evict_n]
                for _ek, _ in _oldest:
                    _site_perf_cache.pop(_ek, None)
            _site_perf_cache[key] = (now_m, copy.deepcopy(result))
    return result


def _analyze_site_performance_uncached(live_stores: list = None, days: int = 14, allow_blocking: bool = True) -> dict:
    """Run Phase-Adaptive Check diagnostics across pure external inflows, WoW growth, SEO, Short Video/SNS, and funnels."""
    import ops_governance
    live_stores = ops_governance.filter_visible_stores(list(live_stores or []))
    settings = get_pdca_settings(allow_blocking=allow_blocking)
    stats = analytics.summary(days=days, allow_blocking=allow_blocking)
    stats_7d = stats if days == 7 else analytics.summary(days=7, allow_blocking=allow_blocking)
    stats_14d = stats if days == 14 else analytics.summary(days=14, allow_blocking=allow_blocking)
    all_labels = stats.get("all_by_label") or {}
    pv_by_path = all_labels.get("page_view") or {}
    aff_by_label = all_labels.get("affiliate_click") or {}
    cushion_by_label = all_labels.get("cushion_click") or {}
    inflow_by_label = all_labels.get("sns_inflow") or {}
    store_opens = all_labels.get("store_open") or {}
    funnel = stats.get("funnel") or {}

    days_int = max(1, int(days or 14))
    days_norm = float(days_int)
    pv_tot = int(funnel.get("pv_total") or 0)
    human_pv_tot = int(funnel.get("human_pv_total") or 0)
    human_pv_days = int(funnel.get("human_pv_days") or 0)
    human_pv_span_days = int(funnel.get("human_pv_span_days") or 0)
    raw_daily_pv = pv_tot / days_norm
    if human_pv_tot > 0:
        raw_hpv_span = max(1, min(days_int, human_pv_span_days if human_pv_span_days > 0 else max(1, human_pv_days)))
        # M1: Clamp denominator to at least min(days_int, 7) days so a 1-day burst cannot inflate daily_pv
        # and prematurely auto-promote Phase 1 -> Phase 2 before a 7-day baseline is observed.
        eff_hpv_span = max(min(days_int, 7), raw_hpv_span)
        daily_pv = human_pv_tot / float(eff_hpv_span)
        pv_basis = "human_pv"
    else:
        eff_hpv_span = 0
        daily_pv = raw_daily_pv
        pv_basis = "raw_pv_fallback"
    growth_phase = resolve_growth_phase(
        daily_pv=daily_pv,
        mode=settings.get("growth_phase_mode", "auto"),
        pv_basis=pv_basis,
        raw_daily_pv=raw_daily_pv,
    )
    active_phase = growth_phase["active_phase"]
    is_inflow_first = active_phase in ("phase1_inflow", "phase2_engagement")

    # 1. Cross-reference PV, Cushion Bridge Clicks, and Affiliate Clicks per page
    aff_by_ref_path = collections.defaultdict(int)
    aff_by_item = collections.defaultdict(int)
    for raw_lbl, cnt in aff_by_label.items():
        if "@" in raw_lbl:
            item_id, ref_p = [p.strip() for p in raw_lbl.split("@", 1)]
        else:
            item_id, ref_p = raw_lbl.strip(), "-"
        canon_item = _canonicalize_aff_slot_id(item_id) or item_id
        aff_by_ref_path[ref_p] += cnt
        aff_by_item[canon_item] += cnt

    cushion_by_ref_path = collections.defaultdict(int)
    for raw_lbl, cnt in cushion_by_label.items():
        if "@" in raw_lbl:
            _tgt_p, ref_p = [p.strip() for p in raw_lbl.split("@", 1)]
            if ref_p:
                cushion_by_ref_path[ref_p] += int(cnt or 0)

    known_cols = _collect_all_known_columns(allow_blocking=allow_blocking)
    article_perf = []
    for col in known_cols:
        p = col["path"]
        pvs = int(pv_by_path.get(p, 0))
        affs = int(aff_by_ref_path.get(p, 0))
        cush_clks = int(cushion_by_ref_path.get(p, 0))
        ctr = round((affs / pvs) * 100, 2) if pvs > 0 else 0.0
        bridge_pct = round((cush_clks / pvs) * 100, 2) if pvs > 0 else 0.0
        if pvs >= 8 and (ctr >= 2.0 or bridge_pct >= 8.0):
            badge = "🏆 高CV牽引"
        elif col.get("is_new_release") and pvs < 15:
            badge = "🌱 新着・露出強化枠"
        elif pvs >= 15 and affs == 0 and cush_clks == 0:
            badge = "⚠️ 導線ロス（高PV・CV0）"
        elif pvs >= 10:
            badge = "📈 集客牽引中"
        elif pvs == 0:
            badge = "💤 SNS・検索露出待ち"
        else:
            badge = "📈 安定稼働"
        article_perf.append({
            "slug": col["slug"],
            "path": p,
            "title": col["title"],
            "is_new_release": col["is_new_release"],
            "pv": pvs,
            "aff_clicks": affs,
            "cushion_clicks": cush_clks,
            "bridge_rate_pct": bridge_pct,
            "ctr_pct": ctr,
            "status_badge": badge,
        })

    if is_inflow_first:
        article_perf_by_score = sorted(
            article_perf,
            key=lambda x: (
                x["pv"] * 3 + x["cushion_clicks"] * 12 + x["aff_clicks"] * 20,
                x["pv"],
                1 if x["is_new_release"] else 0,
            ),
            reverse=True,
        )
    else:
        article_perf_by_score = sorted(
            article_perf,
            key=lambda x: (x["aff_clicks"] * 15 + x["ctr_pct"] * 5 + x["pv"], 1 if x["is_new_release"] else 0),
            reverse=True,
        )

    # 2. Aggregate External & SNS `?s=` Inflow Cohorts + SNS Editorial Logs
    inflow_visits = collections.defaultdict(int)
    inflow_cvs = collections.defaultdict(int)
    inflow_cushions = collections.defaultdict(int)
    inflow_slot_clicks = collections.defaultdict(lambda: collections.defaultdict(int))
    style_inflow_score = collections.defaultdict(int)

    for raw_lbl, cnt in inflow_by_label.items():
        c_int = int(cnt or 0)
        if c_int <= 0:
            continue
        src_part = raw_lbl.split("@", 1)[0].strip() if "@" in raw_lbl else raw_lbl.strip()
        rhs_part = raw_lbl.split("@", 1)[1].strip() if "@" in raw_lbl else ""
        if src_part.startswith("cv:"):
            tag = analytics.normalize_inflow_source(src_part[3:])
            if tag:
                inflow_cvs[tag] += c_int
        elif src_part.startswith("cush:"):
            tag = analytics.normalize_inflow_source(src_part[5:])
            if tag:
                inflow_cushions[tag] += c_int
        elif src_part.startswith("cvslot:"):
            tag = analytics.normalize_inflow_source(src_part[7:])
            if tag and rhs_part:
                inflow_slot_clicks[tag][rhs_part.lower()] += c_int
        else:
            tag = analytics.normalize_inflow_source(src_part)
            if tag:
                inflow_visits[tag] += c_int

    inflow_cohorts = []
    all_tags = sorted(
        set(inflow_visits.keys()) | set(inflow_cvs.keys()) | set(inflow_cushions.keys()) | set(inflow_slot_clicks.keys()),
        key=lambda t: (
            (inflow_visits[t] * 10 + inflow_cushions[t] * 3 + inflow_cvs[t] * 2)
            if is_inflow_first
            else (inflow_cvs[t] * 12 + inflow_cushions[t] * 3 + inflow_visits[t])
        ),
        reverse=True,
    )
    for tag in all_tags:
        v_cnt = inflow_visits[tag]
        cv_cnt = inflow_cvs[tag]
        cush_cnt = inflow_cushions[tag]
        slot_map = inflow_slot_clicks.get(tag) or {}
        slot_rev_yen = 0
        top_slot_id = ""
        if slot_map:
            top_slot_id = max(slot_map, key=slot_map.get)
            for s_id, s_c in slot_map.items():
                cat_k = _classify_offer_category(s_id)
                unit_epc = int((_OFFER_META_BY_KEY.get(cat_k) or {}).get("epc_yen") or 95)
                slot_rev_yen += s_c * unit_epc
            counted_slot_cvs = sum(slot_map.values())
            if cv_cnt > counted_slot_cvs:
                slot_rev_yen += (cv_cnt - counted_slot_cvs) * 95
        elif cv_cnt > 0:
            slot_rev_yen = cv_cnt * 95

        epc_per_visit = round(slot_rev_yen / float(v_cnt), 1) if v_cnt > 0 else 0.0
        inflow_cohorts.append({
            "tag": tag,
            "label": analytics.describe_inflow_tag(tag),
            "visits": v_cnt,
            "cushion_clicks": cush_cnt,
            "cushion_rate_pct": round((cush_cnt / v_cnt) * 100, 1) if v_cnt > 0 else 0.0,
            "conversions": cv_cnt,
            "cvr_pct": round((cv_cnt / v_cnt) * 100, 1) if v_cnt > 0 else 0.0,
            "est_revenue_yen": slot_rev_yen,
            "epc_yen": epc_per_visit,
            "top_slot": top_slot_id,
        })
        _, _, st_key = _classify_sns_tag(tag)
        if st_key:
            if is_inflow_first:
                # Phase 1/2: rank SNS editorial styles primarily by real visitor inflow
                style_inflow_score[st_key] += (v_cnt * 4) + (cush_cnt * 2) + cv_cnt
            else:
                style_inflow_score[st_key] += v_cnt + (cush_cnt * 2) + (cv_cnt * 5) + int(slot_rev_yen / 45)

    # Inspect recent SNS Editorial history across X, Threads, Instagram, and TikTok/Reels
    x_hist = sns_editorial.get_recent_editorial_history("x", limit=10, allow_blocking=allow_blocking)
    th_hist = sns_editorial.get_recent_editorial_history("threads", limit=8, allow_blocking=allow_blocking)
    ig_hist = sns_editorial.get_recent_editorial_history("instagram", limit=10, allow_blocking=allow_blocking)
    reels_logged_extra = 0
    sns_delivery_errors = []
    try:
        deliv = sns_editorial.get_sns_delivery_health(allow_blocking=allow_blocking) or {}
        tt_health = deliv.get("tiktok") or {}
        if tt_health.get("last_status") == "ok" or tt_health.get("latest_slot_key"):
            reels_logged_extra += 1
        for ch_k, ch_info in deliv.items():
            if isinstance(ch_info, dict) and ch_info.get("state") == "error" and ch_info.get("last_error"):
                sns_delivery_errors.append((ch_info.get("label") or ch_k, str(ch_info.get("last_error"))))
    except Exception:
        pass
    all_sns_hist = x_hist + th_hist + ig_hist
    style_usage_counts = collections.Counter(h.get("selected_style") for h in all_sns_hist if h.get("selected_style"))

    # Compute smart SNS style score boosts (-2 to +4) across all selectable editorial styles
    candidate_styles = [
        "ai_live_adlib",
        "hub_gap",
        "surge_focus",
        "usual_anomaly",
        "east_west_talk",
        "ladies_night",
        "threads_city_story",
        "threads_anomaly_insight",
        "threads_hopping_guide",
        "threads_ladies_zero_wait",
    ]
    scored_candidates = [st for st in candidate_styles if style_inflow_score.get(st, 0) > 0]
    sns_style_boosts = {}
    if scored_candidates:
        top_st = max(scored_candidates, key=lambda k: style_inflow_score[k])
        top_pts = style_inflow_score.get(top_st, 0)
        for st in candidate_styles:
            pts = style_inflow_score.get(st, 0)
            if st == top_st and pts >= 3:
                sns_style_boosts[st] = 4
            elif st == top_st and pts >= 2:
                sns_style_boosts[st] = 2
            elif pts >= 3:
                sns_style_boosts[st] = 2
            elif pts >= 1:
                sns_style_boosts[st] = 1
            elif style_usage_counts.get(st, 0) >= 4 and pts == 0:
                sns_style_boosts[st] = -2
        if top_pts < 3:
            sns_style_boosts.setdefault("ai_live_adlib", 2)
    else:
        # Cold-start smart rotation: encourage high-curiosity city gap & live ad-lib hooks
        sns_style_boosts["ai_live_adlib"] = 3
        sns_style_boosts["hub_gap"] = 3 if style_usage_counts.get("hub_gap", 0) < 3 else 0
        sns_style_boosts["usual_anomaly"] = 2 if style_usage_counts.get("usual_anomaly", 0) < 2 else 0

    # Closed-Loop Feedback Integration (Threads/X Hook Insights + IG Reels Bandit + note CTR + GSC)
    closed_loop_feedback = {}
    try:
        import sns_insights_loop
        closed_loop_feedback = sns_insights_loop.compute_loop_feedback(
            days=max(days, 28),
            allow_blocking=allow_blocking,
        ) or {}
        for st_k, delta_pts in (closed_loop_feedback.get("sns_style_boosts_delta") or {}).items():
            cur_pts = int(sns_style_boosts.get(st_k, 0))
            sns_style_boosts[st_k] = max(-4, min(5, cur_pts + int(delta_pts)))
    except Exception as loop_err:
        logger.debug(f"[pdca_engine] closed_loop_feedback skipped: {loop_err}")
        closed_loop_feedback = {}

    style_labels_map = {}
    for st_k in set(list(candidate_styles) + list(sns_style_boosts.keys())):
        style_labels_map[st_k] = (
            analytics._INFLOW_STYLE_JA.get(st_k)
            or sns_editorial.STYLE_LABELS.get(st_k)
            or st_k
        )

    # 3. Column Queue & Quality Audit
    col_audit = column_editorial.audit_existing_columns()
    upcoming_cols = scheduled_articles.get_upcoming_articles(allow_blocking=allow_blocking)
    upcoming_count = len(upcoming_cols)

    store_pvs = {p: c for p, c in pv_by_path.items() if p.startswith(("/store/", "/area/"))}
    demand_gap = _detect_hot_cluster_and_topic(
        live_stores or [],
        store_opens,
        store_pvs,
        allow_blocking=allow_blocking,
    )

    # 3b. North Star Inflow & 4-Gear Flywheel Intelligence
    offer_breakdown = _build_offer_breakdown(stats, stats_7d, days=days)
    monetization_gaps = _detect_monetization_gaps(
        pv_by_path,
        aff_by_ref_path,
        known_cols,
        cushion_by_ref_path=cushion_by_ref_path,
        active_phase=active_phase,
        effective_daily_pv=daily_pv,
    )
    yt_shorts_logged = int(((closed_loop_feedback.get("sample_counts") or {}).get("youtube_shorts_with_metrics")) or 0)
    sns_channel_efficiency = _build_sns_channel_efficiency(
        stats=stats,
        inflow_cohorts=inflow_cohorts,
        x_hist=x_hist,
        th_hist=th_hist,
        ig_hist=ig_hist,
        demand_gap=demand_gap,
        offer_breakdown=offer_breakdown,
        reels_logged_extra=reels_logged_extra,
        yt_shorts_logged=yt_shorts_logged,
        active_phase=active_phase,
    )
    flywheel_status = _build_flywheel_status(
        closed_loop_feedback=closed_loop_feedback,
        sns_channel_efficiency=sns_channel_efficiency,
        upcoming_cols=upcoming_cols,
        settings=settings,
        demand_gap=demand_gap,
        allow_blocking=allow_blocking,
    )
    inflow_north_star = _compute_inflow_north_star(
        stats_window=stats,
        stats_7d=stats_7d,
        stats_14d=stats_14d,
        days=days,
        closed_loop_feedback=closed_loop_feedback,
        effective_daily_pv=daily_pv,
        pv_basis=pv_basis,
    )
    weekly_growth_actions = _build_weekly_growth_actions(
        demand_gap=demand_gap,
        offer_breakdown=offer_breakdown,
        monetization_gaps=monetization_gaps,
        sns_efficiency=sns_channel_efficiency,
        upcoming_count=upcoming_count,
        growth_phase=growth_phase,
        closed_loop_feedback=closed_loop_feedback,
        flywheel_status=flywheel_status,
    )

    leaky_articles = [
        a for a in article_perf
        if a["pv"] >= 10 and a["aff_clicks"] == 0 and a.get("cushion_clicks", 0) == 0
    ]
    leaky_path_set = {a["path"] for a in leaky_articles}

    # Select Top 4 priority columns for SNS & X 18:00 promotion:
    # In Phase 1/2 (Inflow First): prioritize engaged/high-inflow winners + GSC opportunity pages + new releases + high-search-intent comparison guides!
    if is_inflow_first:
        winners_paths = [
            a["path"] for a in article_perf_by_score
            if (a["aff_clicks"] > 0 or a["cushion_clicks"] > 0 or (a["pv"] >= 3 and a["path"] not in leaky_path_set))
        ][:2]
        gsc_opp_paths = []
        for q_item in (((closed_loop_feedback.get("gsc_diagnostics") or {}).get("opportunity_queries")) or []):
            qp = str(q_item.get("page") or q_item.get("target_path") or "")
            if qp.startswith("http"):
                try:
                    qp = urllib.parse.urlsplit(qp).path
                except Exception:
                    qp = ""
            if qp.startswith("/guide/") and qp not in gsc_opp_paths:
                gsc_opp_paths.append(qp)
    else:
        winners_paths = [a["path"] for a in article_perf_by_score if a["aff_clicks"] > 0][:2]
        gsc_opp_paths = []

    new_rel_paths = [a["path"] for a in article_perf if a["is_new_release"]][:1]
    killer_seed_paths = [
        "/guide/last-train-cliff",
        "/guide/lounge-vs-screened-date",
        "/guide/price-event-comparison",
        "/guide/first-10-minutes",
        "/guide/dress-code-cleanliness",
    ]
    under_exposed = [
        a["path"] for a in article_perf
        if a["pv"] < 5 and a["path"] in killer_seed_paths and a["path"] not in winners_paths + new_rel_paths
    ][:2]
    if len(under_exposed) < 2:
        for a in article_perf:
            if a["pv"] < 5 and a["path"] not in winners_paths + new_rel_paths + under_exposed:
                under_exposed.append(a["path"])
                if len(under_exposed) >= 2:
                    break
    priority_column_paths = []
    for p in (winners_paths + gsc_opp_paths[:1] + new_rel_paths + under_exposed + killer_seed_paths):
        if p not in priority_column_paths:
            priority_column_paths.append(p)
        if len(priority_column_paths) >= 4:
            break

    # 4. Build Phase-Aware "What's Working (Wins)" & "What's Not Working (Bottlenecks)"
    wins = []
    issues = []

    aff_tot = sum(item["clicks"] for item in offer_breakdown) or funnel.get("aff_total", 0)
    aff_clicks_7d = sum(int(item.get("clicks_7d") or 0) for item in offer_breakdown)
    eng_rate = funnel.get("engagement_rate_pct", 0.0)
    human_eng_rate = funnel.get("human_engagement_rate_pct")
    effective_eng_rate = human_eng_rate if (human_pv_tot > 0 and human_eng_rate is not None) else eng_rate
    ext_tot = inflow_north_star["external_inflow_total"]
    inf_wow = inflow_north_star["inflow_wow"]

    if ext_tot > 0:
        gsc_wow = inflow_north_star.get("gsc_wow")
        gsc_wow_note = f" ／ GSC実測 {gsc_wow['short_label']}" if isinstance(gsc_wow, dict) else ""
        wins.append({
            "title": f"外部純新規流入が稼働中：計 {ext_tot:,}件（{inf_wow['short_label']}{gsc_wow_note}｜検索 {inflow_north_star['search_inflow_total']}・動画 {inflow_north_star['short_video_inflow_total']}）",
            "detail": f"最上位流入枠は『{(inflow_cohorts[0]['label'] if inflow_cohorts else sns_channel_efficiency['top_channel_label'])}』。流入実績の高い切り口をSNS鬼デスクの採点へ自動加点しています。",
        })
    if pv_tot > 0:
        if human_pv_tot > 0:
            wins.append({
                "title": f"実ブラウザ訪問・店舗比較が稼働中（実測 {inflow_north_star['daily_pv']} PV/日・期間計 {human_pv_tot:,} PV ／ 人間操作率 {effective_eng_rate}%）",
                "detail": f"店舗詳細タップ {funnel.get('store_open_total', 0)}回・2ステップ遷移 {funnel.get('cushion_clicks_total', 0)}回・マップ/公式 {funnel.get('map_total', 0) + funnel.get('official_total', 0)}回（参考：クローラ等含む生PV計 {pv_tot:,}回）。",
            })
        else:
            wins.append({
                "title": f"サイト訪問・店舗比較が稼働中（直近{days}日 PV {pv_tot:,}回 ／ 1日平均 {inflow_north_star['daily_pv']} PV ／ 回遊率 {eng_rate}%）",
                "detail": f"店舗詳細タップ {funnel.get('store_open_total', 0)}回・記事閲覧 {funnel.get('article_views', 0)}回が発生し、直帰せず比較ツールとして使われています。",
            })
    if aff_clicks_7d > 0 and aff_tot > 0 and article_perf_by_score and article_perf_by_score[0]["aff_clicks"] > 0:
        top_art = article_perf_by_score[0]
        wins.append({
            "title": f"送客反応コンテンツ：『{top_art['title'][:26]}…』（広告クリック {top_art['aff_clicks']}件 / CTR {top_art['ctr_pct']}%）",
            "detail": f"全体でアフィリエイト送客 {aff_tot}件（直近7日 {aff_clicks_7d}件・ASP確定値は別管理）。高反応記事を優先紹介リストへ自動セットしました。",
        })
    elif aff_clicks_7d > 0 and aff_tot > 0 and offer_breakdown:
        top_off = offer_breakdown[0]
        wins.append({
            "title": f"収益送客が稼働中：『{top_off['short_label']}』を筆頭に計{aff_tot}件クリック（直近7日 {aff_clicks_7d}件）",
            "detail": f"首位カテゴリは {top_off['clicks']}件（シェア {top_off['share_pct']}%）。ASP確定成果の実測蓄積を継続中です。",
        })
    wins.append({
        "title": f"検索SEO＆AI引用の基盤コラム {col_audit['total_columns']}本が監査クリア（平均 {col_audit['avg_desk_score']}点）",
        "detail": f"会話ボックス導入 {col_audit['with_dialogue_count']}/{col_audit['total_columns']}本・ブランドNG略称 {col_audit['total_bare_oriental']}件と、Google/AI検索に評価される一次データ品質を維持中です。",
    })

    # Bottlenecks / Issues (Always surface SNS delivery errors first so operational alerts are never truncated)
    for err_label, err_msg in sns_delivery_errors[:2]:
        issues.append({
            "title": f"SNS配信ヘルス警告：{err_label} で送信エラーを検知",
            "detail": err_msg[:180],
            "action_taken": "SNS編集部（/staff/sns-editorial）でトークン状態・直近配信ログを確認してください。",
        })

    high_pv_leaky = [a for a in leaky_articles if int(a.get("pv") or 0) >= int(growth_phase.get("min_pv_for_funnel_action") or 50)]
    if active_phase == "phase1_inflow" and daily_pv < 100.0 and pv_basis == "human_pv":
        issues.append({
            "title": f"初期グロース第1関門（100 PV/日・実ブラウザPV基準）まであと {max(1, int(round(100.0 - daily_pv)))} PV/日（現在 {inflow_north_star['daily_pv']} PV/日）",
            "detail": "いまはアフィリエイトCV率を追うより、①Google/AI検索（店名×人数・レビュー・営業状況）と ②X週末実況・縦型ショート動画からの純新規着地を増やすことが最優先です。",
            "action_taken": f"TOP3提案と自動加点を『流入拡大最優先』へ統一し、優先紹介記事に『{priority_column_paths[0]}』などをセットしました。",
        })
    elif high_pv_leaky or (leaky_articles and (active_phase != "phase1_inflow" or pv_basis != "human_pv")):
        lk = (high_pv_leaky or leaky_articles)[0]
        issues.append({
            "title": f"導線ボトルネック記事：『{lk['title'][:26]}…』（{lk['pv']} PVあるのに広告クリック0件）",
            "detail": "記事への流入は取れていますが、読了後に離脱しています。2ステップ原則に沿った /concierge・/grooming・/for-women への回遊ブリッジ強化が有効です。",
            "action_taken": "優先送客枠を高エンゲージ記事へ切り替えつつ、回遊改善リストにマークしました。",
        })
    elif active_phase == "phase1_inflow" and daily_pv < 100.0:
        issues.append({
            "title": f"初期グロース第1関門（100 PV/日・生PV基準）まであと {max(1, int(round(100.0 - daily_pv)))} PV/日（現在 {inflow_north_star['daily_pv']} PV/日）",
            "detail": "いまはアフィリエイトCV率を追うより、①Google/AI検索（店舗・エリア・ドーム終演）と ②縦型ショート動画（3Dもしも＆15秒街別ギャップ）からの純新規着地を増やすことが最優先です。",
            "action_taken": f"TOP3提案と自動加点を『流入拡大最優先』へ統一し、優先紹介記事に『{priority_column_paths[0]}』などをセットしました。",
        })
    elif aff_tot == 0 and active_phase != "phase1_inflow":
        issues.append({
            "title": "アフィリエイト遷移データがまだ少なめ（記事・ガイドへの2ステップ送客強化）",
            "detail": "トップページの人数確認だけで離脱するユーザーを /concierge・/grooming・/for-women へ回遊させる余地があります。",
            "action_taken": f"優先紹介コラムに『{priority_column_paths[0]}』などキラー記事4本を自動セットしました。",
        })

    sleeping_articles = [a for a in article_perf if a["pv"] <= 2]
    if sleeping_articles:
        issues.append({
            "title": f"露出不足の埋もれコラムが {len(sleeping_articles)}本 存在（例：『{sleeping_articles[0]['title'][:24]}…』）",
            "detail": "一次データ入りのコラム資産がSNS・内部リンクから十分露出しておらず、検索クローラーと新規読者の流入機会を逃しています。",
            "action_taken": "X 18:00枠の優先ローテーションに未露出の高検索意図コラムを自動で組み込みました。",
        })

    if demand_gap["top_hub"].get("has_measured_gap"):
        gap_mode_note = (
            f"ピーク実測男女比差 {demand_gap['top_hub']['gap_pt']}pt"
            if demand_gap["top_hub"].get("is_off_hours_benchmark")
            else f"男女比差 {demand_gap['top_hub']['gap_pt']}pt"
        )
    else:
        gap_mode_note = "夜間ピーク注目クラスタ（波形蓄積中）"
    issues.append({
        "title": f"検索＆SNS需要ギャップ：いま『{demand_gap['top_hub']['cluster_label']}』の注目度が高い（{gap_mode_note}）",
        "detail": f"稼働波形・店舗チェック数に対して『{demand_gap['top_hub']['cluster_label']}』特化の実測比較コンテンツを足すと、エリア検索とショート動画の双方で新規流入が伸びやすい状態です。",
        "action_taken": f"コラム編集部の次回特命お題に『{demand_gap['recommended_topic']}』を自動発注しました。",
    })

    if upcoming_count <= 1:
        issues.append({
            "title": f"公開予約待機中のコラム在庫が残り {upcoming_count}本 です",
            "detail": "Google検索インデックス拡大と新着スレッド流入を止めないため、週1〜2本の予約ストック維持が効果的です。",
            "action_taken": "下の特命お題ボタン（または在庫自動補充モードON）で次週分を補充できます。",
        })

    # 5. Phase-Calibrated 4-Axis 100-Point Score
    classified_clicks_present = sum(int(o.get("clicks") or 0) for o in offer_breakdown) > 0
    aff_clicks_7d_eff = aff_clicks_7d if classified_clicks_present else int(((stats_7d or {}).get("funnel") or {}).get("aff_total") or 0)
    eff_aff_tot = aff_clicks_7d_eff if human_pv_tot > 0 else aff_tot
    eff_daily_aff = (eff_aff_tot / 7.0) if human_pv_tot > 0 else (aff_tot / days_norm)
    attributed_aff = sum(cnt for rp, cnt in aff_by_ref_path.items() if rp and rp != "-")
    active_offer_cats = sum(1 for o in offer_breakdown if o.get("clicks", 0) > 0)
    active_sns_channels = sum(
        1 for ch in sns_channel_efficiency.get("channels", [])
        if ch.get("channel_key") != "search" and (ch.get("visits", 0) > 0 or ch.get("posts_logged", 0) > 0)
    )
    reels_ch = next((ch for ch in sns_channel_efficiency.get("channels", []) if ch.get("channel_key") == "reels"), {})
    reels_active = (reels_ch.get("visits", 0) > 0) or (reels_ch.get("posts_logged", 0) > 0)
    moshimo_enabled = bool(((flywheel_status.get("moshimo_queue") or {}).get("enabled", True)))
    sleeping_ratio = (len(sleeping_articles) / max(1, len(article_perf))) if article_perf else 0.0

    eff_pv_for_score = human_pv_tot if human_pv_tot > 0 else pv_tot
    hpv_7d = int(((stats_7d or {}).get("funnel") or {}).get("human_pv_total") or 0)
    ctr_pv_denom = hpv_7d if (human_pv_tot > 0 and hpv_7d > 0) else eff_pv_for_score
    eff_aff_ctr_pct = (
        round((eff_aff_tot / ctr_pv_denom) * 100.0, 2)
        if ctr_pv_denom > 0
        else float(funnel.get("aff_ctr_pct") or 0.0)
    )

    overall_score, score_breakdown, score_axes = _compute_phase_calibrated_scores(
        active_phase=active_phase,
        pv_tot=eff_pv_for_score,
        daily_pv=daily_pv,
        eng_rate=effective_eng_rate,
        aff_tot=eff_aff_tot,
        daily_aff=eff_daily_aff,
        aff_ctr_pct=eff_aff_ctr_pct,
        attributed_aff=attributed_aff,
        active_offer_cats=active_offer_cats,
        cushion_total=int(funnel.get("cushion_clicks_total") or 0),
        inflow_ns=inflow_north_star,
        active_sns_channels=active_sns_channels,
        reels_active=reels_active,
        moshimo_enabled=moshimo_enabled,
        style_diversity_count=len(style_usage_counts),
        col_audit=col_audit,
        upcoming_count=upcoming_count,
        sleeping_ratio=sleeping_ratio,
    )

    directives = {
        "updated_at": datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "growth_phase": active_phase,
        "growth_phase_label": growth_phase["label"],
        "sns_style_boosts": sns_style_boosts,
        "hook_archetype_boosts": dict(closed_loop_feedback.get("hook_archetype_boosts") or {}),
        "sns_prompt_feedback": str(closed_loop_feedback.get("sns_prompt_feedback") or ""),
        "gsc_seo_directive": str(((closed_loop_feedback.get("gsc_diagnostics") or {}).get("seo_prompt_directive")) or ""),
        "reel_template_boosts": dict(closed_loop_feedback.get("reel_template_boosts") or {}),
        "note_ctr_feedback": dict(closed_loop_feedback.get("note_ctr_feedback") or {}),
        "priority_column_paths": priority_column_paths,
        "recommended_column_theme": demand_gap["recommended_theme"],
        "recommended_custom_topic": demand_gap["recommended_topic"],
        "focus_hub_label": demand_gap["top_hub"]["cluster_label"],
        "bottleneck_paths": [a["path"] for a in leaky_articles[:3]],
    }

    raw_rev = dict(stats.get("revenue") or {})
    classified_clicks = sum(int(o.get("clicks") or 0) for o in offer_breakdown)
    weighted_aff_yen = sum(int(o.get("est_revenue_yen") or 0) for o in offer_breakdown)
    est_adsense_yen = int(raw_rev.get("est_adsense_yen") if "est_adsense_yen" in raw_rev else 0)
    est_aff_yen = weighted_aff_yen if classified_clicks > 0 else int(raw_rev.get("est_aff_yen") or round(aff_tot * 90))
    est_total_yen = est_adsense_yen + est_aff_yen
    pace_denom_days = max(1, int(raw_rev.get("pace_denom_days") or days_norm))
    if aff_clicks_7d_eff == 0 or aff_tot == 0:
        reconciled_revenue = {
            "est_adsense_yen": est_adsense_yen,
            "est_aff_yen": 0,
            "est_total_yen": est_adsense_yen,
            "est_monthly_pace_yen": None,
            "pace_denom_days": pace_denom_days,
            "epc_basis": "assumed_fixed",
            "aff_clicks_7d": aff_clicks_7d_eff,
            "revenue_status": "unverified_accumulating",
            "revenue_status_label": "ASP確定値未連携（直近7日クリック0件・月額試算停止中）",
        }
    else:
        reconciled_revenue = {
            "est_adsense_yen": est_adsense_yen,
            "est_aff_yen": est_aff_yen,
            "est_total_yen": est_total_yen,
            "est_monthly_pace_yen": round((est_total_yen / pace_denom_days) * 30),
            "pace_denom_days": pace_denom_days,
            "epc_basis": "assumed_fixed",
            "aff_clicks_7d": aff_clicks_7d_eff,
            "revenue_status": "assumed_epc_only",
            "revenue_status_label": "仮定EPC参考試算（ASP確定値未連携）",
        }

    return {
        "days": days,
        "overall_score": overall_score,
        "growth_phase": growth_phase,
        "inflow_north_star": inflow_north_star,
        "score_axes": score_axes,
        "score_breakdown": score_breakdown,
        "funnel": funnel,
        "revenue": reconciled_revenue,
        "wins": wins[:4],
        "issues": issues[:4],
        "article_perf": article_perf_by_score[:10],
        "article_perf_all": article_perf_by_score,
        "sleeping_count": len(sleeping_articles),
        "style_labels": style_labels_map,
        "inflow_cohorts": inflow_cohorts[:10],
        "demand_gap": demand_gap,
        "upcoming_count": upcoming_count,
        "upcoming_articles": upcoming_cols,
        "proposed_directives": directives,
        # Google AI Ultra Revenue & Growth Intelligence extensions
        "offer_breakdown": offer_breakdown,
        "monetization_gaps": monetization_gaps,
        "sns_channel_efficiency": sns_channel_efficiency,
        "weekly_growth_actions": weekly_growth_actions,
        "closed_loop_feedback": closed_loop_feedback,
        "flywheel_status": flywheel_status,
    }


def _build_rule_based_briefing_text(diag: dict) -> str:
    """Phase-aware Chief Strategist fallback briefing tailored to Inflow-First vs Monetization phases."""
    gp = diag.get("growth_phase") or {}
    active_phase = gp.get("active_phase") or "phase1_inflow"
    phase_lbl = gp.get("label") or "Phase 1｜流入拡大最優先"
    inflow_ns = diag.get("inflow_north_star") or {}
    hub_label = diag["demand_gap"]["top_hub"]["cluster_label"]
    gap_pt = diag["demand_gap"]["top_hub"]["gap_pt"]
    has_measured_gap = bool(diag["demand_gap"]["top_hub"].get("has_measured_gap"))
    gap_note = f"店舗間男女比差 {gap_pt}pt" if has_measured_gap else "店舗間男女比差は計測中"
    next_topic = diag["proposed_directives"]["recommended_custom_topic"]
    sns_eff = diag.get("sns_channel_efficiency") or {}
    top_ch_lbl = sns_eff.get("top_channel_label") or "🔍 自然検索・AI検索 (Google / AI)"

    if active_phase == "phase1_inflow":
        ext_tot = int(inflow_ns.get("external_inflow_total") or 0)
        wow_lbl = ((inflow_ns.get("inflow_wow") or {}).get("short_label")) or "計測蓄積中"
        dpv = inflow_ns.get("daily_pv", 0.0)
        hint = gp.get("next_phase_hint") or "第1目標 100〜300 PV/日"
        return (
            f"現在は【{phase_lbl}】モード（スコア {diag['overall_score']}点/100点 ｜ 1日平均 {dpv} PV・外部純新規流入 {ext_tot:,}件［{wow_lbl}］）。"
            f"{hint}の突破が最優先ミッションです。\n\n"
            f"いまは細かいアフィリエイトCTRを追うより、複利で積み上がる『Google検索・AI検索SEO（店舗別・エリア比較・ドーム終演サージ）』と、"
            f"非フォロワー層へ一気に届く『縦型ショート動画（平日20時 3Dもしも自動配信＋YouTubeショート同時展開）』の2軸にリソースを集中させましょう（現在首位チャネル：{top_ch_lbl}）。\n\n"
            f"今週は現場需要が最も熱い『{hub_label}』（{gap_note}）を狙う特命コラム『{next_topic}』の予約と、Search Consoleお宝クエリのタイトル改善をセットで進めるのが最短ルートです。"
        )

    offers = diag.get("offer_breakdown") or []
    gaps = diag.get("monetization_gaps") or []
    top_offer = offers[0] if offers else None
    if top_offer and top_offer.get("clicks", 0) > 0:
        offer_line = f"収益面では『{top_offer['short_label']}』が {top_offer['clicks']}クリック（シェア {top_offer['share_pct']}%）と好反応です。"
    else:
        offer_line = f"集客母数を回遊導線へ繋ぐフェーズに入り、『{top_offer['short_label'] if top_offer else 'マッチングアプリ'}』への2ステップ送客が鍵になります。"

    if gaps and gaps[0].get("pv", 0) >= int(gp.get("min_pv_for_funnel_action") or 20):
        g0 = gaps[0]
        gap_line = f"高流入ページ『{g0['path']}』（{g0['pv']} PV）の {g0['placement_position']} に {g0['recommended_funnel']} を配置して取りこぼしを回収しましょう。"
    else:
        gap_line = "検索SEOとショート動画での純新規流入ペースを維持しながら、/concierge・/grooming・/for-women への2ステップ回遊率を高めていきましょう。"

    return (
        f"現在は【{phase_lbl}】モード（総合スコア {diag['overall_score']}点/100点）。{offer_line}"
        f"集客は『{top_ch_lbl}』が牽引しています。\n\n"
        f"{gap_line}\n\n"
        f"今週の特命コラム『{next_topic}』（注目エリア：{hub_label}・{gap_note}）と週末15秒ギャップ動画でさらなる流入上積みを狙います。"
    )


def _generate_pdca_briefing(diag: dict, use_ai: bool = True, cache_only: bool = False, force_refresh: bool = False) -> dict:
    """Generate human-toned Chief Strategist briefing (Gemini / Claude bounded thinking with 0ms rule-based fallback)."""
    fallback_summary = _build_rule_based_briefing_text(diag)
    if not use_ai or not sns_editorial.has_any_ai_key(allow_blocking=(not cache_only)):
        return {"text": fallback_summary, "is_ai": False, "model": ""}

    gp = diag.get("growth_phase") or {}
    active_phase = gp.get("active_phase") or "phase1_inflow"
    top_offer_key = diag["offer_breakdown"][0]["category_key"] if diag.get("offer_breakdown") else ""
    top_gap_path = diag["monetization_gaps"][0]["path"] if diag.get("monetization_gaps") else ""
    sig = (
        "v5_inflow_phase",
        active_phase,
        sns_editorial.get_ai_engine_pref(allow_blocking=False),
        bool(sns_editorial.get_claude_api_key(allow_blocking=False)),
        diag["overall_score"],
        diag["funnel"].get("pv_total", 0),
        (diag.get("inflow_north_star") or {}).get("external_inflow_total", 0),
        diag["proposed_directives"]["recommended_custom_topic"],
        top_offer_key,
        top_gap_path,
    )
    now_t = time.time()
    if not force_refresh:
        with _pdca_lock:
            if sig in _briefing_ai_cache:
                cached = _briefing_ai_cache[sig]
                if now_t - cached["ts"] < _BRIEFING_TTL_SEC:
                    return dict(cached["val"])
        shared = turbo_store.get_ai_cache("pdca_briefing", sig)
        if shared and isinstance(shared.get("val"), dict):
            with _pdca_lock:
                _briefing_ai_cache[sig] = {"ts": now_t, "val": shared["val"]}
            return dict(shared["val"])

    if cache_only:
        return {"text": fallback_summary, "is_ai": False, "model": "", "pending_async": True}

    sns_eff = diag.get("sns_channel_efficiency") or {}
    prompt_payload = {
        "growth_phase": gp,
        "inflow_north_star": diag.get("inflow_north_star") or {},
        "overall_score": diag["overall_score"],
        "score_axes": diag.get("score_axes") or [],
        "funnel": diag["funnel"],
        "sns_efficiency": {
            "top_channel": sns_eff.get("top_channel_label"),
            "best_slot": sns_eff.get("recommended_time_slot"),
            "best_style": sns_eff.get("recommended_style_label"),
            "reels_hook": ((sns_eff.get("reels_recommendations") or [{}])[0]).get("title"),
        },
        "weekly_top3_actions": [a["title"] for a in (diag.get("weekly_growth_actions") or [])[:3]],
        "wins": [w["title"] for w in diag["wins"]],
        "issues": [i["title"] for i in diag["issues"]],
        "next_directives": diag["proposed_directives"],
    }
    if active_phase == "phase1_inflow":
        focus_instruction = (
            "現在は『Phase 1：流入拡大最優先（目標 100〜300 PV/日）』です。アフィリエイト収益や細かいCVRの話は控え、"
            "「①外部純新規流入と前週比（WoW）の現在地」「②最優先2大エンジンである Google/AI検索SEO（GSCお宝クエリ・店舗/エリア/ドーム終演特集）と縦型ショート動画（IGリール・YouTubeショート 3Dもしも＆街別ギャップ）の伸ばし方」「③今週仕掛ける特命コラムと具体アクション」を、"
        )
    else:
        focus_instruction = (
            f"現在は『{gp.get('label', 'グロースフェーズ')}』です。"
            "「①純新規流入・チャネル別の勝ち筋」「②高PVページの2ステップ回遊・収益導線の改善点」「③今週仕掛ける特命コラムとショート動画の勝負手」を、"
        )
    prompt = (
        "あなたは『相席ナビ』のグロース統括・作戦参謀長（編集長）です。以下の実測PDCA診断データを読み、"
        f"マスター管理者に向けて{focus_instruction}"
        "頼もしい人間の参謀が隣で本音でブリーフィングするような鋭く自然な口語（260〜380文字程度、箇条書きではなく読みやすい2〜3段落）でまとめてください。\n"
        "※存在しない数値の捏造は厳禁。ブランド名は必ず正式名称『オリエンタルラウンジ』と表記し、『ラウンジ』を省いた呼称や『オリラジ』は絶対に使わないこと。JSON形式 `{\"briefing\": \"...\"}` のみ返してください。\n\n"
        f"{json.dumps(prompt_payload, ensure_ascii=False)}"
    )

    try:
        parsed, model_used, _ = sns_editorial.call_editorial_ai_json(
            prompt=prompt,
            max_output_tokens=2000,
            thinking_budget=1024,
            total_deadline_sec=46.0,
            per_call_timeout_sec=32.0,
            allow_blocking=True,
            prefer_fable=True,
        )
        if parsed and parsed.get("briefing"):
            b_txt = column_editorial._sanitize_brand_names((parsed.get("briefing") or "").strip())
            if b_txt:
                res = {"text": b_txt, "is_ai": True, "model": model_used, "pending_async": False}
                with _pdca_lock:
                    _briefing_ai_cache[sig] = {"ts": now_t, "val": res}
                    while len(_briefing_ai_cache) > 16:
                        _briefing_ai_cache.popitem(last=False)
                turbo_store.set_ai_cache("pdca_briefing", sig, {"ts": now_t, "val": res}, ttl_sec=3600)
                return res
    except Exception as e:
        logger.warning(f"[pdca_engine] AI briefing fallback: {e}")

    return {"text": fallback_summary, "is_ai": False, "model": "", "pending_async": False}


# =====================================================================
# Phase 2: ACT & PLAN — Apply Directives & Optional Auto-Replenishment
# =====================================================================
def run_pdca_cycle(
    live_stores: list = None,
    days: int = 14,
    apply_actions: bool = True,
    use_ai: bool = True,
    use_ai_for_replenish: bool = None,
    ai_cache_only: bool = False,
    force_ai_refresh: bool = False,
    record_log: bool = False,
    allow_blocking: bool = True,
) -> dict:
    """
    Execute full Plan-Do-Check-Act cycle:
      1. Analyze traffic, CTR, offer categories, monetization gaps, SNS `?s=` inflows, and column inventory.
      2. Persist updated `pdca_active_directives` if `apply_actions=True`.
      3. If `auto_replenish_columns` is enabled and upcoming scheduled columns <= 1,
         automatically draft, Desk-verify (>=85 pts), and schedule a new column!
      Note: `ai_cache_only=True` skips slow outbound LLM generation while still allowing fast
      analytics/directive state reads when `allow_blocking=True`.
    """
    import ops_governance
    visible_stores = ops_governance.filter_visible_stores(list(live_stores or []))
    settings = get_pdca_settings(allow_blocking=allow_blocking)
    diag = analyze_site_performance(live_stores=visible_stores, days=days, allow_blocking=allow_blocking)
    briefing = _generate_pdca_briefing(
        diag,
        use_ai=use_ai,
        cache_only=ai_cache_only,
        force_refresh=force_ai_refresh,
    )

    auto_published_result = None
    directives = diag["proposed_directives"]
    replenish_ai = use_ai if use_ai_for_replenish is None else bool(use_ai_for_replenish)

    if apply_actions:
        job_state.set_value(PDCA_DIRECTIVES_KEY, json.dumps(directives, ensure_ascii=False))
        with _pdca_lock:
            _directives_mem_cache["ts"] = 0.0
            _directives_mem_cache["data"] = None
            _directives_mem_cache["verified"] = False

        # Optional Full-Auto Column Inventory Replenishment
        if settings.get("auto_replenish_columns") and diag["upcoming_count"] <= 1 and not ai_cache_only:
            try:
                col_ed = column_editorial.run_column_editorial_pipeline(
                    visible_stores,
                    theme_key=directives["recommended_column_theme"],
                    custom_topic=directives["recommended_custom_topic"],
                    use_ai=replenish_ai,
                )
                winner = col_ed.get("winner")
                if winner and int(winner.get("total_score") or 0) >= 85:
                    now_naive = datetime.datetime.now(JST).replace(tzinfo=None)
                    base_dt = now_naive + datetime.timedelta(days=2)
                    for up_item in (diag.get("upcoming_articles") or []):
                        try:
                            up_dt = scheduled_articles.parse_publish_at(up_item.get("publish_at"))
                            if up_dt.year < 9000 and up_dt + datetime.timedelta(days=3) > base_dt:
                                base_dt = up_dt + datetime.timedelta(days=3)
                        except Exception:
                            pass
                    next_pub = base_dt.replace(hour=19, minute=0, second=0, microsecond=0)
                    save_res = column_editorial.save_editorial_column(
                        candidate=winner,
                        publish_mode="schedule",
                        publish_at_str=next_pub.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    if save_res.get("ok"):
                        auto_published_result = save_res
                        diag["upcoming_count"] += 1
            except Exception as e:
                logger.warning(f"[pdca_engine] Auto column replenishment skipped: {e}")

        if record_log:
            applied_boosts = (
                [f"{k}({v:+d})" for k, v in directives["sns_style_boosts"].items() if v != 0]
                if settings.get("auto_optimize_sns", True)
                else []
            )
            _record_pdca_history({
                "timestamp": directives["updated_at"],
                "overall_score": diag["overall_score"],
                "growth_phase": (diag.get("growth_phase") or {}).get("active_phase", "phase1_inflow"),
                "growth_phase_label": (diag.get("growth_phase") or {}).get("label", "Phase 1｜流入拡大最優先"),
                "focus_hub": directives["focus_hub_label"],
                "recommended_topic": directives["recommended_custom_topic"],
                "boosted_styles": applied_boosts,
                "auto_scheduled_slug": auto_published_result["slug"] if auto_published_result else "",
            })

    return {
        "settings": settings,
        "diagnostics": diag,
        "briefing": briefing,
        "active_directives": get_active_directives(allow_blocking=allow_blocking),
        "auto_published_result": auto_published_result,
        "gemini_status": sns_editorial.get_gemini_status(allow_blocking=False),
        "history": get_pdca_history(limit=8, allow_blocking=allow_blocking),
        # Top-level convenience mirrors for Growth & Revenue Intelligence
        "growth_phase": diag.get("growth_phase") or {},
        "inflow_north_star": diag.get("inflow_north_star") or {},
        "offer_breakdown": diag["offer_breakdown"],
        "monetization_gaps": diag["monetization_gaps"],
        "sns_channel_efficiency": diag["sns_channel_efficiency"],
        "weekly_growth_actions": diag["weekly_growth_actions"],
        "closed_loop_feedback": diag.get("closed_loop_feedback") or {},
        "flywheel_status": diag.get("flywheel_status") or {},
    }


def evaluate_pdca_cycle(live_stores: list = None, days: int = 14, **kwargs) -> dict:
    """Alias wrapper for `run_pdca_cycle` preserving full backward/forward compatibility."""
    return run_pdca_cycle(live_stores=live_stores, days=days, **kwargs)


def build_pdca_dashboard_context(live_stores: list = None, days: int = 14, **kwargs) -> dict:
    """Dashboard context builder alias returning complete PDCA & Revenue Intelligence report."""
    kwargs.setdefault("apply_actions", False)
    kwargs.setdefault("ai_cache_only", True)
    return run_pdca_cycle(live_stores=live_stores, days=days, **kwargs)
