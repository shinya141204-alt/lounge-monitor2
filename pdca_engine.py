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

_directives_mem_cache = {"ts": 0.0, "data": None}
_briefing_ai_cache = collections.OrderedDict()
_pdca_lock = threading.Lock()
_BRIEFING_TTL_SEC = 900


def reset_pdca_cache_for_tests():
    with _pdca_lock:
        _directives_mem_cache["ts"] = 0.0
        _directives_mem_cache["data"] = None
        _briefing_ai_cache.clear()
    turbo_store.clear_ai_cache("pdca_briefing")


def get_pdca_settings(allow_blocking: bool = True) -> dict:
    defaults = {
        "auto_optimize_sns": True,
        "auto_replenish_columns": False,
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
                "auto_replenish_columns": bool(parsed.get("auto_replenish_columns", False)),
                "updated_at": str(parsed.get("updated_at") or ""),
            })
    except Exception:
        pass
    return defaults


def update_pdca_settings(auto_optimize_sns: bool = None, auto_replenish_columns: bool = None) -> dict:
    cur = get_pdca_settings(allow_blocking=True)
    if auto_optimize_sns is not None:
        cur["auto_optimize_sns"] = bool(auto_optimize_sns)
    if auto_replenish_columns is not None:
        cur["auto_replenish_columns"] = bool(auto_replenish_columns)
    cur["updated_at"] = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    job_state.set_value(PDCA_SETTINGS_KEY, json.dumps(cur, ensure_ascii=False))
    with _pdca_lock:
        _directives_mem_cache["ts"] = 0.0
    return cur


def get_active_directives(allow_blocking: bool = False) -> dict:
    """Fast cached read of current PDCA directives consumed by SNS & Column editorial pipelines."""
    now_t = time.monotonic()
    with _pdca_lock:
        if _directives_mem_cache["data"] is not None and (now_t - _directives_mem_cache["ts"] < 30.0):
            return dict(_directives_mem_cache["data"])

    settings = get_pdca_settings(allow_blocking=allow_blocking)
    raw = job_state.get_value(PDCA_DIRECTIVES_KEY, allow_blocking=allow_blocking)
    data = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}

    if not settings.get("auto_optimize_sns", True):
        data["sns_style_boosts"] = {}
        data["priority_column_paths"] = []

    with _pdca_lock:
        _directives_mem_cache["ts"] = now_t
        _directives_mem_cache["data"] = data
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
        "winning_pitch": "「男余り・入場待ちの夜に90分粘って課金するより、今日すぐ会える恋活枠・審査制1対1へ切り替える」混雑回避文脈が最強です。",
    },
    {
        "category_key": "orbis",
        "pattern": "orbis",
        "label": "🧴 メンズスキンケア（ORBIS Mr.等）",
        "short_label": "メンズスキンケア (orbis)",
        "audience": "men",
        "epc_yen": 95,
        "primary_link": "/link/orbis",
        "default_priority": 4,
        "winning_pitch": "「間接照明×ソファ席70cmでは夕方の肌テカリ・乾燥が残酷に見える」という女性目線の本音パート直後が鉄板の勝ち筋です。",
    },
    {
        "category_key": "hair_removal",
        "pattern": "hair_removal",
        "label": "🪒 メンズ医療脱毛・青ヒゲ対策クリニック",
        "short_label": "医療脱毛 (hair_removal)",
        "audience": "men",
        "epc_yen": 160,
        "primary_link": "/link/hair_removal",
        "default_priority": 3,
        "winning_pitch": "仕事帰りの「19時以降の青ヒゲ減点」と絡め、初回体験トライアル枠として /grooming や乾杯10分コラムで訴求すると高単価CVに繋がります。",
    },
    {
        "category_key": "spu",
        "pattern": "spu",
        "label": "👔 メンズファッション（SPUスタイルアップ便）",
        "short_label": "メンズファッション (spu)",
        "audience": "men",
        "epc_yen": 120,
        "primary_link": "/link/spu",
        "default_priority": 2,
        "winning_pitch": "「ハイブランドのロゴより、シワのない無地ジャストサイズが一番モテる」文脈で /grooming 服装セクション直下に置くと高CTRが出ます。",
    },
    {
        "category_key": "women",
        "pattern": "women_*",
        "label": "💄 女性向け特典（無料ハイクラスデート・美容医療・サロン）",
        "short_label": "女性向け特典 (women_*)",
        "audience": "women",
        "epc_yen": 105,
        "primary_link": "/link/women_dating_bachelor",
        "default_priority": 1,
        "winning_pitch": "Threads・Instagramの『女子組・待ちゼロ速報』から /for-women へ誘導し、完全無料デート＆美肌クリニック枠をセット提示すると伸びます。",
    },
]
_OFFER_META_BY_KEY = {c["category_key"]: c for c in OFFER_CATEGORIES}


