#!/usr/bin/env python3
"""
archive_data.py - Automated Google Sheets Capacity Guard, Daily Compression & Multi-Sheet Rotation.

Prevents the 'Lounge Monitor Data' workbook from ever hitting Google Sheets' hard 10,000,000-cell limit:
  1. `Sheet1` (5-col raw 10-min headcount logs):
     - Compresses rows older than `RETENTION_DAYS` (default 35 days) into 1-row-per-store-per-day summaries
       in `Daily Store Summary` (~99.3% cell reduction: 7,600 rows/day -> 53 rows/day!), then deletes the old rows
       from `Sheet1` in safe bounded batches.
  2. `Analytics Daily` (4-col event counters):
     - Compacts multiple 10-min flush rows for completed past days into a single `(date, event, label)` row
       and trims rows older than `ANALYTICS_RETENTION_DAYS` (default 120 days).
  3. `Crowd Votes` (4-col user crowd votes):
     - Trims vote logs older than `VOTES_RETENTION_DAYS` (default 14 days; app only queries last 24h).
  4. `Job State` (2-col append-only state/KV log):
     - Rewrites/compacts `Job State` when row count exceeds 600 so obsolete historical KV rows never accumulate.

Also preserves CLI compatibility: `python archive_data.py [--dry-run]`.
"""

from collections import defaultdict
import datetime
import json
import logging
import sys
import threading
import time

import logger

RETENTION_DAYS = 35
ANALYTICS_RETENTION_DAYS = 120
VOTES_RETENTION_DAYS = 14
JOB_STATE_COMPACT_THRESHOLD = 600
MAX_DELETE_ROWS_PER_RUN = 25000
GOOGLE_SHEETS_CELL_LIMIT = 10_000_000

SPREADSHEET_NAME = "Lounge Monitor Data"
MAIN_SHEET_NAME = "Sheet1"
SUMMARY_SHEET_NAME = "Daily Store Summary"
SUMMARY_HEADER = ["date", "store_name", "peak_women", "peak_men", "avg_women", "avg_men", "samples"]

RETENTION_STATE_KEY = "sheets_retention_report"

JST = datetime.timezone(datetime.timedelta(hours=9))

_retention_lock = threading.Lock()
_mem_report_cache = {"ts": 0.0, "data": None}


def get_cutoff_date(retention_days: int = RETENTION_DAYS) -> datetime.datetime:
    """Get naive JST cutoff datetime — raw rows strictly older than this will be compressed & rotated."""
    return datetime.datetime.now(JST).replace(tzinfo=None) - datetime.timedelta(days=retention_days)


