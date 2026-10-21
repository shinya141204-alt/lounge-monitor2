import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import sys

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_path = 'google_credentials.json'

if not os.path.exists(creds_path):
    print("No creds file!")
    sys.exit(1)

creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
client = gspread.authorize(creds)
sheet = client.open('Lounge Monitor Data').sheet1

print(f"Row count: {sheet.row_count}")
print(f"Col count: {sheet.col_count}")