def _classify_offer_category(raw_item_id: str) -> str:
    """Map raw `/link/<id>` or `aff_click:<id>` identifier into one of the 5 canonical offer categories."""
    it = (raw_item_id or "").strip().lower()
    if it.startswith("aff_click:"):
        it = it.split(":", 1)[1].strip()
    if not it:
        return "dating"
    if it.startswith("women_") or it == "women":
        return "women"
    if it in ("spu", "spu_styleup") or "spu" in it or "fashion" in it or "styleup" in it:
        return "spu"
    if it in ("orbis", "mens_skincare_clinic") or "orbis" in it or "skincare" in it:
        return "orbis"
    if it in ("hair_removal", "mens_hair_removal", "hair_removal_trial") or "hair" in it or "datsumo" in it:
        return "hair_removal"
    if it in ("dating", "dating_instant", "dating_bachelor", "dating_casual") or "dating" in it or "bachelor" in it or "app" in it:
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

    for raw_lbl, cnt in raw_aff.items():
        c = int(cnt or 0)
        if c <= 0:
            continue
        if "@" in str(raw_lbl):
            item_part, ref_p = [p.strip() for p in str(raw_lbl).split("@", 1)]
        else:
            item_part, ref_p = str(raw_lbl).strip(), "-"
        clean_item = item_part[10:].strip().lower() if item_part.lower().startswith("aff_click:") else item_part.lower()
        cat_k = _classify_offer_category(clean_item)
        cat_clicks[cat_k] += c
        item_clicks[clean_item] += c
        if ref_p and ref_p != "-":
            cat_refs[cat_k][ref_p] += c

    # Also support `by_event['aff_click:<id>']` (and optional `all_by_label['aff_click:<id>']`) without double-counting
    for ev_k, ev_cnt in by_event.items():
        ev_str = str(ev_k or "").strip()
        if not ev_str.lower().startswith("aff_click:"):
            continue
        c = int(ev_cnt or 0)
        if c <= 0:
            continue
        sub_item = ev_str.split(":", 1)[1].strip().lower()
        cat_k = _classify_offer_category(sub_item)
        already = cat_clicks[cat_k] if sub_item in _OFFER_META_BY_KEY else item_clicks[sub_item]
        delta = max(0, c - already)
        if delta > 0:
            cat_clicks[cat_k] += delta
            item_clicks[sub_item] += delta
            ev_lbl_map = all_labels.get(ev_str) or {}
            if isinstance(ev_lbl_map, dict):
                for ref_p, ref_c in ev_lbl_map.items():
                    rp = str(ref_p or "").strip()
                    if rp and rp != "-":
                        cat_refs[cat_k][rp] += int(ref_c or 0)

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
        else:
            top_ref_path = "-"
            top_ref_title = "未計測（これからの導線配置枠）"

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
            "recommended_funnel": "3大身だしなみ枠（ORBIS Mr.・ヒゲ脱毛・SPU私服）の即決CTAボタン固定",
            "recommended_links": ["/link/orbis", "/link/hair_removal", "/link/spu"],
            "placement_position": "冒頭の「距離70cmチェックリスト」直下および各パート終端の比較カード内",
            "action_proposal": "せっかく /grooming まで到達した顕在読者を逃さないよう、ファーストビュー直下に『肌テカリ対策（/link/orbis）』『青ヒゲ医療脱毛（/link/hair_removal）』『私服コーデ便（/link/spu）』の3択ボタンを並べて配置してください。",
        }

    if p == "/for-women" or "women" in p_low or "ladies" in p_low or "male-lead" in p_low:
        return {
            "recommended_funnel": "女性完全無料・審査制デート枠 ＋ 美容クリニック＆ご褒美サロン枠",
            "recommended_links": ["/for-women", "/link/women_dating_bachelor", "/link/women_beauty_clinic"],
            "placement_position": "「待ちゼロ店舗ボード」直下および特典・女子会メリット解説パートの直後",
            "action_proposal": "待ち時間ゼロで楽しみたい女性読者向けに、店舗ボード直下へ『完全無料・顔出しなしの審査制カフェデート（/link/women_dating_bachelor）』と『照明映え美肌クリニック枠（/link/women_beauty_clinic）』の2段カードを挿入してください。",
        }

    if any(k in p_low for k in ("first-10", "second-venue", "solo-vs-pair", "beauty-area", "talk", "impression", "clean")):
        grooming_note = "（現在サイト全体の /grooming 遷移率も低めのため最優先）" if grooming_rate_low else ""
        return {
            "recommended_funnel": f"会話前の清潔感ケア特集（/grooming）＋ スキンケア・医療脱毛直結導線{grooming_note}",
            "recommended_links": ["/grooming", "/link/orbis", "/link/hair_removal"],
            "placement_position": "記事中盤「女性目線の減点サイン（会話ボックス直後）」に /grooming 内部リンク、記事末尾まとめ前にスキンケア＆ヒゲ脱毛CTA",
            "action_proposal": "会話術や2軒目打診を調べる読者は対面直前の本気層です。会話例の直後に『実は会話以前に距離70cmで見られている清潔感チェック（/grooming）』を挟み、末尾に /link/orbis と /link/hair_removal を配置してください。",
        }

    if any(k in p_low for k in ("last-train", "weekend", "cost-break", "price", "local-vs-urban", "midnight", "hopping", "stats")):
        return {
            "recommended_funnel": "混雑・男余り回避の即時合流アプリ枠 ＋ 審査制1対1デート導線",
            "recommended_links": ["/link/dating_instant", "/link/dating_bachelor", "/grooming"],
            "placement_position": "実測データ比較表の直下（「男性待ち・損益分岐ライン」解説横）および記事末尾の固定CTA枠",
            "action_proposal": "待ち時間や料金コスパを気にしている読者に最も刺さる『男余りの夜に無理して粘るより、今すぐ近くで会えるアプリ（/link/dating_instant）・審査制デート（/link/dating_bachelor）を併用する』比較ボックスを表の直下へ追加してください。",
        }

    if p == "/" or p.startswith(("/store/", "/area/", "/areas", "/oriental-lounge", "/ag", "/jis")):
        return {
            "recommended_funnel": "満席・男性先行時の混雑回避アプリ導線 ＋ 入店前3分 /grooming リンク",
            "recommended_links": ["/link/dating_instant", "/grooming", "/for-women"],
            "placement_position": "リアルタイム人数カードの「男性先行・待ち注意」エリア直下および店舗詳細モーダル下部",
            "action_proposal": "現在人数だけ見て離脱するユーザーを受け止めるため、男性比率が高めの店舗ブロック直下に『今夜これから近くで飲める即時マッチング枠（/link/dating_instant）』と『入店前の清潔感チェック（/grooming）』を併設してください。",
        }

    return {
        "recommended_funnel": "清潔感ガイド（/grooming）経由の私服コーデ（SPU）＋ 1対1アプリ2段導線",
        "recommended_links": ["/grooming", "/link/spu", "/link/dating_bachelor"],
        "placement_position": "最初のH2見出し終わり（スクロール50%地点）に /grooming 文脈リンク、記事末尾に私服＆アプリ比較カード",
        "action_proposal": "記事読了前の途中離脱を防ぐため、中盤の体験談直後に『浮かない私服＆清潔感の基準（/grooming → /link/spu）』への内部リンクを置き、末尾に審査制デート（/link/dating_bachelor）の受け皿を足してください。",
    }


def _detect_monetization_gaps(pv_by_path: dict, aff_by_ref_path: dict, known_cols: list) -> list:
    """
    Auto-detect "High PV × Low CVR" opportunity-loss pages (`monetization_gaps`) and prescribe
    which funnel (`/grooming`,混雑回避アプリ, 女性特典 etc.) to add at which exact position.
    """
    col_title_map = {c["path"]: c["title"] for c in (known_cols or [])}
    total_col_pv = sum(int(pv_by_path.get(c["path"], 0)) for c in (known_cols or []))
    grooming_pv = int(pv_by_path.get("/grooming", 0))
    grooming_rate_low = grooming_pv < max(2, int(total_col_pv * 0.08))

    candidate_paths = []
    seen_p = set()
    for p in list(pv_by_path.keys()) + list(col_title_map.keys()):
        if not p or p in seen_p or p == "/privacy" or p.startswith("/staff"):
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
        cvr_pct = round((aff / pv) * 100, 2)

        if p in ("/grooming", "/for-women"):
            target_cvr = 4.0
        elif p.startswith("/guide"):
            target_cvr = 2.5
        elif p in ("/price-comparison", "/stats", "/columns"):
            target_cvr = 1.8
        else:
            target_cvr = 1.2

        # Pages already converting near or above target CVR are winners, not gaps
        if aff > 0 and cvr_pct >= target_cvr * 0.8:
            continue

        raw_title = col_title_map.get(p) or analytics.describe_label("page_view", p).get("title") or p
        clean_title = column_editorial._sanitize_brand_names(raw_title)
        rx = _prescribe_page_funnel_upgrade(p, grooming_rate_low=grooming_rate_low)

        est_missed = max(1, int(round(pv * (target_cvr / 100.0) - aff)))
        intent_weight = 1.45 if (p.startswith("/guide") or p in ("/price-comparison", "/for-women", "/grooming")) else 1.0
        gap_score = round(pv * max(0.4, target_cvr - cvr_pct) * intent_weight, 2)

        if pv >= 10 and aff == 0:
            sev = "critical"
            sev_badge = "🔥 最優先（高PV・CVゼロ）"
        elif pv >= 8 and cvr_pct < target_cvr * 0.5:
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
    """Classify a normalized `?s=` inflow tag into `(channel_key, hour_str, style_key)`."""
    t = (tag or "").strip().lower()
    if t.startswith("sns_inflow:"):
        t = t.split(":", 1)[1].strip()
    if not t:
        return "x", "", "live"

    if any(t.startswith(px) for px in ("reel", "ig_reel", "short", "tiktok")):
        st = re.sub(r"^(?:ig_)?reels?_?", "", t) or "short_hook"
        return "reels", "21:30", st

    m_x = re.match(r"^x(\d{2})_(.+)$", t)
    if m_x:
        return "x", f"{m_x.group(1)}:00", m_x.group(2)
    if t in ("x_rel", "x_wk", "x"):
        return "x", "19:00" if t == "x_rel" else "20:00", t

    m_th = re.match(r"^th(\d{2})_(.+)$", t)
    if m_th:
        return "threads", f"{m_th.group(1)}:00", m_th.group(2)
    if t.startswith(("th_", "threads")):
        st = re.sub(r"^th(?:reads)?_?", "", t) or "threads_city_story"
        return "threads", "20:30", st

    m_ig = re.match(r"^ig(\d{2})_(.+)$", t)
    if m_ig:
        return "instagram", f"{m_ig.group(1)}:00", m_ig.group(2)
    if t.startswith(("ig", "insta")):
        st = re.sub(r"^insta(?:gram)?_?|^ig_?", "", t) or "ig_empathy_story"
        return "instagram", "19:30", st

    return "x", "22:00", t


