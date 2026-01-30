#!/usr/bin/env python3
"""
Data Archival Script for Lounge Monitor

This script moves old data (older than RETENTION_DAYS) from the main sheet
to an archive sheet to prevent the main sheet from getting too large.

Usage:
    python archive_data.py [--dry-run]

Options:
    --dry-run: Show what would be archived without actually moving data
"""

import datetime
import sys
import logger

# Configuration
RETENTION_DAYS = 14  # Keep last 14 days in main sheet
SPREADSHEET_NAME = 'Lounge Monitor Data'
MAIN_SHEET_NAME = 'Sheet1'
ARCHIVE_SHEET_NAME = 'Archive'

def get_cutoff_date():
    """Get the cutoff date - data older than this will be archived."""
    return datetime.datetime.now() - datetime.timedelta(days=RETENTION_DAYS)

def archive_old_data(dry_run=False):
    """Move old data from main sheet to archive sheet."""
    
    print(f"=== Data Archival Script ===")
    print(f"Retention period: {RETENTION_DAYS} days")
    print(f"Cutoff date: {get_cutoff_date().strftime('%Y-%m-%d')}")
    print(f"Dry run: {dry_run}")
    print()
    
    # Get Google Sheets client
    client = logger.get_client()
    if not client:
        print("ERROR: Could not get Google Sheets client")
        return False
    
    try:
        # Open spreadsheet
        spreadsheet = client.open(SPREADSHEET_NAME)
        main_sheet = spreadsheet.sheet1
        
        # Get or create archive sheet
        try:
            archive_sheet = spreadsheet.worksheet(ARCHIVE_SHEET_NAME)
            print(f"Found existing archive sheet: {ARCHIVE_SHEET_NAME}")
        except:
            # Create archive sheet if it doesn't exist
            if dry_run:
                print(f"Would create archive sheet: {ARCHIVE_SHEET_NAME}")
                archive_sheet = None
            else:
                archive_sheet = spreadsheet.add_worksheet(
                    title=ARCHIVE_SHEET_NAME,
                    rows=1000,
                    cols=10
                )
                # Copy header from main sheet
                header = main_sheet.row_values(1)
                if header:
                    archive_sheet.append_row(header)
                print(f"Created archive sheet: {ARCHIVE_SHEET_NAME}")
        
        # Get all data from main sheet
        all_values = main_sheet.get_all_values()
        if len(all_values) <= 1:
            print("No data to archive (only header row exists)")
            return True
        
        header = all_values[0]
        data_rows = all_values[1:]
        
        print(f"Total rows in main sheet: {len(data_rows)}")
        
        # Find rows to archive (older than cutoff)
        cutoff = get_cutoff_date()
        rows_to_archive = []
        rows_to_keep = []
        
        for row in data_rows:
            if len(row) < 1:
                continue
            
            try:
                # Parse timestamp from first column
                ts_str = row[0]
                ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                
                if ts < cutoff:
                    rows_to_archive.append(row)
                else:
                    rows_to_keep.append(row)
            except ValueError:
                # If we can't parse the date, keep the row
                rows_to_keep.append(row)
        
        print(f"Rows to archive: {len(rows_to_archive)}")
        print(f"Rows to keep: {len(rows_to_keep)}")
        
        if len(rows_to_archive) == 0:
            print("No rows old enough to archive")
            return True
        
        if dry_run:
            print("\n[DRY RUN] Would perform the following actions:")
            print(f"  - Append {len(rows_to_archive)} rows to archive sheet")
            print(f"  - Clear main sheet and rewrite with {len(rows_to_keep)} rows")
            return True
        
        # Append old data to archive sheet
        print(f"\nArchiving {len(rows_to_archive)} rows...")
        
        # Batch append for efficiency (100 rows at a time)
        batch_size = 100
        for i in range(0, len(rows_to_archive), batch_size):
            batch = rows_to_archive[i:i+batch_size]
            archive_sheet.append_rows(batch)
            print(f"  Archived rows {i+1} to {min(i+batch_size, len(rows_to_archive))}")
        
        # Clear main sheet and rewrite with kept data
        print(f"\nRewriting main sheet with {len(rows_to_keep)} rows...")
        
        # Clear all data except header
        main_sheet.clear()
        
        # Write header + kept rows
        all_new_data = [header] + rows_to_keep
        
        # Batch update for efficiency
        main_sheet.update(f'A1:E{len(all_new_data)}', all_new_data)
        
        print(f"\n=== Archival Complete ===")
        print(f"Archived: {len(rows_to_archive)} rows")
        print(f"Remaining in main sheet: {len(rows_to_keep)} rows")
        
        return True
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    dry_run = '--dry-run' in sys.argv
    
    success = archive_old_data(dry_run=dry_run)
    
    if success:
        print("\nArchival completed successfully!")
    else:
        print("\nArchival failed!")
        sys.exit(1)

if __name__ == "__main__":
    main()
