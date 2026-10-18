import datetime
from collections import defaultdict
import statistics
import json
import urllib.parse
import requests
import logger

_report_cache = {
    'last_fetched': None,
    'data': None
}
REPORT_CACHE_DURATION = 1800  # Cache for 30 minutes


def get_business_date_str(dt):
    """Treat 00:00 - 06:59 as the previous business day."""
    if dt.hour <= 6:
        return (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def generate_weekly_report():
    global _report_cache
    now_utc = datetime.datetime.now()
    if _report_cache['data'] and _report_cache['last_fetched']:
        if (now_utc - _report_cache['last_fetched']).total_seconds() < REPORT_CACHE_DURATION:
            return _report_cache['data']

    client = logger.get_client()
    access_token = logger.get_access_token()
    if not client or not access_token:
        return {"error": "Google Sheets Authentication Failed"}

    # Define the 7-day period in JST (excluding today to get full past 7 days)
    now_jst = now_utc + datetime.timedelta(hours=9)
    end_date = now_jst.replace(hour=6, minute=59, second=59, microsecond=0)
    start_date = (end_date - datetime.timedelta(days=7)).replace(hour=18, minute=0, second=0)
    start_ts_str = start_date.strftime("%Y-%m-%d %H:%M:%S")

    try:
        sheet = client.open(logger.SHEET_NAME)
        spreadsheet_id = sheet.id

        # Query only rows from the last 7 days via gviz API to prevent OOM
        query = f"SELECT A, B, C, D, E WHERE A >= '{start_ts_str}'"
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={urllib.parse.quote(query)}"
        headers = {"Authorization": f"Bearer {access_token}"}

        response = requests.get(url, headers=headers, timeout=25)
        if response.status_code != 200:
            return {"error": f"Failed to fetch data via gviz: HTTP {response.status_code}"}

        text = response.text
        if "google.visualization.Query.setResponse(" not in text:
            return {"error": "Invalid response format from Google Sheets"}

        json_str = text[text.find("{"):text.rfind("}") + 1]
        data_json = json.loads(json_str)
        rows = data_json.get('table', {}).get('rows', [])
    except Exception as e:
        return {"error": f"Failed to fetch data: {str(e)}"}

    if not rows:
        return {"error": "No data found for the last 7 days"}

    store_data = defaultdict(lambda: {
        'brand': '',
        'daily_peaks_w': defaultdict(int),
        'daily_peaks_m': defaultdict(int),
        'hours': defaultdict(list),
        'days_active': set()
    })

    brand_totals = defaultdict(lambda: {'women': 0, 'men': 0, 'stores': set()})

    for row in rows:
        try:
            cols = row.get('c', [])
            if len(cols) < 4:
                continue

            ts_str = str(cols[0].get('v') or cols[0].get('f') or '') if cols[0] else ''
            name = str(cols[1].get('v') or '') if cols[1] else ''
            men = int(cols[2].get('v') or 0) if cols[2] and cols[2].get('v') is not None else 0
            women = int(cols[3].get('v') or 0) if cols[3] and cols[3].get('v') is not None else 0
            brand = str(cols[4].get('v') or 'Unknown').upper() if len(cols) >= 5 and cols[4] and cols[4].get('v') else 'UNKNOWN'

            if not ts_str or not name or ts_str == 'Timestamp':
                continue

            dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")

            if start_date <= dt <= end_date:
                # Use business date (until 6 AM belongs to previous day)
                date_str = get_business_date_str(dt)
                hour_str = dt.strftime("%H")

                sd = store_data[name]
                if brand != 'UNKNOWN':
                    sd['brand'] = brand
                elif not sd['brand']:
                    sd['brand'] = name.split(' ')[0].upper()

                sd['days_active'].add(date_str)
                sd['hours'][hour_str].append(women)

                if women > sd['daily_peaks_w'][date_str]:
                    sd['daily_peaks_w'][date_str] = women
                if men > sd['daily_peaks_m'][date_str]:
                    sd['daily_peaks_m'][date_str] = men

                brand_totals[sd['brand']]['stores'].add(name)
        except Exception:
            continue

    period_start = (now_jst - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    period_end = (now_jst - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    report = {
        'period': f"{period_start} ~ {period_end}",
        'generated_at': now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        'by_brand': {},
        'stores': []
    }

    for name, data in store_data.items():
        if not data['daily_peaks_w']:
            continue

        avg_w = statistics.mean(data['daily_peaks_w'].values())
        avg_m = statistics.mean(data['daily_peaks_m'].values())
        max_w = max(data['daily_peaks_w'].values())

        peak_hour = "22:00"
        max_avg_h = -1
        for h, counts in data['hours'].items():
            avg_h = statistics.mean(counts)
            if avg_h > max_avg_h:
                max_avg_h = avg_h
                peak_hour = f"{h}:00"

        brand = data['brand'] or 'OTHER'
        report['stores'].append({
            'name': name,
            'brand': brand,
            'avg_women': round(avg_w, 1),
            'avg_men': round(avg_m, 1),
            'max_women': max_w,
            'peak_hour': peak_hour,
            'days_with_data': len(data['days_active'])
        })

        if brand not in report['by_brand']:
            report['by_brand'][brand] = {'total_avg_women': 0, 'total_avg_men': 0, 'store_count': 0}
        report['by_brand'][brand]['total_avg_women'] += avg_w
        report['by_brand'][brand]['total_avg_men'] += avg_m

    for brand, bt in report['by_brand'].items():
        bt['total_avg_women'] = round(bt['total_avg_women'], 1)
        bt['total_avg_men'] = round(bt['total_avg_men'], 1)
        bt['store_count'] = len(brand_totals[brand]['stores'])

    report['stores'].sort(key=lambda x: x['avg_women'], reverse=True)

    _report_cache['data'] = report
    _report_cache['last_fetched'] = now_utc
    return report
