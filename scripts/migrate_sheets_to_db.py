#!/usr/bin/env python3
"""One-command zero-loss data migration from Google Sheets into Tokyo PostgreSQL + Redis (Plan Take).

Migrates:
  1. `monitor_logs` (last N days of 53-store headcount logs via `local_db.sync_from_google_sheets`)
  2. `crowd_votes` (recent live crowd votes via `local_db.restore_votes_from_sheets`)
  3. `Job State` (all idempotency keys + encrypted Gemini / Claude / Threads 60-day / IG tokens + PDCA directives)
  4. `Analytics Daily` (full historical PV / SNS inflow / affiliate CV counters)

Usage:
  python3 scripts/migrate_sheets_to_db.py --days 30
  python3 scripts/migrate_sheets_to_db.py --days 30 --dry-run
"""

import argparse
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import analytics
import job_state
import local_db
import sheets_store
import turbo_store


def run_migration(days: int = 30, vote_hours: int = 48, dry_run: bool = False) -> dict:
    started = time.monotonic()
    backend = turbo_store.get_backend_status()
    print("=== 相席ナビ プラン竹 データ移行ツール (Google Sheets -> Tokyo PostgreSQL / Redis) ===")
    print(f"Backend status: {backend}")

    if sheets_store.is_disabled():
        raise RuntimeError("DISABLE_SHEETS=1 が設定されているため Google Sheets を読み取れません。")

    summary = {
        "backend": backend,
        "dry_run": dry_run,
        "monitor_days": days,
        "job_state_keys": 0,
        "analytics_rows": 0,
    }

    if dry_run:
        js_rows = sheets_store.read_rows_or_none(job_state.SHEET_TITLE, job_state.HEADER) or []
        an_rows = sheets_store.read_rows_or_none(analytics.SHEET_TITLE, analytics.HEADER) or []
        summary["job_state_sheet_rows"] = len(js_rows)
        summary["analytics_sheet_rows"] = len(an_rows)
        print(f"[DRY-RUN] Job State rows found: {len(js_rows)}")
        print(f"[DRY-RUN] Analytics Daily rows found: {len(an_rows)}")
        return summary

    # 1. Initialize schemas
    print("[1/4] PostgreSQL / ローカルDB スキーマを初期化中...")
    local_db.init_db()
    turbo_store.init_postgres_aux_schema()

    # 2. Sync monitor_logs & crowd_votes
    print(f"[2/4] 過去 {days} 日分の53店舗人数ログ＆直近 {vote_hours} 時間の現場投票を同期中...")
    local_db.sync_from_google_sheets(days=days)
    local_db.restore_votes_from_sheets(hours=vote_hours)
    stats = local_db.get_db_stats()
    summary["db_stats"] = stats
    print(f"      -> DB統計: {stats}")

    # 3. Migrate Job State (including encrypted Threads/IG 60-day tokens & Gemini/Claude keys)
    print("[3/4] Job State（暗号化SNSトークン・AIキー・重複防止フラグ・PDCA指令）を同期中...")
    job_state.reset_cache_for_tests()
    all_k = job_state.all_keys()
    job_state._seed_turbo_from_memory()
    summary["job_state_keys"] = len(all_k)
    print(f"      -> 完了キー＆KV 移行数: {len(all_k)} 件")

    # 4. Migrate Analytics Daily
    print("[4/4] Analytics Daily（PV・SNS流入・アフィリエイトCV履歴）を同期中...")
    an_rows = sheets_store.read_rows_or_none(analytics.SHEET_TITLE, analytics.HEADER) or []
    summary["analytics_rows"] = len(an_rows)
    if an_rows:
        ok_an = turbo_store.analytics_replace_from_sheets(an_rows)
        print(f"      -> Analytics Daily {len(an_rows)} 行を高速ストアへ反映: {'OK' if ok_an else 'Local fallback'}")

    elapsed = round(time.monotonic() - started, 2)
    summary["elapsed_sec"] = elapsed
    print(f"=== 移行完了 ({elapsed}s) ===")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Migrate Google Sheets history & state into Tokyo PostgreSQL + Redis.")
    parser.add_argument("--days", type=int, default=30, help="Number of days of monitor_logs to import (default: 30)")
    parser.add_argument("--vote-hours", type=int, default=48, help="Hours of crowd votes to import (default: 48)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect Google Sheets counts without writing to DB")
    args = parser.parse_args()
    run_migration(days=args.days, vote_hours=args.vote_hours, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
