import gspread
from oauth2client.service_account import ServiceAccountCredentials
import httplib2
import socket
import datetime
import time
import threading
import requests
import os
import sys

# Force IPv4 globally so httplib2 (oauth2client) and requests never hang on broken IPv6 on Render
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
if getattr(socket.getaddrinfo, '__name__', '') != '_ipv4_only_getaddrinfo':
    socket.getaddrinfo = _ipv4_only_getaddrinfo

# Constants
CREDENTIALS_FILE = 'google_credentials.json'
SHEET_NAME = 'Lounge Monitor Data'

_auth_lock = threading.Lock()
_cached_creds = None
_cached_client = None
_cached_token = None
_token_expiry = 0
_cached_spreadsheet_id = None
_cached_spreadsheet = None
_cached_spreadsheet_client_id = None

# Shared HTTP session for connection pooling (Keep-Alive to docs.google.com)
http_session = requests.Session()


def _find_creds_path():
    possible_paths = [
        os.path.join(os.path.dirname(__file__), CREDENTIALS_FILE),
        os.path.join('/etc/secrets/', CREDENTIALS_FILE),
        '/tmp/aiseki_google_credentials.json',
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    raw_env = (os.environ.get('GOOGLE_CREDENTIALS_JSON') or os.environ.get('GOOGLE_CREDENTIALS_B64') or '').strip()
    if raw_env:
        try:
            import base64
            import json
            decoded = raw_env if raw_env.startswith('{') else base64.b64decode(raw_env).decode('utf-8')
            json.loads(decoded)  # validate JSON
            tmp_path = '/tmp/aiseki_google_credentials.json'
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(decoded)
            return tmp_path
        except Exception as e:
            print(f"GOOGLE_CREDENTIALS_JSON decode error: {e}", file=sys.stderr)
    return None


def get_client():
    """Authenticates and returns a cached gspread client without holding _auth_lock across network I/O."""
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
        new_creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
        new_client = gspread.authorize(new_creds)
        if hasattr(new_client, 'set_timeout'):
            new_client.set_timeout(10)
        new_token = getattr(new_creds, 'access_token', None)
        if not new_token:
            token_info = new_creds.get_access_token(httplib2.Http(timeout=8))
            new_token = token_info.access_token
        with _auth_lock:
            _cached_creds = new_creds
            _cached_client = new_client
            _cached_token = new_token
            # Cache token for 50 minutes (Google tokens last 60 minutes)
            _token_expiry = time.time() + 3000
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
        creds_ref = _cached_creds

    if creds_ref is not None:
        try:
            token_info = creds_ref.get_access_token(httplib2.Http(timeout=8))
            if token_info and token_info.access_token:
                with _auth_lock:
                    _cached_token = token_info.access_token
                    _token_expiry = time.time() + 3000
                    return _cached_token
        except Exception:
            pass

    # Full client/token refresh fallback
    client = get_client()
    if not client:
        return None
    with _auth_lock:
        return _cached_token


def get_spreadsheet_id():
    """Returns cached spreadsheet ID so we don't query Google Drive on every analysis request."""
    global _cached_spreadsheet_id
    with _auth_lock:
        if _cached_spreadsheet_id:
            return _cached_spreadsheet_id

    client = get_client()
    if not client:
        return None
    try:
        sheet = client.open(SHEET_NAME)
        sid = getattr(sheet, 'id', None)
        if sid:
            with _auth_lock:
                _cached_spreadsheet_id = sid
        return sid
    except Exception as e:
        print(f"Error resolving spreadsheet ID: {e}", file=sys.stderr)
        return None


def open_spreadsheet(client=None):
    """Returns a cached gspread Spreadsheet object (opening by key when ID is known to skip Google Drive search API)."""
    global _cached_spreadsheet_id, _cached_spreadsheet, _cached_spreadsheet_client_id
    if client is None:
        client = get_client()
    if not client:
        return None
    cid = id(client)
    with _auth_lock:
        if _cached_spreadsheet is not None and _cached_spreadsheet_client_id == cid:
            return _cached_spreadsheet
        known_sid = _cached_spreadsheet_id
        is_default_client = (client is _cached_client)

    sh = None
    if known_sid and is_default_client and hasattr(client, 'open_by_key'):
        try:
            sh = client.open_by_key(known_sid)
        except Exception:
            sh = None
    if sh is None:
        sh = client.open(SHEET_NAME)

    with _auth_lock:
        if getattr(sh, 'id', None) and isinstance(sh.id, str) and (client is _cached_client or is_default_client):
            _cached_spreadsheet_id = sh.id
        if client is _cached_client or is_default_client:
            _cached_spreadsheet = sh
            _cached_spreadsheet_client_id = cid
    return sh


last_error = None


def log_data(data):
    """
    Logs the provided data to local SQLite and Google Sheets.
    data: list of dicts [{'name': '...', 'men': 10, 'women': 10, 'source': '...'}, ...]
    """
    global last_error, _cached_spreadsheet
    # Always obtain accurate JST timestamp regardless of OS timezone (UTC or JST)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Always mirror into local SQLite database first so local analytics never miss data
    try:
        import local_db
        local_db.insert_logs(data, timestamp_str)
    except Exception as dbe:
        print(f"Local SQLite mirror error: {dbe}", file=sys.stderr)

    # 2. Append to Google Sheets
    client = get_client()
    if not client:
        last_error = "No valid credentials or client."
        print("Skipping Google Sheets logging: No valid credentials or client.")
        return

    try:
        sheet = open_spreadsheet(client).sheet1

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

    except Exception as e:
        # Reset cached client on error so next attempt re-authenticates cleanly
        global _token_expiry
        _token_expiry = 0
        _cached_spreadsheet = None
        last_error = str(e)
        print(f"Google Sheets Logging Error: {e}", file=sys.stderr)