def _build_sns_channel_efficiency(
    stats: dict,
    inflow_cohorts: list,
    x_hist: list,
    th_hist: list,
    ig_hist: list,
    demand_gap: dict,
    offer_breakdown: list,
) -> dict:
    """
    Compare X / Threads / Instagram / Reels inflow efficiency (`sns_channel_efficiency`),
    surface the #1 channel, best time slot & editorial style, and generate Reels video concepts.
    """
    channel_defs = [
        {
            "channel_key": "x",
            "label": "𝕏 X（旧Twitter）",
            "default_slot": "22:00（1軒目→2軒目移動前）",
            "default_style": "街別ギャップ・ハシゴ型 (hub_gap)",
            "posts_logged": len(x_hist or []),
            "priority": 4,
            "base_tactic": "20時・22時速報の1行目フックを『街別ギャップ・普段比異変』に寄せ、リプ欄に高CTRコラム＋?s=識別リンクを徹底固定。",
        },
        {
            "channel_key": "threads",
            "label": "🧵 Threads（スレッズ）",
            "default_slot": "20:30（仕事帰り・女子組リサーチ帯）",
            "default_style": "街の温度差エッセイ型 (threads_city_story)",
            "posts_logged": len(th_hist or []),
            "priority": 3,
            "base_tactic": "宣伝感を消した『運営モニター裏話・男女の本音エッセイ』で共感保存を取り、奇数日リンク＆プロフ導線で /for-women とトップへ送客。",
        },
        {
            "channel_key": "instagram",
            "label": "📸 Instagram（カルーセル・保存枠）",
            "default_slot": "19:30（木〜土曜の予定決め・保存帯）",
            "default_style": "週末予測カルーセル＆女子得保存ガイド",
            "posts_logged": len(ig_hist or []),
            "priority": 2,
            "base_tactic": "週末穴場5枚スライド（/staff/sns-kit）で保存数を稼ぎ、ストーリーズのリンクスタンプ（?s=ig_story）からリアルタイム順位へ直結。",
        },
        {
            "channel_key": "reels",
            "label": "🎬 ショート動画（Reels / Shorts）",
            "default_slot": "21:30（金曜・土曜の縦型回遊ピーク）",
            "default_style": "3秒ギャップ実証・女子目線減点リアル型",
            "posts_logged": 0,
            "priority": 1,
            "base_tactic": "フォロワー外リーチが最大の伸び代。15〜20秒の『同じ街の男女比格差』『距離70cmの減点サイン』動画からプロフ ?s=reel_gap へ誘導。",
        },
    ]

    ch_visits = collections.defaultdict(int)
    ch_cvs = collections.defaultdict(int)
    ch_hours = collections.defaultdict(lambda: collections.defaultdict(int))
    ch_styles = collections.defaultdict(lambda: collections.defaultdict(int))
    style_agg = collections.defaultdict(lambda: {"visits": 0, "conversions": 0, "channel_key": "x"})

    for c in (inflow_cohorts or []):
        tag = c.get("tag") or ""
        v = int(c.get("visits") or 0)
        cv = int(c.get("conversions") or 0)
        ck, hr, st = _classify_sns_tag(tag)
        ch_visits[ck] += v
        ch_cvs[ck] += cv
        score_w = v + cv * 5
        if hr:
            ch_hours[ck][hr] += score_w
        if st:
            ch_styles[ck][st] += score_w
            style_agg[st]["visits"] += v
            style_agg[st]["conversions"] += cv
            style_agg[st]["channel_key"] = ck

    # Support optional `by_event['sns_inflow:<channel_or_tag>']` without double-counting
    by_event = (stats or {}).get("by_event") or {}
    for ev_k, ev_cnt in by_event.items():
        ev_str = str(ev_k or "").strip()
        if not ev_str.lower().startswith("sns_inflow:"):
            continue
        sub = ev_str.split(":", 1)[1].strip()
        if sub.lower().startswith("cv:"):
            ck_cv, _, st_cv = _classify_sns_tag(sub[3:])
            delta_cv = max(0, int(ev_cnt or 0) - ch_cvs[ck_cv])
            if delta_cv > 0:
                ch_cvs[ck_cv] += delta_cv
                style_agg[st_cv]["conversions"] += delta_cv
        else:
            ck_v, hr_v, st_v = _classify_sns_tag(sub)
            delta_v = max(0, int(ev_cnt or 0) - ch_visits[ck_v])
            if delta_v > 0:
                ch_visits[ck_v] += delta_v
                if hr_v:
                    ch_hours[ck_v][hr_v] += delta_v
                if st_v:
                    ch_styles[ck_v][st_v] += delta_v
                    style_agg[st_v]["visits"] += delta_v

    total_sns_visits = sum(ch_visits.values())
    channels_out = []
    for cdef in channel_defs:
        ck = cdef["channel_key"]
        v = int(ch_visits.get(ck, 0))
        cv = int(ch_cvs.get(ck, 0))
        cvr = round((cv / v) * 100, 1) if v > 0 else 0.0
        share_pct = round((v / total_sns_visits) * 100, 1) if total_sns_visits > 0 else 0.0
        eff_score = round(cv * 15.0 + v * 2.5 + cvr * 0.3, 1)

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
            "conversions": cv,
            "cvr_pct": cvr,
            "share_pct": share_pct,
            "posts_logged": cdef["posts_logged"],
            "efficiency_score": eff_score,
            "best_time_slot": best_slot,
            "best_style_label": best_style,
            "tactic": cdef["base_tactic"],
            "_sort_key": (eff_score, v, cdef["priority"]),
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
            ch["status_badge"] = "🥇 サイト誘導効率 No.1"
        elif ch["channel_key"] == "reels" and ch["visits"] == 0:
            ch["status_badge"] = "🚀 未開拓・高リーチ枠"
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
            "score": sv + scv * 6,
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
    gap_pt = max(12, int(hub_info.get("gap_pt") or 15))
    live_w = max(28, int(hub_info.get("live_women") or 48))
    top_offer_label = offer_breakdown[0]["short_label"] if offer_breakdown else "メンズスキンケア"

    reels_recommendations = [
        {
            "idea_no": 1,
            "theme_tag": "reel_hub_gap",
            "title": f"【15秒・街別格差】同じ{hub_name}・徒歩5分で男女比が{gap_pt}%割れる金曜22時のリアル",
            "hook_0_3s": f"「正直、金曜の{hub_name}で看板だけ見て1軒目に並ぶ人、かなり損してます…」",
            "body_outline": f"スマホのリアルタイム画面を映しながら、徒歩5分圏内のオリエンタルラウンジ・ag・JISで女性{live_w}名規模でも男女比が{gap_pt}pt割れる実測ギャップを3カットで比較。",
            "cta_route": "プロフリンク『?s=reel_hub_gap』→ トップ現在人数＆混雑回避アプリ枠",
            "best_post_time": "金曜・土曜 21:15〜21:45",
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
            "body_outline": "滞在時間と課金メーターの損益分岐グラフを1枚見せ、渋い夜は40分で2軒目移動または即時マッチング枠へ切り替える常連の立ち回りを解説。",
            "cta_route": "プロフリンク『?s=reel_40min』→ /guide/cost-break-even ＆ 即時合流アプリ枠",
            "best_post_time": "水曜・金曜 20:30〜21:00",
        },
    ]

    if total_sns_visits > 0:
        summary_insight = (
            f"現在の最効率チャネルは『{top_ch['label']}』（流入 {top_ch['visits']}件 / CV {top_ch['conversions']}件）。"
            f"強化すべき時間帯は {top_ch['best_time_slot']}、勝ち切り口は『{rec_style_label}』です。"
            f"さらに未開拓リーチを広げるなら Reels での『{hub_name}男女比ギャップ15秒動画』投入が最短ルートです。"
        )
    else:
        summary_insight = (
            f"『?s=』流入ログ蓄積フェーズです。初動のサイト誘導効率が最も高い『{top_ch['label']}』の 22:00枠（{rec_style_label}）を軸にしつつ、"
            f"木〜土曜 21時台に Reels ショート動画（{hub_name}ギャップ実証）を掛け合わせると新規流入が一気に伸びます。"
        )

    return {
        "top_channel_key": top_ch["channel_key"],
        "top_channel_label": top_ch["label"],
        "recommended_time_slot": top_ch["best_time_slot"],
        "recommended_style_key": rec_style_key,
        "recommended_style_label": rec_style_label,
        "total_sns_visits": total_sns_visits,
        "total_sns_conversions": sum(ch_cvs.values()),
        "summary_insight": summary_insight,
        "channels": channels_out,
        "style_ranking": style_ranking[:8],
        "reels_recommendations": reels_recommendations,
    }


