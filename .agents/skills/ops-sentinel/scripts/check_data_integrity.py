#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_data_integrity.py - 相席ナビ データモートDB整合性・ロールアップ連続性・14日匿名化監査ツール
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(str(REPO_ROOT))

import local_db

JST = datetime.timezone(datetime.timedelta(hours=9))


def run_integrity_checks(days: int = 90) -> dict:
    now_jst = datetime.datetime.now(JST).replace(tzinfo=None)
    checks = []
    overall = "ok"

    def _record(name: str, status: str, detail: str, metrics: dict | None = None):
        nonlocal overall
        if status == "error":
            overall = "error"
        elif status == "warn" and overall == "ok":
            overall = "warn"
        checks.append({
            "check": name,
            "status": status,
            "detail": detail,
            "metrics": metrics or {},
        })

    if not local_db._db_available():
        _record("db_connection", "warn", "ローカルDBファイルが未初期化または接続対象なしです。")
        return {"checked_at": now_jst.strftime("%Y-%m-%d %H:%M:%S JST"), "overall": overall, "checks": checks}

    local_db._ensure_moat_rollup_schema()
    cov = local_db.get_moat_coverage_summary() or {}
    _record(
        "moat_coverage",
        "ok",
        f"生ログ {cov.get('raw_snapshots', 0):,}件 / 1hロールアップ {cov.get('rolled_up_hours', 0):,}行 / 投票 {cov.get('crowd_votes_total', 0):,}件",
        cov,
    )

    # 1. 90日超の生ログ残存チェック（prune_old_logs 稼働確認）
    cutoff_95d = (now_jst - datetime.timedelta(days=max(days, 90) + 5)).strftime("%Y-%m-%d 00:00:00")
    try:
        rows = local_db._fetch_all_rows("SELECT COUNT(*) FROM monitor_logs WHERE timestamp < ?", (cutoff_95d,))
        stale_raw = int(rows[0][0] or 0) if rows and rows[0] else 0
        if stale_raw > 0:
            _record(
                "raw_retention_window",
                "warn",
                f"{days + 5}日以前の古い生ログが {stale_raw:,} 件残存しています（次回 prune_old_logs で7日バッチ単位でロールアップ＆整理予定）。",
                {"stale_raw_rows": stale_raw, "cutoff": cutoff_95d},
            )
        else:
            _record("raw_retention_window", "ok", f"{days}日保持ウィンドウ超過の未整理ログなし", {"stale_raw_rows": 0})
    except Exception as exc:
        _record("raw_retention_window", "warn", f"生ログ保持チェック例外: {exc}")

    # 2. crowd_votes & store_watch_intents の14日超 IPハッシュ未匿名化チェック（プライバシー厳守）
    vote_cutoff = (now_jst - datetime.timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        r_v = local_db._fetch_all_rows(
            "SELECT COUNT(*) FROM crowd_votes WHERE timestamp < ? AND ip_hash IS NOT NULL AND ip_hash != ''",
            (vote_cutoff,),
        )
        r_w = local_db._fetch_all_rows(
            "SELECT COUNT(*) FROM store_watch_intents WHERE timestamp < ? AND ip_hash IS NOT NULL AND ip_hash != ''",
            (vote_cutoff,),
        )
        un_v = int(r_v[0][0] or 0) if r_v and r_v[0] else 0
        un_w = int(r_w[0][0] or 0) if r_w and r_w[0] else 0
        if un_v > 0 or un_w > 0:
            _record(
                "privacy_14d_anonymization",
                "warn",
                f"15日経過レコードに未匿名化 ip_hash があります（votes={un_v}, watch={un_w}）。prune_old_logs() の実行を推奨します。",
                {"unanonymized_votes": un_v, "unanonymized_watch_intents": un_w},
            )
        else:
            _record("privacy_14d_anonymization", "ok", "14日超レコードの未匿名化 ip_hash ゼロ（プライバシー規約準拠）")
    except Exception as exc:
        _record("privacy_14d_anonymization", "warn", f"匿名化チェック例外: {exc}")

    # 3. store_hourly_rollups の日付連続性チェック
    try:
        r_dates = local_db._fetch_all_rows(
            "SELECT DISTINCT business_date FROM store_hourly_rollups ORDER BY business_date ASC"
        )
        b_dates = [str(r[0]) for r in (r_dates or []) if r and r[0]]
        missing_dates = []
        if len(b_dates) >= 2:
            d_start = datetime.date.fromisoformat(b_dates[-min(len(b_dates), 30)])
            d_end = datetime.date.fromisoformat(b_dates[-1])
            existing = set(b_dates)
            cur = d_start
            while cur <= d_end:
                s_d = cur.isoformat()
                if s_d not in existing:
                    missing_dates.append(s_d)
                cur += datetime.timedelta(days=1)
        if missing_dates:
            _record(
                "rollup_continuity",
                "warn",
                f"直近ロールアップ期間に {len(missing_dates)} 日の未集計日があります: {', '.join(missing_dates[:5])}",
                {"total_rollup_days": len(b_dates), "missing_recent_dates": missing_dates},
            )
        else:
            _record(
                "rollup_continuity",
                "ok",
                f"ロールアップ集計日数 {len(b_dates)} 日（直近区間の欠損なし）",
                {"total_rollup_days": len(b_dates)},
            )
    except Exception as exc:
        _record("rollup_continuity", "warn", f"ロールアップ連続性チェック例外: {exc}")

    # 4. 60分後予測精度サマリ確認
    try:
        acc = local_db.get_forecast_accuracy_report(days=days, allow_blocking=False) or {}
        sm = acc.get("summary") or {}
        resolved = int(sm.get("resolved_count") or 0)
        _record(
            "forecast_verification",
            "ok",
            f"60分後予測 照合完了サンプル: {resolved:,}件",
            {"resolved_count": resolved, "mae_wpct": sm.get("mae_wpct")},
        )
    except Exception as exc:
        _record("forecast_verification", "warn", f"予測精度レポート取得例外: {exc}")

    return {
        "checked_at": now_jst.strftime("%Y-%m-%d %H:%M:%S JST"),
        "overall": overall,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="相席ナビ データモートDB整合性・匿名化監査")
    parser.add_argument("--days", type=int, default=90, help="検証対象日数 (既定: 90)")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    args = parser.parse_args()

    res = run_integrity_checks(days=args.days)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"=== 相席ナビ データモートDB整合性レポート ({res['checked_at']}) ===")
        for c in res["checks"]:
            icon = "✅" if c["status"] == "ok" else ("⚠️" if c["status"] == "warn" else "❌")
            print(f"  {icon} [{c['check']}] {c['detail']}")
        print(f"\n総合判定: {res['overall'].upper()}")

    return 1 if res["overall"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
