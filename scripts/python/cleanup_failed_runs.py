#!/usr/bin/env python3
"""
Clean up failed runs from database to free up space.
Keeps logs but removes job records older than specified days.
"""

import os
import sys
import psycopg2
from datetime import datetime, timedelta

def get_db_connection():
    """Get PostgreSQL database connection from Heroku DATABASE_URL"""
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        print("ERROR: DATABASE_URL not found")
        return None

    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)

    try:
        conn = psycopg2.connect(database_url, sslmode='require')
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return None

def cleanup_failed_runs(days_old=7, dry_run=True):
    """Delete failed/error runs older than days_old"""
    conn = get_db_connection()
    if not conn:
        return

    cutoff_date = datetime.now() - timedelta(days=days_old)

    try:
        with conn.cursor() as cur:
            # Count what will be deleted
            cur.execute("""
                SELECT COUNT(*),
                       SUM(pg_column_size(output_log)) as log_size,
                       SUM(pg_column_size(progress)) as progress_size
                FROM runs
                WHERE status IN ('failed', 'error')
                AND started_at < %s
            """, (cutoff_date,))

            count, log_size, progress_size = cur.fetchone()
            log_size_mb = (log_size or 0) / 1024 / 1024
            progress_size_mb = (progress_size or 0) / 1024 / 1024
            total_mb = log_size_mb + progress_size_mb

            print(f"\n{'DRY RUN - ' if dry_run else ''}Found {count} failed/error runs older than {days_old} days")
            print(f"Estimated space to free: {total_mb:.2f} MB")
            print(f"  - Logs: {log_size_mb:.2f} MB")
            print(f"  - Progress data: {progress_size_mb:.2f} MB")

            if count == 0:
                print("Nothing to clean up!")
                return

            # Show sample of what will be deleted
            cur.execute("""
                SELECT run_id, status, started_at
                FROM runs
                WHERE status IN ('failed', 'error')
                AND started_at < %s
                ORDER BY started_at DESC
                LIMIT 10
            """, (cutoff_date,))

            print("\nSample of runs to be deleted:")
            for run_id, status, started_at in cur.fetchall():
                print(f"  - {run_id} ({status}) started {started_at}")

            if not dry_run:
                # Delete the runs
                cur.execute("""
                    DELETE FROM runs
                    WHERE status IN ('failed', 'error')
                    AND started_at < %s
                """, (cutoff_date,))

                deleted = cur.rowcount
                conn.commit()
                print(f"\n✅ Deleted {deleted} failed/error runs")

                # Vacuum to reclaim space
                print("\n🔧 Running VACUUM to reclaim space...")
                conn.autocommit = True
                cur.execute("VACUUM FULL runs")
                print("✅ VACUUM complete")
            else:
                print("\n⚠️  This was a DRY RUN. Run with --execute to actually delete.")

    except Exception as e:
        print(f"Error during cleanup: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Clean up failed runs from database")
    parser.add_argument("--days", type=int, default=7, help="Delete runs older than N days (default: 7)")
    parser.add_argument("--execute", action="store_true", help="Actually delete (default is dry-run)")
    args = parser.parse_args()

    cleanup_failed_runs(days_old=args.days, dry_run=not args.execute)
