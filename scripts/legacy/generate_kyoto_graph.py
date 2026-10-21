#!/usr/bin/env python3
"""
OLG Kyoto Hourly Graph Generator
Creates a bar chart of hourly attendance.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import defaultdict
import statistics
import os
import matplotlib.pyplot as plt
import matplotlib

# Use non-interactive backend for saving files
matplotlib.use('Agg')

# Configuration
SPREADSHEET_NAME = "Lounge Monitor Data"
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "google_credentials.json")
TARGET_STORE = "OLG 京都"
OUTPUT_PATH = "/Users/kuwanoshinya/.gemini/antigravity/brain/a0c451f6-cd6f-4c8d-8dc7-6fc8fb8f6110/kyoto_hourly_graph.png"

def get_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    return gspread.authorize(creds)

def load_and_analyze():
    print("データを読み込み中...")
    client = get_client()
    sheet = client.open(SPREADSHEET_NAME).sheet1
    all_values = sheet.get_all_values()
    
    # Group by hour for OLG Kyoto
    hourly_data = defaultdict(list)
    
    for row in all_values[1:]:  # Skip header
        if len(row) >= 4 and TARGET_STORE in row[1]:
            try:
                timestamp = row[0]
                hour = int(timestamp.split(' ')[1].split(':')[0])
                women = int(row[3]) if row[3] else 0
                hourly_data[hour].append(women)
            except (IndexError, ValueError):
                continue
    
    # Calculate averages for business hours only (18:00 - 05:00)
    hours = []
    avg_women = []
    hour_labels = []
    
    # Business hours order: 18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5
    business_hours = [18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5]
    
    for hour in business_hours:
        if hour in hourly_data and len(hourly_data[hour]) >= 3:
            hours.append(hour)
            avg_women.append(statistics.mean(hourly_data[hour]))
            hour_labels.append(f"{hour}:00")
        else:
            hours.append(hour)
            avg_women.append(0)
            hour_labels.append(f"{hour}:00")
    
    return hours, avg_women, hour_labels

def create_graph(hours, avg_women, hour_labels):
    print("グラフを作成中...")
    
    # Set up Japanese font support
    plt.rcParams['font.family'] = ['Hiragino Sans', 'Yu Gothic', 'Meiryo', 'sans-serif']
    
    # Create figure
    fig, ax = plt.subplots(figsize=(12, 6))
    
    # Color bars based on value
    colors = ['#ff6b6b' if v >= 20 else '#ffa8a8' if v >= 10 else '#ced4da' for v in avg_women]
    
    # Create bar chart with x positions
    x_pos = range(len(hours))
    bars = ax.bar(x_pos, avg_women, color=colors, edgecolor='white', linewidth=0.5)
    
    # Styling
    ax.set_xlabel('時間帯', fontsize=12)
    ax.set_ylabel('平均女性数', fontsize=12)
    ax.set_title('OLG 京都 - 時間帯別 平均女性数 (18:00〜05:00)', fontsize=16, fontweight='bold')
    ax.set_xticks(x_pos)
    ax.set_xticklabels(hour_labels, rotation=45, ha='right')
    ax.set_ylim(0, max(avg_women) * 1.2 if max(avg_women) > 0 else 10)
    
    # Add value labels on bars
    for bar, val in zip(bars, avg_women):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5, 
                   f'{val:.1f}', ha='center', va='bottom', fontsize=9)
    
    # Grid
    ax.yaxis.grid(True, linestyle='--', alpha=0.7)
    ax.set_axisbelow(True)
    
    # Legend
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor='#ff6b6b', label='20人以上'),
        Patch(facecolor='#ffa8a8', label='10-19人'),
        Patch(facecolor='#ced4da', label='10人未満')
    ]
    ax.legend(handles=legend_elements, loc='upper left')
    
    plt.tight_layout()
    plt.savefig(OUTPUT_PATH, dpi=150, bbox_inches='tight')
    print(f"✅ グラフを保存しました: {OUTPUT_PATH}")

def main():
    print("OLG京都 時間帯別グラフを作成します...")
    print("-" * 40)
    
    hours, avg_women, hour_labels = load_and_analyze()
    create_graph(hours, avg_women, hour_labels)

if __name__ == "__main__":
    main()
