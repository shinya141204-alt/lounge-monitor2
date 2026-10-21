import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import sys

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_path = 'google_credentials.json'

creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
client = gspread.authorize(creds)
sheet = client.open('Lounge Monitor Data').sheet1

rc = sheet.row_count
print(f"Row count: {rc}")
start_row = max(2, rc - 500000)
print(f"Fetching from {start_row} to {rc}")
vals = sheet.get(f'A{start_row}:E{rc}')
print(f"Fetched {len(vals)} rows")
