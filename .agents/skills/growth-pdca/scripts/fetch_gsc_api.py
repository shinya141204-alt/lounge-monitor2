#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_gsc_api.py - Google Search Console API 読み取り専用・自動取得スクリプト

既存の Google サービスアカウント認証情報（logger._find_creds_path()）を利用して、
相席ナビ（aiseki-navi.com）やマチナビ（machinavi-data.com）の検索クエリ・ページ別パフォーマンスを
ワンコマンドで CSV / JSON 出力し、`collect_demand_signals.py --gsc-csv` と直結させるツールです。

初回セットアップ確認:
  python3 .agents/skills/growth-pdca/scripts/fetch_gsc_api.py --setup-info
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import sys
import urllib.parse
from pathlib import Path

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

import logger

GSC_READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
JST = datetime.timezone(datetime.timedelta(hours=9))


def _load_creds_meta_and_token(need_token: bool = True):
    creds_path = logger._find_creds_path()
    if not creds_path or not os.path.exists(creds_path):
        raise FileNotFoundError(
            "Google サービスアカウント認証ファイル (google_credentials.json) が見つかりません。"
        )
    raw = json.loads(Path(creds_path).read_text(encoding="utf-8"))
    meta = {
        "client_email": raw.get("client_email", ""),
        "project_id": raw.get("project_id", ""),
    }
    if not need_token:
        return meta, None
    creds = ServiceAccountCredentials.from_service_account_file(
        creds_path,
        scopes=[GSC_READONLY_SCOPE],
    )
    creds.refresh(GoogleAuthRequest())
    return meta, creds.token


def _list_sites(token: str) -> list[dict]:
    resp = requests.get(
        "https://www.googleapis.com/webmasters/v3/sites",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"GSC sites API error ({resp.status_code}): {resp.text[:300]}")
    return resp.json().get("siteEntry", []) or []


def _query_search_analytics(
    token: str,
    site_url: str,
    start_date: str,
    end_date: str,
    dimension: str,
    row_limit: int = 250,
) -> list[dict]:
    enc_site = urllib.parse.quote(site_url, safe="")
    endpoint = f"https://www.googleapis.com/webmasters/v3/sites/{enc_site}/searchAnalytics/query"
    payload = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": [dimension],
        "rowLimit": row_limit,
    }
    resp = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"GSC query API error ({resp.status_code}) for {site_url}: {resp.text[:300]}")
    return resp.json().get("rows", []) or []


def main() -> int:
    parser = argparse.ArgumentParser(description="Search Console API 読み取り専用・自動取得ツール")
    parser.add_argument("--setup-info", action="store_true", help="Search Console に追加すべきサービスアカウントのメールアドレスと手順を表示")
    parser.add_argument("--site", default="https://aiseki-navi.com/", help="対象プロパティURL (例: https://aiseki-navi.com/ または sc-domain:aiseki-navi.com)")
    parser.add_argument("--days", type=int, default=28, help="集計日数 (既定: 28)")
    args = parser.parse_args()

    meta, token = _load_creds_meta_and_token(need_token=not args.setup_info)
    if args.setup_info:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0

    sites = _list_sites(token)
    available_urls = [s.get("siteUrl") for s in sites]
    target_site = args.site
    if target_site not in available_urls:
        # Auto-fallback between URL-prefix and Domain property if registered under the other form
        host = urllib.parse.urlparse(target_site).netloc or target_site.replace("sc-domain:", "").strip("/")
        candidates = [f"https://{host}/", f"sc-domain:{host}"]
        matched = next((c for c in candidates if c in available_urls), None)
        if matched:
            target_site = matched
        else:
            print(
                f"⚠️ プロパティ '{args.site}' がまだ閲覧許可されていません。\n"
                f"  追加先メール: {meta['client_email']}\n"
                f"  現在閲覧可能なサイト一覧: {available_urls}",
                file=sys.stderr,
            )
            return 2

    today = datetime.datetime.now(JST).date()
    end_dt = today - datetime.timedelta(days=2)
    start_dt = end_dt - datetime.timedelta(days=max(1, args.days - 1))
    start_s, end_s = start_dt.isoformat(), end_dt.isoformat()

    queries = _query_search_analytics(token, target_site, start_s, end_s, "query")
    pages = _query_search_analytics(token, target_site, start_s, end_s, "page")

    reports_dir = REPO_ROOT / ".agents" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    q_csv = reports_dir / "gsc_queries_latest.csv"
    p_csv = reports_dir / "gsc_pages_latest.csv"

    with open(q_csv, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["上位のクエリ", "クリック数", "表示回数", "CTR", "掲載順位"])
        for r in queries:
            key = (r.get("keys") or [""])[0]
            w.writerow([
                key,
                int(round(r.get("clicks", 0))),
                int(round(r.get("impressions", 0))),
                f"{r.get('ctr', 0.0) * 100:.2f}%",
                f"{r.get('position', 0.0):.2f}",
            ])

    with open(p_csv, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["上位のページ", "クリック数", "表示回数", "CTR", "掲載順位"])
        for r in pages:
            key = (r.get("keys") or [""])[0]
            w.writerow([
                key,
                int(round(r.get("clicks", 0))),
                int(round(r.get("impressions", 0))),
                f"{r.get('ctr', 0.0) * 100:.2f}%",
                f"{r.get('position', 0.0):.2f}",
            ])

    print(f"✅ Search Console API 取得完了 ({target_site}: {start_s} 〜 {end_s})")
    print(f"  - クエリ数: {len(queries)}件 -> {q_csv}")
    print(f"  - ページ数: {len(pages)}件 -> {p_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
