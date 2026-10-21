import gspread
from oauth2client.service_account import ServiceAccountCredentials
import sys

def delete_old_rows(rows_to_delete=500000):
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name('google_credentials.json', scope)
    client = gspread.authorize(creds)
    
    sheet = client.open('Lounge Monitor Data').sheet1
    total_rows = sheet.row_count
    
    print(f"Total rows currently in sheet: {total_rows}")
    
    if total_rows > rows_to_delete + 1000:
        print(f"Deleting rows 2 to {rows_to_delete + 1}...")
        # delete_rows is 1-indexed. row 1 is likely header.
        # So we delete from index 2 up to rows_to_delete + 1
        # But gspread delete_rows takes start_index, end_index (inclusive)
        # Wait, delete_rows(start_index, end_index)
        # Actually gspread has `delete_rows(start_index, end_index=None)`
        sheet.delete_rows(2, rows_to_delete + 1)
        print("Deletion complete.")
    else:
        print(f"Not enough rows to delete {rows_to_delete}. Total is {total_rows}.")

if __name__ == '__main__':
    delete_old_rows(500000)
