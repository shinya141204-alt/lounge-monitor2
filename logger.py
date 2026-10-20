import gspread
from oauth2client.service_account import ServiceAccountCredentials
import datetime
import time
import threading
import requests
import os
import sys

# Constants
CREDENTIALS_FILE = 'google_credentials.json'
SHEET_NAME = 'Lounge Monitor Data'

_auth_lock = threading.Lock()
_cached_creds = None
_cached_client = None
_cached_token = None
_token_expiry = 0
_cached_spreadsheet_id = None

# Shared HTTP session for connection pooling (Keep-Alive to docs.google.com)
http_session = requests.Session()


def _find_creds_path():
    possible_paths = [
        os.path.join(os.path.dirname(__file__), CREDENTIALS_FILE),
        os.path.join('/etc/secrets/', CREDENTIALS_FILE)
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return None


def get_client():
    """Authenticates and returns a cached gspread client."""
    global _cached_creds, _cached_client, _cached_token, _token_expiry
    now = time.time()
    with _auth_lock:
        if _cached_client and _cached_creds and now < _token_expiry:
            return _cached_client

        creds_path = _find_creds_path()
        if not creds_path:
            print(f"Auth Error: Credentials file '{CREDENTIALS_FILE}' not found.", file=sys.stderr)
            return None

        try:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            _cached_creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
            _cached_client = gspread.authorize(_cached_creds)
            token_info = _cached_creds.get_access_token()
            _cached_token = token_info.access_token
            # Cache token for 50 minutes (Google tokens last 60 minutes)
            _token_expiry = now + 3000
            return _cached_client
        except Exception as e:
            print(f"Google Sheets Auth Error: {e}", file=sys.stderr)
            return None


def get_access_token():
    """Returns a cached OAuth2 access token without re-authenticating every call."""
    global _cached_creds, _cached_token, _token_expiry
    now = time.time()
    with _auth_lock:
        if _cached_token and now < _token_expiry:
            return _cached_token

    # Trigger client/token refresh
    client = get_client()
    if not client:
        return None
    return _cached_token


def get_spreadsheet_id():
    """Returns cached spreadsheet ID so we don't query Google Drive on every analysis request."""
    global _cached_spreadsheet_id
    if _cached_spreadsheet_id:
        return _cached_spreadsheet_id

    client = get_client()
    if not client:
        return None
    try:
        sheet = client.open(SHEET_NAME)
        _cached_spreadsheet_id = sheet.id
        return _cached_spreadsheet_id
    except Exception as e:
        print(f"Error resolving spreadsheet ID: {e}", file=sys.stderr)
        return None


last_error = None


def log_data(data):
    """
    Logs the provided data to Google Sheets.
    data: list of dicts [{'name': '...', 'men': 10, 'women': 10, 'source': '...'}, ...]
    """
    global last_error
    client = get_client()
    if not client:
        last_error = "No valid credentials or client."
        print("Skipping logging: No valid credentials or client.")
        return

    try:
        sheet = client.open(SHEET_NAME).sheet1
        now = datetime.datetime.now() + datetime.timedelta(hours=9)  # JST
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
            last_error = None
            print(f"Logged {len(rows_to_append)} rows to Google Sheets.")

        # Concurrently mirror into local SQLite database for instant analytical queries
        try:
            import local_db
            local_db.insert_logs(data, timestamp_str)
        except Exception as dbe:
            print(f"Local SQLite mirror error: {dbe}", file=sys.stderr)

    except Exception as e:
        # Reset cached client on error so next attempt re-authenticates cleanly
        global _token_expiry
        _token_expiry = 0
        last_error = str(e)
        print(f"Google Sheets Logging Error: {e}", file=sys.stderr)
