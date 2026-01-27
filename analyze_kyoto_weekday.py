#!/usr/bin/env python3
"""
OLG Kyoto Day-of-Week Analysis
Analyzes attendance by day of the week.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import defaultdict
import statistics
import os
from datetime import datetime
import matplotlib.pyplot as plt
import matplotlib

matplotlib.use('Agg')

SPREADSHEET_NAME = "Lounge Monitor Data"
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "google_credentials.json")
TARGET_STORE = "OLG 京都"
OUTPUT_PATH = "/Users/kuwanoshinya/.gemini/antigravity/brain/a0c451f6-cd6f-4c8d-8dc7-6fc8fb8f6110/kyoto_weekday_graph.png"

WEEKDAY_NAMES = ['月', '火', '水', '木', '金', '土', '日']
WEEKDAY_NAMES_EN = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

def get_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    return gspread.authorize(creds)

def load_and_analyze():
    print("データを読み込み中...")
    client = get_client()
    sheet = client.open(SPREADSHEET_NAME).sheet1
    all_values = sheet.get_all_values()
    
    # Group by weekday for OLG Kyoto
    weekday_data = defaultdict(list)
    
    for row in all_values[1:]:  # Skip header
        if len(row) >= 4 and TARGET_STORE in row[1]:
            try:
                timestamp_str = row[0]  # "2026-01-18 03:00:00"
                dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                weekday = dt.weekday()  # 0=Monday, 6=Sunday
                women = int(row[3]) if row[3] else 0
                weekday_data[weekday].append(women)
            except (IndexError, ValueError):
                continue
    
    print(f"OLG京都のデータを曜日別に集計しました")
    
    # Calculate averages
    results = []
    for day in range(7):
        if day in weekday_data and len(weekday_data[day]) >= 3:
            avg = statistics.mean(weekday_data[day])
            max_val = max(weekday_data[day])
            count = len(weekday_data[day])
            results.append({
                'day': day,
                'day_name': WEEKDAY_NAMES[day],
                'avg_women': round(avg, 1),
                'max_women': max_val,
                'data_points': count
            })
        else:
            results.append({
                'day': day,
                'day_name': WEEKDAY_NAMES[day],
                'avg_women': 0,
                'max_women': 0,
                'data_points': 0
            })
    
    return results

def create_graph(results):
    print("グラフを作成中...")
    
    plt.rcParams['font.family'] = ['Hiragino Sans', 'sans-serif']
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    days = [r['day_name'] for r in results]
    avg_women = [r['avg_women'] for r in results]
    
    # Color weekend differently
    colors = ['#ff6b6b' if r['day'] >= 5 else '#4dabf7' for r in results]
    
    bars = ax.bar(days, avg_women, color=colors, edgecolor='white', linewidth=0.5)
    
    ax.set_xlabel('曜日', fontsize=12)
    ax.set_ylabel('平均女性数', fontsize=12)
    ax.set_title('OLG 京都 - 曜日別 平均女性数', fontsize=16, fontweight='bold')
    ax.set_ylim(0, max(avg_women) * 1.2 if max(avg_women) > 0 else 10)
    
    # Add value labels
    for bar, val in zip(bars, avg_women):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5, 
                   f'{val:.1f}', ha='center', va='bottom', fontsize=11, fontweight='bold')
    
    ax.yaxis.grid(True, linestyle='--', alpha=0.7)
    ax.set_axisbelow(True)
    
    # Legend
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor='#ff6b6b', label='週末（土日）'),
        Patch(facecolor='#4dabf7', label='平日')
    ]
    ax.legend(handles=legend_elements, loc='upper right')
    
    plt.tight_layout()
    plt.savefig(OUTPUT_PATH, dpi=150, bbox_inches='tight')
    print(f"✅ グラフを保存しました: {OUTPUT_PATH}")

def print_report(results):
    print("\n" + "=" * 55)
    print(" OLG 京都 - 曜日別分析")
    print("=" * 55)
    print(f"{'曜日':<6} {'平均女性':<10} {'最大女性':<10} {'データ数':<8}")
    print("-" * 55)
    
    for r in results:
        print(f"{r['day_name']:<6} {r['avg_women']:<10} {r['max_women']:<10} {r['data_points']:<8}")
    
    print("-" * 55)
    
    # Best day
    if results:
        best = max(results, key=lambda x: x['avg_women'])
        print(f"\n【ベストデー】{best['day_name']}曜日 → 平均 {best['avg_women']} 人")

def main():
    print("OLG京都 曜日別分析を開始します...")
    print("-" * 40)
    
    results = load_and_analyze()
    print_report(results)
    create_graph(results)

if __name__ == "__main__":
    main()
