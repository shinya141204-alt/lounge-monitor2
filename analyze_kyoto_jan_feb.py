#!/usr/bin/env python3
"""
Analyze store women count (18:00-05:59) comparing January and February.
Includes both all-days view and excluding specific peak/weekend days.
"""

import logger
import datetime
from collections import defaultdict
import statistics

def get_sheet_data(sheet_name):
    print(f"Fetching data from {sheet_name}...")
    client = logger.get_client()
    if not client:
        print("Failed to get Google Sheets client.")
        return []
    
    try:
        sheet = client.open('Lounge Monitor Data').worksheet(sheet_name)
        values = sheet.get_all_values()
        
        data = []
        for row in values[1:]: # Skip header
             if len(row) >= 4:
                 try:
                     # Row format: Timestamp, Store Name, Men, Women, Source
                     data.append({
                         'ts': row[0],
                         'name': row[1],
                         'women': int(row[3]) if row[3] else 0
                     })
                 except:
                     continue
        return data
    except Exception as e:
        print(f"Error fetching sheet {sheet_name}: {e}")
        return []

def filter_store_data(data, store_keyword):
    """Filter data for specific store between 18:00 and 05:59 next day."""
    filtered_data = []
    for d in data:
        if store_keyword not in d['name']:
            continue
            
        try:
            dt = datetime.datetime.strptime(d['ts'], "%Y-%m-%d %H:%M:%S")
            # Include 18:00 to 05:59
            if dt.hour >= 18 or dt.hour <= 5:
                # Calculate business date: 0:00-5:59 belongs to the previous calendar day
                if dt.hour <= 5:
                    business_date = (dt - datetime.timedelta(days=1)).date()
                else:
                    business_date = dt.date()
                
                filtered_data.append({
                    'business_date': business_date,
                    'hour': dt.hour,
                    'women': d['women']
                })
        except:
             continue
    return filtered_data

def analyze_month_hourly(data, month_num, excluded_days=None):
    """Analyze data for a specific month, broken down by hour. Excludes specific days if provided."""
    excluded_days = excluded_days or set()
    
    # Filter by business month and exclude specific days
    month_data = [d for d in data if d['business_date'].month == month_num and d['business_date'].day not in excluded_days]
    
    if not month_data:
         return None
         
    # Group by date and hour to find peak women count per hour per day
    daily_hourly_peaks = defaultdict(lambda: defaultdict(int))
    for d in month_data:
        date_str = d['business_date'].strftime("%Y-%m-%d")
        hour = d['hour']
        daily_hourly_peaks[date_str][hour] = max(daily_hourly_peaks[date_str][hour], d['women'])
        
    if not daily_hourly_peaks:
        return None
        
    # Calculate average across days for each hour
    hourly_avgs = {}
    hours_list = [18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5]
    for h in hours_list:
        peaks = [daily_hourly_peaks[date][h] for date in daily_hourly_peaks if h in daily_hourly_peaks[date]]
        if peaks:
            hourly_avgs[h] = statistics.mean(peaks)
        else:
            hourly_avgs[h] = 0.0
            
    days_recorded = len(daily_hourly_peaks)
    
    return {
        'days_recorded': days_recorded,
        'hourly_avgs': hourly_avgs
    }

def print_result_table(jan_analysis, feb_analysis, title):
    print(f"\n--- {title} ---")
    hours_list = [18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5]
    
    if not jan_analysis or not feb_analysis:
        print("Sufficient data not found for comparison.")
        return
        
    print(f"\n【取得日数】 1月: {jan_analysis['days_recorded']}日 / 2月: {feb_analysis['days_recorded']}日")
    print(f"\n時間帯 | 1月平均 | 2月平均 | 差分")
    print("------|---------|---------|-------")
    
    for h in hours_list:
        jan_avg = jan_analysis['hourly_avgs'][h]
        feb_avg = feb_analysis['hourly_avgs'][h]
        diff = feb_avg - jan_avg
        percent = (diff / jan_avg) * 100 if jan_avg > 0 else 0
        
        time_label = f"{h:02d}:00-{h:02d}:59"
        print(f"{time_label} | {jan_avg:5.1f}人 | {feb_avg:5.1f}人 | {diff:+5.1f}人 ({percent:+5.1f}%)")


def main():
    store_keyword = '長崎'
    print(f"Starting hourly analysis of {store_keyword} store (Jan vs Feb, 18:00-05:59)...")
    
    main_data = get_sheet_data('Lounge Monitor Data')
    archive_data = get_sheet_data('Archive')
    
    all_data = main_data + archive_data
    
    store_evening_data = filter_store_data(all_data, store_keyword)
    print(f"{store_keyword} evening records (18:00-05:59): {len(store_evening_data)}")
    
    jan_excluded = {1, 2, 3, 4, 9, 10, 11, 16, 17, 23, 24, 30, 31}
    feb_excluded = {6, 7, 10, 13, 14, 20, 21, 22, 27, 28}
    
    # Analysis 1: Including all days (No exclusions)
    jan_all = analyze_month_hourly(store_evening_data, 1)
    feb_all = analyze_month_hourly(store_evening_data, 2)
    print_result_table(jan_all, feb_all, f"特定日を含めたバージョン ({store_keyword}店)")

    # Analysis 2: Excluding specific days
    jan_filtered = analyze_month_hourly(store_evening_data, 1, excluded_days=jan_excluded)
    feb_filtered = analyze_month_hourly(store_evening_data, 2, excluded_days=feb_excluded)
    print_result_table(jan_filtered, feb_filtered, f"特定日を含めないバージョン ({store_keyword}店)")

if __name__ == "__main__":
    main()
