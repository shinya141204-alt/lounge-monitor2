#!/usr/bin/env python3
"""
Batch Rename Script
Replaces "Oriental" with "OLG" in all store names in Google Sheets.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
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

def rename_oriental_to_olg():
    """Rename all 'Oriental' occurrences to 'OLG' in store names."""
    print("スプレッドシートに接続中...")
    client = get_client()
    sheet = client.open(SPREADSHEET_NAME).sheet1
    
    print("データを読み込み中...")
    all_values = sheet.get_all_values()
    
    # Column B (index 1) is store_name
    updates = []
    updated_count = 0
    
    for row_idx, row in enumerate(all_values, start=1):
        if len(row) >= 2:
            store_name = row[1]
            if "Oriental" in store_name:
                new_name = store_name.replace("Oriental", "OLG")
                updates.append({
                    'range': f'B{row_idx}',
                    'values': [[new_name]]
                })
                updated_count += 1
    
    print(f"変更対象: {updated_count} 件")
    
    if updates:
        print("更新を実行中...")
        # Batch update for efficiency
        sheet.batch_update(updates)
        print(f"✅ 完了! {updated_count} 件の「Oriental」を「OLG」に変更しました。")
    else:
        print("変更対象のデータがありませんでした。")

if __name__ == "__main__":
    rename_oriental_to_olg()
