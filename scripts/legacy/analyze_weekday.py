#!/usr/bin/env python3
"""
General Weekday Analysis Script
1. Analyzes overall trends by day of the week.
2. Identifies top performing stores for each day.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import defaultdict
import statistics
import os
from datetime import datetime

# Configuration
SPREADSHEET_NAME = "Lounge Monitor Data"
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "google_credentials.json")

WEEKDAY_NAMES = ['月', '火', '水', '木', '金', '土', '日']

def get_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    return gspread.authorize(creds)

def load_data():
    client = get_client()
    sheet = client.open(SPREADSHEET_NAME).sheet1
    all_values = sheet.get_all_values()
    
    records = []
    for row in all_values[1:]:
        if len(row) >= 4:
            try:
                records.append({
                    'timestamp': row[0],
                    'store_name': row[1],
                    'men': int(row[2]) if row[2] else 0,
                    'women': int(row[3]) if row[3] else 0,
                })
            except (ValueError, IndexError):
                continue
    return records

def analyze_weekday_trends(records):
    # Global stats by day
    # store_daily_stats[day][store_name] = [list of counts]
    global_stats = defaultdict(list)
    store_daily_stats = defaultdict(lambda: defaultdict(list))
    
    for record in records:
        try:
            dt = datetime.strptime(record['timestamp'], "%Y-%m-%d %H:%M:%S")
            weekday = dt.weekday()
            
            global_stats[weekday].append(record['women'])
            store_daily_stats[weekday][record['store_name']].append(record['women'])
        except ValueError:
            continue
            
    # 1. Overall Rankings (Which day is busiest?)
    print("\n" + "=" * 50)
    print(" 曜日別 全体平均ランキング (全店舗合計)")
    print("=" * 50)
    
    day_avgs = []
    for day in range(7):
        if global_stats[day]:
            avg = statistics.mean(global_stats[day])
            day_avgs.append((day, avg))
    
    # Sort by avg
    day_avgs.sort(key=lambda x: x[1], reverse=True)
    
    for rank, (day, avg) in enumerate(day_avgs, 1):
        print(f"{rank}位: {WEEKDAY_NAMES[day]}曜日 (平均 {avg:.1f} 人)")

    # 2. Top Store per Day
    print("\n" + "=" * 50)
    print(" 曜日別 No.1 店舗")
    print("=" * 50)
    
    for day in range(7):
        # Find best store for this day
        best_store = None
        best_avg = -1
        
        for store, counts in store_daily_stats[day].items():
            if len(counts) > 10: # Filter low data
                avg = statistics.mean(counts)
                if avg > best_avg:
                    best_avg = avg
                    best_store = store
        
        if best_store:
            print(f"【{WEEKDAY_NAMES[day]}】 Top: {best_store} (平均 {best_avg:.1f} 人)")

def main():
    print("曜日別トレンド分析を開始します...")
    records = load_data()
    analyze_weekday_trends(records)

if __name__ == "__main__":
    main()
