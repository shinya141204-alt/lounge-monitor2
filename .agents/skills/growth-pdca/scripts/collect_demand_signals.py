#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_demand_signals.py - 相席ナビ WebグロースPDCA用 需要＆収益シグナル非汚染収集スクリプト
"""

import argparse
import csv
import datetime
import glob
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

import affiliate_manager
import local_db
import pdca_engine
import scheduled_articles
import store_master

JST = datetime.timezone(datetime.timedelta(hours=9))
AUDIT_UA = "AisekiNavi-GrowthPDCA/1.0 (curl/8.0; staff_test)"


def _fetch_live_stores() -> list[dict]:
    req = urllib.request.Request(
        "https://aiseki-navi.com/api/status?staff_test=1",
        headers={"User-Agent": AUDIT_UA, "Accept": "application/json", "Accept-Encoding": "identity"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict):
                return data.get("ranking") or data.get("stores") or data.get("data") or []
    except Exception:
        pass
    return []


def _parse_gsc_csv(csv_path: str) -> list[dict]:
    if not csv_path or not os.path.exists(csv_path):
        return []
    out = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                q = row.get("上位のクエリ") or row.get("Top queries") or row.get("query") or ""
                clicks = row.get("クリック数") or row.get("Clicks") or row.get("clicks") or "0"
                impr = row.get("表示回数") or row.get("Impressions") or row.get("impressions") or "0"
                pos = row.get("掲載順位") or row.get("Position") or row.get("position") or "0"
                if not q:
                    continue
                try:
                    out.append({
                        "query": q.strip(),
                        "clicks": float(str(clicks).replace(",", "") or 0),
                        "impressions": float(str(impr).replace(",", "") or 0),
                        "position": float(str(pos).replace(",", "") or 0),
                    })
                except (ValueError, TypeError):
                    continue
        out.sort(key=lambda x: (-x["impressions"], -x["clicks"]))
    except Exception:
        return []
    return out[:50]


def _parse_gsc_pages_csv(csv_path: str) -> list[dict]:
    if not csv_path or not os.path.exists(csv_path):
        return []
    out = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                page = row.get("上位のページ") or row.get("Top pages") or row.get("page") or ""
                clicks = row.get("クリック数") or row.get("Clicks") or row.get("clicks") or "0"
                impr = row.get("表示回数") or row.get("Impressions") or row.get("impressions") or "0"
                pos = row.get("掲載順位") or row.get("Position") or row.get("position") or "0"
                if not page:
                    continue
                try:
                    out.append({
                        "page": page.strip(),
                        "clicks": float(str(clicks).replace(",", "") or 0),
                        "impressions": float(str(impr).replace(",", "") or 0),
                        "position": float(str(pos).replace(",", "") or 0),
                    })
                except (ValueError, TypeError):
                    continue
        out.sort(key=lambda x: (-x["impressions"], -x["clicks"]))
    except Exception:
        return []
    return out[:50]


def _scan_existing_guide_coverage() -> dict:
    guide_files = sorted(glob.glob(str(REPO_ROOT / "templates" / "guide_*.html")))
    slugs = []
    combined_text_parts = []
    for p in guide_files:
        bn = os.path.basename(p)
        slug = bn[len("guide_") : -len(".html")].replace("_", "-")
        slugs.append(slug)
        try:
            combined_text_parts.append(Path(p).read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass
    corpus = "\n".join(combined_text_parts)

    area_mentions = {}
    for city in ("新宿", "渋谷", "恵比寿", "上野", "大宮", "横浜", "梅田", "難波", "心斎橋", "茶屋町", "天満", "京都", "神戸", "名古屋", "福岡", "札幌", "仙台", "広島", "熊本", "長崎", "鹿児島", "沖縄"):
        area_mentions[city] = corpus.count(city)

    scheduled_upcoming = []
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    for item in getattr(scheduled_articles, "SCHEDULED_ARTICLES", []) or []:
        if str(item.get("publish_at") or "") > now_str:
            scheduled_upcoming.append(item.get("slug"))

    return {
        "total_guide_templates": len(guide_files),
        "guide_slugs": slugs,
        "upcoming_scheduled_slugs": scheduled_upcoming,
        "city_mention_counts_in_guides": area_mentions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="相席ナビ 需要＆収益シグナル収集ツール")
    parser.add_argument("--days", type=int, default=14, help="集計対象日数 (既定: 14)")
    parser.add_argument("--live", action="store_true", help="本番 /api/status?staff_test=1 からライブ客数を非汚染取得")
    parser.add_argument("--gsc-csv", default="", help="Google Search Console クエリCSVパス（任意）")
    parser.add_argument("--out", default="", help="出力JSONパス")
    args = parser.parse_args()

    now_jst = datetime.datetime.now(JST)
    reports_dir = REPO_ROOT / ".agents" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    live_stores = _fetch_live_stores() if args.live else []
    monthly_stats = local_db.get_monthly_store_stats_rows(days=max(7, min(90, args.days * 2))) or []
    watch_counts = local_db.get_watch_intent_counts(hours=args.days * 24) or {}
    recent_votes = local_db.get_recent_votes(hours=args.days * 24) or {}
    arena_upcoming = local_db.get_upcoming_arena_events(days_ahead=30, limit=15) or []
    guide_cov = _scan_existing_guide_coverage()

    try:
        pdca_diag = pdca_engine.analyze_site_performance(
            live_stores=live_stores or None,
            days=args.days,
            allow_blocking=False,
        ) or {}
    except Exception:
        pdca_diag = {}

    aff_status = affiliate_manager.get_affiliate_slots_status(allow_blocking=False) or {}
    unmonetized_slots = [
        {
            "slot_id": s.get("slot_id"),
            "label": s.get("category_label") or s.get("title") or s.get("label"),
            "active_url": s.get("active_url"),
            "is_duplicate_url": bool(s.get("is_duplicate_url")),
            "is_unmonetized_url": bool(s.get("is_unmonetized_url")),
        }
        for s in (aff_status.get("slots") or [])
        if s.get("is_unmonetized_url") or s.get("is_duplicate_url")
    ]

    offers = [
        {
            "category_key": c.get("category_key"),
            "short_label": c.get("short_label"),
            "audience": c.get("audience"),
            "epc_yen": c.get("epc_yen"),
            "primary_link": c.get("primary_link"),
        }
        for c in getattr(pdca_engine, "OFFER_CATEGORIES", [])
    ]

    safe_watch_counts = {
        (":".join(str(x) for x in k) if isinstance(k, tuple) else str(k)): v
        for k, v in (watch_counts.items() if isinstance(watch_counts, dict) else [])
    }

    signals = {
        "collected_at": now_jst.strftime("%Y-%m-%d %H:%M:%S JST"),
        "window_days": args.days,
        "live_stores_sampled": len(live_stores),
        "top_live_stores": [
            {
                "seo_name": store_master.seo_name(
                    store_master.to_internal_store_name(s.get("slug") or s.get("name") or s.get("store_name") or "")
                    or s.get("name")
                    or s.get("store_name")
                    or ""
                ),
                "women": s.get("women") or s.get("female") or 0,
                "men": s.get("men") or s.get("male") or 0,
            }
            for s in live_stores[:10]
        ] if live_stores else None,
        "store_stats_top20": monthly_stats[:20],
        "watch_intent_counts": safe_watch_counts,
        "crowd_vote_stores_active": len(recent_votes) if isinstance(recent_votes, dict) else 0,
        "upcoming_arena_events": arena_upcoming,
        "guide_coverage": guide_cov,
        "offer_categories_epc": offers,
        "affiliate_slot_optimization_candidates": unmonetized_slots,
        "pdca_engine_summary": {
            "hot_cluster": pdca_diag.get("hot_cluster"),
            "recommended_actions": (pdca_diag.get("weekly_actions") or pdca_diag.get("actions") or [])[:5],
            "monetization_gaps": (pdca_diag.get("monetization_gaps") or [])[:5],
        },
        "gsc_top_queries": _parse_gsc_csv(args.gsc_csv or str(reports_dir / "gsc_queries_latest.csv")) or None,
        "gsc_top_pages": _parse_gsc_pages_csv(str(reports_dir / "gsc_pages_latest.csv")) or None,
    }

    dated_path = reports_dir / f"signals_{now_jst.strftime('%Y%m%d')}.json"
    latest_path = Path(args.out) if args.out else (reports_dir / "signals_latest.json")
    encoded = json.dumps(signals, ensure_ascii=False, indent=2, default=str) + "\n"
    dated_path.write_text(encoded, encoding="utf-8")
    latest_path.write_text(encoded, encoding="utf-8")

    print(f"✅ 需要＆収益シグナルを収集しました: {latest_path}")
    print(
        f"  - 実測統計店舗: {len(monthly_stats)}店 | ライブ取得: {len(live_stores)}店 | "
        f"既存コラム: {guide_cov['total_guide_templates']}本 | 改善候補アフィリ枠: {len(unmonetized_slots)}枠"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
