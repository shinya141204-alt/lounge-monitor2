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

    for d in target_data:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            if dt > now_jst or not is_business_hours(dt):
                continue
            biz_dt = get_business_dt(dt)
            b_weekday = biz_dt.weekday()

            hourly_women[dt.hour].append(d['women'])
            hourly_men[dt.hour].append(d['men'])
            weekday_women[b_weekday].append(d['women'])
            weekday_men[b_weekday].append(d['men'])
            if is_peak_hours(dt):
                weekday_peak_women[b_weekday].append(d['women'])
                weekday_peak_men[b_weekday].append(d['men'])
            hourly_women_by_day[b_weekday][dt.hour].append(d['women'])
            hourly_men_by_day[b_weekday][dt.hour].append(d['men'])
        except Exception:
            continue

    hourly_result = {}
    hourly_men_result = {}
    for h in range(24):
        w_vals = hourly_women.get(h, [])
        m_vals = hourly_men.get(h, [])
        hourly_result[h] = round(statistics.mean(w_vals), 1) if w_vals else 0
        hourly_men_result[h] = round(statistics.mean(m_vals), 1) if m_vals else 0

    weekday_result = {}
    weekday_men_result = {}
    weekdays_str = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for i in range(7):
        w_vals = weekday_peak_women.get(i) or weekday_women.get(i, [])
        m_vals = weekday_peak_men.get(i) or weekday_men.get(i, [])
        weekday_result[weekdays_str[i]] = round(statistics.mean(w_vals), 1) if w_vals else 0
        weekday_men_result[weekdays_str[i]] = round(statistics.mean(m_vals), 1) if m_vals else 0

    hourly_by_weekday_result = {}
    hourly_men_by_weekday_result = {}

    for i in range(7):
        day_avg_w = {}
        day_avg_m = {}
        for h in range(24):
            w_vals = hourly_women_by_day[i].get(h, [])
            m_vals = hourly_men_by_day[i].get(h, [])
            day_avg_w[h] = round(statistics.mean(w_vals), 1) if w_vals else 0
            day_avg_m[h] = round(statistics.mean(m_vals), 1) if m_vals else 0
        hourly_by_weekday_result[weekdays_str[i]] = day_avg_w
        hourly_men_by_weekday_result[weekdays_str[i]] = day_avg_m

    recent_data_women = []
    recent_data_men = []
    sorted_target = sorted(target_data, key=lambda x: x['ts'])
    seen_minutes = set()

    for d in sorted_target:
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            if dt > now_jst or not is_business_hours(dt):
                continue
            business_date = get_business_dt(dt).strftime("%Y-%m-%d")
            min_key = f"{business_date} {dt.strftime('%H:%M')}"
            if min_key in seen_minutes:
                continue
            seen_minutes.add(min_key)
            business_ts = f"{business_date} {dt.strftime('%H:%M:%S')}"
            recent_data_women.append({'x': business_ts, 'y': d['women']})
            recent_data_men.append({'x': business_ts, 'y': d['men']})
        except (ValueError, KeyError):
            continue

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
    }

    analysis_cache['processed_per_store'][store_name] = result
    return result, 200