def _build_weekly_growth_actions(
    demand_gap: dict,
    offer_breakdown: list,
    monetization_gaps: list,
    sns_efficiency: dict,
    upcoming_count: int,
) -> list:
    """
    Build prioritized TOP 3 Weekly Growth Actions (`weekly_growth_actions`) for the Master administrator:
      1. Funnel Tuning Proposal (導線改善提案)
      2. Column Theme Proposal (コラムテーマ提案)
      3. Reels Short-Video Proposal (リール動画ネタ提案)
    """
    top_gap = monetization_gaps[0] if monetization_gaps else None
    top_offer = offer_breakdown[0] if offer_breakdown else OFFER_CATEGORIES[0]
    hub_info = (demand_gap or {}).get("top_hub") or {}
    hub_label = hub_info.get("cluster_label") or "梅田・茶屋町"
    rec_theme = (demand_gap or {}).get("recommended_theme") or "area_hopping"
    rec_topic = (demand_gap or {}).get("recommended_topic") or f"{hub_label}エリアの男女比ギャップと徒歩5分ハシゴ基準"
    reels_top = ((sns_efficiency or {}).get("reels_recommendations") or [{}])[0]

    # Action A: Funnel Improvement (導線改善提案)
    if top_gap:
        funnel_urgency = 95 if top_gap.get("severity") in ("critical", "high") else 82
        funnel_action = {
            "action_type": "funnel_tuning",
            "category_badge": "💰 導線改善提案",
            "title": f"『{top_gap['title'][:28]}』に {top_gap['recommended_funnel'][:24]} を追加",
            "why": (
                f"現在 {top_gap['pv']} PV（広告CV {top_gap['aff_clicks']}件 / CVR {top_gap['cvr_pct']}%）で機会損失が発生中。"
                f"勝ち案件『{top_offer['short_label']}』への送客余地が最大です。"
            ),
            "how_to": f"配置位置：{top_gap['placement_position']}（推奨リンク：{', '.join(top_gap['recommended_links'][:2])}）",
            "expected_impact": f"ページCVR {top_gap['target_cvr_pct']}%到達で期間あたり +{top_gap['est_missed_clicks']}件以上の広告クリック増",
            "cta_label": "対象ページと収益枠を確認する →",
            "cta_url": top_gap["path"] if top_gap["path"].startswith("/") else "/staff/analytics",
            "_urgency": funnel_urgency,
        }
    else:
        funnel_action = {
            "action_type": "funnel_tuning",
            "category_badge": "💰 導線改善提案",
            "title": f"勝ち枠『{top_offer['short_label']}』を /grooming ＆上位コラム中盤へ横展開",
            "why": f"現在クリックシェア首位の『{top_offer['short_label']}』（{top_offer['clicks']}件）は対面直前読者との相性が抜群です。",
            "how_to": "会話ボックス直後に /grooming 内部リンク、記事末尾まとめ前に直接CTAボタンを2段配置。",
            "expected_impact": "サイト全体のアフィリエイトCTR +0.8〜1.5pt 底上げ",
            "cta_label": "収益スロット設定を確認する →",
            "cta_url": "/staff/analytics",
            "_urgency": 78,
        }

    # Action B: Column Theme Proposal (コラムテーマ提案)
    col_urgency = 92 if upcoming_count <= 1 else 84
    encoded_topic = requests.utils.quote(rec_topic, safe="")
    column_action = {
        "action_type": "column_theme",
        "category_badge": "📖 コラムテーマ提案",
        "title": f"特命コラム『{rec_topic}』を執筆・予約（現在待機在庫 {upcoming_count}本）",
        "why": (
            f"現場データで『{hub_label}』（稼働女性 {hub_info.get('live_women', 0)}名 / 店舗間差 {hub_info.get('gap_pt', 0)}pt）の注目度がトップ。"
            "検索SEOとX 18:00・新着スレッド双方で流入を取りやすいテーマです。"
        ),
        "how_to": "オリエンタルラウンジ・ag・JISの実測比較表と会話ボックスを入れ、中盤に /grooming、末尾に混雑回避アプリ枠を自然に組み込む。",
        "expected_impact": "公開初週のSNSスレッド流入＋ロングテール検索からの高意欲PV獲得",
        "cta_label": "コラム編集部でこのお題を開く →",
        "cta_url": f"/staff/column-editorial?theme={rec_theme}&topic={encoded_topic}",
        "_urgency": col_urgency,
    }

    # Action C: Reels Video Proposal (リール動画ネタ提案)
    reels_action = {
        "action_type": "reels_video",
        "category_badge": "🎬 リール動画ネタ提案",
        "title": reels_top.get("title") or f"【15秒Reels】{hub_label}の店舗間男女比ギャップ実証ショート動画",
        "why": (
            f"SNS集客首位『{(sns_efficiency or {}).get('top_channel_label', 'X')}』に加え、"
            "フォロワー外へ爆発的に届く縦型ショート動画（Reels）を掛け合わせると新規母数が一気に広がります。"
        ),
        "how_to": (
            f"冒頭3秒フック：{reels_top.get('hook_0_3s', '')} ｜ "
            f"推奨投稿帯：{reels_top.get('best_post_time', '金曜 21:30')} ｜ 誘導先：{reels_top.get('cta_route', '?s=reel_hub_gap')}"
        ),
        "expected_impact": "非フォロワー層からのプロフ遷移＆『?s=reel_*』経由の新規サイト流入開拓",
        "cta_label": "SNSスライド素材キットを開く →",
        "cta_url": "/staff/sns-kit",
        "_urgency": 80,
    }

    actions = sorted([funnel_action, column_action, reels_action], key=lambda x: x["_urgency"], reverse=True)
    for idx, act_item in enumerate(actions, start=1):
        act_item["priority"] = idx
        act_item.pop("_urgency", None)
    return actions


