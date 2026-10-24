#!/usr/bin/env python3
"""
fetch_theme_stats.py - Extract real statistical insights from lounge_data.db for `/article <theme>`
"""
import os
import sys
import json

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if not os.path.exists(os.path.join(ROOT_DIR, "local_db.py")):
    ROOT_DIR = "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"

sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)

import local_db
import weekly_report
import store_master


def main():
    theme = " ".join(sys.argv[1:]).strip()
    report = weekly_report.generate_weekly_report() or {}
    stores = report.get("stores") or []
    anaba = report.get("anaba_stores") or []
    weekday_summary = report.get("weekday_summary") or []
    timeslot_summary = report.get("timeslot_summary") or []

    matched_stores = []
    if theme:
        for s in stores:
            name = s.get("name", "")
            seo = store_master.seo_name(name)
            tokens = [t for t in seo.replace("（", " ").replace("）", " ").split() if len(t) >= 2]
            if any(k in theme for k in [name, seo] + tokens) or any(t in seo for t in theme.split()):
                matched_stores.append({
                    "name": name,
                    "seo_name": seo,
                    "slug": store_master.slug_of(name),
                    "avg_women": s.get("avg_women"),
                    "max_women": s.get("max_women"),
                    "avg_men": s.get("avg_men"),
                })

    summary = {
        "theme": theme,
        "period": report.get("period"),
        "total_records_analyzed": report.get("total_records_analyzed"),
        "top5_stores": [
            {
                "name": s.get("name"),
                "seo_name": store_master.seo_name(s.get("name", "")),
                "slug": store_master.slug_of(s.get("name", "")),
                "avg_women": s.get("avg_women"),
                "max_women": s.get("max_women"),
            }
            for s in stores[:5]
        ],
        "top5_anaba": [
            {
                "name": a.get("name"),
                "seo_name": store_master.seo_name(a.get("name", "")),
                "slug": store_master.slug_of(a.get("name", "")),
                "avg_wpct": a.get("avg_wpct"),
                "avg_women": a.get("avg_women"),
            }
            for a in anaba[:5]
        ],
        "matched_stores": matched_stores[:8],
        "weekday_summary": weekday_summary,
        "timeslot_summary": timeslot_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
