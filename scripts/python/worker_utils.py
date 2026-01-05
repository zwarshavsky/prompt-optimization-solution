#!/usr/bin/env python3
"""
Worker utilities for database operations and job management
"""

import os
import json
import psycopg2
from datetime import datetime
from typing import List, Dict, Optional, Any
from pathlib import Path


def get_db_connection():
    """Get PostgreSQL database connection from Heroku DATABASE_URL"""
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        return None
    
    # Parse DATABASE_URL (format: postgres://user:password@host:port/database)
    # Heroku uses postgres:// but psycopg2 needs postgresql://
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    
    try:
        conn = psycopg2.connect(database_url, sslmode='require')
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}", flush=True)
        return None


def get_queued_jobs() -> List[str]:
    """Get list of run_ids for jobs with status='queued'"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id 
                FROM runs 
                WHERE status = 'queued'
                ORDER BY started_at ASC
            """)
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        print(f"Error getting queued jobs: {e}", flush=True)
        return []
    finally:
        conn.close()


def get_interrupted_jobs() -> List[Dict[str, Any]]:
    """Get list of interrupted jobs with their checkpoint info for resume"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, checkpoint_info, progress
                FROM runs 
                WHERE status = 'interrupted'
                ORDER BY started_at ASC
            """)
            jobs = []
            for row in cur.fetchall():
                jobs.append({
                    'run_id': row[0],
                    'checkpoint_info': row[1] if row[1] else {},
                    'progress': row[2] if row[2] else {}
                })
            return jobs
    except Exception as e:
        print(f"Error getting interrupted jobs: {e}", flush=True)
        return []
    finally:
        conn.close()


def update_job_heartbeat(run_id: str) -> bool:
    """Update heartbeat timestamp for a running job"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs 
                SET heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s AND status = 'running'
            """, (run_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"Error updating heartbeat: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()


def mark_job_as_running(run_id: str) -> bool:
    """Mark job as running and update heartbeat"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs 
                SET status = 'running',
                    heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
            """, (run_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"Error marking job as running: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()


def mark_job_as_interrupted(run_id: str, checkpoint_info: Dict[str, Any]) -> bool:
    """Mark job as interrupted with checkpoint info for resume"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs 
                SET status = 'interrupted',
                    checkpoint_info = %s::jsonb,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
            """, (json.dumps(checkpoint_info), run_id))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"Error marking job as interrupted: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()


def mark_job_as_failed(run_id: str, error: str, error_details: Optional[str] = None) -> bool:
    """Mark job as failed with error message"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs 
                SET status = 'failed',
                    error = %s,
                    error_details = %s,
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
            """, (error, error_details, run_id))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"Error marking job as failed: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()


def mark_job_as_completed(run_id: str, results: Dict[str, Any]) -> bool:
    """Mark job as completed with results"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            # Extract excel_file_path from results if available
            excel_file_path = results.get('excel_file', '')
            
            cur.execute("""
                UPDATE runs 
                SET status = 'completed',
                    results = %s::jsonb,
                    excel_file_path = COALESCE(%s, excel_file_path),
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
            """, (json.dumps(results), excel_file_path, run_id))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"Error marking job as completed: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()


def load_pdfs_from_db(run_id: str, output_dir: Optional[str] = None) -> List[str]:
    """Load PDF files from Postgres database and save to filesystem"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        import json
        import base64
        from pathlib import Path
        
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pdf_files 
                FROM runs 
                WHERE run_id = %s AND pdf_files IS NOT NULL
            """, (run_id,))
            
            row = cur.fetchone()
            if not row or not row[0]:
                return []
            
            pdf_data = row[0]
            if isinstance(pdf_data, str):
                pdf_data = json.loads(pdf_data)
            
            # Create output directory
            if not output_dir:
                # Use app_data/uploads directory
                script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                uploads_dir = Path(script_dir) / "scripts" / "python" / "app_data" / "uploads"
                uploads_dir.mkdir(parents=True, exist_ok=True)
                output_dir = str(uploads_dir)
            
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            
            # Restore PDF files
            restored_paths = []
            for pdf_info in pdf_data:
                filename = pdf_info.get('filename')
                content_b64 = pdf_info.get('content')
                
                if filename and content_b64:
                    pdf_file_path = output_path / filename
                    # Convert base64 string back to bytes
                    pdf_content = base64.b64decode(content_b64)
                    with open(pdf_file_path, 'wb') as f:
                        f.write(pdf_content)
                    restored_paths.append(str(pdf_file_path))
            
            return restored_paths
    except Exception as e:
        print(f"Error loading PDF files from database: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return []
    finally:
        conn.close()


def update_job_progress(run_id: str, progress: Dict[str, Any], output_line: Optional[str] = None) -> bool:
    """Update job progress and optionally add output line. Also ensures status is 'running' if job is active."""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            # Get current output_lines and status
            cur.execute("SELECT output_lines, status FROM runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
            output_lines = row[0] if row and row[0] else []
            current_status = row[1] if row and len(row) > 1 else 'unknown'
            
            # Add new output line if provided
            if output_line:
                if not isinstance(output_lines, list):
                    output_lines = []
                output_lines.append(output_line)
                # Keep only last 1000 lines
                if len(output_lines) > 1000:
                    output_lines = output_lines[-1000:]
            
            # If job is queued/interrupted but has active progress, mark as running
            # This ensures status stays synchronized with actual work being done
            status_to_set = current_status
            if current_status in ['queued', 'interrupted']:
                # If we're getting progress updates, the job is actually running
                status_to_set = 'running'
            
            cur.execute("""
                UPDATE runs 
                SET progress = %s::jsonb,
                    output_lines = %s::jsonb,
                    status = %s,
                    heartbeat_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
            """, (json.dumps(progress), json.dumps(output_lines), status_to_set, run_id))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"Error updating job progress: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()

