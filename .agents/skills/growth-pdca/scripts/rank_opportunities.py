#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rank_opportunities.py - `signals.json` から「需要量 × コンテンツ未充足度 × 収益期待EPC」を計算し、次週の必勝3手を出力する
"""

import argparse
import datetime
import glob
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
JST = datetime.timezone(datetime.timedelta(hours=9))


def _score_article_opportunities(signals: dict) -> list[dict]:
    stats = signals.get("store_stats_top20") or []
    cov = signals.get("guide_coverage") or {}
    city_mentions = cov.get("city_mention_counts_in_guides") or {}
    arenas = signals.get("upcoming_arena_events") or []
    gsc = signals.get("gsc_top_queries") or []

    candidates = []

    # 1. 高需要エリア × コラム言及少エリアのギャップ検出
    area_demand: dict[str, dict] = {}
    for row in stats:
        area = str(row.get("area_label") or "").strip()
        if not area:
            continue
        matched_city = next((c for c in city_mentions if c in area or c in str(row.get("store_name") or "")), area)
        agg = area_demand.setdefault(matched_city, {"peak_sum": 0, "stores": []})
        agg["peak_sum"] += int(row.get("peak_women") or 0)
        agg["stores"].append(row.get("store_name"))

    for city, info in area_demand.items():
        mentions = int(city_mentions.get(city, 0))
        scarcity = 1.0 / (1.0 + mentions * 0.35)
        score = round(info["peak_sum"] * scarcity * 1.15, 1)
        candidates.append({
            "type": "new_article",
            "skill": "/article",
            "score": score,
            "title": f"{city}エリアの時間帯別男女比波形・1軒目/2軒目ブランド使い分け検証コラム",
            "rationale": f"実測ピーク女性合計 {info['peak_sum']}名 に対し、既存コラムの『{city}』言及は {mentions}回 と未充足（対象店: {', '.join(info['stores'][:3])}）。",
            "target_files": ["templates/guide_<slug>.html", "routes/portal_bp.py", "templates/columns.html"],
        })

    # 2. 直近ドーム・アリーナ公演 × 終演サージ特集（音楽ライブ・ドームツアーを優先ランク付け）
    if arenas:
        def _arena_priority(item: dict) -> tuple:
            cat = str(item.get("category") or item.get("event_type") or "").lower()
            t_str = str(item.get("title") or item.get("event_title") or "")
            is_music = (
                cat in ("music", "concert", "live")
                or any(kw in t_str.upper() for kw in ("LIVE", "TOUR", "DOME", "CONCERT", "ARENA", "FES"))
                or any(kw in t_str for kw in ("ドーム", "ライブ", "ツアー", "コンサート", "公演"))
            ) and not any(sp in t_str for sp in ("ラグビー", "サッカー", "野球", "プロレス", "ボクシング"))
            att = int(item.get("expected_attendance") or item.get("capacity") or 0)
            return (1 if is_music else 0, att)

        ranked_arenas = sorted(arenas, key=_arena_priority, reverse=True)
        ev = ranked_arenas[0]
        vname = ev.get("venue_name") or ev.get("venue") or "主要ドーム"
        etitle = ev.get("title") or ev.get("event_title") or "大型ライブ"
        edate = ev.get("event_date") or ev.get("date") or ""
        is_top_music = _arena_priority(ev)[0] == 1
        candidates.append({
            "type": "event_surge_article",
            "skill": "/article",
            "score": 135.0 if is_top_music else 95.0,
            "title": f"ドーム・アリーナ終演21時台のターミナル相席サージ検証（直近注目: {edate} {vname}）",
            "rationale": f"直近30日以内に {len(arenas)} 件の大型公演予定あり（最優先音楽公演: {vname}『{etitle}』）。競合サイトが持たない独自イベント相関モートを検索流入へ転換可能。",
            "target_files": ["templates/guide_<slug>.html", "arena_calendar.py"],
        })

    # 3. Search Console クエリ＆ページ（表示10回以上・3〜30位圏内、特にクリック0件や『レビュー・評判』系検索意図を優先抽出）
    for q in gsc[:15]:
        impr = float(q.get("impressions") or 0)
        pos = float(q.get("position") or 0)
        clicks = float(q.get("clicks") or 0)
        q_text = str(q.get("query") or "")
        if impr >= 10 and 3.0 <= pos <= 30.0:
            is_review_intent = any(w in q_text for w in ("レビュー", "評判", "口コミ", "客層"))
            zero_click_bonus = 1.35 if clicks == 0 else 1.0
            intent_mult = 2.6 if is_review_intent else 1.6
            candidates.append({
                "type": "seo_rewrite_or_new",
                "skill": "/ui" if is_review_intent else "/article",
                "score": round(impr * intent_mult * zero_click_bonus, 1),
                "title": f"検索クエリ『{q_text}』のSERPタイトル＆一次データ検証セクション強化",
                "rationale": f"GSC表示 {int(impr)}回・クリック {int(clicks)}件・平均順位 {pos:.1f}位（{'『レビュー/評判』検索意図に対する実測データ提示で即CTR改善可能' if is_review_intent else 'あと少しで上位流入圏内'}）。",
                "target_files": ["templates/store_detail.html", "templates/columns.html", "routes/portal_bp.py"],
            })

    gsc_pages = signals.get("gsc_top_pages") or []
    for pg in gsc_pages[:10]:
        p_impr = float(pg.get("impressions") or 0)
        p_clicks = float(pg.get("clicks") or 0)
        p_pos = float(pg.get("position") or 0)
        p_url = str(pg.get("page") or "")
        if p_impr >= 10 and p_clicks == 0 and 3.0 <= p_pos <= 25.0:
            candidates.append({
                "type": "seo_page_ctr_rewrite",
                "skill": "/ui",
                "score": round(p_impr * 2.2, 1),
                "title": f"GSCゼロクリック上位ページ（`{p_url}`）のメタディスクリプション＆初出カナ・構造化見出し改善",
                "rationale": f"GSCページ別で表示 {int(p_impr)}回・クリック 0件・平均順位 {p_pos:.1f}位。タイトルと冒頭サマリーの検索意図整合で即クリック獲得が見込める。",
                "target_files": ["templates/store_detail.html", "routes/portal_bp.py"],
            })

    candidates.sort(key=lambda x: -x["score"])
    return candidates


def _score_short_video_and_sns_opportunities(signals: dict) -> list[dict]:
    pdca_sum = signals.get("pdca_engine_summary") or {}
    sns_eff = pdca_sum.get("sns_channel_efficiency") or {}
    dgap = pdca_sum.get("demand_gap") or {}
    top_hub = (dgap.get("top_hub") or {}).get("cluster_label") or "新宿・梅田"
    reels_list = sns_eff.get("reels_recommendations") or []
    r0 = reels_list[0] if reels_list else {}
    short_v = int(((pdca_sum.get("inflow_north_star") or {}).get("short_video_inflow_total")) or 0)

    return [
        {
            "type": "short_video_inflow",
            "skill": "/ui",
            "score": 142.0,
            "title": r0.get("title") or f"縦型ショート動画（3Dもしも平日20時＋{top_hub}15秒ギャップ実証）のプロフ誘導強化",
            "rationale": (
                f"非フォロワー層へ爆発的に届くショート動画（IGリール・YouTubeショート・TikTok｜現在純流入 {short_v}件）は、"
                "Phase 1（目標 100〜300 PV/日）でGoogle検索SEOと並ぶ最優先集客エンジン。"
                f"冒頭2秒フック『{r0.get('hook_0_3s', '同駅徒歩5分の男女比ギャップ実証')}』から `?s=short_bio` / `?s=reel_hub_gap` へ直結させる。"
            ),
            "target_files": ["moshimo_reel_queue.py", "reel_generator.py", "templates/staff_sns_editorial.html"],
        }
    ]


def _score_monetization_and_ui_opportunities(signals: dict, active_phase: str = "phase1_inflow", min_pv_gate: int = 50) -> tuple[list[dict], list[dict]]:
    """Return `(eligible_for_top3, deferred_monetization_backlog)` respecting the Phase 1 High-PV gate."""
    eligible = []
    backlog = []
    aff_cands = signals.get("affiliate_slot_optimization_candidates") or []
    unmonetized = [c for c in aff_cands if c.get("is_unmonetized_url")]
    duplicates = [c for c in aff_cands if c.get("is_duplicate_url")]

    if unmonetized:
        names = ", ".join(f"`{c['slot_id']}`" for c in unmonetized)
        item = {
            "type": "affiliate_upgrade",
            "skill": "/affiliate",
            "score": 160.0 if active_phase == "phase3_monetize" else 88.0,
            "title": f"未収益化公式直URL枠（{len(unmonetized)}枠）のASP計測リンク差し替え",
            "rationale": f"現在 {names} がASP計測タグなしの公式直URL（Phase 2以降の収益取りこぼし防止用として事前整備推奨）。",
            "target_files": ["affiliate_manager.py", "app.py", "static/affiliate_config.js", "templates/for_women.html"],
        }
        if active_phase == "phase1_inflow":
            backlog.append(item)
        else:
            eligible.append(item)

    if duplicates:
        names = ", ".join(f"`{c['slot_id']}`" for c in duplicates)
        item = {
            "type": "affiliate_diversify",
            "skill": "/affiliate",
            "score": 120.0 if active_phase == "phase3_monetize" else 82.0,
            "title": f"重複遷移先アフィリエイト枠（{len(duplicates)}枠）の別ASP案件分離テスト",
            "rationale": f"{names} が同一遷移先URLを共有中。『即時合流アプリ』と『審査制1対1カフェデート』の分離はトラフィック拡大後に効果大。",
            "target_files": ["affiliate_manager.py", "static/affiliate_config.js", "templates/grooming.html"],
        }
        if active_phase == "phase1_inflow":
            backlog.append(item)
        else:
            eligible.append(item)

    pdca_sum = signals.get("pdca_engine_summary") or {}
    for gap in (pdca_sum.get("monetization_gaps") or [])[:3]:
        if not isinstance(gap, dict) or not gap.get("title"):
            continue
        gpv = int(gap.get("pv") or 0)
        gp_path = str(gap.get("path") or "")
        entry = {
            "type": "funnel_ui_upgrade",
            "skill": "/ui",
            "score": 148.0 if gpv >= min_pv_gate else 85.0,
            "title": f"高流入ページ『{gap.get('title')}』（{gpv} PV）の2ステップ導線改善",
            "rationale": str(gap.get("action_proposal") or f"現在 {gpv} PV に対し広告遷移 {gap.get('aff_clicks', 0)}件。/grooming・/for-women への2ステップ送客余地あり。"),
            "target_files": ["templates/store_detail.html", "templates/price_comparison.html"],
        }
        if (gpv >= min_pv_gate and (active_phase != "phase1_inflow" or gp_path != "/")) or active_phase != "phase1_inflow":
            eligible.append(entry)
        else:
            backlog.append(entry)

    eligible.sort(key=lambda x: -x["score"])
    backlog.sort(key=lambda x: -x["score"])
    return eligible, backlog


def _find_previous_signals(current_path: Path, current_collected_at: str = "") -> dict | None:
    reports_dir = REPO_ROOT / ".agents" / "reports"
    files = sorted(glob.glob(str(reports_dir / "signals_20*.json")))
    cur_date_prefix = (current_collected_at or "")[:10]
    for p_str in reversed(files):
        p = Path(p_str)
        if p.resolve() != current_path.resolve():
            try:
                cand = json.loads(p.read_text(encoding="utf-8"))
                if cur_date_prefix and str(cand.get("collected_at") or "").startswith(cur_date_prefix):
                    continue
                return cand
            except Exception:
                continue
    return None


def main() -> int:
    now_jst = datetime.datetime.now(JST)
    iso_year, iso_week, _ = now_jst.isocalendar()
    default_in = REPO_ROOT / ".agents" / "reports" / "signals_latest.json"
    default_out = REPO_ROOT / ".agents" / "reports" / f"pdca_{iso_year}-W{iso_week:02d}.md"

    parser = argparse.ArgumentParser(description="相席ナビ PDCA優先アクションランキング生成")
    parser.add_argument("--in", dest="in_path", default=str(default_in), help="入力 signals.json パス")
    parser.add_argument("--out", dest="out_path", default=str(default_out), help="出力 Markdown パス")
    parser.add_argument("--json", action="store_true", help="JSON形式で標準出力へ出す")
    args = parser.parse_args()

    in_p = Path(args.in_path)
    if not in_p.exists():
        print(f"❌ 入力ファイルが見つかりません: {in_p}（先に collect_demand_signals.py を実行してください）", file=sys.stderr)
        return 1

    signals = json.loads(in_p.read_text(encoding="utf-8"))
    pdca_sum = signals.get("pdca_engine_summary") or {}
    gp = pdca_sum.get("growth_phase") or {}
    ins = pdca_sum.get("inflow_north_star") or {}
    active_phase = gp.get("active_phase") or "phase1_inflow"
    phase_label = gp.get("label") or "Phase 1｜流入拡大最優先"
    target_label = gp.get("target_label") or "第1目標: 100〜300 PV/日（月間3,000〜1万PV）"
    min_pv_gate = int(gp.get("min_pv_for_funnel_action") or (50 if active_phase == "phase1_inflow" else 20))

    art_ops = _score_article_opportunities(signals)
    short_ops = _score_short_video_and_sns_opportunities(signals)
    mon_eligible, mon_backlog = _score_monetization_and_ui_opportunities(
        signals,
        active_phase=active_phase,
        min_pv_gate=min_pv_gate,
    )

    # Phase 1（流入拡大最優先）：①検索SEO/CTR改善またはイベント・エリア記事 ②需要ギャップ記事 ③ショート動画プロフ誘導（高PV記事のみ導線改善参入）
    top3 = []
    seo_ops = [x for x in art_ops if x["type"] in ("seo_rewrite_or_new", "seo_page_ctr_rewrite")]
    content_ops = [x for x in art_ops if x["type"] not in ("seo_rewrite_or_new", "seo_page_ctr_rewrite")]

    if active_phase == "phase1_inflow":
        if seo_ops:
            top3.append(seo_ops[0])
        if content_ops:
            top3.append(content_ops[0])
        if short_ops and len(top3) < 3:
            top3.append(short_ops[0])
        pool = [x for x in (seo_ops[1:] + content_ops[1:] + mon_eligible + short_ops[1:]) if x not in top3]
        pool.sort(key=lambda x: -x["score"])
        while len(top3) < 3 and pool:
            top3.append(pool.pop(0))
        remaining = pool + mon_backlog
    else:
        if mon_eligible:
            top3.append(mon_eligible[0])
        if art_ops:
            top3.append(art_ops[0])
        pool = sorted([x for x in (mon_eligible[1:] + art_ops[1:] + short_ops) if x not in top3], key=lambda x: -x["score"])
        if pool:
            top3.append(pool.pop(0))
        remaining = pool + mon_backlog

    prev = _find_previous_signals(in_p, str(signals.get("collected_at") or ""))
    diff_note = "初回スナップショット（次回実行時に前回との外部純新規流入・記事数・需要推移差分が自動表示されます）"
    if isinstance(prev, dict):
        prev_guides = int(((prev.get("guide_coverage") or {}).get("total_guide_templates")) or 0)
        cur_guides = int(((signals.get("guide_coverage") or {}).get("total_guide_templates")) or 0)
        prev_inf = int(((((prev.get("pdca_engine_summary") or {}).get("inflow_north_star")) or {}).get("external_inflow_total")) or 0)
        cur_inf = int(ins.get("external_inflow_total") or 0)
        diff_note = (
            f"前回（{prev.get('collected_at', '-')}）比：外部純新規流入 `{prev_inf} → {cur_inf}件` "
            f"／ 公開コラム `{prev_guides} → {cur_guides}本`"
        )

    wow_note = ((ins.get("inflow_wow") or {}).get("short_label")) or "計測蓄積中"
    md_lines = [
        f"# 相席ナビ 週次グロースPDCA 最優先アクションプラン（{iso_year}-W{iso_week:02d}）",
        "",
        f"- **算出日時**: {now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}",
        f"- **現在フェーズ**: **{phase_label}**（{target_label}）",
        f"- **North Star 流入指標**: 外部純新規流入 `{ins.get('external_inflow_total', 0)}件`（{wow_note}） ／ 1日平均 `{ins.get('daily_pv', 0.0)} PV/日`",
        f"- **根拠データ**: `{in_p.relative_to(REPO_ROOT) if in_p.is_relative_to(REPO_ROOT) else in_p}`（集計窓 {signals.get('window_days')}日）",
        f"- **前回サイクル比較**: {diff_note}",
        "",
        "---",
        "",
        f"## 今週実行すべき「必勝3手」（{phase_label}基準）",
        "",
    ]

    for idx, item in enumerate(top3, 1):
        md_lines.extend([
            f"### 第{idx}手：{item['title']} （優先スコア: `{item['score']}` ／ 実行スキル: `{item['skill']}`）",
            f"- **数値根拠・選定理由**: {item['rationale']}",
            f"- **主な対象ファイル**: {', '.join(f'`{f}`' for f in item['target_files'])}",
            "",
        ])

    md_lines.extend([
        "---",
        "",
        "## 次点・Phase 2以降の収益化準備バックログ",
        "",
    ])
    for item in remaining[:5]:
        md_lines.append(f"- **[{item['skill']}] {item['title']}**（スコア `{item['score']}`）：{item['rationale']}")

    out_p = Path(args.out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    md_text = "\n".join(md_lines) + "\n"
    out_p.write_text(md_text, encoding="utf-8")

    if args.json:
        print(json.dumps({"week": f"{iso_year}-W{iso_week:02d}", "top3": top3, "diff": diff_note}, ensure_ascii=False, indent=2))
    else:
        print(f"✅ PDCA最優先アクションプランを出力しました: {out_p}\n")
        print(md_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
