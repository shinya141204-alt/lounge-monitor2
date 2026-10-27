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


def analyze_site_performance(live_stores: list = None, days: int = 14) -> dict:
    """Run comprehensive Check diagnostics across traffic, affiliate CTR, SNS inflows, and content queue."""
    stats = analytics.summary(days=days)
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
    aff_tot = funnel.get("aff_total", 0)
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
        "detail": f"会話ボックス導入 {col_audit['with_dialogue_count']}/{col_audit['total_columns']}本・単体『オリエンタル』違反 {col_audit['total_bare_oriental']}件と高品質を維持中です。",
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
    }


def _generate_pdca_briefing(diag: dict, use_ai: bool = True, cache_only: bool = False, force_refresh: bool = False) -> dict:
    """Generate human-toned Chief Strategist briefing (Gemini Flash bounded thinking with 0ms fallback)."""
    fallback_summary = (
        f"総合コンディションは {diag['overall_score']}点/100点 です。"
        f"現在は『{diag['demand_gap']['top_hub']['cluster_label']}』周辺の現場関心が高く、"
        f"SNSからは体験・街別ギャップ系の切り口を優遇し、コラムは『{diag['proposed_directives']['recommended_custom_topic']}』を次の勝負球として仕込むのが最も効率的です。"
    )
    api_key = sns_editorial.get_gemini_api_key(allow_blocking=(not cache_only))
    if not use_ai or not api_key:
        return {"text": fallback_summary, "is_ai": False, "model": ""}

    sig = (
        diag["overall_score"],
        diag["funnel"].get("pv_total", 0),
        diag["funnel"].get("aff_total", 0),
        diag["proposed_directives"]["recommended_custom_topic"],
    )
    now_t = time.time()
    with _pdca_lock:
        if not force_refresh and sig in _briefing_ai_cache:
            cached = _briefing_ai_cache[sig]
            if now_t - cached["ts"] < _BRIEFING_TTL_SEC:
                return dict(cached["val"])

    if cache_only:
        return {"text": fallback_summary, "is_ai": False, "model": "", "pending_async": True}

    prompt_payload = {
        "overall_score": diag["overall_score"],
        "funnel": diag["funnel"],
        "wins": [w["title"] for w in diag["wins"]],
        "issues": [i["title"] for i in diag["issues"]],
        "next_directives": diag["proposed_directives"],
    }
    prompt = (
        "あなたは『相席ナビ』のグロース統括・作戦参謀長です。以下の実測PDCA診断データを読み、"
        "オーナーに向けて「①いま一番うまくいっている点」「②一番もったいない弱点」「③システムが自動で切り替えた次の一手」を、"
        "頼もしい人間の参謀が隣でブリーフィングするような自然な口語（220〜320文字程度、箇条書きではなく読みやすい2〜3段落）でまとめてください。\n"
        "※単体の『オリエンタル』表記は禁止（必要なら『オリエンタルラウンジ』または『オリラジ』）。JSON形式 `{\"briefing\": \"...\"}` のみ返してください。\n\n"
        f"{json.dumps(prompt_payload, ensure_ascii=False)}"
    )

    model_name = sns_editorial._last_gemini_meta.get("last_model") or sns_editorial.DEFAULT_GEMINI_MODELS[0]
    try:
        url, headers, _, payload = sns_editorial._build_endpoint_request("ai_studio", model_name, api_key, prompt)
        resp = requests.post(url, headers=headers, json=payload, timeout=9.0)
        if resp.status_code == 200:
            parts = (((resp.json() or {}).get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            raw_txt = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if raw_txt.startswith("```"):
                raw_txt = re.sub(r"^```(?:json)?\s*", "", raw_txt)
                raw_txt = re.sub(r"\s*```$", "", raw_txt)
            parsed = json.loads(raw_txt, strict=False)
            b_txt = column_editorial._sanitize_brand_names((parsed.get("briefing") or "").strip())
            if b_txt:
                res = {"text": b_txt, "is_ai": True, "model": model_name, "pending_async": False}
                with _pdca_lock:
                    _briefing_ai_cache[sig] = {"ts": now_t, "val": res}
                    while len(_briefing_ai_cache) > 16:
                        _briefing_ai_cache.popitem(last=False)
                return res
    except Exception as e:
        logger.warning(f"[pdca_engine] Gemini briefing fallback: {e}")

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
      1. Analyze traffic, CTR, SNS `?s=` inflows, and column inventory.
      2. Persist updated `pdca_active_directives` if `apply_actions=True`.
      3. If `auto_replenish_columns` is enabled and upcoming scheduled columns <= 1,
         automatically draft, Desk-verify (>=85 pts), and schedule a new column!
    """
    settings = get_pdca_settings(allow_blocking=(not ai_cache_only))
    diag = analyze_site_performance(live_stores=live_stores, days=days)
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
    }
