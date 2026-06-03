import datetime
from collections import defaultdict
import statistics
import logger

def generate_weekly_report():
    client = logger.get_client()
    if not client:
        return {"error": "Google Sheets Authentication Failed"}

    try:
        sheet = client.open(logger.SHEET_NAME).sheet1
        all_records = sheet.get_all_records()
    except Exception as e:
        return {"error": f"Failed to fetch data: {str(e)}"}

    if not all_records:
        return {"error": "No data found"}

    # Define the 7-day period (excluding today to get full past 7 days)
    now = datetime.datetime.now() + datetime.timedelta(hours=9)
    end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - datetime.timedelta(days=7)

    # Store stats: name -> { men: [], women: [], daily_peaks: {date: max_women}, hours: {hour: [women]} }
    store_data = defaultdict(lambda: {
        'brand': '',
        'daily_peaks_w': defaultdict(int),
        'daily_peaks_m': defaultdict(int),
        'hours': defaultdict(list),
        'days_active': set()
    })

    brand_totals = defaultdict(lambda: {'women': 0, 'men': 0, 'stores': set()})

    for row in all_records:
        try:
            ts_str = str(row.get('Timestamp', ''))
            name = str(row.get('Store Name', ''))
            men = int(row.get('Men count', 0))
            women = int(row.get('Women count', 0))
            brand = str(row.get('Source', 'Unknown')).upper()

            if not ts_str or not name:
                continue

            dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")

            # Only process data within the last 7 days
            if start_date <= dt < end_date:
                date_str = dt.strftime("%Y-%m-%d")
                hour_str = dt.strftime("%H")

                sd = store_data[name]
                sd['brand'] = brand
                sd['days_active'].add(date_str)
                sd['hours'][hour_str].append(women)

                if women > sd['daily_peaks_w'][date_str]:
                    sd['daily_peaks_w'][date_str] = women
                if men > sd['daily_peaks_m'][date_str]:
                    sd['daily_peaks_m'][date_str] = men

                brand_totals[brand]['stores'].add(name)
        except Exception:
            continue

    # Compile the final report
    report = {
        'period': f"{start_date.strftime('%Y-%m-%d')} ~ {(end_date - datetime.timedelta(days=1)).strftime('%Y-%m-%d')}",
        'generated_at': now.strftime("%Y-%m-%d %H:%M:%S"),
        'by_brand': {},
        'stores': []
    }

    # Process store data
    for name, data in store_data.items():
        if not data['daily_peaks_w']:
            continue
            
        avg_w = statistics.mean(data['daily_peaks_w'].values())
        avg_m = statistics.mean(data['daily_peaks_m'].values())
        max_w = max(data['daily_peaks_w'].values())

        # Find peak hour (hour with highest average women)
        peak_hour = "00:00"
        max_avg_h = -1
        for h, counts in data['hours'].items():
            avg_h = statistics.mean(counts)
            if avg_h > max_avg_h:
                max_avg_h = avg_h
                peak_hour = f"{h}:00"

        report['stores'].append({
            'name': name,
            'brand': data['brand'],
            'avg_women': round(avg_w, 1),
            'avg_men': round(avg_m, 1),
            'max_women': max_w,
            'peak_hour': peak_hour,
            'days_with_data': len(data['days_active'])
        })

        # Add to brand totals
        brand = data['brand']
        if brand not in report['by_brand']:
             report['by_brand'][brand] = {'total_avg_women': 0, 'total_avg_men': 0, 'store_count': 0}
        report['by_brand'][brand]['total_avg_women'] += avg_w
        report['by_brand'][brand]['total_avg_men'] += avg_m

    for brand, bt in report['by_brand'].items():
        bt['total_avg_women'] = round(bt['total_avg_women'], 1)
        bt['total_avg_men'] = round(bt['total_avg_men'], 1)
        bt['store_count'] = len(brand_totals[brand]['stores'])

    # Sort stores by avg women descending
    report['stores'].sort(key=lambda x: x['avg_women'], reverse=True)

    return report
