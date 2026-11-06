#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_monthly_release.py - 相席ナビ 月次一次統計リリース＆CC BY 4.0 引用キット自動生成スクリプト

出力ファイル（既定: `.agents/reports/release_<YYYY-MM>/`）:
  - `release.json`    : 推測値ゼロの構造化統計データ（不足指標は `null`）
  - `release.md`      : 調査レポート／プレスリリース原稿ドラフト（`/link/` ゼロ・正式ブランド名準拠）
  - `citation_kit.md` : 外部メディア・ブログ・note向け CC BY 4.0 コピペ引用スニペット＆アイキャッチ生成コマンド
"""

import argparse
import csv
import datetime
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(str(REPO_ROOT))

import local_db

JST = datetime.timezone(datetime.timedelta(hours=9))
AUDIT_UA = "AisekiNavi-DataRelease/1.0 (curl/8.0; staff_test)"


def _fetch_url_text(url: str, timeout_sec: float = 12.0) -> str | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": AUDIT_UA,
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return None


def _load_live_csv_rows(days: int) -> list[dict]:
    url = f"https://aiseki-navi.com/stats/monthly.csv?days={days}&staff_test=1"
    raw = _fetch_url_text(url)
    if not raw:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if len(lines) < 2:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = []
    for r in reader:
        try:
            rows.append({
                "store_slug": r.get("store_slug") or "",
                "store_name": r.get("store_name") or "",
                "brand_name": r.get("brand_name") or "",
                "area_label": r.get("area_label") or "",
                "window_days": int(r.get("window_days") or days),
                "sample_count": int(float(r.get("sample_count") or 0)),
                "avg_women": float(r["avg_women"]) if r.get("avg_women") not in (None, "") else None,
                "peak_women": int(float(r["peak_women"])) if r.get("peak_women") not in (None, "") else None,
                "avg_men": float(r["avg_men"]) if r.get("avg_men") not in (None, "") else None,
                "peak_men": int(float(r["peak_men"])) if r.get("peak_men") not in (None, "") else None,
                "avg_female_ratio_pct": float(r["avg_female_ratio_pct"]) if r.get("avg_female_ratio_pct") not in (None, "") else None,
                "store_url": r.get("store_url") or "",
            })
        except Exception:
            continue
    rows.sort(key=lambda x: (-(x.get("peak_women") or 0), -(x.get("sample_count") or 0)))
    return rows


def _load_live_facts() -> dict | None:
    raw = _fetch_url_text("https://aiseki-navi.com/api/llms-facts.json?staff_test=1")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _build_release_markdown(payload: dict) -> str:
    month = payload["month"]
    days = payload["window_days"]
    as_of = payload["data_as_of"]
    store_cnt = payload["domestic_store_count"]
    total_samples = payload["total_samples"]
    top_peak = payload.get("top_peak_women_stores") or []
    top_ratio = payload.get("top_female_ratio_stores") or []
    pred = payload.get("prediction_verification_90d")
    arenas = payload.get("upcoming_arena_events") or []

    lines = [
        f"# 【{month} 一次統計レポート】全国相席ラウンジ統計データ白書（直近{days}日集計）",
        "",
        f"- **調査主体**: 相席ナビ編集部（監修：くわもん（相席ナビ編集長））",
        f"- **集計基準時刻**: {as_of}",
        f"- **集計対象**: 国内主要ブランド（オリエンタルラウンジ、ag（アグ）、JIS（ジス）、ASOBIBAR（アソビバー）、ヤタコイ ほか）実測有効 {store_cnt} 店舗",
        f"- **総観測サンプル数**: {total_samples:,} スナップショット" if total_samples else "- **総観測サンプル数**: 集計中",
        f"- **ライセンス**: CC BY 4.0（出典明記により報道・ブログ・研究での自由な引用・転載が可能）",
        f"- **一次データCSV**: https://aiseki-navi.com/stats/monthly.csv?days={days}",
        "",
        "---",
        "",
        f"## 1. 直近{days}日間の女性最大入店数（ピーク観測値）上位店舗",
        "",
    ]
    has_spike_outlier = False
    if top_peak:
        lines.append("| 順位 | 店舗名 | エリア | 女性ピーク人数 | 女性平均人数 | 男性平均人数 | 平均女性比率 | 観測件数 |")
        lines.append("| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: |")
        for idx, s in enumerate(top_peak[:10], 1):
            wpct = f"{s['avg_female_ratio_pct']:.1f}%" if s.get("avg_female_ratio_pct") is not None else "-"
            aw_val = s.get("avg_women")
            pw_val = s.get("peak_women")
            is_spike = bool(aw_val and pw_val and aw_val > 0 and pw_val >= aw_val * 8 and (s.get("sample_count") or 0) < 1000)
            if is_spike:
                has_spike_outlier = True
            aw = f"{aw_val:.1f}名" if aw_val is not None else "-"
            am = f"{s['avg_men']:.1f}名" if s.get("avg_men") is not None else "-"
            pw = f"{pw_val}名（※）" if (pw_val is not None and is_spike) else (f"{pw_val}名" if pw_val is not None else "-")
            lines.append(
                f"| {idx} | [{s['store_name']}]({s['store_url']}) | {s.get('area_label') or '-'} | "
                f"{pw} | {aw} | {am} | {wpct} | {s.get('sample_count', 0):,} |"
            )
        if has_spike_outlier:
            lines.extend(["", "※ピーク値が期間平均の8倍以上かつ観測件数1,000件未満の店舗は単発イベント・一時観測値を含むため参考値として併記しています。"])
    else:
        lines.append("現在、対象期間の実測スナップショットを集計中です。")

    lines.extend(["", "## 2. 平均女性比率が高い注目店舗（平均女性10名以上対象）", ""])
    if top_ratio:
        for idx, s in enumerate(top_ratio[:5], 1):
            lines.append(
                f"- **{idx}. {s['store_name']}**（{s.get('area_label') or '-'}）："
                f"平均女性比率 **{s['avg_female_ratio_pct']:.1f}%** ／ 平均女性 {s['avg_women']:.1f}名・平均男性 {s['avg_men']:.1f}名（観測 n={s['sample_count']:,}）"
            )
    else:
        lines.append("- 条件を満たす店舗サンプルを蓄積中です。")

    if isinstance(pred, dict) and (pred.get("resolved_count") or pred.get("verified_samples")):
        n_ver = pred.get("resolved_count") or pred.get("verified_samples")
        mae = pred.get("mae_wpct") or pred.get("female_ratio_mae_pt")
        lines.extend([
            "",
            "## 3. 60分後男女比予測モデルの答え合わせ検証実績",
            "",
            f"- 直近検証完了件数: **{int(n_ver):,} 件**"
            + (f"（女性比率 平均絶対誤差 MAE: **{float(mae):.2f} pt**）" if isinstance(mae, (int, float)) else ""),
        ])

    if arenas:
        lines.extend(["", "## 4. 今後予定されている主要ドーム・アリーナ大型公演（終演後ターミナル流入注目日）", ""])
        for ev in arenas[:8]:
            edate = ev.get("event_date") or ev.get("date") or ""
            vname = ev.get("venue_name") or ev.get("venue") or ""
            title = ev.get("title") or ev.get("event_title") or ""
            area = ev.get("impact_area") or ev.get("nearest_area") or ""
            src_u = ev.get("source_url") or ""
            src_part = f"（出典: {src_u}）" if src_u else ""
            lines.append(f"- **{edate}**｜{vname}：『{title}』{f'（周辺注目エリア: {area}）' if area else ''}{src_part}")

    lines.extend([
        "",
        "---",
        "",
        "## データ利用・引用について（CC BY 4.0）",
        "",
        "本レポートの数値・表は **クリエイティブ・コモンズ 表示 4.0 国際（CC BY 4.0）** のもとで公開しています。",
        "記事・SNS・動画・研究資料等で引用される際は、`出典：相席ナビ編集部（https://aiseki-navi.com/stats）` のリンク明記のみで自由にご利用いただけます。",
        "",
    ])
    return "\n".join(lines)


def _build_citation_kit(payload: dict) -> str:
    month = payload["month"]
    days = payload["window_days"]
    store_cnt = payload["domestic_store_count"]
    total_samples = payload["total_samples"]
    top_r = (payload.get("top_female_ratio_stores") or [None])[0]
    top_r_title = top_r["store_name"] if top_r else "全国主要店舗"
    top_r_val = f"{top_r['avg_female_ratio_pct']:.1f}" if (top_r and top_r.get("avg_female_ratio_pct") is not None) else "-"

    return f"""# {month} 一次統計 引用キット（CC BY 4.0）