def _collect_all_known_columns() -> list:
    cols = []
    seen = set()
    for item in scheduled_articles.get_published_articles():
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


def _detect_hot_cluster_and_topic(live_stores: list, store_opens: dict, store_pvs: dict) -> dict:
    """Identify which city hub has the strongest user interest + live headcount right now."""
    domestic = sns_editorial._domestic_stores(live_stores or [])
    enriched = sns_editorial._enrich_with_benchmarks(domestic)

    cluster_scores = []
    for cl_label, cl_slug, kws in sns_editorial.URBAN_CLUSTERS:
        members = [
            s for s in enriched
            if any(k in (s.get("name") or "").upper() or k in (s.get("ja_name") or "") for k in kws)
        ]
        live_w = sum(int(s.get("women") or 0) for s in members)
        live_m = sum(int(s.get("men") or 0) for s in members)
        opens = 0
        for raw_lbl, cnt in store_opens.items():
            sname = raw_lbl.split(":", 1)[0].strip()
            if any(k in sname.upper() or k in sname for k in kws):
                opens += cnt
        pvs = sum(cnt for path, cnt in store_pvs.items() if cl_slug in path)
        gap_pt = 0
        active_m = [m for m in members if m.get("total", 0) >= 6]
        if len(active_m) >= 2:
            wpcts = [m.get("wpct", 50) for m in active_m]
            gap_pt = max(wpcts) - min(wpcts)

        demand_score = (opens * 4) + (pvs * 2) + live_w + (gap_pt * 2)
        cluster_scores.append({
            "cluster_label": cl_label,
            "area_slug": cl_slug,
            "live_women": live_w,
            "live_men": live_m,
            "opens": opens,
            "gap_pt": gap_pt,
            "demand_score": demand_score,
        })

    cluster_scores.sort(key=lambda x: x["demand_score"], reverse=True)
    top_hub = cluster_scores[0] if cluster_scores else {
        "cluster_label": "梅田・茶屋町",
        "area_slug": "umeda",
        "live_women": 80,
        "live_men": 55,
        "opens": 0,
        "gap_pt": 14,
        "demand_score": 100,
    }

    hub = top_hub["cluster_label"]
    if top_hub["gap_pt"] >= 12:
        rec_theme = "area_hopping"
        rec_topic = f"{hub}エリアの店舗間男女比ギャップ（最大{top_hub['gap_pt']}pt差）と失敗しない徒歩5分ハシゴ基準"
    elif hub in ("梅田・茶屋町", "難波・心斎橋", "渋谷・恵比寿"):
        rec_theme = "midnight_wave"
        rec_topic = f"{hub}で23時過ぎから逆転する「深夜0時半の第2波」と2軒目ラウンジの選び方"
    else:
        rec_theme = "second_venue_move"
        rec_topic = f"{hub}で気を使って90分粘らず「40分見切り＆2軒目バー誘導」で勝率を上げる実践ルート"

    return {
        "top_hub": top_hub,
        "cluster_ranking": cluster_scores[:4],
        "recommended_theme": rec_theme,
        "recommended_topic": rec_topic,
    }