def _business_date_str(dt: datetime.datetime) -> str:
    if dt.hour <= 6:
        return (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def _measure_workbook_cells(spreadsheet) -> dict:
    """Inspect all worksheets in `spreadsheet` to calculate grid cells vs Google's 10M cell ceiling."""
    sheets_meta = []
    total_grid_cells = 0
    for ws in spreadsheet.worksheets():
        r_cnt = int(getattr(ws, "row_count", 0) or 0)
        c_cnt = int(getattr(ws, "col_count", 0) or 0)
        cells = r_cnt * c_cnt
        total_grid_cells += cells
        sheets_meta.append({
            "title": ws.title,
            "rows": r_cnt,
            "cols": c_cnt,
            "grid_cells": cells,
        })
    usage_pct = round((total_grid_cells / GOOGLE_SHEETS_CELL_LIMIT) * 100.0, 2)
    if usage_pct >= 75.0:
        health_level = "critical"
        health_label = "🔴 要整理（75%超）"
    elif usage_pct >= 45.0:
        health_level = "warn"
        health_label = "🟡 注意圏（45%超）"
    else:
        health_level = "ok"
        health_label = "🟢 安全圏（自動ローテーション稼働中）"
    return {
        "total_grid_cells": total_grid_cells,
        "cell_limit": GOOGLE_SHEETS_CELL_LIMIT,
        "usage_pct": usage_pct,
        "health_level": health_level,
        "health_label": health_label,
        "worksheets": sheets_meta,
    }


def _get_or_create_summary_sheet(spreadsheet, dry_run: bool = False):
    try:
        return spreadsheet.worksheet(SUMMARY_SHEET_NAME)
    except Exception:
        if dry_run:
            return None
        ws = spreadsheet.add_worksheet(title=SUMMARY_SHEET_NAME, rows=2000, cols=len(SUMMARY_HEADER))
        ws.append_row(SUMMARY_HEADER, value_input_option="RAW")
        return ws


def _compress_rows_to_daily_summaries(rows_to_archive: list) -> list:
    """Aggregate raw 10-min rows `[ts, store_name, men, women, source]` into `(biz_date, store_name)` rows."""
    buckets = defaultdict(lambda: [0, 0, 0, 0, 0])  # [max_w, max_m, sum_w, sum_m, cnt]
    for row in rows_to_archive:
        if len(row) < 4:
            continue
        try:
            dt = datetime.datetime.strptime(str(row[0]).strip(), "%Y-%m-%d %H:%M:%S")
            sname = str(row[1]).strip()
            if not sname:
                continue
            m = int(float(row[2] or 0))
            w = int(float(row[3] or 0))
        except Exception:
            continue
        bdate = _business_date_str(dt)
        b = buckets[(bdate, sname)]
        if w > b[0]:
            b[0] = w
        if m > b[1]:
            b[1] = m
        b[2] += w
        b[3] += m
        b[4] += 1

    summary_rows = []
    for (bdate, sname) in sorted(buckets.keys()):
        max_w, max_m, sum_w, sum_m, cnt = buckets[(bdate, sname)]
        if cnt <= 0:
            continue
        summary_rows.append([
            bdate,
            sname,
            max_w,
            max_m,
            round(sum_w / cnt, 1),
            round(sum_m / cnt, 1),
            cnt,
        ])
    return summary_rows


def _rotate_main_sheet(spreadsheet, retention_days: int, max_delete_rows: int, dry_run: bool) -> dict:
    main_sheet = spreadsheet.sheet1
    cutoff = get_cutoff_date(retention_days)

    # Memory-safe path for production gspread Worksheet: inspect column A timestamps first
    # instead of loading 250k+ 5-column rows into RAM at once (prevents Render 512MB OOM).
    if hasattr(main_sheet, "col_values") and hasattr(main_sheet, "get"):
        col_a = main_sheet.col_values(1) or []
        total_data_rows = max(0, len(col_a) - 1)
        if total_data_rows == 0:
            return {"data_rows_before": 0, "archived_raw_rows": 0, "summary_rows_added": 0, "deleted_rows": 0}

        old_count = 0
        for raw_ts in col_a[1:]:
            ts_str = str(raw_ts or "").strip()
            if not ts_str:
                old_count += 1
            else:
                try:
                    ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    break
                if ts < cutoff:
                    old_count += 1
                else:
                    break
            if old_count >= max_delete_rows:
                break

        if old_count == 0:
            return {
                "data_rows_before": total_data_rows,
                "archived_raw_rows": 0,
                "summary_rows_added": 0,
                "deleted_rows": 0,
            }

        contiguous_old = []
        chunk_rows = 8000
        for offset in range(0, old_count, chunk_rows):
            r_start = 2 + offset
            r_end = 1 + min(old_count, offset + chunk_rows)
            fetched = main_sheet.get(f"A{r_start}:E{r_end}") or []
            expected_len = r_end - r_start + 1
            if len(fetched) < expected_len:
                fetched = list(fetched) + ([[]] * (expected_len - len(fetched)))
            contiguous_old.extend(fetched)
    else:
        all_values = main_sheet.get_all_values()
        total_data_rows = max(0, len(all_values) - 1)
        if total_data_rows == 0:
            return {"data_rows_before": 0, "archived_raw_rows": 0, "summary_rows_added": 0, "deleted_rows": 0}

        data_rows = all_values[1:]
        # Identify contiguous leading prefix of rows older than cutoff so `delete_rows(2, end_row)`
        # NEVER accidentally deletes a newer row even if timestamps were ever out-of-order.
        contiguous_old = []
        for row in data_rows:
            if not row or not str(row[0]).strip():
                contiguous_old.append(row)
                continue
            try:
                ts = datetime.datetime.strptime(str(row[0]).strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                break
            if ts < cutoff:
                contiguous_old.append(row)
            else:
                break
            if len(contiguous_old) >= max_delete_rows:
                break

        if not contiguous_old:
            return {
                "data_rows_before": total_data_rows,
                "archived_raw_rows": 0,
                "summary_rows_added": 0,
                "deleted_rows": 0,
            }

    summary_candidates = _compress_rows_to_daily_summaries(contiguous_old)
    summary_added = 0

    if not dry_run:
        summary_ws = _get_or_create_summary_sheet(spreadsheet, dry_run=False)
        existing_vals = summary_ws.get_all_values()
        existing_keys = {
            (str(r[0]).strip(), str(r[1]).strip())
            for r in (existing_vals[1:] if len(existing_vals) > 1 else [])
            if len(r) >= 2
        }
        new_summary_rows = [
            sr for sr in summary_candidates
            if (str(sr[0]), str(sr[1])) not in existing_keys
        ]
        for idx in range(0, len(new_summary_rows), 500):
            summary_ws.append_rows(new_summary_rows[idx:idx + 500], value_input_option="RAW")
        summary_added = len(new_summary_rows)

        # Safe 1-indexed deletion: row 1 is header, so delete rows 2 .. (1 + len(contiguous_old))
        main_sheet.delete_rows(2, 1 + len(contiguous_old))
    else:
        summary_added = len(summary_candidates)

    return {
        "data_rows_before": total_data_rows,
        "archived_raw_rows": len(contiguous_old),
        "summary_rows_added": summary_added,
        "deleted_rows": len(contiguous_old),
    }


def _compact_analytics_sheet(spreadsheet, analytics_days: int, dry_run: bool) -> dict:
    import analytics
    try:
        ws = spreadsheet.worksheet(analytics.SHEET_TITLE)
    except Exception:
        return {"rows_before": 0, "rows_after": 0, "deleted_rows": 0}

    vals = ws.get_all_values()
    if len(vals) <= 1:
        return {"rows_before": 0, "rows_after": 0, "deleted_rows": 0}

    data_rows = vals[1:]
    rows_before = len(data_rows)
    now_jst = datetime.datetime.now(JST)
    today_str = now_jst.strftime("%Y-%m-%d")
    cutoff_str = (now_jst - datetime.timedelta(days=analytics_days)).strftime("%Y-%m-%d")

    compacted_past = defaultdict(int)
    today_rows = []
    expired_count = 0

    for r in data_rows:
        if len(r) < 4:
            continue
        d_str = str(r[0]).strip()
        if not d_str:
            continue
        if d_str < cutoff_str:
            expired_count += 1
            continue
        if d_str >= today_str:
            today_rows.append([r[0], r[1], r[2], r[3]])
            continue
        try:
            compacted_past[(d_str, str(r[1]), str(r[2]))] += int(r[3])
        except ValueError:
            continue

    merged_rows = [
        [d, e, l, str(c)]
        for (d, e, l), c in sorted(compacted_past.items())
    ] + today_rows
    rows_after = len(merged_rows)
    saved_rows = max(0, rows_before - rows_after)

    # Rewrite only when meaningful reduction is achieved (avoids unnecessary API churn)
    should_rewrite = (expired_count > 0) or (saved_rows >= 80 and saved_rows >= rows_before * 0.15)
    if should_rewrite and not dry_run:
        payload = [analytics.HEADER] + merged_rows
        ws.clear()
        for idx in range(0, len(payload), 2000):
            ws.append_rows(payload[idx:idx + 2000], value_input_option="RAW")
        with analytics._lock:
            analytics._sheet_rows_cache["ts"] = time.monotonic()
            analytics._sheet_rows_cache["rows"] = merged_rows
        return {"rows_before": rows_before, "rows_after": rows_after, "deleted_rows": saved_rows}

    return {
        "rows_before": rows_before,
        "rows_after": rows_after if should_rewrite else rows_before,
        "deleted_rows": saved_rows if should_rewrite else 0,
    }


def _trim_votes_sheet(spreadsheet, votes_days: int, dry_run: bool) -> dict:
    import local_db
    try:
        ws = spreadsheet.worksheet(local_db.VOTE_SHEET_TITLE)
    except Exception:
        return {"rows_before": 0, "deleted_rows": 0}

    vals = ws.get_all_values()
    if len(vals) <= 1:
        return {"rows_before": 0, "deleted_rows": 0}

    data_rows = vals[1:]
    cutoff_str = (datetime.datetime.now(JST).replace(tzinfo=None) - datetime.timedelta(days=votes_days)).strftime("%Y-%m-%d %H:%M:%S")
    del_cnt = 0
    for r in data_rows:
        ts_s = str(r[0]).strip() if r else ""
        if not ts_s or ts_s < cutoff_str:
            del_cnt += 1
        else:
            break

    if del_cnt > 0 and not dry_run:
        ws.delete_rows(2, 1 + del_cnt)

    return {"rows_before": len(data_rows), "deleted_rows": del_cnt}


def _compact_job_state_sheet(spreadsheet, dry_run: bool) -> dict:
    import job_state
    try:
        ws = spreadsheet.worksheet(job_state.SHEET_TITLE)
    except Exception:
        return {"rows_before": 0, "rows_after": 0, "deleted_rows": 0}

    vals = ws.get_all_values()
    rows_before = max(0, len(vals) - 1)
    if rows_before < JOB_STATE_COMPACT_THRESHOLD:
        return {"rows_before": rows_before, "rows_after": rows_before, "deleted_rows": 0}

    job_state._ensure_loaded(allow_blocking=True)
    now_s = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    with job_state._lock:
        keys_list = list((job_state._keys or {}).keys())
        kv_snap = dict(job_state._kv or {})
        kv_ts_snap = dict(job_state._kv_ts or {})

    compact_rows = [[k, now_s] for k in keys_list]
    for prefix, val_str in sorted(kv_snap.items()):
        ts_tag = kv_ts_snap.get(prefix) or datetime.datetime.now(JST).strftime("%Y%m%d%H%M%S")
        sheet_val = job_state._obfuscate_secret(val_str) if str(prefix).startswith("token:") else str(val_str)
        compact_rows.append([f"{prefix}={ts_tag}|{sheet_val}", now_s])

    rows_after = len(compact_rows)
    saved = max(0, rows_before - rows_after)
    if saved >= 100 and not dry_run:
        payload = [job_state.HEADER] + compact_rows
        ws.clear()
        ws.append_rows(payload, value_input_option="RAW")
        return {"rows_before": rows_before, "rows_after": rows_after, "deleted_rows": saved}

    return {"rows_before": rows_before, "rows_after": rows_after if dry_run else rows_before, "deleted_rows": saved if dry_run else 0}


def run_full_sheets_retention(
    retention_days: int = RETENTION_DAYS,
    analytics_days: int = ANALYTICS_RETENTION_DAYS,
    votes_days: int = VOTES_RETENTION_DAYS,
    max_delete_rows: int = MAX_DELETE_ROWS_PER_RUN,
    dry_run: bool = False,
) -> dict:
    """Execute multi-worksheet compression, old-row rotation, and 10M-cell capacity audit."""
    import job_state
    import sheets_store

    if sheets_store.is_disabled():
        return {
            "ok": False,
            "disabled": True,
            "error": "Google Sheets 連携が無効化されています（DISABLE_SHEETS=1）。",
        }

    if not _retention_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "現在バックグラウンドでシート整理を実行中です。"}

    try:
        client = logger.get_client()
        if not client:
            return {"ok": False, "error": "Google Sheets 認証クライアントを取得できませんでした。"}

        spreadsheet = logger.open_spreadsheet(client) if hasattr(logger, "open_spreadsheet") else client.open(SPREADSHEET_NAME)
        before_cap = _measure_workbook_cells(spreadsheet)

        main_res = _rotate_main_sheet(spreadsheet, retention_days=retention_days, max_delete_rows=max_delete_rows, dry_run=dry_run)
        ana_res = _compact_analytics_sheet(spreadsheet, analytics_days=analytics_days, dry_run=dry_run)
        vote_res = _trim_votes_sheet(spreadsheet, votes_days=votes_days, dry_run=dry_run)
        js_res = _compact_job_state_sheet(spreadsheet, dry_run=dry_run)

        after_cap = before_cap if dry_run else _measure_workbook_cells(spreadsheet)
        total_deleted_rows = (
            int(main_res.get("deleted_rows", 0))
            + int(ana_res.get("deleted_rows", 0))
            + int(vote_res.get("deleted_rows", 0))
            + int(js_res.get("deleted_rows", 0))
        )

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        report = {
            "ok": True,
            "dry_run": bool(dry_run),
            "executed_at": now_str,
            "retention_days": retention_days,
            "analytics_days": analytics_days,
            "votes_days": votes_days,
            "total_deleted_rows": total_deleted_rows,
            "capacity_before": before_cap,
            "capacity": after_cap,
            "details": {
                "main_logs": main_res,
                "analytics_daily": ana_res,
                "crowd_votes": vote_res,
                "job_state": js_res,
            },
        }

        if not dry_run:
            _mem_report_cache["ts"] = time.monotonic()
            _mem_report_cache["data"] = report
            try:
                job_state.set_value(RETENTION_STATE_KEY, json.dumps(report, ensure_ascii=False))
            except Exception as err:
                logging.warning(f"[archive_data] Failed saving retention report to job_state: {err}")

        return report
    except Exception as e:
        logging.error(f"[archive_data] Retention job failed: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        _retention_lock.release()


def get_retention_status(allow_blocking: bool = False) -> dict:
    """Return latest Google Sheets capacity & auto-rotation report in 0ms for `/staff/analytics`."""
    import job_state

    if _mem_report_cache["data"] is not None:
        return dict(_mem_report_cache["data"])

    raw = job_state.get_value(RETENTION_STATE_KEY, allow_blocking=allow_blocking, default="") or ""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                _mem_report_cache["ts"] = time.monotonic()
                _mem_report_cache["data"] = parsed
                return dict(parsed)
        except Exception:
            pass

    return {
        "ok": True,
        "never_run_yet": True,
        "executed_at": "未実行（毎日 06:20 JST に自動実行）",
        "retention_days": RETENTION_DAYS,
        "analytics_days": ANALYTICS_RETENTION_DAYS,
        "votes_days": VOTES_RETENTION_DAYS,
        "total_deleted_rows": 0,
        "capacity": {
            "total_grid_cells": 0,
            "cell_limit": GOOGLE_SHEETS_CELL_LIMIT,
            "usage_pct": 0.0,
            "health_level": "ok",
            "health_label": "🟢 自動ローテーション待機中（毎日 06:20 JST 稼働）",
            "worksheets": [],
        },
        "details": {},
    }


def run_scheduled_retention_job():
    """Daily APScheduler entrypoint (06:20 JST)."""
    res = run_full_sheets_retention(dry_run=False)
    if res.get("ok"):
        cap = res.get("capacity") or {}
        logging.info(
            f"[archive_data] Daily Sheets rotation complete: deleted={res.get('total_deleted_rows')} rows, "
            f"cells={cap.get('total_grid_cells')}/{cap.get('cell_limit')} ({cap.get('usage_pct')}%)"
        )
    return res


def archive_old_data(dry_run: bool = False) -> bool:
    """Backward-compatible CLI entrypoint."""
    res = run_full_sheets_retention(dry_run=dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return bool(res.get("ok"))


def main():
    dry_run = "--dry-run" in sys.argv
    if not archive_old_data(dry_run=dry_run):
        sys.exit(1)


if __name__ == "__main__":
    main()