## 1. 1行クレジット表記（SNS・YouTube概要欄・note用）
出典：相席ナビ編集部「全国相席ラウンジ統計データ白書（{month}・国内{store_cnt}店舗集計）」 https://aiseki-navi.com/stats （CC BY 4.0）

## 2. Markdown 引用スニペット（ブログ・note・Qiita・Zenn用）
```markdown
> 出典：[相席ナビ編集部「全国相席ラウンジ統計データ白書（直近{days}日・n={total_samples:,}）」](https://aiseki-navi.com/stats)（CC BY 4.0）
> - 一次統計CSV：https://aiseki-navi.com/stats/monthly.csv?days={days}
> - 年末統計白書：https://aiseki-navi.com/stats/year-end-2026
```

## 3. HTML 埋め込みスニペット（Webメディア・WordPress記事用）
```html
<blockquote cite="https://aiseki-navi.com/stats">
  <p>全国{store_cnt}店舗のリアルタイム入店数・男女比・60分後予測データを継続観測（直近{days}日間・総観測 n={total_samples:,}）。</p>
  <footer>出典：<a href="https://aiseki-navi.com/stats" rel="noopener">相席ナビ編集部「全国相席ラウンジ統計データ白書」</a> ／ ライセンス：<a href="https://creativecommons.org/licenses/by/4.0/" rel="license noopener">CC BY 4.0</a></footer>
</blockquote>
```

