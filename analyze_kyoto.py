#!/usr/bin/env python3
"""
OLG Kyoto Hourly Analysis
Analyzes attendance by hour for OLG Kyoto specifically.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import defaultdict
import statistics
import os

# Configuration
SPREADSHEET_NAME = "Lounge Monitor Data"
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "google_credentials.json")
TARGET_STORE = "OLG 京都"  # Target store to analyze

def get_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    return gspread.authorize(creds)

def load_data():
    client = get_client()
    sheet = client.open(SPREADSHEET_NAME).sheet1
    all_values = sheet.get_all_values()
    
    # Skip header row
    records = []
    for row in all_values[1:]:  # Skip header
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
    
    print(f"Loaded {len(records)} records.")
    return records

def analyze_kyoto_hourly(records):
    # Filter for OLG Kyoto
    kyoto_data = [r for r in records if TARGET_STORE in r['store_name']]
    print(f"OLG京都のデータ: {len(kyoto_data)} 件")
    
    if not kyoto_data:
        print(f"'{TARGET_STORE}' のデータが見つかりませんでした。")
        # Show available stores containing "京都"
        kyoto_stores = set(r['store_name'] for r in records if '京都' in r['store_name'])
        if kyoto_stores:
            print(f"見つかった京都関連の店舗: {kyoto_stores}")
        return None
    
    # Group by hour
    hourly_data = defaultdict(lambda: {'women': [], 'men': []})
    
    for record in kyoto_data:
        try:
            # Parse timestamp: "2026-01-18 03:00:00"
            timestamp = record['timestamp']
            hour = int(timestamp.split(' ')[1].split(':')[0])
            hourly_data[hour]['women'].append(record['women'])
            hourly_data[hour]['men'].append(record['men'])
        except (IndexError, ValueError):
            continue
    
    # Calculate averages
    results = []
    for hour, data in hourly_data.items():
        if len(data['women']) >= 3:  # At least 3 data points
            avg_women = statistics.mean(data['women'])
            avg_men = statistics.mean(data['men'])
            max_women = max(data['women'])
            count = len(data['women'])
            
            results.append({
                'hour': hour,
                'avg_women': round(avg_women, 1),
                'avg_men': round(avg_men, 1),
                'max_women': max_women,
                'data_points': count
            })
    
    # Sort by hour
    results.sort(key=lambda x: x['hour'])
    return results

def print_report(results):
    print("\n" + "=" * 65)
    print(f" OLG 京都 - 時間帯別分析")
    print("=" * 65)
    print(f"{'時間':<8} {'平均女性':<10} {'平均男性':<10} {'最大女性':<10} {'データ数':<8}")
    print("-" * 65)
    
    for r in results:
        hour_str = f"{r['hour']:02d}:00"
        print(f"{hour_str:<8} {r['avg_women']:<10} {r['avg_men']:<10} {r['max_women']:<10} {r['data_points']:<8}")
    
    print("-" * 65)
    
    # Find best hours
    if results:
        best = max(results, key=lambda x: x['avg_women'])
        print(f"\n【ベストタイム】{best['hour']:02d}:00 → 平均 {best['avg_women']} 人")
        
        # Top 3
        top3 = sorted(results, key=lambda x: x['avg_women'], reverse=True)[:3]
        print("\n【TOP 3 時間帯】")
        for i, r in enumerate(top3, 1):
            print(f"  {i}位: {r['hour']:02d}:00 (平均 {r['avg_women']}人)")

def main():
    print("OLG京都 時間帯別分析を開始します...")
    print("-" * 40)
    
    records = load_data()
    results = analyze_kyoto_hourly(records)
    
    if results:
        print_report(results)
    else:
        print("分析に十分なデータがありませんでした。")

if __name__ == "__main__":
    main()
