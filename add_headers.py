#!/usr/bin/env python3
"""
Add header row to spreadsheet for Looker Studio compatibility.
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os

SPREADSHEET_NAME = "Lounge Monitor Data"
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "google_credentials.json")

def get_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, scope)
    return gspread.authorize(creds)

def add_headers():
    print("スプレッドシートに接続中...")
    client = get_client()
    sheet = client.open(SPREADSHEET_NAME).sheet1
    
    # Check if header already exists
    first_row = sheet.row_values(1)
    if first_row and first_row[0] == 'timestamp':
        print("ヘッダーは既に存在します。")
        return
    
    # Insert header row at the top
    headers = ['timestamp', 'store_name', 'men', 'women', 'source']
    print("ヘッダー行を追加中...")
    sheet.insert_row(headers, index=1)
    print("✅ ヘッダー行を追加しました: " + str(headers))

if __name__ == "__main__":
    add_headers()
