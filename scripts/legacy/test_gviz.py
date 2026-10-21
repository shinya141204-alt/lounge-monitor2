import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import sys
import requests
import json

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_path = 'google_credentials.json'

creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
client = gspread.authorize(creds)
sheet = client.open('Lounge Monitor Data')
spreadsheet_id = sheet.id

# Get access token
access_token = creds.get_access_token().access_token

store_name = 'JIS UMEDA'
query = f"SELECT * WHERE B='{store_name}'"
url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tq={requests.utils.quote(query)}"

headers = {
    "Authorization": f"Bearer {access_token}"
}

response = requests.get(url, headers=headers)
print(f"Status: {response.status_code}")
text = response.text
# The response is in the format: /*O_o*/\ngoogle.visualization.Query.setResponse({...});
if "google.visualization.Query.setResponse(" in text:
    json_str = text[text.find("{"):text.rfind("}")+1]
    data = json.loads(json_str)
    rows = data.get('table', {}).get('rows', [])
    print(f"Fetched {len(rows)} rows for {store_name}")
    if len(rows) > 0:
        print("First row:", rows[0])
else:
    print("Invalid response:")
    print(text[:200])