def analyze_site_performance(live_stores: list = None, days: int = 14, allow_blocking: bool = True) -> dict:
    """Run comprehensive Check diagnostics across traffic, affiliate CTR, SNS inflows, and content queue."""
    stats = analytics.summary(days=days, allow_blocking=allow_blocking)
    stats_7d = stats if days == 7 else analytics.summary(days=7, allow_blocking=False)
    all_labels = stats.get("all_by_label") or {}
    pv_by_path = all_labels.get("page_view") or {}
    aff_by_label = all_labels.get("affiliate_click") or {}
    inflow_by_label = all_labels.get("sns_inflow") or {}
    store_opens = all_labels.get("store_open") or {}
    funnel = stats.get("funnel") or {}

    # 1. Cross-reference PV and Affiliate Clicks per page
    aff_by_ref_path = collections.defaultdict(int)
    aff_by_item = collections.defaultdict(int)
    for raw_lbl, cnt in aff_by_label.items():
        if "@" in raw_lbl:
            item_id, ref_p = [p.strip() for p in raw_lbl.split("@", 1)]
        else:
            item_id, ref_p = raw_lbl.strip(), "-"
        aff_by_ref_path[ref_p] += cnt
        aff_by_item[item_id] += cnt

    known_cols = _collect_all_known_columns()
    article_perf = []
    for col in known_cols:
        p = col["path"]
        pvs = int(pv_by_path.get(p, 0))
        affs = int(aff_by_ref_path.get(p, 0))
        ctr = round((affs / pvs) * 100, 2) if pvs > 0 else 0.0
        if pvs >= 8 and ctr >= 2.0:
            badge = "🏆 高CV牽引"
        elif col.get("is_new_release") and pvs < 15:
            badge = "🌱 新着・露出強化枠"
        elif pvs >= 15 and affs == 0:
            badge = "⚠️ 導線ロス（高PV・CV0）"
        elif pvs == 0:
            badge = "💤 SNS送客待ち"
        else:
            badge = "📈 安定稼働"
        article_perf.append({
            "slug": col["slug"],
            "path": p,
            "title": col["title"],
            "is_new_release": col["is_new_release"],
            "pv": pvs,
            "aff_clicks": affs,
            "ctr_pct": ctr,
            "status_badge": badge,
        })

    article_perf_by_score = sorted(
        article_perf,
        key=lambda x: (x["aff_clicks"] * 15 + x["ctr_pct"] * 5 + x["pv"], 1 if x["is_new_release"] else 0),
        reverse=True,
    )

    # 2. Aggregate SNS `?s=` Inflow Cohorts + SNS Editorial Logs
    inflow_visits = collections.defaultdict(int)
    inflow_cvs = collections.defaultdict(int)
    style_inflow_score = collections.defaultdict(int)

    for raw_lbl, cnt in inflow_by_label.items():
        src_part = raw_lbl.split("@", 1)[0].strip() if "@" in raw_lbl else raw_lbl.strip()
        if src_part.startswith("cv:"):
            tag = analytics.normalize_inflow_source(src_part[3:])
            if tag:
                inflow_cvs[tag] += cnt
        else:
            tag = analytics.normalize_inflow_source(src_part)
            if tag:
                inflow_visits[tag] += cnt

    inflow_cohorts = []
    all_tags = sorted(set(inflow_visits.keys()) | set(inflow_cvs.keys()), key=lambda t: (inflow_cvs[t] * 10 + inflow_visits[t]), reverse=True)
    for tag in all_tags:
        v_cnt = inflow_visits[tag]
        cv_cnt = inflow_cvs[tag]
        inflow_cohorts.append({
            "tag": tag,
            "label": analytics.describe_inflow_tag(tag),
            "visits": v_cnt,
            "conversions": cv_cnt,
            "cvr_pct": round((cv_cnt / v_cnt) * 100, 1) if v_cnt > 0 else 0.0,
        })
        m_st = re.match(r"^(?:x|th)\d{2}_(.+)$", tag)
        if m_st:
            st_key = m_st.group(1)
            style_inflow_score[st_key] += v_cnt + (cv_cnt * 5)

    # Inspect recent SNS Editorial history across X, Threads, Instagram
    x_hist = sns_editorial.get_recent_editorial_history("x", limit=8, allow_blocking=False)
    th_hist = sns_editorial.get_recent_editorial_history("threads", limit=6, allow_blocking=False)
    ig_hist = sns_editorial.get_recent_editorial_history("instagram", limit=6, allow_blocking=False)
    all_sns_hist = x_hist + th_hist + ig_hist
    style_usage_counts = collections.Counter(h.get("selected_style") for h in all_sns_hist if h.get("selected_style"))

    # Compute smart SNS style score boosts (-3 to +5)
    candidate_styles = ["ai_live_adlib", "hub_gap", "surge_focus", "usual_anomaly", "east_west_talk", "threads_city_story", "threads_anomaly_insight"]
    sns_style_boosts = {}
    if style_inflow_score:
        top_st = max(style_inflow_score, key=style_inflow_score.get)
        for st in candidate_styles:
            pts = style_inflow_score.get(st, 0)
            if st == top_st and pts > 0:
                sns_style_boosts[st] = 4
            elif pts >= 3:
                sns_style_boosts[st] = 2
            elif style_usage_counts.get(st, 0) >= 4 and pts == 0:
                sns_style_boosts[st] = -2
    else:
        # Cold-start smart rotation: encourage under-used high-humanity styles & AI live ad-lib
        sns_style_boosts["ai_live_adlib"] = 3
        sns_style_boosts["hub_gap"] = 3 if style_usage_counts.get("hub_gap", 0) < 3 else 0
        sns_style_boosts["usual_anomaly"] = 2 if style_usage_counts.get("usual_anomaly", 0) < 2 else 0

    # 3. Column Queue & Quality Audit
    col_audit = column_editorial.audit_existing_columns()
    upcoming_cols = scheduled_articles.get_upcoming_articles()
    upcoming_count = len(upcoming_cols)

    store_pvs = {p: c for p, c in pv_by_path.items() if p.startswith(("/store/", "/area/"))}
    demand_gap = _detect_hot_cluster_and_topic(live_stores or [], store_opens, store_pvs)

    # 3b. Google AI Ultra Revenue & Growth Intelligence (4 Pillars)
    offer_breakdown = _build_offer_breakdown(stats, stats_7d, days=days)
    monetization_gaps = _detect_monetization_gaps(pv_by_path, aff_by_ref_path, known_cols)
    sns_channel_efficiency = _build_sns_channel_efficiency(
        stats=stats,
        inflow_cohorts=inflow_cohorts,
        x_hist=x_hist,
        th_hist=th_hist,
        ig_hist=ig_hist,
        demand_gap=demand_gap,
        offer_breakdown=offer_breakdown,
    )
    weekly_growth_actions = _build_weekly_growth_actions(
        demand_gap=demand_gap,
        offer_breakdown=offer_breakdown,
        monetization_gaps=monetization_gaps,
        sns_efficiency=sns_channel_efficiency,
        upcoming_count=upcoming_count,
    )

    # Select Top 3 priority columns for SNS promotion (high CTR winners + new releases + under-exposed gems)
    winners_paths = [a["path"] for a in article_perf_by_score if a["aff_clicks"] > 0][:2]
    new_rel_paths = [a["path"] for a in article_perf if a["is_new_release"]][:1]
    under_exposed = [a["path"] for a in article_perf if a["pv"] < 5 and a["path"] not in winners_paths + new_rel_paths][:2]
    priority_column_paths = []
    for p in (winners_paths + new_rel_paths + under_exposed + ["/guide/last-train-cliff", "/guide/first-10-minutes", "/guide/price-event-comparison"]):
        if p not in priority_column_paths:
            priority_column_paths.append(p)
        if len(priority_column_paths) >= 4:
            break

    # 4. Build "What's Working (Wins)" & "What's Not Working (Bottlenecks)"
    wins = []
    issues = []

    pv_tot = funnel.get("pv_total", 0)
    aff_tot = sum(item["clicks"] for item in offer_breakdown) or funnel.get("aff_total", 0)
    eng_rate = funnel.get("engagement_rate_pct", 0.0)

    if pv_tot > 0:
        wins.append({
            "title": f"サイト回遊・店舗チェックが稼働中（直近{days}日 PV {pv_tot:,}回 ／ エンゲージ率 {eng_rate}%）",
            "detail": f"店舗詳細タップ {funnel.get('store_open_total', 0)}回・2軒目マップ検索 {funnel.get('map_total', 0)}回が発生し、リアルタイム比較ツールとして機能しています。",
        })
    if aff_tot > 0 and article_perf_by_score:
        top_art = article_perf_by_score[0]
        wins.append({
            "title": f"収益牽引コンテンツ：『{top_art['title'][:26]}…』（広告クリック {top_art['aff_clicks']}件 / CTR {top_art['ctr_pct']}%）",
            "detail": f"全体でアフィリエイト送客 {aff_tot}件（CTR {funnel.get('aff_ctr_pct', 0)}%）。勝ち記事をXリプ欄の優先送客リストへ自動セットしました。",
        })
    if inflow_cohorts:
        best_in = inflow_cohorts[0]
        wins.append({
            "title": f"SNS集客トップ枠：{best_in['label']}（{best_in['visits']}流入 / CV {best_in['conversions']}件）",
            "detail": "実際にサイト訪問を生んでいる投稿切り口を特定し、SNS鬼デスクの採点ボーナスに自動反映しています。",
        })
    wins.append({
        "title": f"全{col_audit['total_columns']}コラムの人間味＆ブランド呼称監査クリア（平均 {col_audit['avg_desk_score']}点）",
        "detail": f"会話ボックス導入 {col_audit['with_dialogue_count']}/{col_audit['total_columns']}本・ブランド略称NG表記 {col_audit['total_bare_oriental']}件（オリエンタルラウンジ・オリラジ統一済）と高品質を維持中です。",
    })

    # Bottlenecks / Issues
    leaky_articles = [a for a in article_perf if a["pv"] >= 10 and a["aff_clicks"] == 0]
    if leaky_articles:
        lk = leaky_articles[0]
        issues.append({
            "title": f"導線ボトルネック記事：『{lk['title'][:26]}…』（{lk['pv']} PVあるのに広告クリック0件）",
            "detail": "記事は読まれていますが収益・次アクションに繋がっていません。身だしなみ・アプリ比較・リアルタイム店舗への内部導線強化が有効です。",
            "action_taken": "優先送客枠をCTR上位コラムへ切り替えつつ、改善対象リストにマークしました。",
        })
    elif aff_tot == 0:
        issues.append({
            "title": "アフィリエイト遷移データがまだ少なめ（記事ページへの送客母数アップが必要）",
            "detail": "トップページの人数確認だけで離脱するユーザーが多いため、SNSリプ欄やトップから実践コラム（清潔感・40分見切り等）への誘導を増やす余地があります。",
            "action_taken": f"X 18:00枠・リプ欄の優先紹介コラムに『{priority_column_paths[0]}』などキラー記事4本を自動セットしました。",
        })

    sleeping_articles = [a for a in article_perf if a["pv"] <= 2]
    if sleeping_articles:
        issues.append({
            "title": f"露出不足の埋もれコラムが {len(sleeping_articles)}本 存在（例：『{sleeping_articles[0]['title'][:24]}…』）",
            "detail": "せっかく人間味のある会話入りコラムがあるのに、SNSからのリンク露出が回っておらず検索流入待ちになっています。",
            "action_taken": "Xコラムの優先ローテーション枠に未露出記事を自動で組み込みました。",
        })

    issues.append({
        "title": f"エリア需要ギャップ：いま『{demand_gap['top_hub']['cluster_label']}』の注目度が高い（男女比差 {demand_gap['top_hub']['gap_pt']}pt）",
        "detail": f"現在の稼働人数・店舗チェック数に対して、『{demand_gap['top_hub']['cluster_label']}』特化の実践ストーリー記事を増やすと検索・SNS双方で伸びやすい状態です。",
        "action_taken": f"コラム編集部の次回特命お題に『{demand_gap['recommended_topic']}』を自動発注しました。",
    })

    if upcoming_count <= 1:
        issues.append({
            "title": f"公開予約待機中のコラム在庫が残り {upcoming_count}本 です",
            "detail": "継続的なSEO評価とX新着スレッド流入を止めないため、週1〜2本の予約ストック維持が理想です。",
            "action_taken": "下の特命お題ボタン（または在庫自動補充モードON）で即座に次週分を補充できます。",
        })

    # 5. 4-Axis 100-Point Health Score
    s_traffic = min(25, 12 + int(min(13, pv_tot / 15)) + (3 if eng_rate >= 15 else 0))
    s_monetize = min(25, 12 + min(10, aff_tot * 2) + (3 if funnel.get("aff_ctr_pct", 0) >= 1.0 else 0))
    s_sns = min(25, 16 + min(5, len(inflow_cohorts) * 2) + (4 if len(style_usage_counts) >= 2 else 2))
    s_content = min(25, 15 + (6 if col_audit["total_bare_oriental"] == 0 else 0) + min(4, upcoming_count * 2))
    overall_score = s_traffic + s_monetize + s_sns + s_content

    directives = {
        "updated_at": datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "sns_style_boosts": sns_style_boosts,
        "priority_column_paths": priority_column_paths,
        "recommended_column_theme": demand_gap["recommended_theme"],
        "recommended_custom_topic": demand_gap["recommended_topic"],
        "focus_hub_label": demand_gap["top_hub"]["cluster_label"],
        "bottleneck_paths": [a["path"] for a in leaky_articles[:3]],
    }

    return {
        "days": days,
        "overall_score": overall_score,
        "score_breakdown": {
            "traffic_engagement": s_traffic,
            "monetization_efficiency": s_monetize,
            "sns_diversity_inflow": s_sns,
            "content_pipeline_health": s_content,
        },
        "funnel": funnel,
        "revenue": stats.get("revenue") or {},
        "wins": wins[:4],
        "issues": issues[:4],
        "article_perf": article_perf_by_score[:10],
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
    }


