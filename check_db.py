#!/usr/bin/env python3
"""Quick script to check Postgres database for Excel files"""
import os
import sys

# Add scripts/python to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts', 'python'))

from app import get_db_connection
import psycopg2

conn = get_db_connection()
if not conn:
    print("❌ Could not connect to database")
    sys.exit(1)

try:
    cur = conn.cursor()
    
    # Check table schema
    cur.execute("""
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_name = 'runs'
        ORDER BY ordinal_position;
    """)
    print('=== Table Schema ===')
    for row in cur.fetchall():
        print(f'  {row[0]}: {row[1]}')
    
    # Check runs and Excel file status
    cur.execute("""
        SELECT 
            run_id, 
            status, 
            excel_file_path,
            CASE 
                WHEN excel_file_content IS NULL THEN 'NULL'
                WHEN length(excel_file_content) = 0 THEN 'EMPTY'
                ELSE 'HAS_CONTENT (' || length(excel_file_content) || ' bytes)'
            END as excel_status,
            started_at,
            completed_at
        FROM runs 
        ORDER BY started_at DESC 
        LIMIT 10;
    """)
    
    print('\n=== Recent Runs ===')
    rows = cur.fetchall()
    if rows:
        for row in rows:
            print(f'\nRun ID: {row[0]}')
            print(f'  Status: {row[1]}')
            print(f'  Excel Path: {row[2] or "N/A"}')
            print(f'  Excel Content: {row[3]}')
            print(f'  Started: {row[4]}')
            print(f'  Completed: {row[5] or "N/A"}')
    else:
        print('  No runs found in database')
    
    cur.close()
except Exception as e:
    print(f'❌ Error: {e}')
    import traceback
    traceback.print_exc()
finally:
    conn.close()


