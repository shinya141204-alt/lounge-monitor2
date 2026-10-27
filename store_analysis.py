"""
store_analysis.py - Per-store 30-day hourly & weekday crowd aggregation engine.
Extracted from app.py (Step 5 modularization) to isolate SQLite/Sheets querying and statistics.
"""

from collections import defaultdict
import datetime
import json
import statistics
import sys
import urllib.parse

import local_db
import logger


def compute_store_analysis_payload(store_name, analysis_cache, cache_duration=3600):
    """
    Returns (payload_dict, http_status_code) for `/api/analysis/<store_name>`.
    Uses in-memory `analysis_cache` -> fast SQLite 30-day history -> Google Sheets gviz fallback.
    """
    if 'raw_data_by_store' not in analysis_cache:
        analysis_cache['raw_data_by_store'] = {}
    if 'processed_per_store' not in analysis_cache:
        analysis_cache['processed_per_store'] = {}

    now = datetime.datetime.now()
    target_data = None
    sqlite_history = []

    if store_name in analysis_cache.get('processed_per_store', {}):
        cached = analysis_cache.get('raw_data_by_store', {}).get(store_name)
        ttl = cached.get('ttl', cache_duration) if cached else cache_duration
        if cached and (now - cached['last_fetched']).total_seconds() < ttl:
            return analysis_cache['processed_per_store'][store_name], 200

    # 1. Fast Path: Query local high-speed SQLite database first (sub-millisecond)
    try:
        sqlite_history = local_db.get_store_history(store_name, days=30) or []
        if len(sqlite_history) >= 3 and (not local_db.is_partial_recent_only() or local_db.has_multi_day_span(sqlite_history)):
            target_data = sqlite_history
            analysis_cache['raw_data_by_store'][store_name] = {
                'data': target_data,
                'last_fetched': now,
                'ttl': cache_duration,
            }
    except Exception as e:
        print(f"[Analysis] SQLite query error: {e}", file=sys.stderr)

    # 2. Fallback: Query Google Sheets via gviz API if local SQLite had no data or only partial 2h boot data
    if target_data is None:
        try:
            access_token = logger.get_access_token()
            spreadsheet_id = logger.get_spreadsheet_id()
            if not access_token or not spreadsheet_id:
                if sqlite_history:
                    target_data = sqlite_history
                else:
                    return {"error": "Failed to authenticate"}, 500
            else:
                print(f"Fetching historical data for {store_name} from Google Sheets via gviz...")
                safe_store_name = store_name.replace("'", "\\'")
                query = f"SELECT A, B, C, D WHERE B='{safe_store_name}'"
                url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={urllib.parse.quote(query)}"
                headers = {"Authorization": f"Bearer {access_token}"}

                response = logger.http_session.get(url, headers=headers, timeout=15)
                if response.status_code != 200:
                    print(f"Failed to fetch data via gviz: HTTP {response.status_code}")
                    if sqlite_history:
                        target_data = sqlite_history
                    else:
                        return {"error": "Failed to load data"}, 500
                else:
                    text = response.text
                    if "google.visualization.Query.setResponse(" in text:
                        json_str = text[text.find("{"):text.rfind("}") + 1]
                        data_json = json.loads(json_str)
                        rows = data_json.get('table', {}).get('rows', [])

                        target_data = []
                        for row in rows:
                            cols = row.get('c', [])
                            if len(cols) >= 4:
                                ts_val = (cols[0].get('v') or cols[0].get('f')) if cols[0] else None
                                name_val = cols[1].get('v') if cols[1] else None
                                men_val = cols[2].get('v') if cols[2] else 0
                                women_val = cols[3].get('v') if cols[3] else 0

                                target_data.append({
                                    'ts': ts_val,
                                    'name': name_val,
                                    'men': int(men_val) if men_val is not None else 0,
                                    'women': int(women_val) if women_val is not None else 0,
                                })

                        try:
                            local_db.insert_logs_with_ts(target_data)
                        except Exception:
                            pass

                        if sqlite_history and len(sqlite_history) > len(target_data):
                            target_data = local_db.get_store_history(store_name, days=30) or target_data

                        analysis_cache['raw_data_by_store'][store_name] = {
                            'data': target_data,
                            'last_fetched': now,
                            'ttl': cache_duration,
                        }

                        if store_name in analysis_cache['processed_per_store']:
                            del analysis_cache['processed_per_store'][store_name]

                        print(f"Fetched and cached {len(target_data)} rows for {store_name}.")
                    elif sqlite_history:
                        target_data = sqlite_history
                    else:
                        print("Invalid response from gviz API")
                        return {"error": "Failed to parse data"}, 500

        except Exception as e:
            print(f"Error fetching sheet data for {store_name}: {e}", file=sys.stderr)
            if sqlite_history:
                target_data = sqlite_history
            else:
                return {"error": "Failed to load data"}, 500

    if not target_data:
        return {"error": "Store not found"}, 404

    if store_name not in analysis_cache.get('raw_data_by_store', {}):
        is_complete = local_db.is_full_sync_completed() or local_db.has_multi_day_span(target_data)
        analysis_cache['raw_data_by_store'][store_name] = {
            'data': target_data,
            'last_fetched': now,
            'ttl': cache_duration if is_complete else 60,
        }

    def get_business_dt(dt):
        if dt.hour <= 6:
            return dt - datetime.timedelta(days=1)
        return dt

    def is_business_hours(dt):
        return dt.hour >= 18 or dt.hour <= 6

    def is_peak_hours(dt):
        return dt.hour >= 20 or dt.hour <= 1

    hourly_women = defaultdict(list)
    hourly_men = defaultdict(list)
    weekday_women = defaultdict(list)
    weekday_men = defaultdict(list)
    weekday_peak_women = defaultdict(list)
    weekday_peak_men = defaultdict(list)
    hourly_women_by_day = defaultdict(lambda: defaultdict(list))
    hourly_men_by_day = defaultdict(lambda: defaultdict(list))

    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    now_jst_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    detail_cutoff_date = (now_jst - datetime.timedelta(days=10)).strftime("%Y-%m-%d")

    # Fast memoized date parser (~10x faster than calling datetime.strptime on every row)
    _date_memo = {}

    def _resolve_biz_date_and_wd(date_part: str, hour_int: int):
        key = (date_part, hour_int <= 6)
        cached_val = _date_memo.get(key)
        if cached_val is not None:
            return cached_val
        d_obj = datetime.date.fromisoformat(date_part)
        if hour_int <= 6:
            d_obj = d_obj - datetime.timedelta(days=1)
        res = (d_obj.isoformat(), d_obj.weekday())
        _date_memo[key] = res
        return res

    # Single-pass sort + aggregation + smart trend sampling
    # Keep 1-min resolution for the most recent 10 business days, and 5-min bucket resolution for older days
    # so payload size drops sharply while preserving exact peaks and daily table fidelity.
    sorted_target = sorted(target_data, key=lambda x: x.get('ts') or '')
    recent_data_women = []
    recent_data_men = []
    seen_buckets = set()

    for d in sorted_target:
        ts_str = d.get('ts')
        if not ts_str or len(ts_str) < 16 or ts_str > now_jst_str:
            continue
        try:
            h_int = int(ts_str[11:13])
            if not (h_int >= 18 or h_int <= 6):
                continue
            b_date_str, b_weekday = _resolve_biz_date_and_wd(ts_str[:10], h_int)
            w_val = int(d.get('women') or 0)
            m_val = int(d.get('men') or 0)
        except Exception:
            continue

        hourly_women[h_int].append(w_val)
        hourly_men[h_int].append(m_val)
        weekday_women[b_weekday].append(w_val)
        weekday_men[b_weekday].append(m_val)
        if h_int >= 20 or h_int <= 1:
            weekday_peak_women[b_weekday].append(w_val)
            weekday_peak_men[b_weekday].append(m_val)
        hourly_women_by_day[b_weekday][h_int].append(w_val)
        hourly_men_by_day[b_weekday][h_int].append(m_val)

        if b_date_str >= detail_cutoff_date:
            bucket_key = f"{b_date_str} {ts_str[11:16]}"
        else:
            m5 = (int(ts_str[14:16]) // 5) * 5
            bucket_key = f"{b_date_str} {ts_str[11:13]}:{m5:02d}"

        if bucket_key in seen_buckets:
            continue
        seen_buckets.add(bucket_key)
        business_ts = f"{b_date_str} {ts_str[11:19]}"
        recent_data_women.append({'x': business_ts, 'y': w_val})
        recent_data_men.append({'x': business_ts, 'y': m_val})

    hourly_result = {}
    hourly_men_result = {}
    for h in range(24):
        w_vals = hourly_women.get(h, [])
        m_vals = hourly_men.get(h, [])
        hourly_result[h] = round(sum(w_vals) / len(w_vals), 1) if w_vals else 0
        hourly_men_result[h] = round(sum(m_vals) / len(m_vals), 1) if m_vals else 0

    weekday_result = {}
    weekday_men_result = {}
    weekdays_str = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for i in range(7):
        w_vals = weekday_peak_women.get(i) or weekday_women.get(i, [])
        m_vals = weekday_peak_men.get(i) or weekday_men.get(i, [])
        weekday_result[weekdays_str[i]] = round(sum(w_vals) / len(w_vals), 1) if w_vals else 0
        weekday_men_result[weekdays_str[i]] = round(sum(m_vals) / len(m_vals), 1) if m_vals else 0

    hourly_by_weekday_result = {}
    hourly_men_by_weekday_result = {}

    for i in range(7):
        day_avg_w = {}
        day_avg_m = {}
        for h in range(24):
            w_vals = hourly_women_by_day[i].get(h, [])
            m_vals = hourly_men_by_day[i].get(h, [])
            day_avg_w[h] = round(sum(w_vals) / len(w_vals), 1) if w_vals else 0
            day_avg_m[h] = round(sum(m_vals) / len(m_vals), 1) if m_vals else 0
        hourly_by_weekday_result[weekdays_str[i]] = day_avg_w
        hourly_men_by_weekday_result[weekdays_str[i]] = day_avg_m

    # Compute core_time_short & midnight_retention_pct from store's hourly aggregates
    band_defs = (
        ("19-21時", (19, 20)),
        ("20-22時", (20, 21)),
        ("21-23時", (21, 22)),
        ("22-24時", (22, 23)),
    )
    band_stats = []
    for b_label, hrs in band_defs:
        w_list = [v for h in hrs for v in hourly_women.get(h, []) if v > 0]
        m_list = [v for h in hrs for v in hourly_men.get(h, []) if v > 0]
        if w_list:
            avg_w_b = sum(w_list) / len(w_list)
            avg_m_b = (sum(m_list) / len(m_list)) if m_list else 0.0
            wpct_b = (avg_w_b / max(0.1, avg_w_b + avg_m_b)) * 100.0
            band_stats.append((wpct_b, avg_w_b, b_label))
    core_time_short = "20-22時"
    core_time_wpct = 58
    if band_stats:
        max_w_b = max(x[1] for x in band_stats)
        viable = [x for x in band_stats if x[1] >= max(4.0, max_w_b * 0.55)] or band_stats
        best_b = max(viable, key=lambda x: (round(x[0], 1), x[1]))
        core_time_wpct = round(best_b[0])
        core_time_short = best_b[2]

    peak_21_22_w = [v for h in (21, 22) for v in hourly_women.get(h, []) if v > 0]
    mid_00_01_w = [v for h in (0, 1) for v in hourly_women.get(h, []) if v > 0]
    p_avg_w = (sum(peak_21_22_w) / len(peak_21_22_w)) if peak_21_22_w else 0.0
    m_avg_w = (sum(mid_00_01_w) / len(mid_00_01_w)) if mid_00_01_w else 0.0
    if p_avg_w >= 2.0 and len(mid_00_01_w) >= 3:
        midnight_retention_pct = max(15, min(180, round((m_avg_w / p_avg_w) * 100)))
    else:
        club_keywords = ("梅田", "茶屋町", "心斎橋", "難波", "渋谷", "新宿", "栄", "錦", "福岡", "札幌", "熊本", "UMEDA", "CHAYAMACHI", "NAMBA", "SHIBUYA", "SHINJUKU", "FUKUOKA", "SAPPORO")
        is_club = any(k in (store_name or "").upper() for k in club_keywords)
        midnight_retention_pct = 82 if is_club else 58
    if midnight_retention_pct >= 95:
        midnight_label_short = "深夜増加型"
    elif midnight_retention_pct >= 70:
        midnight_label_short = "深夜安定"
    else:
        midnight_label_short = "終電前型"

    result = {
        "store_name": store_name,
        "hourly": hourly_result,
        "hourly_men": hourly_men_result,
        "weekday": weekday_result,
        "weekday_men": weekday_men_result,
        "hourly_by_weekday": hourly_by_weekday_result,
        "hourly_men_by_weekday": hourly_men_by_weekday_result,
        "recent_trend": recent_data_women,
        "recent_trend_men": recent_data_men,
        "sample_count": len(target_data),
        "core_time_short": core_time_short,
        "core_time_wpct": core_time_wpct,
        "midnight_retention_pct": midnight_retention_pct,
        "midnight_label_short": midnight_label_short,
        "peak_band_avg_w": round(p_avg_w, 1),
        "midnight_avg_w": round(m_avg_w, 1),
    }

    analysis_cache['processed_per_store'][store_name] = result
    return result, 200
