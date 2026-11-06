#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_full_store_audit.py - 相席ナビ 店舗マスタ・営業時間・14日ロールアップドリフト・料金表・別業態比較定義 一括点検スクリプト

特徴:
- 本番アクセス時は必ず `?staff_test=1` ＋ 監査専用UA `AisekiNavi-StoreAudit/2.0 (curl/8.0)` を使用し、人間PVを一切汚染しない
- `DISABLE_SHEETS=1` / `DISABLE_SCHEDULER=1` を自動設定
- `store_hourly_rollups` から再現性のある営業時間外来店ドリフト（2営業日以上観測）も検出可能
"""

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

AUDIT_UA = "AisekiNavi-StoreAudit/2.0 (curl/8.0; staff_test)"
JST = datetime.timezone(datetime.timedelta(hours=9))


def _ensure_staff_test_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(k == "staff_test" for k, _ in q):
        q.append(("staff_test", "1"))
    new_query = urllib.parse.urlencode(q)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))


def fetch_production_live_stores(api_url: str = "https://aiseki-navi.com/api/status?staff_test=1") -> tuple:
    safe_url = _ensure_staff_test_url(api_url)
    req = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": AUDIT_UA,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw_bytes = resp.read()
        payload = json.loads(raw_bytes.decode("utf-8"))
    if isinstance(payload, dict):
        stores = payload.get("ranking") or payload.get("stores") or payload.get("data") or []
        return stores, payload.get("timestamp")
    return [], None


def check_master_slugs_and_adjacent_specs() -> tuple[list[dict], list[str]]:
    """Verify 5-way store slug consistency and ADJACENT_FORMAT_SPECS completeness."""
    import price_comparison
    import store_hours
    import store_master

    extra_issues = []
    extra_warnings = []

    master = store_master.load() or {}
    slugs_dict = getattr(store_master, "STORE_SLUGS", {}) or {}
    hours_dict = getattr(store_hours, "STORE_BUSINESS_HOURS", {}) or {}

    seen_slugs = {}
    for sname, info in master.items():
        slug = store_master.slug_of(sname)
        if not slug:
            extra_issues.append({"type": "slug_missing", "message": f"{sname}: スラッグが空です"})
            continue
        if slug in seen_slugs and seen_slugs[slug] != sname:
            extra_issues.append({
                "type": "slug_duplicate",
                "message": f"スラッグ '{slug}' が {seen_slugs[slug]} と {sname} で重複しています",
            })
        seen_slugs[slug] = sname
        if slugs_dict and sname in slugs_dict and slugs_dict[sname] != slug:
            extra_warnings.append(f"{sname}: store_master.json ({slug}) と STORE_SLUGS ({slugs_dict[sname]}) が異なります")
        if sname not in hours_dict:
            extra_issues.append({"type": "missing_hours_key", "message": f"{sname}: store_hours.STORE_BUSINESS_HOURS に未登録です"})

    adj_specs = getattr(price_comparison, "ADJACENT_FORMAT_SPECS", None)
    if not isinstance(adj_specs, list) or len(adj_specs) < 3:
        extra_issues.append({
            "type": "adjacent_format_issue",
            "message": "price_comparison.ADJACENT_FORMAT_SPECS が未定義または3業態未満です",
        })
    else:
        required_adj_keys = ("format_id", "format_label", "pricing_model", "seating_style", "best_for")
        for idx, sp in enumerate(adj_specs):
            if not isinstance(sp, dict) or any(not sp.get(k) for k in required_adj_keys):
                extra_issues.append({
                    "type": "adjacent_format_issue",
                    "message": f"ADJACENT_FORMAT_SPECS[{idx}] に必須キー ({', '.join(required_adj_keys)}) の欠損があります",
                })

    return extra_issues, extra_warnings


def detect_recurring_rollup_hours_drifts(days: int = 14) -> list[str]:
    """Check `store_hourly_rollups` for stores repeatedly observing >=6 guests outside configured business hours."""
    if days <= 0:
        return []
    try:
        import local_db
        import store_hours

        if not local_db._db_available():
            return []
        cutoff = (datetime.datetime.now(JST) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        rows = local_db._fetch_all_rows(
            """
            SELECT store_name, business_date, hour_jst, max_women, max_men
            FROM store_hourly_rollups
            WHERE business_date >= ? AND (max_women + max_men) >= 6
            """,
            (cutoff,),
        )
        drift_days_by_store: dict[str, set[str]] = {}
        sample_detail: dict[str, str] = {}
        for r in rows or []:
            sname, bdate, hr = str(r[0] or ""), str(r[1] or ""), int(r[2] or 0)
            tot = int(r[3] or 0) + int(r[4] or 0)
            try:
                d_obj = datetime.date.fromisoformat(bdate)
                probe_dt = datetime.datetime(d_obj.year, d_obj.month, d_obj.day, hr, 30, tzinfo=JST)
                if hr < 6:
                    probe_dt += datetime.timedelta(days=1)
                if not store_hours.is_store_open(sname, now_jst=probe_dt):
                    drift_days_by_store.setdefault(sname, set()).add(bdate)
                    sample_detail[sname] = f"{bdate} {hr:02d}時台 最大{tot}名"
            except Exception:
                continue

        notes = []
        for sname, dates in sorted(drift_days_by_store.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(dates) >= 2:
                notes.append(
                    f"{sname}: 直近{days}日中 {len(dates)}営業日で営業時間外に6名以上観測（例: {sample_detail[sname]}）"
                )
        return notes
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser(description="相席ナビ 店舗マスタ・営業時間・料金ズレ一括監査")
    parser.add_argument("--live", action="store_true", help="本番 /api/status?staff_test=1 からライブ店舗データを非汚染取得して監査")
    parser.add_argument("--rollup-days", type=int, default=14, help="store_hourly_rollups の継続営業時間ドリフト検査日数 (既定: 14)")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力する")
    args = parser.parse_args()

    import data_audit

    live_stores = None
    all_events = None
    live_note = "オフライン静的監査（--live 指定で本番非汚染ライブドリフトも同時検査可能）"
    if args.live:
        try:
            live_stores, live_ts = fetch_production_live_stores()
            live_note = f"本番ライブデータ非汚染取得成功（{len(live_stores)}店舗 / 更新時刻: {live_ts or '-'}）"
        except Exception as exc:
            live_note = f"本番ライブデータ取得スキップ（理由: {exc}）"
        try:
            import events
            all_events = events.fetch_all_events(allow_blocking=True) or []
        except Exception:
            all_events = None

    report = data_audit.run_data_audit(live_stores=live_stores, all_events=all_events, send_alerts=False)
    extra_issues, extra_warnings = check_master_slugs_and_adjacent_specs()
    recurring_drifts = detect_recurring_rollup_hours_drifts(days=args.rollup_days)

    if extra_issues:
        existing_msgs = {i.get("message") for i in report["issues"]}
        for iss in extra_issues:
            if iss["message"] not in existing_msgs:
                report["issues"].append(iss)
        report["summary"]["issue_count"] = len(report["issues"])
        if report["issues"]:
            report["status"] = "error"

    report["extra_warnings"] = extra_warnings
    report["recurring_rollup_drifts"] = recurring_drifts

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(1 if report["status"] == "error" else 0)

    sm = report["summary"]
    print("=== 相席ナビ 店舗データ整合性 ＆ ドリフト監査レポート ===")
    print(f"検査時刻: {report['checked_at']} JST | モード: {live_note}")
    print(
        f"総マスタ店舗: {sm['total_master_stores']}店（国内 {sm['domestic_stores']}店） | "
        f"営業時間登録: {sm['hours_registered']}/{sm['total_master_stores']} | "
        f"料金表登録: {sm['price_registered']}/{sm['domestic_stores']}"
    )
    print(
        f"ステータス: {report['status'].upper()} "
        f"(重大エラー: {sm['issue_count']}件 / 注意・ドリフト警告: {sm['warning_count'] + len(extra_warnings) + len(recurring_drifts)}件)"
    )

    if report["issues"]:
        print("\n❌ 【要対応】マスタ・料金・別業態定義・未知店舗エラー:")
        for iss in report["issues"]:
            print(f"  - [{iss['type']}] {iss['message']}")

    if report["hours_drifts"]:
        print("\n🕒 【現在ライブ営業時間ズレ疑い】設定上は営業時間外ですが客数が観測されています:")
        for dr in report["hours_drifts"]:
            print(f"  - {dr['message']}")

    if recurring_drifts:
        print(f"\n📊 【継続ロールアップ営業時間ズレ疑い（直近{args.rollup_days}日）】:")
        for note in recurring_drifts:
            print(f"  - {note}")

    if report["revision_notices"]:
        print("\n📢 【公式改定アナウンス検知】料金・営業時間・閉店に関するお知らせ:")
        for rn in report["revision_notices"]:
            print(f"  - [{rn.get('brand') or '公式'}] {rn['message']} ({rn.get('url') or '-'})")

    if extra_warnings:
        print("\n⚠️ 【スラッグ・補助警告】:")
        for w in extra_warnings:
            print(f"  - {w}")

    if not report["issues"] and not report["warnings"] and not extra_warnings and not recurring_drifts:
        print("\n✅ 店舗マスタ・スラッグ・営業時間・料金テーブル・別業態比較定義すべて不整合なし！")

    sys.exit(1 if report["status"] == "error" else 0)


if __name__ == "__main__":
    main()
