#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_theme_stats.py - 相席ナビ `/article <テーマ>` 用の非汚染・多層データモート実測抽出スクリプト

特徴:
- `DISABLE_SHEETS=1` / `DISABLE_SCHEDULER=1` を自動適用し、本番スプレッドシートやスケジューラを一切汚染しない
- `--source {local,live,both}` に対応し、本番 `/api/llms-facts.json?staff_test=1`（監査UA `curl/8.0`）と
  ローカルDB（90日生ログ・`store_hourly_rollups`・ドーム公演・現地待ち分数・別業態比較）を統合出力
- 未算出・データ不足の項目は推測値を埋めず `null` として出力する
"""

import argparse
import datetime
import json
import os
import statistics
import sys
import urllib.request

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if not os.path.exists(os.path.join(ROOT_DIR, "local_db.py")):
    ROOT_DIR = "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)

import local_db
import price_comparison
import store_master
import weekly_report

JST = datetime.timezone(datetime.timedelta(hours=9))
AUDIT_UA = "AisekiNavi-ArticleStats/2.0 (curl/8.0; staff_test)"


def _fetch_live_llms_facts(timeout_sec: float = 10.0) -> dict | None:
    url = "https://aiseki-navi.com/api/llms-facts.json?staff_test=1"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": AUDIT_UA,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_local_hourly_rollup_profile(days: int = 30) -> list[dict]:
    cutoff_date = (datetime.datetime.now(JST) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        rows = local_db._fetch_all_rows(
            """
            SELECT hour_jst,
                   SUM(samples) AS n,
                   AVG(avg_women) AS w,
                   AVG(avg_men) AS m,
                   AVG(avg_female_pct) AS wpct
            FROM store_hourly_rollups
            WHERE business_date >= ?
            GROUP BY hour_jst
            ORDER BY hour_jst ASC
            """,
            (cutoff_date,),
        )
        out = []
        for r in rows or []:
            n_val = int(r[1] or 0)
            if n_val <= 0:
                continue
            out.append({
                "hour_jst": int(r[0]),
                "samples": n_val,
                "avg_women": round(float(r[2] or 0.0), 1),
                "avg_men": round(float(r[3] or 0.0), 1),
                "avg_female_pct": round(float(r[4]), 1) if r[4] is not None else None,
            })
        return out
    except Exception:
        return []


def _extract_local_crowd_vote_wait_summary() -> dict | None:
    try:
        rows = local_db._fetch_all_rows(
            "SELECT wait_minutes FROM crowd_votes WHERE wait_minutes IS NOT NULL AND wait_minutes >= 0"
        )
        vals = [int(r[0]) for r in (rows or []) if r and r[0] is not None]
        if not vals:
            return None
        return {
            "sample_n": len(vals),
            "median_wait_minutes": round(float(statistics.median(vals)), 1),
            "mean_wait_minutes": round(float(statistics.mean(vals)), 1),
            "max_wait_minutes": max(vals),
        }
    except Exception:
        return None


def _match_stores_by_theme(theme: str, monthly_rows: list[dict], weekly_stores: list[dict]) -> list[dict]:
    if not theme:
        return []
    theme_norm = theme.lower()
    master = store_master.load() or {}
    weekly_by_name = {s.get("name"): s for s in (weekly_stores or []) if s.get("name")}
    monthly_by_slug = {r.get("store_slug"): r for r in (monthly_rows or []) if r.get("store_slug")}

    brand_keywords = {
        "オリエンタルラウンジ": "oriental",
        "ag": "ag",
        "アグ": "ag",
        "jis": "jis",
        "ジス": "jis",
        "asobibar": "asobibar",
        "アソビバー": "asobibar",
        "ヤタコイ": "yatakoi",
    }
    matched_brands = {code for kw, code in brand_keywords.items() if kw in theme_norm}

    results = []
    seen_slugs = set()
    for internal_name, info in master.items():
        if str(info.get("region") or "") == "海外" or "ソウル" in internal_name:
            continue
        slug = store_master.slug_of(internal_name)
        if not slug or slug in seen_slugs:
            continue
        seo = store_master.seo_name(internal_name)
        area_label = str(info.get("area_label") or "")
        brand_code = str(info.get("brand") or "").lower()

        reasons = []
        if slug.lower() in theme_norm or seo.lower() in theme_norm:
            reasons.append("store_exact")
        if area_label and len(area_label) >= 2 and area_label.lower() in theme_norm:
            reasons.append(f"area:{area_label}")
        for city_tok in ("新宿", "渋谷", "恵比寿", "上野", "銀座", "町田", "大宮", "横浜", "梅田", "難波", "心斎橋", "茶屋町", "天満", "京都", "神戸", "三宮", "名古屋", "栄", "錦", "福岡", "天神", "小倉", "札幌", "仙台", "広島", "岡山", "熊本", "長崎", "鹿児島", "沖縄"):
            if city_tok in theme and (city_tok in seo or city_tok in area_label):
                reasons.append(f"city:{city_tok}")
                break
        if brand_code in matched_brands:
            reasons.append(f"brand:{brand_code}")

        if not reasons:
            continue
        seen_slugs.add(slug)
        m_row = monthly_by_slug.get(slug) or {}
        w_row = weekly_by_name.get(internal_name) or {}
        results.append({
            "seo_name": seo,
            "slug": slug,
            "store_url": f"/store/{slug}",
            "area_label": area_label,
            "match_reasons": sorted(set(reasons)),
            "avg_women": m_row.get("avg_women") if m_row.get("avg_women") is not None else w_row.get("avg_women"),
            "peak_women": m_row.get("peak_women") if m_row.get("peak_women") is not None else w_row.get("max_women"),
            "avg_men": m_row.get("avg_men") if m_row.get("avg_men") is not None else w_row.get("avg_men"),
            "avg_female_ratio_pct": m_row.get("avg_female_ratio_pct") or None,
            "sample_n": m_row.get("sample_count"),
        })

    results.sort(key=lambda x: (-(x.get("peak_women") or 0), x["slug"]))
    return results[:10]


def main() -> int:
    parser = argparse.ArgumentParser(description="相席ナビ 記事執筆用データモート実測抽出ツール")
    parser.add_argument("theme", nargs="*", help="記事テーマ（例: '梅田 金曜 2軒目'）")
    parser.add_argument("--days", type=int, choices=[7, 30, 90], default=30, help="集計対象日数 (7/30/90)")
    parser.add_argument("--source", choices=["local", "live", "both"], default="both", help="データ取得元")
    parser.add_argument("--json-out", default="", help="JSONファイルへの保存先パス（任意）")
    args = parser.parse_args()

    theme_str = " ".join(args.theme).strip()
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    live_facts = None
    sources_used = []
    if args.source in ("live", "both"):
        live_facts = _fetch_live_llms_facts()
        if live_facts:
            sources_used.append("live_llms_facts")

    report = {}
    monthly_rows = []
    moat_cov = {}
    hourly_profile = []
    arena_upcoming = []
    vote_wait_summary = None
    pred_accuracy = None

    if args.source in ("local", "both") or not live_facts:
        try:
            report = weekly_report.generate_weekly_report() or {}
            monthly_rows = local_db.get_monthly_store_stats_rows(days=args.days) or []
            moat_cov = local_db.get_moat_coverage_summary() or {}
            hourly_profile = _extract_local_hourly_rollup_profile(days=args.days)
            arena_upcoming = local_db.get_upcoming_arena_events(days_ahead=45, limit=12) or []
            vote_wait_summary = _extract_local_crowd_vote_wait_summary()
            acc_rep = local_db.get_forecast_accuracy_report(days=90, allow_blocking=False) or {}
            acc_sum = acc_rep.get("summary") or {}
            if int(acc_sum.get("resolved_count") or 0) > 0:
                pred_accuracy = acc_sum
            sources_used.append("local_db")
        except Exception:
            pass

    if live_facts:
        if not pred_accuracy:
            live_acc = live_facts.get("prediction_accuracy") or live_facts.get("forecast_verification_90d")
            if isinstance(live_acc, dict) and (live_acc.get("resolved_count") or live_acc.get("verified_samples")):
                pred_accuracy = live_acc
        if not arena_upcoming:
            live_arena = live_facts.get("upcoming_arena_events") or live_facts.get("arena_schedule_events")
            if isinstance(live_arena, list):
                arena_upcoming = live_arena[:12]
        if not moat_cov and isinstance(live_facts.get("dataset_coverage"), dict):
            moat_cov = live_facts["dataset_coverage"]

    stores = report.get("stores") or []
    anaba = report.get("anaba_stores") or []

    adjacent_formats = []
    for spec in getattr(price_comparison, "ADJACENT_FORMAT_SPECS", []) or []:
        if isinstance(spec, dict):
            adjacent_formats.append({
                "format_id": spec.get("format_id"),
                "format_label": spec.get("format_label"),
                "brand_examples": spec.get("brand_examples"),
                "seating_style": spec.get("seating_style"),
                "pricing_model": spec.get("pricing_model"),
                "live_headcount_public": spec.get("live_headcount_public"),
                "best_for": spec.get("best_for"),
            })

    summary = {
        "theme": theme_str,
        "data_as_of": now_str,
        "window_days": args.days,
        "source_used": "+".join(sources_used) if sources_used else "none",
        "live_schema_version": live_facts.get("schema_version") if isinstance(live_facts, dict) else None,
        "moat_coverage": moat_cov or None,
        "period": report.get("period"),
        "total_records_analyzed": report.get("total_records_analyzed") or (moat_cov.get("raw_snapshots") if moat_cov else None),
        "top5_stores": [
            {
                "seo_name": store_master.seo_name(s.get("name", "")),
                "slug": store_master.slug_of(s.get("name", "")),
                "avg_women": s.get("avg_women"),
                "max_women": s.get("max_women"),
            }
            for s in stores[:5]
        ],
        "top5_anaba": [
            {
                "seo_name": store_master.seo_name(a.get("name", "")),
                "slug": store_master.slug_of(a.get("name", "")),
                "avg_wpct": a.get("avg_wpct"),
                "avg_women": a.get("avg_women"),
            }
            for a in anaba[:5]
        ],
        "monthly_store_stats_top10": monthly_rows[:10] if monthly_rows else None,
        "matched_stores": _match_stores_by_theme(theme_str, monthly_rows, stores),
        "weekday_summary": report.get("weekday_summary") or None,
        "timeslot_summary": report.get("timeslot_summary") or None,
        "hourly_rollup_profile": hourly_profile or None,
        "prediction_accuracy_90d": pred_accuracy,
        "arena_events_upcoming": arena_upcoming or None,
        "crowd_vote_wait_minutes": vote_wait_summary,
        "adjacent_format_specs": adjacent_formats or None,
    }

    encoded = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.json_out:
        out_p = os.path.abspath(args.json_out)
        os.makedirs(os.path.dirname(out_p), exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as fp:
            fp.write(encoded + "\n")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
