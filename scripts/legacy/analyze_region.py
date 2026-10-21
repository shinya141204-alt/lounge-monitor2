#!/usr/bin/env python3
"""
Regional Analysis Script
Analyzes store data grouped by region.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from collections import defaultdict
import statistics
import os

# Configuration
SPREADSHEET_NAME = "Lounge Monitor Data"
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "google_credentials.json")

# Region mapping (same as app.py)
REGIONS = {
    'Hokkaido': ['Sapporo', '札幌', 'SAPPORO'],
    'Tohoku': ['Sendai', '仙台'],
    'Kanto': ['Shibuya', 'Ebisu', 'Shinjuku', 'Ueno', 'Kashiwa', 'Machida', 'Yokohama', 'Omiya', 'Utsunomiya', 'Takasaki', '渋谷', '恵比寿', '新宿', '上野', '柏', '町田', '横浜', '大宮', '宇都宮', '高崎', 'OMIYA', 'SHINJUKU', 'NISHISHINJUKU'],
    'Chubu': ['Nagoya', 'Shizuoka', 'Hamamatsu', 'Kanazawa', '名古屋', '静岡', '浜松', '金沢', '錦'],
    'Kinki': ['Osaka', 'Umeda', 'Tenma', 'Shinsaibashi', 'Namba', 'Kyoto', 'Kobe', 'Chayamachi', '大阪', '梅田', '天満', '心斎橋', '難波', '京都', '神戸', '茶屋町', '大阪駅前', 'UMEDA', 'NAMBA', 'CHAYAMACHI'],
    'Chugoku': ['Okayama', 'Hiroshima', '岡山', '広島', 'OKAYAMA', 'HIROSHIMA'],
    'Shikoku': ['Matsuyama', '松山', 'MATSUYAMA'],
    'Kyushu': ['Fukuoka', 'Kokura', 'Nagasaki', 'Oita', 'Kumamoto', 'Miyazaki', 'Kagoshima', 'Okinawa', '福岡', '小倉', '長崎', '大分', '熊本', '宮崎', '鹿児島', '沖縄', 'FUKUOKA', 'KUMAMOTO'],
}

REGION_NAMES_JP = {
    'Hokkaido': '北海道',
    'Tohoku': '東北',
    'Kanto': '関東',
    'Chubu': '中部',
    'Kinki': '関西',
    'Chugoku': '中国',
    'Shikoku': '四国',
    'Kyushu': '九州',
    'Other': 'その他'
}

def get_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    return gspread.authorize(creds)

def detect_region(store_name):
    for region, keywords in REGIONS.items():
        for keyword in keywords:
            if keyword in store_name:
                return region
    return 'Other'

def load_data():
    client = get_client()
    sheet = client.open(SPREADSHEET_NAME).sheet1
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
                })
            except (ValueError, IndexError):
                continue
    
    print(f"Loaded {len(records)} records.")
    return records

def analyze_by_region(records):
    # Group by region
    region_data = defaultdict(lambda: {'women': [], 'men': [], 'stores': set()})
    
    for record in records:
        store_name = record['store_name']
        region = detect_region(store_name)
        region_data[region]['women'].append(record['women'])
        region_data[region]['men'].append(record['men'])
        region_data[region]['stores'].add(store_name)
    
    results = []
    for region, data in region_data.items():
        if len(data['women']) < 10:
            continue
        
        avg_women = statistics.mean(data['women'])
        avg_men = statistics.mean(data['men'])
        std_women = statistics.stdev(data['women']) if len(data['women']) > 1 else 0
        store_count = len(data['stores'])
        data_points = len(data['women'])
        
        # Stability score
        stability = avg_women / (1 + std_women) if std_women > 0 else avg_women
        
        results.append({
            'region': region,
            'region_jp': REGION_NAMES_JP.get(region, region),
            'avg_women': round(avg_women, 1),
            'avg_men': round(avg_men, 1),
            'std_women': round(std_women, 1),
            'store_count': store_count,
            'data_points': data_points,
            'stability': round(stability, 2)
        })
    
    # Sort by average women (descending)
    results.sort(key=lambda x: x['avg_women'], reverse=True)
    return results

def get_top_stores_per_region(records):
    """Get top store for each region."""
    store_data = defaultdict(list)
    
    for record in records:
        store_name = record['store_name']
        store_data[store_name].append(record['women'])
    
    # Calculate averages
    store_averages = {}
    for store, women_list in store_data.items():
        if len(women_list) >= 5:
            store_averages[store] = statistics.mean(women_list)
    
    # Group by region
    region_top = {}
    for store, avg in store_averages.items():
        region = detect_region(store)
        if region not in region_top or avg > region_top[region][1]:
            region_top[region] = (store, avg)
    
    return region_top

def print_report(results, top_stores):
    print("\n" + "=" * 75)
    print(" 地域別ランキング")
    print("=" * 75)
    print(f"{'順位':<4} {'地域':<8} {'平均女性':<10} {'平均男性':<10} {'店舗数':<8} {'安定度':<8}")
    print("-" * 75)
    
    for i, r in enumerate(results, 1):
        print(f"{i:<4} {r['region_jp']:<8} {r['avg_women']:<10} {r['avg_men']:<10} {r['store_count']:<8} {r['stability']:<8}")
    
    print("-" * 75)
    
    print("\n【各地域のNo.1店舗】")
    for region in ['Kinki', 'Kanto', 'Chubu', 'Kyushu', 'Hokkaido', 'Chugoku', 'Shikoku']:
        if region in top_stores:
            store, avg = top_stores[region]
            jp_name = REGION_NAMES_JP.get(region, region)
            print(f"  {jp_name}: {store} (平均 {avg:.1f}人)")

def main():
    print("地域別分析を開始します...")
    print("-" * 40)
    
    records = load_data()
    results = analyze_by_region(records)
    top_stores = get_top_stores_per_region(records)
    
    if results:
        print_report(results, top_stores)
    else:
        print("分析に十分なデータがありませんでした。")

if __name__ == "__main__":
    main()