## 4. 編集部用：16:9 図解アイキャッチ生成メモ（※外部配布時は除外）
```bash
python3 .agents/skills/article/scripts/render_editorial_eyecatch.py \\
  --slug "data-release-{month}" \\
  --cat "MONTHLY STATS" \\
  --sample "LOG n = {total_samples:,}" \\
  --title "{month} 全国相席ラウンジ統計データ白書" \\
  --sub "国内{store_cnt}店舗のピーク人数・男女比率・60分後予測検証レポート" \\
  --k1-kicker "MONITORED" --k1-head "国内実測有効店舗" --k1-val "{store_cnt}" --k1-unit "店" --k1-desc "主要ブランド横断集計" \\
  --k2-kicker "TOP RATIO" --k2-head "{top_r_title}" --k2-val "{top_r_val}" --k2-unit "%" --k2-desc "直近{days}日 平均女性比率1位" \\
  --k3-kicker "OPEN DATA" --k3-head "CC BY 4.0" --k3-val "CSV" --k3-unit "公開" --k3-desc "/stats/monthly.csv"
```
"""


def main() -> int:
    now_jst = datetime.datetime.now(JST)
    default_month = now_jst.strftime("%Y-%m")

    parser = argparse.ArgumentParser(description="相席ナビ 月次一次統計リリース＆引用キット生成")
    parser.add_argument("--month", default=default_month, help="対象年月 YYYY-MM (既定: 当月)")
    parser.add_argument("--days", type=int, choices=[7, 30, 90], default=30, help="集計窓日数 (7/30/90)")
    parser.add_argument("--source", choices=["local", "live", "both"], default="both", help="データ取得元")
    parser.add_argument("--out", default="", help="出力先ディレクトリ")
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else (REPO_ROOT / ".agents" / "reports" / f"release_{args.month}")
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    facts = None
    sources = []

    if args.source in ("live", "both"):
        rows = _load_live_csv_rows(args.days)
        facts = _load_live_facts()
        if rows:
            sources.append("live_monthly_csv")
        if facts:
            sources.append("live_llms_facts")

    if not rows and args.source in ("local", "both"):
        raw_local = local_db.get_monthly_store_stats_rows(days=args.days) or []
        for r in raw_local:
            rows.append({
                "store_slug": r.get("store_slug") or "",
                "store_name": r.get("store_name") or "",
                "brand_name": r.get("brand_name") or "",
                "area_label": r.get("area_label") or "",
                "window_days": int(r.get("window_days") or args.days),
                "sample_count": int(r.get("sample_count") or 0),
                "avg_women": r.get("avg_women"),
                "peak_women": r.get("peak_women"),
                "avg_men": r.get("avg_men"),
                "peak_men": r.get("peak_men"),
                "avg_female_ratio_pct": float(r["avg_female_ratio_pct"]) if r.get("avg_female_ratio_pct") not in (None, "") else None,
                "store_url": r.get("store_url") or "",
            })
        if rows:
            sources.append("local_db")

    pred_90d = None
    arenas = []
    if isinstance(facts, dict):
        pred_90d = facts.get("prediction_accuracy") or facts.get("forecast_verification_90d")
        arenas = facts.get("upcoming_arena_events") or facts.get("arena_schedule_events") or []

    if not pred_90d and args.source in ("local", "both"):
        rep = local_db.get_forecast_accuracy_report(days=90, allow_blocking=False) or {}
        if int((rep.get("summary") or {}).get("resolved_count") or 0) > 0:
            pred_90d = rep["summary"]
    if not arenas and args.source in ("local", "both"):
        arenas = local_db.get_upcoming_arena_events(days_ahead=45, limit=12) or []

    total_samples = sum(int(r.get("sample_count") or 0) for r in rows)
    ratio_candidates = [
        r for r in rows
        if r.get("avg_female_ratio_pct") is not None and (r.get("avg_women") or 0) >= 10.0
    ]
    ratio_candidates.sort(key=lambda x: (-x["avg_female_ratio_pct"], -(x.get("avg_women") or 0)))

    payload = {
        "month": args.month,
        "window_days": args.days,
        "data_as_of": now_jst.strftime("%Y-%m-%d %H:%M:%S JST"),
        "license": "CC BY 4.0",
        "attribution": "相席ナビ編集部（https://aiseki-navi.com）",
        "source_used": "+".join(sources) if sources else "none",
        "domestic_store_count": len(rows),
        "total_samples": total_samples if total_samples > 0 else None,
        "top_peak_women_stores": rows[:15] if rows else None,
        "top_female_ratio_stores": ratio_candidates[:10] if ratio_candidates else None,
        "prediction_verification_90d": pred_90d,
        "upcoming_arena_events": arenas[:12] if arenas else None,
    }

    json_path = out_dir / "release.json"
    md_path = out_dir / "release.md"
    kit_path = out_dir / "citation_kit.md"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_build_release_markdown(payload), encoding="utf-8")
    kit_path.write_text(_build_citation_kit({**payload, "total_samples": total_samples}), encoding="utf-8")

    print(f"✅ 月次一次統計パッケージを生成しました: {out_dir}")
    print(f"  - データ取得元: {payload['source_used']} | 有効店舗数: {len(rows)}店 | 総観測件数: {total_samples:,}")
    print(f"  - JSON      : {json_path}")
    print(f"  - 原稿ドラフト: {md_path}")
    print(f"  - 引用キット : {kit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
