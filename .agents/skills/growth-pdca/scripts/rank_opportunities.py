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

    # 2. 直近ドーム・アリーナ公演 × 終演サージ特集
    if arenas:
        ev = arenas[0]
        vname = ev.get("venue_name") or ev.get("venue") or "主要ドーム"
        etitle = ev.get("title") or ev.get("event_title") or "大型ライブ"
        edate = ev.get("event_date") or ev.get("date") or ""
        candidates.append({
            "type": "event_surge_article",
            "skill": "/article",
            "score": 135.0,
            "title": f"ドーム・アリーナ終演21時台のターミナル相席サージ検証（直近注目: {edate} {vname}）",
            "rationale": f"直近30日以内に {len(arenas)} 件の大型公演予定あり（直近: {vname}『{etitle}』）。競合サイトが持たない独自イベント相関モートを検索流入へ転換可能。",
            "target_files": ["templates/guide_<slug>.html", "arena_calendar.py"],
        })

    # 3. Search Console 高表示・低順位クエリ（任意投入時）
    for q in gsc[:5]:
        if q["impressions"] >= 50 and 4.0 <= q["position"] <= 20.0:
            candidates.append({
                "type": "seo_rewrite_or_new",
                "skill": "/article",
                "score": round(q["impressions"] * 0.8, 1),
                "title": f"検索クエリ『{q['query']}』特化のデータ検証セクション強化",
                "rationale": f"GSC表示 {int(q['impressions'])}回・平均順位 {q['position']:.1f}位（あと少しでTOP3圏内）。",
                "target_files": ["templates/columns.html", "routes/portal_bp.py"],
            })

    candidates.sort(key=lambda x: -x["score"])
    return candidates


def _score_monetization_and_ui_opportunities(signals: dict) -> list[dict]:
    out = []
    aff_cands = signals.get("affiliate_slot_optimization_candidates") or []
    unmonetized = [c for c in aff_cands if c.get("is_unmonetized_url")]
    duplicates = [c for c in aff_cands if c.get("is_duplicate_url")]

    if unmonetized:
        names = ", ".join(f"`{c['slot_id']}`" for c in unmonetized)
        out.append({
            "type": "affiliate_upgrade",
            "skill": "/affiliate",
            "score": 160.0,
            "title": f"未収益化公式直URL枠（{len(unmonetized)}枠）のASP計測リンク差し替え",
            "rationale": f"現在 {names} がASP計測タグなしの公式直URLになっているため、クリックが発生しても成果報酬が ¥0 になる機会損失が発生中（想定EPC ¥105〜¥160）。",
            "target_files": ["affiliate_manager.py", "app.py", "static/affiliate_config.js", "templates/for_women.html"],
        })

    if duplicates:
        names = ", ".join(f"`{c['slot_id']}`" for c in duplicates)
        out.append({
            "type": "affiliate_diversify",
            "skill": "/affiliate",
            "score": 120.0,
            "title": f"重複遷移先アフィリエイト枠（{len(duplicates)}枠）の別ASP案件分離テスト",
            "rationale": f"{names} が同一遷移先URLを共有中。『即時合流アプリ』と『審査制1対1カフェデート』を別案件に分離することで2ステップ誘導のCVRとEPCを最大化可能。",
            "target_files": ["affiliate_manager.py", "static/affiliate_config.js", "templates/grooming.html"],
        })

    pdca_sum = signals.get("pdca_engine_summary") or {}
    for gap in (pdca_sum.get("monetization_gaps") or [])[:2]:
        if isinstance(gap, dict) and gap.get("title"):
            out.append({
                "type": "funnel_ui_upgrade",
                "skill": "/ui",
                "score": 115.0,
                "title": str(gap.get("title")),
                "rationale": str(gap.get("reason") or gap.get("detail") or "高PVページから /grooming・/for-women への2ステップ送客率改善余地あり。"),
                "target_files": ["templates/store_detail.html", "templates/price_comparison.html"],
            })

    if not out:
        out.append({
            "type": "funnel_ui_upgrade",
            "skill": "/ui",
            "score": 110.0,
            "title": "店舗詳細（`/store/<slug>`）の『男余り・待ち発生時』別業態/事前準備2ステップ導線のマイクロコピー最適化",
            "rationale": "混雑・待機発生タイミングのユーザーは代替手段（審査制デート・2軒目予約・身だしなみ準備）への関心が高く、押し売り感ゼロで最高EPC帯へ送客可能。",
            "target_files": ["templates/store_detail.html", "templates/grooming.html", "templates/for_women.html"],
        })

    out.sort(key=lambda x: -x["score"])
    return out


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
    art_ops = _score_article_opportunities(signals)
    mon_ops = _score_monetization_and_ui_opportunities(signals)

    # バランスよく「収益/導線 最優先1〜2手」＋「データコラム 最優先1〜2手」から上位3手を厳選
    top3 = []
    if mon_ops:
        top3.append(mon_ops[0])
    if art_ops:
        top3.append(art_ops[0])
    remaining = sorted(mon_ops[1:] + art_ops[1:], key=lambda x: -x["score"])
    if remaining:
        top3.append(remaining[0])

    prev = _find_previous_signals(in_p, str(signals.get("collected_at") or ""))
    diff_note = "初回スナップショット（次回実行時に前回との記事数・未収益化枠数・需要推移差分が自動表示されます）"
    if isinstance(prev, dict):
        prev_guides = int(((prev.get("guide_coverage") or {}).get("total_guide_templates")) or 0)
        cur_guides = int(((signals.get("guide_coverage") or {}).get("total_guide_templates")) or 0)
        prev_unmon = len(prev.get("affiliate_slot_optimization_candidates") or [])
        cur_unmon = len(signals.get("affiliate_slot_optimization_candidates") or [])
        diff_note = (
            f"前回（{prev.get('collected_at', '-')}）比：公開コラム `{prev_guides} → {cur_guides}本` "
            f"／ 要改善アフィリ枠 `{prev_unmon} → {cur_unmon}枠`"
        )

    md_lines = [
        f"# 相席ナビ 週次グロースPDCA 最優先アクションプラン（{iso_year}-W{iso_week:02d}）",
        "",
        f"- **算出日時**: {now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}",
        f"- **根拠データ**: `{in_p.relative_to(REPO_ROOT) if in_p.is_relative_to(REPO_ROOT) else in_p}`（集計窓 {signals.get('window_days')}日）",
        f"- **前回サイクル比較**: {diff_note}",
        "",
        "---",
        "",
        "## 今週実行すべき「必勝3手」（需要 × 未充足度 × 収益期待EPC順）",
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
        "## バックログ候補（次点）",
        "",
    ])
    for item in remaining[1:5]:
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
