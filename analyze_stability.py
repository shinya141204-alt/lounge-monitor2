#!/usr/bin/env python3
"""
Store Stability Analysis Script
Analyzes Google Sheets data to find stores with consistently high attendance.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import defaultdict
import statistics
import os

# Configuration
SPREADSHEET_NAME = "Lounge Monitor Data"
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "google_credentials.json")

def get_client():
    """Authenticate and return gspread client."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    return gspread.authorize(creds)

def load_data():
    """Load all data from the spreadsheet."""
    client = get_client()
    sheet = client.open(SPREADSHEET_NAME).sheet1
    
    # Get all values (no header row assumed)
    # Format: [timestamp, store_name, men, women, source]
    all_values = sheet.get_all_values()
    
    records = []
    for row in all_values:
        if len(row) >= 4:
            try:
                records.append({
                    'timestamp': row[0],
                    'store_name': row[1],
                    'men': int(row[2]) if row[2] else 0,
                    'women': int(row[3]) if row[3] else 0,
                    'source': row[4] if len(row) > 4 else ''
                })
            except (ValueError, IndexError):
                continue
    
    print(f"Loaded {len(records)} records from Google Sheets.")
    return records

def analyze_stability(records):
    """
    Calculate stability metrics for each store.
    
    Stability Score = Average Women / (1 + StdDev)
    Higher score = More women on average, with less fluctuation.
    """
    # Group data by store
    store_data = defaultdict(list)
    
    for record in records:
        store_name = record.get('store_name', '')
        women = record.get('women', 0)
        
        if store_name and women is not None:
            try:
                store_data[store_name].append(int(women))
            except (ValueError, TypeError):
                continue
    
    # Calculate metrics
    results = []
    
    for store_name, women_counts in store_data.items():
        if len(women_counts) < 5:  # Skip stores with too few data points
            continue
            
        avg = statistics.mean(women_counts)
        std = statistics.stdev(women_counts) if len(women_counts) > 1 else 0
        max_count = max(women_counts)
        min_count = min(women_counts)
        count = len(women_counts)
        
        # Stability Score: Higher average with lower variance is better
        stability_score = avg / (1 + std) if std > 0 else avg
        
        results.append({
            'store': store_name,
            'avg_women': round(avg, 1),
            'std_dev': round(std, 1),
            'max': max_count,
            'min': min_count,
            'data_points': count,
            'stability_score': round(stability_score, 2)
        })
    
    # Sort by stability score (descending)
    results.sort(key=lambda x: x['stability_score'], reverse=True)
    
    return results

def print_report(results):
    """Print a formatted report."""
    print("\n" + "=" * 70)
    print(" 店舗安定度ランキング (Stability Score)")
    print("=" * 70)
    print(f"{'順位':<4} {'店舗名':<25} {'平均女性数':<10} {'標準偏差':<8} {'スコア':<8}")
    print("-" * 70)
    
    for i, r in enumerate(results[:20], 1):  # Top 20
        print(f"{i:<4} {r['store']:<25} {r['avg_women']:<10} {r['std_dev']:<8} {r['stability_score']:<8}")
    
    print("-" * 70)
    print("\n【解説】")
    print("・スコア = 平均女性数 / (1 + 標準偏差)")
    print("・スコアが高い = 「常に女性が多く、ブレが少ない」優良店舗")
    print("・標準偏差が低い = 安定している（日によって差が少ない）")
    
    # Top 3 summary
    print("\n【TOP 3 安定優良店舗】")
    for i, r in enumerate(results[:3], 1):
        print(f"  {i}位: {r['store']} (平均 {r['avg_women']}人, 偏差 {r['std_dev']})")

def main():
    print("店舗安定度分析を開始します...")
    print("-" * 40)
    
    try:
        records = load_data()
        results = analyze_stability(records)
        
        if results:
            print_report(results)
        else:
            print("分析に十分なデータがありませんでした。")
            
    except FileNotFoundError:
        print(f"Error: 認証ファイルが見つかりません: {CREDENTIALS_PATH}")
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"Error: スプレッドシート '{SPREADSHEET_NAME}' が見つかりません。")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
