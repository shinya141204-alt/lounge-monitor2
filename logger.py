import gspread
from oauth2client.service_account import ServiceAccountCredentials
import datetime
import os
import sys

# Constants
# The user needs to put their JSON key here
CREDENTIALS_FILE = 'google_credentials.json' 
# The name of the Google Sheet to write to
SHEET_NAME = 'Lounge Monitor Data' 

def get_client():
    """Authenticates and returns the gspread client."""
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    
    # Check for credentials in possible locations
    # 1. Local development (same folder)
    # 2. Render Secret Files (/etc/secrets/)
    possible_paths = [
        os.path.join(os.path.dirname(__file__), CREDENTIALS_FILE),
        os.path.join('/etc/secrets/', CREDENTIALS_FILE)
    ]
    
    creds_path = None
    for path in possible_paths:
        if os.path.exists(path):
            creds_path = path
            break
            
    if not creds_path:
        print(f"Auth Error: Credentials file '{CREDENTIALS_FILE}' not found in local or /etc/secrets/", file=sys.stderr)
        return None
        
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        print(f"Google Sheets Auth Error: {e}", file=sys.stderr)
        return None

def log_data(data):
    """
    Logs the provided data to Google Sheets.
    data: list of dicts [{'name': '...', 'men': 10, 'women': 10, 'source': '...'}, ...]
    """
    client = get_client()
    if not client:
        print("Skipping logging: No valid credentials or client.")
        return

    try:
        sheet = client.open(SHEET_NAME).sheet1
        
        # timestamp
        now = datetime.datetime.now() + datetime.timedelta(hours=9) # JST
        timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
        
        rows_to_append = []
        for item in data:
            row = [
                timestamp_str,
                item.get('name', ''),
                item.get('men', 0),
                item.get('women', 0),
                item.get('source', '')
            ]
            rows_to_append.append(row)
            
        if rows_to_append:
            sheet.append_rows(rows_to_append)
            print(f"Logged {len(rows_to_append)} rows to Google Sheets.")
            
    except Exception as e:
        print(f"Google Sheets Logging Error: {e}", file=sys.stderr)