def _build_rule_based_briefing_text(diag: dict) -> str:
    """Rich, sharp Chief Strategist fallback briefing weaving live revenue, funnel gaps, SNS efficiency, and top actions."""
    hub_label = diag["demand_gap"]["top_hub"]["cluster_label"]
    gap_pt = diag["demand_gap"]["top_hub"]["gap_pt"]
    offers = diag.get("offer_breakdown") or []
    gaps = diag.get("monetization_gaps") or []
    sns_eff = diag.get("sns_channel_efficiency") or {}

    top_offer = offers[0] if offers else None
    if top_offer and top_offer.get("clicks", 0) > 0:
        offer_line = f"収益面では『{top_offer['short_label']}』が {top_offer['clicks']}クリック（シェア {top_offer['share_pct']}%）と頭ひとつ抜けて反応が良く、ここが今の明確な勝ち筋です。"
    else:
        offer_line = f"収益面はこれからクリック母数を積むフェーズですが、相席直前の読者には『{top_offer['short_label'] if top_offer else 'メンズスキンケア'}』と混雑回避アプリ枠の2軸が一番刺さります。"

    if gaps and gaps[0].get("pv", 0) > 0:
        g0 = gaps[0]
        gap_line = (
            f"一方で一番もったいない機会損失は『{g0['path']}』（{g0['pv']} PV / CVR {g0['cvr_pct']}%）です。"
            f"{g0['placement_position']}に {g0['recommended_funnel']} を足すだけで、取りこぼしている成約をすぐ拾えます。"
        )
    else:
        gap_line = "導線面では、会話系・2軒目系コラムの中盤に『/grooming（身だしなみ特集）』、料金・終電系コラムの表直下に『即時合流アプリ枠』を先回り配置しておくのが鉄則です。"

    top_ch_lbl = sns_eff.get("top_channel_label") or "𝕏 X（旧Twitter）"
    best_slot = sns_eff.get("recommended_time_slot") or "22:00枠"
    next_topic = diag["proposed_directives"]["recommended_custom_topic"]

    return (
        f"総合コンディションは {diag['overall_score']}点/100点。{offer_line}"
        f"集客では『{top_ch_lbl}』の {best_slot} が最も効率よくサイト誘導を作れています。\n\n"
        f"{gap_line}\n\n"
        f"今週の勝負手として、現場関心が最も熱い『{hub_label}』（店舗間男女比差 {gap_pt}pt）を狙い撃つ特命コラム『{next_topic}』の予約と、"
        f"金曜21時半の15秒リール動画（街別ギャップ実証）をセットで回していきましょう。"
    )


def _generate_pdca_briefing(diag: dict, use_ai: bool = True, cache_only: bool = False, force_refresh: bool = False) -> dict:
    """Generate human-toned Chief Strategist briefing (Gemini / Claude bounded thinking with 0ms rule-based fallback)."""
    fallback_summary = _build_rule_based_briefing_text(diag)
    if not use_ai or not sns_editorial.has_any_ai_key(allow_blocking=(not cache_only)):
        return {"text": fallback_summary, "is_ai": False, "model": ""}

    top_offer_key = diag["offer_breakdown"][0]["category_key"] if diag.get("offer_breakdown") else ""
    top_gap_path = diag["monetization_gaps"][0]["path"] if diag.get("monetization_gaps") else ""
    sig = (
        sns_editorial.get_ai_engine_pref(allow_blocking=False),
        diag["overall_score"],
        diag["funnel"].get("pv_total", 0),
        diag["funnel"].get("aff_total", 0),
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
        "overall_score": diag["overall_score"],
        "funnel": diag["funnel"],
        "revenue": diag.get("revenue") or {},
        "offer_breakdown_top3": [
            {
                "category": o["short_label"],
                "clicks": o["clicks"],
                "share_pct": o["share_pct"],
                "trend_7d": o["trend_7d_label"],
            }
            for o in (diag.get("offer_breakdown") or [])[:3]
        ],
        "monetization_gaps_top2": [
            {
                "path": g["path"],
                "pv": g["pv"],
                "cvr_pct": g["cvr_pct"],
                "add_funnel": g["recommended_funnel"],
                "where": g["placement_position"],
            }
            for g in (diag.get("monetization_gaps") or [])[:2]
        ],
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
    prompt = (
        "あなたは『相席ナビ』のグロース統括・作戦参謀長（編集長）です。以下の実測PDCA＆収益インテリジェンス診断データを読み、"
        "マスター管理者に向けて「①いま一番数字が出ている勝ち筋（案件カテゴリ・SNSチャネル）」「②高PVなのに取りこぼしている機会損失ページと足すべき具体導線（/grooming や混雑回避アプリ等）」「③今週仕掛けるべき特命コラム・リール動画の勝負手」を、"
        "頼もしい人間の参謀が隣で本音でブリーフィングするような鋭く自然な口語（260〜380文字程度、箇条書きではなく読みやすい2〜3段落）でまとめてください。\n"
        "※ブランド名は必ず『オリエンタルラウンジ』または略称『オリラジ』と表記し、『ラウンジ』を省いた呼称は絶対に使わないこと。JSON形式 `{\"briefing\": \"...\"}` のみ返してください。\n\n"
        f"{json.dumps(prompt_payload, ensure_ascii=False)}"
    )

    try:
        parsed, model_used, _ = sns_editorial.call_editorial_ai_json(
            prompt=prompt,
            max_output_tokens=2000,
            thinking_budget=1024,
            total_deadline_sec=28.0,
            per_call_timeout_sec=14.0,
            allow_blocking=True,
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
    ai_cache_only: bool = False,
    force_ai_refresh: bool = False,
    record_log: bool = False,
) -> dict:
    """
    Execute full Plan-Do-Check-Act cycle:
      1. Analyze traffic, CTR, offer categories, monetization gaps, SNS `?s=` inflows, and column inventory.
      2. Persist updated `pdca_active_directives` if `apply_actions=True`.
      3. If `auto_replenish_columns` is enabled and upcoming scheduled columns <= 1,
         automatically draft, Desk-verify (>=85 pts), and schedule a new column!
    """
    allow_io_blocking = False if ai_cache_only else bool(apply_actions or not ai_cache_only)
    settings = get_pdca_settings(allow_blocking=allow_io_blocking)
    diag = analyze_site_performance(live_stores=live_stores, days=days, allow_blocking=allow_io_blocking)
    briefing = _generate_pdca_briefing(
        diag,
        use_ai=use_ai,
        cache_only=ai_cache_only,
        force_refresh=force_ai_refresh,
    )

    auto_published_result = None
    directives = diag["proposed_directives"]

    if apply_actions:
        job_state.set_value(PDCA_DIRECTIVES_KEY, json.dumps(directives, ensure_ascii=False))
        with _pdca_lock:
            _directives_mem_cache["ts"] = time.monotonic()
            _directives_mem_cache["data"] = dict(directives)

        # Optional Full-Auto Column Inventory Replenishment
        if settings.get("auto_replenish_columns") and diag["upcoming_count"] <= 1 and not ai_cache_only:
            try:
                col_ed = column_editorial.run_column_editorial_pipeline(
                    live_stores or [],
                    theme_key=directives["recommended_column_theme"],
                    custom_topic=directives["recommended_custom_topic"],
                    use_ai=use_ai,
                )
                winner = col_ed.get("winner")
                if winner and int(winner.get("total_score") or 0) >= 85:
                    next_pub = (datetime.datetime.now(JST).replace(tzinfo=None) + datetime.timedelta(days=2)).replace(
                        hour=19, minute=0, second=0, microsecond=0
                    )
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
            _record_pdca_history({
                "timestamp": directives["updated_at"],
                "overall_score": diag["overall_score"],
                "focus_hub": directives["focus_hub_label"],
                "recommended_topic": directives["recommended_custom_topic"],
                "boosted_styles": [f"{k}({v:+d})" for k, v in directives["sns_style_boosts"].items() if v != 0],
                "auto_scheduled_slug": auto_published_result["slug"] if auto_published_result else "",
            })

    return {
        "settings": settings,
        "diagnostics": diag,
        "briefing": briefing,
        "active_directives": get_active_directives(allow_blocking=False),
        "auto_published_result": auto_published_result,
        "gemini_status": sns_editorial.get_gemini_status(allow_blocking=False),
        "history": get_pdca_history(limit=8, allow_blocking=False),
        # Top-level convenience mirrors for Revenue & Growth Intelligence
        "offer_breakdown": diag["offer_breakdown"],
        "monetization_gaps": diag["monetization_gaps"],
        "sns_channel_efficiency": diag["sns_channel_efficiency"],
        "weekly_growth_actions": diag["weekly_growth_actions"],
    }


def evaluate_pdca_cycle(live_stores: list = None, days: int = 14, **kwargs) -> dict:
    """Alias wrapper for `run_pdca_cycle` preserving full backward/forward compatibility."""
    return run_pdca_cycle(live_stores=live_stores, days=days, **kwargs)


def build_pdca_dashboard_context(live_stores: list = None, days: int = 14, **kwargs) -> dict:
    """Dashboard context builder alias returning complete PDCA & Revenue Intelligence report."""
    kwargs.setdefault("apply_actions", False)
    kwargs.setdefault("ai_cache_only", True)
    return run_pdca_cycle(live_stores=live_stores, days=days, **kwargs)
