#!/usr/bin/env python3
"""
Streamlit Web Interface for Prompt Optimization Workflow
Matches the mockup design and functionality exactly
"""

import streamlit as st
import yaml
import pandas as pd
from pathlib import Path
import sys
import os
from datetime import datetime
import threading
import json
import streamlit.components.v1 as components
from typing import Any, Dict, List

# Debug logging setup
  # Silently fail if logging fails

# Add parent directory to path to import main
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

try:
    import importlib
    import main as main_module
    run_full_workflow = main_module.run_full_workflow
except (ImportError, KeyError, AttributeError) as e:
    st.error(f"Failed to import main module: {e}")
    st.stop()

# ============================================================================
# PATH MANAGEMENT: Self-contained app_data structure
# ============================================================================

def get_app_data_dir():
    """Get app_data directory, create if needed"""
    app_data = Path(__file__).parent / "app_data"
    app_data.mkdir(exist_ok=True)
    return app_data

def get_config_dir():
    """Get config directory, create if needed"""
    config_dir = Path(__file__).parent / "config"
    config_dir.mkdir(exist_ok=True)
    return config_dir

# Path to store runs data (relative to script) - fallback for local development
RUNS_DATA_FILE = get_app_data_dir() / "runs_data.json"

def get_db_connection():
    """Get PostgreSQL database connection from Heroku DATABASE_URL"""
    try:
        import psycopg2
    except ImportError:
        return None
    
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
        print(f"Warning: Could not connect to database: {e}")
        return None

def init_database():
    """Initialize database table if it doesn't exist"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            # Create runs table if it doesn't exist
            cur.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id VARCHAR(255) PRIMARY KEY,
                    status VARCHAR(50) NOT NULL,
                    config JSONB,
                    progress JSONB,
                    output_lines JSONB,
                    results JSONB,
                    error TEXT,
                    error_details TEXT,
                    excel_file_path TEXT,
                    excel_file_content BYTEA,
                    pdf_files JSONB,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    heartbeat_at TIMESTAMP,
                    checkpoint_info JSONB
                )
            """)
            
            # Add pdf_files column if it doesn't exist (for existing databases)
            cur.execute("""
                DO $$ 
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns 
                        WHERE table_name='runs' AND column_name='pdf_files'
                    ) THEN
                        ALTER TABLE runs ADD COLUMN pdf_files JSONB;
                    END IF;
                END $$;
            """)
            
            # Add heartbeat_at column if it doesn't exist (for existing databases)
            cur.execute("""
                ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP
            """)
            
            # Add checkpoint_info column if it doesn't exist (for resume functionality)
            cur.execute("""
                ALTER TABLE runs ADD COLUMN IF NOT EXISTS checkpoint_info JSONB
            """)
            
            # Create index on status for faster filtering
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)
            """)
            
            # Create index on started_at for sorting
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC)
            """)
            
            # Create composite index on status and heartbeat_at for worker polling and dead job detection
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_runs_status_heartbeat ON runs(status, heartbeat_at)
            """)
            
            conn.commit()
            return True
    except Exception as e:
        print(f"Error initializing database: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def serialize_datetime(obj: Any) -> str:
    """Convert datetime objects to ISO format strings for JSON serialization"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

def deserialize_datetime(obj: Dict) -> Dict:
    """Convert ISO format strings back to datetime objects"""
    for key in ['started_at', 'completed_at']:
        if key in obj and isinstance(obj[key], str):
            try:
                obj[key] = datetime.fromisoformat(obj[key])
            except (ValueError, AttributeError):
                pass
    return obj

def save_excel_to_db(run_id: str, excel_file_path: str) -> bool:
    """Save Excel file content to Postgres database"""
    if not os.path.exists(excel_file_path):
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        import psycopg2
        
        with open(excel_file_path, 'rb') as f:
            excel_content = f.read()
        
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs 
                SET excel_file_path = %s, excel_file_content = %s, updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
            """, (excel_file_path, psycopg2.Binary(excel_content), run_id))
            conn.commit()
            return True
    except Exception as e:
        print(f"Error saving Excel file to database: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def load_excel_from_db(run_id: str, output_path: str = None) -> str:
    """Load Excel file from Postgres database and save to filesystem"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT excel_file_path, excel_file_content 
                FROM runs 
                WHERE run_id = %s AND excel_file_content IS NOT NULL
            """, (run_id,))
            
            row = cur.fetchone()
            if not row:
                return None
            
            stored_path, excel_content = row
            
            # If no output path specified, use the stored path or create in app_data/outputs
            if not output_path:
                if stored_path and os.path.exists(stored_path):
                    return stored_path
                # Create path in app_data/outputs
                outputs_dir = get_app_data_dir() / "outputs"
                outputs_dir.mkdir(exist_ok=True)
                output_path = outputs_dir / f"run_{run_id}.xlsx"
            else:
                output_path = Path(output_path)
            
            # Ensure directory exists
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write Excel file to filesystem
            with open(output_path, 'wb') as f:
                f.write(excel_content)
            
            return str(output_path)
    except Exception as e:
        print(f"Error loading Excel file from database: {e}")
        return None
    finally:
        conn.close()

def save_pdfs_to_db(run_id: str, pdf_file_paths: List[str]) -> bool:
    """Save PDF files to Postgres database as JSONB array"""
    if not pdf_file_paths:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        import psycopg2
        import json
        import base64
        
        pdf_data = []
        for pdf_path in pdf_file_paths:
            if os.path.exists(pdf_path):
                with open(pdf_path, 'rb') as f:
                    pdf_content = f.read()
                # Store as base64 string for JSONB storage
                pdf_data.append({
                    'filename': os.path.basename(pdf_path),
                    'content': base64.b64encode(pdf_content).decode('utf-8')
                })
        
        if not pdf_data:
            return False
        
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs 
                SET pdf_files = %s::jsonb, updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
            """, (json.dumps(pdf_data), run_id))
            conn.commit()
            return True
    except Exception as e:
        print(f"Error saving PDF files to database: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def kill_job(run_id: str) -> bool:
    """Kill a running/queued job by marking it as failed"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE runs 
                SET status = 'failed',
                    error = 'Manually killed by user',
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s AND status IN ('running', 'queued', 'interrupted')
            """, (run_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        print(f"Error killing job {run_id}: {e}", flush=True)
        conn.rollback()
        return False
    finally:
        conn.close()

def load_pdfs_from_db(run_id: str, output_dir: str = None) -> List[str]:
    """Load PDF files from Postgres database and save to filesystem"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        import json
        import base64
        
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
                uploads_dir = get_app_data_dir() / "uploads"
                uploads_dir.mkdir(exist_ok=True)
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
        print(f"Error loading PDF files from database: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        conn.close()

def load_runs() -> List[Dict]:
    """Load runs from persistent storage (Postgres or JSON fallback)"""
    # Try Postgres first
    conn = get_db_connection()
    if conn:
        try:
            # Initialize database if needed
            init_database()
            
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT run_id, status, config, progress, output_lines, 
                           results, error, error_details, excel_file_path, 
                           started_at, completed_at, heartbeat_at, checkpoint_info
                    FROM runs
                    ORDER BY started_at DESC
                """)
                
                runs = []
                for row in cur.fetchall():
                    run = {
                        'run_id': row[0],
                        'status': row[1],
                        'config': row[2] if row[2] else {},
                        'progress': row[3] if row[3] else {},
                        'output_lines': row[4] if row[4] else [],
                        'results': row[5] if row[5] else {},
                        'error': row[6],
                        'error_details': row[7],
                        'excel_file_path': row[8],
                        'started_at': row[9],
                        'completed_at': row[10],
                        'heartbeat_at': row[11],
                        'checkpoint_info': row[12] if row[12] else {}
                    }
                    # Convert datetime strings to datetime objects
                    deserialize_datetime(run)
                    runs.append(run)
                
                print(f"[APP] Loaded {len(runs)} job(s) from database", flush=True)
                return runs
        except Exception as e:
            print(f"[APP] Error loading runs from database: {e}", flush=True)
            import traceback
            traceback.print_exc()
            # Fall through to JSON fallback
        finally:
            conn.close()
    
    # Fallback to JSON file (for local development)
    if RUNS_DATA_FILE.exists():
        try:
            with open(RUNS_DATA_FILE, 'r') as f:
                runs_data = json.load(f)
                # Convert datetime strings back to datetime objects
                for run in runs_data:
                    deserialize_datetime(run)
                return runs_data
        except Exception as e:
            print(f"Error loading runs from JSON file: {e}")
            return []
    return []

def detect_and_mark_dead_jobs(stale_threshold_minutes: int = 2) -> int:
    """
    Detect jobs that haven't updated heartbeat in > threshold minutes and mark them as failed.
    Returns count of dead jobs marked.
    """
    conn = get_db_connection()
    if not conn:
        return 0
    
    try:
        with conn.cursor() as cur:
            # Find jobs with status='running' that have stale or missing heartbeat
            cur.execute("""
                SELECT run_id, heartbeat_at, updated_at
                FROM runs
                WHERE status = 'running'
                AND (
                    heartbeat_at IS NULL 
                    OR heartbeat_at < NOW() - INTERVAL '%s minutes'
                )
            """, (stale_threshold_minutes,))
            
            dead_jobs = cur.fetchall()
            count = 0
            
            for run_id, heartbeat_at, updated_at in dead_jobs:
                # Mark as failed with appropriate error message
                error_msg = f"Job appears to have stopped (no heartbeat detected for > {stale_threshold_minutes} minutes). Possible dyno restart or process crash."
                
                # Use heartbeat_at if available, otherwise updated_at
                last_activity = heartbeat_at if heartbeat_at else updated_at
                
                cur.execute("""
                    UPDATE runs
                    SET status = 'failed',
                        error = %s,
                        completed_at = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s
                """, (error_msg, last_activity, run_id))
                count += 1
            
            conn.commit()
            if count > 0:
                print(f"[APP] Detected {count} dead job(s) and marked as failed", flush=True)
            return count
    except Exception as e:
        print(f"Error detecting dead jobs: {e}", flush=True)
        conn.rollback()
        return 0
    finally:
        conn.close()

def save_runs(runs: List[Dict]) -> None:
    """Save runs to persistent storage (Postgres or JSON fallback)"""
    # Try Postgres first
    conn = get_db_connection()
    if conn:
        try:
            # Initialize database if needed
            init_database()
            
            with conn.cursor() as cur:
                for run in runs:
                    # Convert datetime objects to strings for JSONB storage
                    run_copy = run.copy()
                    started_at = run_copy.get('started_at')
                    completed_at = run_copy.get('completed_at')
                    heartbeat_at = run_copy.get('heartbeat_at')
                    excel_file_path = run_copy.get('excel_file_path') or run_copy.get('results', {}).get('excel_file', '')
                    checkpoint_info = run_copy.get('checkpoint_info', {})
                    
                    if isinstance(started_at, datetime):
                        started_at = started_at.isoformat()
                    if isinstance(completed_at, datetime):
                        completed_at = completed_at.isoformat()
                    if isinstance(heartbeat_at, datetime):
                        heartbeat_at = heartbeat_at.isoformat()
                    
                    # Use INSERT ... ON CONFLICT to update existing runs
                    cur.execute("""
                        INSERT INTO runs (
                            run_id, status, config, progress, output_lines,
                            results, error, error_details, excel_file_path, 
                            started_at, completed_at, heartbeat_at, checkpoint_info, updated_at
                        ) VALUES (
                            %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                            %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (run_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            config = EXCLUDED.config,
                            progress = EXCLUDED.progress,
                            output_lines = EXCLUDED.output_lines,
                            results = EXCLUDED.results,
                            error = EXCLUDED.error,
                            error_details = EXCLUDED.error_details,
                            excel_file_path = EXCLUDED.excel_file_path,
                            started_at = EXCLUDED.started_at,
                            completed_at = EXCLUDED.completed_at,
                            heartbeat_at = EXCLUDED.heartbeat_at,
                            checkpoint_info = EXCLUDED.checkpoint_info,
                            updated_at = CURRENT_TIMESTAMP
                    """, (
                        run_copy.get('run_id'),
                        run_copy.get('status', 'unknown'),
                        json.dumps(run_copy.get('config', {})),
                        json.dumps(run_copy.get('progress', {})),
                        json.dumps(run_copy.get('output_lines', [])),
                        json.dumps(run_copy.get('results', {})),
                        run_copy.get('error'),
                        run_copy.get('error_details'),
                        excel_file_path,
                        started_at,
                        completed_at,
                        heartbeat_at,
                        json.dumps(checkpoint_info) if checkpoint_info else None
                    ))
                
                conn.commit()
                
                # Save Excel files to database if they exist
                for run in runs:
                    excel_file_path = run.get('excel_file_path') or run.get('results', {}).get('excel_file', '')
                    if excel_file_path and os.path.exists(excel_file_path):
                        save_excel_to_db(run.get('run_id'), excel_file_path)
                
                return
        except Exception as e:
            print(f"Error saving runs to database: {e}")
            conn.rollback()
            # Fall through to JSON fallback
        finally:
            conn.close()
    
    # Fallback to JSON file (for local development)
    try:
        # Ensure directory exists
        RUNS_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        # Convert datetime objects to strings for JSON
        runs_to_save = []
        for run in runs:
            run_copy = run.copy()
            for key in ['started_at', 'completed_at']:
                if key in run_copy and isinstance(run_copy[key], datetime):
                    run_copy[key] = run_copy[key].isoformat()
            runs_to_save.append(run_copy)
        
        with open(RUNS_DATA_FILE, 'w') as f:
            json.dump(runs_to_save, f, indent=2, default=serialize_datetime)
    except Exception as e:
        print(f"Error saving runs to JSON file: {e}")

# Page configuration
st.set_page_config(
    page_title="Prompt Optimization",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS matching mockup exactly
st.markdown("""
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
<style>
    :root {
        --primary-color: #FF4B4B;
        --secondary-color: #0E1117;
        --background-color: #FAFAFA;
        --sidebar-bg: #FFFFFF;
        --text-color: #262730;
        --border-color: #E6E9EF;
    }
    
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    
    .main .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
        max-width: 1200px;
    }
    
    .stButton > button {
        background-color: var(--primary-color);
        color: white;
        border-radius: 0.5rem;
        font-weight: 500;
        padding: 0.625rem 1.25rem;
        border: none;
    }
    
    .stButton > button:hover {
        background-color: #E63946;
        transform: translateY(-1px);
        box-shadow: 0 4px 8px rgba(255, 75, 75, 0.2);
    }
    
    .config-section {
        background: white;
        border: 1px solid var(--border-color);
        border-radius: 0.75rem;
        padding: 1.5rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    }
    
    .section-title {
        font-size: 1.25rem;
        font-weight: 600;
        color: var(--text-color);
        margin-bottom: 1.25rem;
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding-bottom: 0.75rem;
        border-bottom: 1px solid var(--border-color);
    }
    
    .priority-badge {
        background-color: var(--primary-color);
        color: white;
        font-weight: 600;
        min-width: 50px;
        text-align: center;
        border-radius: 0.5rem 0 0 0.5rem;
        cursor: move;
        user-select: none;
        padding: 0.5rem;
    }
    
    .question-item {
        background: #F8F9FA;
        border: 1px solid var(--border-color);
        border-radius: 0.5rem;
        padding: 1rem;
        margin-bottom: 1rem;
    }
    
    .fallback-item {
        transition: all 0.2s;
        cursor: move;
        margin-bottom: 1rem;
    }
    
    .fallback-item:hover {
        transform: translateX(5px);
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
    }
    
    .fallback-item.dragging {
        opacity: 0.5;
        transform: rotate(2deg);
    }
    
    /* Fix white bars - hide empty containers, captions that create boxes */
    div[data-testid*="stTextInput"]:has(input[value=""][placeholder=""]),
    div[data-testid*="stTextInput"]:has(input:not([value]):not(:focus)),
    div[data-testid*="stCaption"]:empty,
    div[data-testid*="stMarkdown"]:has(> p:empty),
    div[data-testid*="column"]:has(> div:empty:not([data-testid])) {
        display: none !important;
    }
    
    /* Hide empty rounded containers that look like input fields */
    div[data-baseweb="input"]:has(input[value=""]:not(:focus):not([placeholder])),
    div.element-container:has(> div:empty),
    div[data-testid*="column"]:empty {
        display: none !important;
    }
    
    /* Ensure config sections don't have extra padding */
    .config-section {
        padding: 1.5rem !important;
        margin-bottom: 1.5rem;
    }
    
    /* Remove extra spacing from form */
    form {
        margin: 0;
        padding: 0;
    }
    
    /* Hide any empty white rounded rectangles */
    div[style*="border-radius"]:empty,
    div[class*="rounded"]:empty:not([data-testid*="st"]) {
        display: none !important;
    }
    
    /* Hide empty config-section divs that create white boxes */
    .config-section:empty,
    .config-section:not(:has(h3)):not(:has(input)):not(:has(select)):not(:has(textarea)):not(:has(button)) {
        display: none !important;
        height: 0 !important;
        padding: 0 !important;
        margin: 0 !important;
    }
    
    /* Hide empty containers after sections */
    .config-section + div:empty,
    .config-section + div:not(:has(*)) {
        display: none !important;
    }
    
    /* Remove spacing and grey bar in Test Questions section */
    .test-questions-section .test-questions-title {
        margin-bottom: 0 !important;
        padding-bottom: 0 !important;
    }
    
    /* Remove grey bar from ALL question items - no grey backgrounds on any questions */
    .test-questions-section .question-item,
    .test-questions-section .question-item.first-question,
    .test-questions-section .first-question,
    div.question-item,
    div.question-item.first-question,
    .config-section.test-questions-section .question-item,
    .config-section.test-questions-section .question-item:first-child,
    .test-questions-section > div:has(.question-item),
    .test-questions-section div:has(.first-question) {
        background: transparent !important;
        background-color: transparent !important;
        border: none !important;
        border-width: 0 !important;
    }
    
    /* First question: no top padding/margin */
    .test-questions-section .question-item.first-question,
    .test-questions-section .question-item:first-child {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
    
    /* Ensure no grey background shows through on any question */
    .test-questions-section .question-item * {
        background: inherit !important;
    }
    
    /* Remove any Streamlit-generated spacing between title and first question */
    .test-questions-section h3 + *,
    .test-questions-section h3 ~ div:first-of-type,
    .test-questions-section [data-testid*="stMarkdown"]:has(h3.test-questions-title) + *,
    .test-questions-section [data-testid*="stMarkdown"]:has(h3.test-questions-title) ~ *,
    .test-questions-section [data-testid*="stMarkdown"]:has(h3.test-questions-title) + [data-testid*="stMarkdown"],
    .test-questions-section [data-testid*="stMarkdown"]:has(h3.test-questions-title) ~ [data-testid*="stMarkdown"]:first-of-type {
        margin-top: 0 !important;
        padding-top: 0 !important;
    }
    
    /* Target the first question-item wrapper specifically */
    .test-questions-section [data-testid*="stMarkdown"]:has(.question-item:first-child) {
        margin-top: 0 !important;
        padding-top: 0 !important;
    }
    
    /* Remove any empty divs or spacing elements */
    .test-questions-section > div:empty,
    .test-questions-section > div:not(:has(*)) {
        display: none !important;
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    
    /* Ensure no grey backgrounds on wrapper elements between questions */
    .test-questions-section [data-testid*="stMarkdown"]:has(.question-item) {
        background: transparent !important;
        background-color: transparent !important;
    }
    
    /* Remove any grey backgrounds from Streamlit column wrappers in question items */
    .test-questions-section .question-item [data-testid*="column"],
    .test-questions-section .question-item [data-testid*="stColumn"] {
        background: transparent !important;
        background-color: transparent !important;
    }
    
    /* Ensure no spacing creates visible grey bars between questions */
    .test-questions-section .question-item + .question-item {
        margin-top: 0 !important;
        padding-top: 0 !important;
    }
</style>
""", unsafe_allow_html=True)

# Initialize session state
# Initialize session state with persistent data
if 'runs' not in st.session_state:
    st.session_state.runs = load_runs()
if 'current_run' not in st.session_state:
    # Find the most recent running job as current run
    running_runs = [r for r in st.session_state.runs if r.get('status') == 'running']
    st.session_state.current_run = running_runs[0] if running_runs else None
if 'show_create_modal' not in st.session_state:
    st.session_state.show_create_modal = False
if 'fallback_models' not in st.session_state:
    st.session_state.fallback_models = [""]
if 'questions' not in st.session_state:
    st.session_state.questions = [{"number": "Q1", "text": "", "expectedAnswer": ""}]
# Removed pdf_directory_path - PDFs must be uploaded via file uploader only

# Helpers for dynamic add/remove without HTML/JS handlers
def add_question():
    qs = st.session_state.get('questions', [])
    new_num = len(qs) + 1
    qs.append({"number": f"Q{new_num}", "text": "", "expectedAnswer": ""})
    st.session_state.questions = qs


def remove_question(idx: int):
    qs = st.session_state.get('questions', [])
    if len(qs) > 1 and 0 <= idx < len(qs):
        qs.pop(idx)
        for i, q in enumerate(qs):
            q["number"] = f"Q{i+1}"
        st.session_state.questions = qs


def add_fallback():
    fbs = st.session_state.get('fallback_models', [""])
    if len(fbs) < 5:
        fbs.append("")
    st.session_state.fallback_models = fbs

# Load local default YAML template (optional, for local testing)
@st.cache_data
def load_local_default_yaml():
    """Load local default YAML from config/default.yaml (optional, for local testing)"""
    local_yaml = get_config_dir() / "default.yaml"
    if local_yaml.exists():
        try:
            with open(local_yaml, 'r') as f:
                loaded = yaml.safe_load(f)
                if loaded and isinstance(loaded, dict):
                    return loaded
        except Exception as e:
            return None
    return None

# Load local default YAML - will be cached by Streamlit
# This is optional - if it doesn't exist, YAML upload will be required
local_default_yaml = load_local_default_yaml()

def build_gemini_instructions_preview(template, refinement_stage, refinement_stages_config, custom_instructions, primary_model, fallback_models):
    """Build a preview of the full Gemini instructions that will be sent, with all known variables substituted"""
    import re
    
    if not template or not refinement_stages_config:
        return "Template or refinement stages not available"
    
    stage_config = refinement_stages_config.get(refinement_stage, {})
    if not stage_config:
        return f"Refinement stage '{refinement_stage}' not found in configuration"
    
    # Get stage-specific values
    refinement_stage_description = stage_config.get('description', '')
    refinement_stage_focus = stage_config.get('focus', '')
    root_cause_guidance = stage_config.get('rootCauseGuidance', '')
    modification_guidance = stage_config.get('modificationGuidance', '')
    
    # Build stage-specific task and focus type
    if refinement_stage == "llm_parser":
        refinement_stage_task = "LLM Parser Optimization"
        refinement_stage_focus_type = "LLM Parser improvements"
        proposed_llm_parser_description = "COMPLETE FULL TEXT of the improved LLM Parser Prompt..."
        proposed_response_prompt_description = "If LLM parser is maximized, provide COMPLETE FULL TEXT..."
    elif refinement_stage == "response_prompt":
        refinement_stage_task = "Response Prompt Template Optimization"
        refinement_stage_focus_type = "Response Prompt Template improvements"
        proposed_llm_parser_description = "Return the current LLM Parser Prompt unchanged..."
        proposed_response_prompt_description = "COMPLETE FULL TEXT of the improved Response Prompt Template..."
    elif refinement_stage == "agentforce_agent":
        refinement_stage_task = "Agentforce Agent Optimization"
        refinement_stage_focus_type = "Agentforce Agent improvements"
        proposed_llm_parser_description = "Return the current LLM Parser Prompt unchanged..."
        proposed_response_prompt_description = "Return the current Response Prompt Template unchanged..."
    else:
        refinement_stage_task = "Unknown"
        refinement_stage_focus_type = "Unknown"
        proposed_llm_parser_description = ""
        proposed_response_prompt_description = ""
    
    # Build available models list
    all_models = [primary_model] + fallback_models
    available_models_text = '\n'.join([f"- {m}" for m in all_models if m])
    
    # Build output format section based on refinement stage
    if refinement_stage == "response_prompt":
        output_format_section = f"""After the array, include a separate JSON object with the proposed prompts:

{{
  "LLM_Parser_Prompt_Proposed_from_Gemini": "{proposed_llm_parser_description}",
  "Prompt_Builder_Prompt_Proposed_from_Gemini": "{proposed_response_prompt_description}",
  "StageStatus": "optimized" or "needs_improvement",
  "StageCompleteReason": "Brief explanation of why this stage is complete or needs more work"
}}"""
        output_format_important = "- The proposed Response Prompt Template should also be the complete template text, ready to use."
    elif refinement_stage == "agentforce_agent":
        output_format_section = f"""After the array, include a separate JSON object with the proposed agent configuration:

{{
  "LLM_Parser_Prompt_Proposed_from_Gemini": "{proposed_llm_parser_description}",
  "Prompt_Builder_Prompt_Proposed_from_Gemini": "{proposed_response_prompt_description}",
  "Agentforce_Agent_Configuration_Proposed_from_Gemini": "COMPLETE configuration for the Agentforce agent...",
  "StageStatus": "optimized" or "needs_improvement",
  "StageCompleteReason": "Brief explanation of why this stage is complete or needs more work"
}}"""
        output_format_important = "- The proposed Agentforce Agent configuration should be complete and ready to use."
    else:
        output_format_section = f"""After the array, include a separate JSON object with the proposed prompt:

{{
  "LLM_Parser_Prompt_Proposed_from_Gemini": "{proposed_llm_parser_description}",
  "StageStatus": "optimized" or "needs_improvement",
  "StageCompleteReason": "Brief explanation of why this stage is complete or needs more work"
}}"""
        output_format_important = ""
    
    # Start building the prompt
    prompt = template
    
    # Substitute all known variables
    prompt = prompt.replace('{{REFINEMENT_STAGE}}', refinement_stage)
    prompt = prompt.replace('{{REFINEMENT_STAGE_DESCRIPTION}}', refinement_stage_description)
    prompt = prompt.replace('{{REFINEMENT_STAGE_FOCUS}}', refinement_stage_focus)
    prompt = prompt.replace('{{REFINEMENT_STAGE_TASK}}', refinement_stage_task)
    prompt = prompt.replace('{{REFINEMENT_STAGE_FOCUS_TYPE}}', refinement_stage_focus_type)
    prompt = prompt.replace('{{ROOT_CAUSE_GUIDANCE}}', root_cause_guidance)
    prompt = prompt.replace('{{MODIFICATION_GUIDANCE}}', modification_guidance)
    prompt = re.sub(r'{{OUTPUT_FORMAT_SECTION}}', output_format_section, prompt)
    prompt = prompt.replace('{{OUTPUT_FORMAT_IMPORTANT}}', output_format_important)
    prompt = prompt.replace('{{RESPONSE_MODEL}}', primary_model or "Not selected")
    prompt = prompt.replace('{{AVAILABLE_MODELS}}', available_models_text)
    
    # Handle custom instructions
    if custom_instructions and custom_instructions.strip():
        prompt = prompt.replace('{{CUSTOM_INSTRUCTIONS}}', custom_instructions.strip())
    else:
        # Remove CUSTOM INSTRUCTIONS section
        prompt = re.sub(r'# CUSTOM INSTRUCTIONS\s*\n\s*{{CUSTOM_INSTRUCTIONS}}\s*\n\s*---\s*\n\s*\n', '', prompt)
        prompt = re.sub(r'# CUSTOM INSTRUCTIONS\s*\n\s*{{CUSTOM_INSTRUCTIONS}}\s*\n\s*---\s*', '', prompt)
        prompt = prompt.replace('{{CUSTOM_INSTRUCTIONS}}', '')
    
    # Placeholders for runtime values (from Salesforce/Excel)
    prompt = prompt.replace('{{LLM_PARSER_PROMPT}}', '[LLM Parser Prompt - Will be retrieved from Salesforce Search Index at runtime]')
    prompt = prompt.replace('{{RESPONSE_PROMPT_TEMPLATE}}', '[Response Prompt Template - Will be retrieved from Salesforce Prompt Builder at runtime]')
    prompt = prompt.replace('{{WORKSHEET_TEXT}}', '[Worksheet Text - Will be generated from Excel test results at runtime]')
    
    return prompt

def progress_callback(status_dict):
    """Update progress in session state and capture output lines (called from background thread)"""
    # Load runs from file (since we're in a background thread, can't use session_state reliably)
    runs = load_runs()
    
    # Try to get run_id from status_dict or find the most recent running run
    run_id = status_dict.get('run_id')
    if not run_id:
        # Find the most recent running run
        running_runs = [r for r in runs if r.get('status') == 'running']
        if running_runs:
            run_id = running_runs[-1].get('run_id')
    
    # If we have a run_id, try to find the matching run
    found_run = None
    if run_id:
        # Find and update the run (try exact match first, then partial match)
        for run in runs:
            if run.get('run_id') == run_id:
                found_run = run
                break
        
        # If not found, try partial match (in case run_id format differs)
        if not found_run:
            for run in runs:
                run_id_from_run = run.get('run_id', '')
                # Check if run_id starts with the same prefix (e.g., "run_20260102_170701")
                # Handle both cases: status_dict has full ID, or runs_data has full ID
                prefix_length = 18  # "run_YYYYMMDD_HHMMSS" = 18 chars
                status_prefix = run_id[:prefix_length] if len(run_id) >= prefix_length else run_id
                run_prefix = run_id_from_run[:prefix_length] if len(run_id_from_run) >= prefix_length else run_id_from_run
                
                if status_prefix == run_prefix:
                    found_run = run
                    # Keep the run_id from the run (don't update it, just use it for matching)
                    # But update the status_dict to use the correct run_id for future callbacks
                    status_dict['run_id'] = run.get('run_id')
                    break
        
        if found_run:
            run = found_run
            run['progress'] = status_dict
            
            # Update heartbeat on every progress update
            run['heartbeat_at'] = datetime.now()
            
            # Store output lines for live display
            if 'output_lines' not in run:
                run['output_lines'] = []
            
            # Always generate descriptive message from status (even if message field exists)
            status = status_dict.get('status', '')
            cycle = status_dict.get('cycle', 0)
            step = status_dict.get('step', 0)
            stage_status = status_dict.get('stage_status', '')
            
            # Create descriptive messages based on status
            step_names = {
                1: 'Updating Search Index',
                2: 'Testing Index & Invoking Prompts',
                3: 'Analyzing Results with Gemini'
            }
            
            # Generate message based on status
            if status == 'starting':
                message = 'Initializing workflow...'
            elif status == 'cycle_start':
                message = f'Starting Cycle {cycle} - Beginning refinement cycle'
            elif status == 'step_complete':
                step_name = step_names.get(step, f'Step {step}')
                if step == 1:
                    message = f'Cycle {cycle} - Step 1 Complete: Search Index updated and rebuilt'
                elif step == 2:
                    message = f'Cycle {cycle} - Step 2 Complete: Test sheet created with prompt responses'
                    # Save Excel file to database immediately after Step 2 completes
                    excel_file_path = status_dict.get('excel_file')
                    if excel_file_path and os.path.exists(excel_file_path):
                        try:
                            save_excel_to_db(run.get('run_id'), excel_file_path)
                            run['excel_file_path'] = excel_file_path
                            print(f"[PROGRESS_CALLBACK] Saved Excel file to DB: {excel_file_path}", flush=True)
                        except Exception as e:
                            print(f"[PROGRESS_CALLBACK] Error saving Excel to DB: {e}", flush=True)
                elif step == 3:
                    message = f'Cycle {cycle} - Step 3 Complete: Gemini analysis finished'
                    if stage_status:
                        message += f' (Stage Status: {stage_status})'
                    # Save Excel file to database after Step 3 (it's been updated with analysis results)
                    excel_file_path = status_dict.get('excel_file')
                    if excel_file_path and os.path.exists(excel_file_path):
                        try:
                            save_excel_to_db(run.get('run_id'), excel_file_path)
                            run['excel_file_path'] = excel_file_path
                            print(f"[PROGRESS_CALLBACK] Updated Excel file in DB after Step 3: {excel_file_path}", flush=True)
                        except Exception as e:
                            print(f"[PROGRESS_CALLBACK] Error updating Excel in DB after Step 3: {e}", flush=True)
                else:
                    message = f'Cycle {cycle} - {step_name} Complete'
            elif status == 'complete':
                message = f'Workflow Complete! Completed {cycle} cycle(s)'
                if stage_status:
                    message += f' (Final Stage Status: {stage_status})'
            elif status == 'error':
                # Handle error status - critical failures
                error_msg = status_dict.get('message', status_dict.get('error', 'Unknown error'))
                error_details = status_dict.get('error', '')
                if error_details and error_details != error_msg:
                    message = f'❌ ERROR: {error_msg} (Details: {error_details})'
                else:
                    message = f'❌ ERROR: {error_msg}'
                # Mark run as failed
                run['status'] = 'failed'
                run['error'] = error_msg
                if error_details:
                    run['error_details'] = error_details
            else:
                # Use provided message or generate one
                message = status_dict.get('message', f'Status: {status}')
                if cycle > 0 and 'Cycle' not in message:
                    message += f' (Cycle {cycle})'
                if step > 0 and 'Step' not in message:
                    message += f' (Step {step})'
            
            # Always add message to output lines (even if empty, to track progress)
            timestamp = datetime.now().strftime('%H:%M:%S')
            output_line = f"[{timestamp}] {message}"
            
            # Ensure output_lines list exists and append
            if 'output_lines' not in run:
                run['output_lines'] = []
            run['output_lines'].append(output_line)
            
            # Keep only last 1000 lines to avoid memory issues
            if len(run['output_lines']) > 1000:
                run['output_lines'] = run['output_lines'][-1000:]
            
            # Always save to file after updating (for real-time updates)
            save_runs(runs)
            
            # Debug: Print to console (will show in Streamlit logs)
            print(f"[PROGRESS_CALLBACK] Updated run {run.get('run_id')}: {output_line}")
    else:
        # If no run_id match found, try to update the most recent running run as fallback
        # This handles cases where run_id format doesn't match (e.g., old runs)
        running_runs = [r for r in runs if r.get('status') == 'running']
        if running_runs:
            found_run = running_runs[-1]  # Use most recent running run
            found_run['progress'] = status_dict
            
            # Generate message and add to output
            status = status_dict.get('status', '')
            cycle = status_dict.get('cycle', 0)
            step = status_dict.get('step', 0)
            message = status_dict.get('message', f'Status: {status} (Cycle {cycle}, Step {step})')
            
            if 'output_lines' not in found_run:
                found_run['output_lines'] = []
            
            timestamp = datetime.now().strftime('%H:%M:%S')
            output_line = f"[{timestamp}] {message}"
            found_run['output_lines'].append(output_line)
            
            if len(found_run['output_lines']) > 1000:
                found_run['output_lines'] = found_run['output_lines'][-1000:]
            
            save_runs(runs)
            print(f"[PROGRESS_CALLBACK] Updated fallback run {found_run.get('run_id')}: {output_line}")

# JavaScript components - directory picker
directory_picker_js = """
<script>
(function() {
    // Create directory input if it doesn't exist
    let input = document.getElementById('hidden-pdf-directory-input');
    if (!input) {
        input = document.createElement('input');
        input.type = 'file';
        input.setAttribute('webkitdirectory', '');
        input.setAttribute('directory', '');
        input.setAttribute('multiple', '');
        input.style.display = 'none';
        input.id = 'hidden-pdf-directory-input';
        document.body.appendChild(input);
    }
    
    // Make function globally accessible
    window.selectPDFDirectory = function() {
        console.log('selectPDFDirectory called');
        if (input) {
            input.click();
        }
    };
    
    input.onchange = function(e) {
        console.log('Directory selected', e.target.files);
        if (e.target.files && e.target.files.length > 0) {
            const path = e.target.files[0].webkitRelativePath;
            const directory = path.substring(0, path.indexOf('/'));
            const pdfCount = Array.from(e.target.files).filter(f => f.name.endsWith('.pdf')).length;
            
            console.log('Directory:', directory, 'PDFs:', pdfCount);
            
            // Find PDF Directory input field
            const textInputs = Array.from(document.querySelectorAll('input[type="text"]'));
            textInputs.forEach(ti => {
                const container = ti.closest('[data-testid*="stTextInput"]');
                if (container) {
                    const label = container.querySelector('label');
                    if (label && label.textContent.includes('PDF Directory')) {
                        ti.value = directory;
                        ti.dispatchEvent(new Event('input', { bubbles: true }));
                        ti.dispatchEvent(new Event('change', { bubbles: true }));
                        
                        // Update help text
                        const caption = container.nextElementSibling;
                        if (caption && caption.querySelector('[data-testid*="stCaption"]')) {
                            caption.querySelector('[data-testid*="stCaption"]').innerHTML = 
                                `<small style="color: green;"><i class="bi bi-check-circle"></i> Selected directory with <strong>${pdfCount}</strong> PDF file(s)</small>`;
                        }
                    }
                }
            });
        }
    };
    
    // Re-initialize on Streamlit reruns
    setTimeout(() => {
        const buttons = document.querySelectorAll('button[onclick*="selectPDFDirectory"]');
        buttons.forEach(btn => {
            btn.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();
                window.selectPDFDirectory();
            };
        });
    }, 500);
})();
</script>
"""

drag_drop_js = """
<script>
(function() {
    function initDragDrop() {
        const container = document.getElementById('fallback-models-container');
        if (!container) return;
        
        const items = container.querySelectorAll('.fallback-item');
        let draggedElement = null;
        
        items.forEach((item) => {
            item.draggable = true;
            
            item.addEventListener('dragstart', function(e) {
                draggedElement = this;
                this.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
            });
            
            item.addEventListener('dragover', function(e) {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                
                const afterElement = getDragAfterElement(container, e.clientY);
                if (afterElement == null) {
                    container.appendChild(draggedElement);
                } else {
                    container.insertBefore(draggedElement, afterElement);
                }
            });
            
            item.addEventListener('drop', function(e) {
                e.preventDefault();
                this.classList.remove('dragging');
                updatePriorityBadges();
            });
            
            item.addEventListener('dragend', function() {
                this.classList.remove('dragging');
                updatePriorityBadges();
            });
        });
    }
    
    function getDragAfterElement(container, y) {
        const draggableElements = [...container.querySelectorAll('.fallback-item:not(.dragging)')];
        return draggableElements.reduce((closest, child) => {
            const box = child.getBoundingClientRect();
            const offset = y - box.top - box.height / 2;
            if (offset < 0 && offset > closest.offset) {
                return { offset: offset, element: child };
            } else {
                return closest;
            }
        }, { offset: Number.NEGATIVE_INFINITY }).element;
    }
    
    function updatePriorityBadges() {
        const items = document.querySelectorAll('.fallback-item');
        items.forEach((item, index) => {
            const badge = item.querySelector('.priority-badge');
            if (badge) {
                badge.innerHTML = `<i class="bi bi-grip-vertical"></i> ${index + 1}`;
            }
        });
    }
    
    setTimeout(initDragDrop, 500);
    const observer = new MutationObserver(() => setTimeout(initDragDrop, 500));
    observer.observe(document.body, { childList: true, subtree: true });
})();
</script>
"""

button_handler_js = """
<script>
(function() {
    // Get the parent window (components.html runs in iframe)
    const parentWindow = window.parent !== window ? window.parent : window;
    const parentDoc = parentWindow.document;
    
    // Clean URL after a delay if it has query params (prevents infinite loops)
    if (parentWindow.location.search.includes('add_question') || 
        parentWindow.location.search.includes('remove_question_index') || 
        parentWindow.location.search.includes('add_fallback')) {
        setTimeout(function() {
            if (parentWindow.location.search) {
                parentWindow.history.replaceState({}, '', parentWindow.location.pathname);
            }
        }, 2000);
    }
    
    function attachButtonHandlers() {
        // Handle Add Question button
        parentDoc.querySelectorAll('button[data-action="add_question"]').forEach(btn => {
            if (!btn.dataset.handlerAttached) {
                btn.dataset.handlerAttached = 'true';
                btn.addEventListener('click', function(e) {
                    e.preventDefault();
                    e.stopPropagation();
                    const timestamp = Date.now();
                    parentWindow.location.href = parentWindow.location.pathname + '?add_question=1&t=' + timestamp;
                }, true); // Use capture phase
            }
        });
        
        // Handle Remove Question buttons
        parentDoc.querySelectorAll('button[data-action="remove_question"]').forEach(btn => {
            if (!btn.dataset.handlerAttached) {
                btn.dataset.handlerAttached = 'true';
                const index = btn.getAttribute('data-question-index');
                if (index !== null) {
                    btn.addEventListener('click', function(e) {
                        e.preventDefault();
                        e.stopPropagation();
                        const timestamp = Date.now();
                        parentWindow.location.href = parentWindow.location.pathname + '?remove_question_index=' + index + '&t=' + timestamp;
                    }, true);
                }
            }
        });
        
        // Handle Add Fallback Model button
        parentDoc.querySelectorAll('button').forEach(btn => {
            if (btn.textContent.includes('Add Fallback Model') && !btn.dataset.handlerAttached) {
                btn.dataset.handlerAttached = 'true';
                btn.addEventListener('click', function(e) {
                    e.preventDefault();
                    e.stopPropagation();
                    parentWindow.location.href = parentWindow.location.pathname + '?add_fallback=1&t=' + Date.now();
                }, true);
            }
        });
    }
    
    // Run immediately and repeatedly
    attachButtonHandlers();
    setTimeout(attachButtonHandlers, 100);
    setTimeout(attachButtonHandlers, 500);
    setTimeout(attachButtonHandlers, 1000);
    setTimeout(attachButtonHandlers, 2000);
    
    // Use MutationObserver on parent document
    if (parentDoc.body) {
        const observer = new MutationObserver(function() {
            setTimeout(attachButtonHandlers, 100);
        });
        observer.observe(parentDoc.body, { 
            childList: true, 
            subtree: true 
        });
    }
    
    // Listen for parent window load
    if (parentWindow.addEventListener) {
        parentWindow.addEventListener('load', attachButtonHandlers);
    }
})();
</script>
"""

# Sidebar Navigation
with st.sidebar:
    st.markdown("""
    <div style="padding: 1rem 0;">
        <h1 style="color: #FF4B4B; font-size: 1.5rem; margin-bottom: 0.25rem; display: flex; align-items: center; gap: 0.5rem;">
            <i class="bi bi-rocket-takeoff"></i> Prompt Optimization
        </h1>
        <p style="color: #666; font-size: 0.875rem; margin-bottom: 2rem;">Automated RAG Workflow</p>
    </div>
    """, unsafe_allow_html=True)
    
    page = st.radio(
        "Navigation",
        ["Create New Run", "Jobs"],
        label_visibility="collapsed",
        key="nav_radio"
    )

# Main Content
if page == "Create New Run":
    # Only generate a new page ID if no YAML is loaded (to force fresh widgets)
    # If YAML is loaded, keep the same page ID so widgets can access session state values
    import time
    import random
    has_yaml_loaded = 'uploaded_yaml_data' in st.session_state and st.session_state.get('uploaded_yaml_data') is not None
    
    if not has_yaml_loaded:
        # No YAML loaded - generate new page ID to force fresh widgets
        st.session_state.create_run_page_id = f"{time.time()}_{random.randint(1000,9999)}"
    else:
        # YAML loaded - keep existing page ID or create one if it doesn't exist
        if 'create_run_page_id' not in st.session_state:
            st.session_state.create_run_page_id = f"{time.time()}_{random.randint(1000,9999)}"
    if not has_yaml_loaded:
        # Clear ALL form-related session state keys BEFORE rendering widgets
        # This ensures fields start blank unless YAML is actively being uploaded
        keys_to_delete = [key for key in list(st.session_state.keys()) if key.startswith('form_')]
        for key in keys_to_delete:
            del st.session_state[key]
        # Only reset fallback models and questions if they don't exist yet
        # Don't reset if user has already added items via buttons
        if 'fallback_models' not in st.session_state:
            st.session_state.fallback_models = [""]
        if 'questions' not in st.session_state:
            st.session_state.questions = [{"number": "Q1", "text": "", "expectedAnswer": ""}]
    
    # Page Header
    st.markdown("""
    <div style="margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 2px solid #E6E9EF;">
        <h2 style="font-size: 2rem; font-weight: 700; color: #262730; display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem;">
            <i class="bi bi-rocket-takeoff"></i> Create New Optimization Run
        </h2>
        <p style="font-size: 1rem; color: #666;">Configure and start a new optimization workflow</p>
    </div>
    """, unsafe_allow_html=True)
    
    # YAML File Uploader (at the top, outside form)
    st.markdown("### 📄 Upload YAML Configuration (Optional)")
    uploaded_yaml = st.file_uploader(
        "Upload YAML file to pre-fill form",
        type=['yaml', 'yml'],
        key="yaml_uploader",
        help="Upload a YAML configuration file to automatically fill in all form fields. You can still edit the values after uploading."
    )
    
    # Parse uploaded YAML and store in session state
    uploaded_yaml_data = None
    if uploaded_yaml:
        # Check if we've already processed this YAML file to prevent infinite rerun loop
        yaml_file_id = f"{uploaded_yaml.name}_{uploaded_yaml.size if hasattr(uploaded_yaml, 'size') else 'unknown'}"
        already_processed = st.session_state.get('last_processed_yaml_id') == yaml_file_id
        
        if not already_processed:
            try:
                uploaded_yaml_data = yaml.safe_load(uploaded_yaml)
                st.session_state.uploaded_yaml_data = uploaded_yaml_data
                st.session_state.last_processed_yaml_id = yaml_file_id
                
                # Extract ALL config and populate ALL form field session state from YAML
                config = uploaded_yaml_data.get('configuration', {})
                
                # Salesforce config
                salesforce_config = config.get('salesforce', {})
                if salesforce_config:
                    username_val = salesforce_config.get('username', "")
                    password_val = salesforce_config.get('password', "")
                    instance_val = salesforce_config.get('instanceUrl', "")
                    st.session_state.form_username = username_val
                    st.session_state.form_password = password_val
                    st.session_state.form_instance = instance_val
                
                # Search Index & Prompt Template
                search_idx_val = config.get('searchIndexId', "")
                prompt_tmpl_val = config.get('promptTemplateApiName', "")
                st.session_state.form_search_index = search_idx_val
                st.session_state.form_prompt_template = prompt_tmpl_val
                refinement_stage_from_yaml = config.get('refinementStage', "")
                if refinement_stage_from_yaml:
                    st.session_state.form_refinement_stage = refinement_stage_from_yaml
                else:
                    st.session_state.form_refinement_stage = "llm_parser"  # Default for selectbox
                
                # Gemini Model
                st.session_state.form_gemini_model = config.get('geminiModel', "gemini-2.5-pro")
                
                # Prompt Builder Models
                prompt_builder_models = config.get('prompt_builder_models', {})
                st.session_state.form_primary_model = prompt_builder_models.get('primary', "")
                fallback_models = prompt_builder_models.get('fallbacks', [])
                if fallback_models:
                    st.session_state.fallback_models = fallback_models
                else:
                    st.session_state.fallback_models = [""]
                
                # Test Questions - check both root level 'questions' and config level 'testQuestions'
                test_questions = uploaded_yaml_data.get('questions', []) or config.get('testQuestions', [])
                if test_questions:
                    st.session_state.questions = test_questions
                    # Initialize widget session state keys for all questions from YAML
                    for i, q in enumerate(test_questions):
                        q_num_key = f"form_q_num_{i}"
                        q_text_key = f"form_q_text_{i}"
                        q_expected_key = f"form_q_expected_{i}"
                        if q.get("number"):
                            st.session_state[q_num_key] = q["number"]
                        if q.get("text"):
                            st.session_state[q_text_key] = q["text"]
                        if q.get("expectedAnswer"):
                            st.session_state[q_expected_key] = q["expectedAnswer"]
                else:
                    st.session_state.questions = [{"number": "Q1", "text": "", "expectedAnswer": ""}]
                
                # Custom Instructions
                st.session_state.form_custom_instructions = config.get('customInstructions', "")
                
                # Mark that we've loaded from YAML to avoid re-clearing later
                st.session_state.yaml_prefilled = True
                
                st.success(f"✅ YAML file loaded: {uploaded_yaml.name}")
                # Force rerun to update form fields with YAML data (only once per file)
                st.rerun()
            except Exception as e:
                st.error(f"❌ Error parsing YAML file: {e}")
                uploaded_yaml_data = None
        else:
            # YAML already processed, use existing data
            uploaded_yaml_data = st.session_state.uploaded_yaml_data
    elif 'uploaded_yaml_data' in st.session_state:
        uploaded_yaml_data = st.session_state.uploaded_yaml_data
        # If we have YAML stored but haven't yet applied it to form fields this session, do it now
        if uploaded_yaml_data and not st.session_state.get('yaml_prefilled'):
            config = uploaded_yaml_data.get('configuration', {})
            salesforce_config = config.get('salesforce', {})
            if salesforce_config:
                st.session_state.form_username = salesforce_config.get('username', "")
                st.session_state.form_password = salesforce_config.get('password', "")
                st.session_state.form_instance = salesforce_config.get('instanceUrl', "")
            st.session_state.form_search_index = config.get('searchIndexId', "")
            st.session_state.form_prompt_template = config.get('promptTemplateApiName', "")
            st.session_state.form_refinement_stage = config.get('refinementStage', "llm_parser")
            st.session_state.form_gemini_model = config.get('geminiModel', "gemini-2.5-pro")
            prompt_builder_models = config.get('prompt_builder_models', {})
            st.session_state.form_primary_model = prompt_builder_models.get('primary', "")
            fallback_models = prompt_builder_models.get('fallbacks', [])
            st.session_state.fallback_models = fallback_models if fallback_models else [""]
            # Initialize fallback model widget keys
            for i, model in enumerate(fallback_models):
                widget_key = f"form_fallback_{i}"
                if widget_key not in st.session_state and model:
                    st.session_state[widget_key] = model
            # Check both root level 'questions' and config level 'testQuestions'
            test_questions = uploaded_yaml_data.get('questions', []) or config.get('testQuestions', [])
            st.session_state.questions = test_questions if test_questions else [{"number": "Q1", "text": "", "expectedAnswer": ""}]
            # Initialize question widget keys
            if test_questions:
                for i, q in enumerate(test_questions):
                    q_num_key = f"form_q_num_{i}"
                    q_text_key = f"form_q_text_{i}"
                    q_expected_key = f"form_q_expected_{i}"
                    if q.get("number"):
                        st.session_state[q_num_key] = q["number"]
                    if q.get("text"):
                        st.session_state[q_text_key] = q["text"]
                    if q.get("expectedAnswer"):
                        st.session_state[q_expected_key] = q["expectedAnswer"]
            st.session_state.form_custom_instructions = config.get('customInstructions', "")
            st.session_state.yaml_prefilled = True
    
    # Use uploaded YAML or local default YAML for template (if available)
    yaml_for_template = uploaded_yaml_data if uploaded_yaml_data else local_default_yaml
    
    # Handle button actions via URL params (outside form)
    # Use timestamp to prevent duplicate processing
    if 'add_fallback' in st.query_params:
        timestamp = st.query_params.get('t', '')
        last_timestamp = st.session_state.get('last_add_fallback_t', '')
        if timestamp != last_timestamp:
            if len(st.session_state.fallback_models) < 5:
                st.session_state.fallback_models.append("")
            st.session_state.last_add_fallback_t = timestamp
            st.rerun()
    
    if 'remove_fallback' in st.query_params:
        timestamp = st.query_params.get('t', '')
        last_timestamp = st.session_state.get('last_remove_fallback_t', '')
        if timestamp != last_timestamp:
            if st.session_state.fallback_models:
                st.session_state.fallback_models.pop()
            st.session_state.last_remove_fallback_t = timestamp
            st.rerun()
    
    
    # Directory picker will be in the button component
    components.html(drag_drop_js, height=0)
    
    # Create Run Form (always visible on this page, not a modal)
    with st.form("create_run_form", clear_on_submit=True):
            # 1. Salesforce Configuration
            st.markdown('<div class="config-section">', unsafe_allow_html=True)
            st.markdown('<h3 class="section-title"><i class="bi bi-cloud"></i> Salesforce Configuration</h3>', unsafe_allow_html=True)
            
            # Get form field values from session state only (populated by YAML upload or user input)
            # All fields start blank unless YAML was uploaded
            username_value = st.session_state.get('form_username', "")
            password_value = st.session_state.get('form_password', "")
            instance_url_value = st.session_state.get('form_instance', "")
            
            col1, col2 = st.columns(2)
            with col1:
                username = st.text_input(
                    "Username", 
                    value=username_value, 
                    key="form_username",
                    placeholder="Enter Salesforce username"
                )
            with col2:
                password = st.text_input(
                    "Password", 
                    type="password", 
                    value=password_value, 
                    key="form_password",
                    placeholder="Enter Salesforce password"
                )
            instance_url = st.text_input(
                "Instance URL", 
                value=instance_url_value, 
                key="form_instance",
                placeholder="https://your-instance.salesforce.com"
            )
            st.markdown('</div>', unsafe_allow_html=True)
            
            # 2. Search Index & Prompt Template
            st.markdown('<div class="config-section">', unsafe_allow_html=True)
            st.markdown('<h3 class="section-title"><i class="bi bi-search"></i> Search Index & Prompt Template</h3>', unsafe_allow_html=True)
            
            # Get values from session state only (populated by YAML upload or user input)
            # Since we cleared session state above if no YAML, these will be empty unless YAML was loaded
            search_index_value = st.session_state.get('form_search_index', "")
            prompt_template_value = st.session_state.get('form_prompt_template', "")
            refinement_stage_value = st.session_state.get('form_refinement_stage', "llm_parser")  # Default for selectbox only
            refinement_stage_index = ["llm_parser", "response_prompt", "agentforce_agent"].index(refinement_stage_value) if refinement_stage_value in ["llm_parser", "response_prompt", "agentforce_agent"] else 0
            
            # Check if YAML was uploaded THIS run - if so, use that value, otherwise force empty
            has_yaml_now = 'uploaded_yaml_data' in st.session_state and st.session_state.get('uploaded_yaml_data') is not None
            
            col1, col2 = st.columns(2)
            with col1:
                # Get value from session state (set by YAML or user input)
                search_index_default = st.session_state.get('form_search_index', "")
                # Use stable key when YAML is loaded, dynamic key when not (to force fresh widget)
                if has_yaml_now:
                    widget_key = "form_search_index"  # Stable key - widget will use session state value
                else:
                    widget_key = f"search_idx_{st.session_state.create_run_page_id}"  # Dynamic key - fresh widget
                
                search_index_id = st.text_input(
                    "Search Index ID", 
                    value=search_index_default, 
                    key=widget_key,
                    placeholder="Enter Search Index ID"
                )
                # Only store in main key if widget key is different (dynamic key case)
                # If widget_key is "form_search_index", Streamlit manages it automatically
                if widget_key != "form_search_index":
                    st.session_state.form_search_index = search_index_id
                st.caption("Salesforce record ID of the Search Index")
            with col2:
                prompt_template_default = st.session_state.get('form_prompt_template', "")
                
                # Use stable key when YAML is loaded, dynamic key when not
                if has_yaml_now:
                    widget_key_template = "form_prompt_template"  # Stable key
                else:
                    widget_key_template = f"prompt_tmpl_{st.session_state.create_run_page_id}"  # Dynamic key
                
                prompt_template_api_name = st.text_input(
                    "Prompt Template API Name", 
                    value=prompt_template_default, 
                    key=widget_key_template,
                    placeholder="Enter Prompt Template API Name"
                )
                # Only store in main key if widget key is different (dynamic key case)
                # If widget_key_template is "form_prompt_template", Streamlit manages it automatically
                if widget_key_template != "form_prompt_template":
                    st.session_state.form_prompt_template = prompt_template_api_name
                st.caption("DeveloperName (with underscores)")
            
            # Refinement stage (selectbox needs a default)
            refinement_stage_value = st.session_state.get('form_refinement_stage', "llm_parser") if has_yaml_now else "llm_parser"
            refinement_stage_index = ["llm_parser", "response_prompt", "agentforce_agent"].index(refinement_stage_value) if refinement_stage_value in ["llm_parser", "response_prompt", "agentforce_agent"] else 0
            refinement_stage = st.selectbox(
                "Refinement Stage",
                ["llm_parser", "response_prompt", "agentforce_agent"],
                index=refinement_stage_index,
                key="form_refinement_stage"
            )
            st.markdown('</div>', unsafe_allow_html=True)
            
            # 3. PDF Files (NOT from YAML - must be uploaded)
            st.markdown('<div class="config-section">', unsafe_allow_html=True)
            st.markdown('<h3 class="section-title"><i class="bi bi-file-pdf"></i> PDF Files</h3>', unsafe_allow_html=True)
            
            # Initialize pdf_directory variable
            pdf_directory = ""
            
            # File uploader for PDFs (primary method - files must be uploaded to server)
            uploaded_pdfs = st.file_uploader(
                "Upload PDF Files",
                type=['pdf'],
                accept_multiple_files=True,
                key="form_pdf_upload",
                help="Upload one or more PDF files that will be used as context for the optimization workflow. Files will be saved to the server."
            )
            
            # Store uploaded files and create directory path
            if uploaded_pdfs and len(uploaded_pdfs) > 0:
                # Initialize uploads directory in session state
                if 'uploaded_pdf_dir' not in st.session_state:
                    from pathlib import Path
                    
                    # Create a persistent uploads directory (not temp - survives server restarts)
                    # Store in app_data/uploads/ (persistent, relative to script)
                    uploads_dir = get_app_data_dir() / "uploads"
                    uploads_dir.mkdir(parents=True, exist_ok=True)
                    
                    st.session_state.uploaded_pdf_dir = str(uploads_dir)
                    st.session_state.uploaded_pdf_files = []
                
                # Save uploaded files to the persistent directory
                saved_files = []
                for uploaded_file in uploaded_pdfs:
                    file_path = Path(st.session_state.uploaded_pdf_dir) / uploaded_file.name
                    with open(file_path, 'wb') as f:
                        f.write(uploaded_file.getbuffer())
                    saved_files.append(str(file_path))
                
                st.session_state.uploaded_pdf_files = saved_files
                pdf_directory = st.session_state.uploaded_pdf_dir
                
                st.success(f"✅ Uploaded {len(uploaded_pdfs)} PDF file(s): {', '.join([f.name for f in uploaded_pdfs])}")
                
                # Note: PDFs will be saved to database when job is created
            
            st.caption("**Note:** PDF files must be uploaded to the server using the file uploader above.")
            st.markdown('</div>', unsafe_allow_html=True)
            
            # 4. Gemini Analysis Model
            st.markdown('<div class="config-section">', unsafe_allow_html=True)
            st.markdown('<h3 class="section-title"><i class="bi bi-cpu"></i> Gemini Analysis Model</h3>', unsafe_allow_html=True)
            
            gemini_models = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-pro-preview-06-05"]
            gemini_value = st.session_state.get('form_gemini_model', "gemini-2.5-pro")
            gemini_index = gemini_models.index(gemini_value) if gemini_value in gemini_models else 0
            
            gemini_model = st.selectbox(
                "Model",
                gemini_models,
                index=gemini_index,
                key="form_gemini_model",
                format_func=lambda x: {
                    "gemini-2.5-pro": "Gemini 2.5 Pro (Recommended)",
                    "gemini-2.5-flash": "Gemini 2.5 Flash (Fast)",
                    "gemini-2.0-flash": "Gemini 2.0 Flash",
                    "gemini-2.5-pro-preview-06-05": "Gemini 2.5 Pro Preview"
                }.get(x, x)
            )
            st.caption("Model used for analyzing/scoring responses")
            st.markdown('</div>', unsafe_allow_html=True)
            
            # 5. Prompt Builder Models
            st.markdown('<div class="config-section">', unsafe_allow_html=True)
            st.markdown('<h3 class="section-title"><i class="bi bi-diagram-3"></i> Prompt Builder Models</h3>', unsafe_allow_html=True)
            
            # Get prompt builder models from session state only (populated by YAML upload or user input)
            primary_model_value = st.session_state.get('form_primary_model', "")
            # Default to OpenAI GPT-4 if nothing loaded yet
            if not primary_model_value:
                primary_model_value = "sfdc_ai__DefaultOpenAIGPT4"
                st.session_state.form_primary_model = primary_model_value
            
            primary_models_list = [
                "sfdc_ai__DefaultBedrockAnthropicClaude45Sonnet",
                "sfdc_ai__DefaultOpenAIGPT5",
                "sfdc_ai__DefaultOpenAIGPT4",
                "sfdc_ai__DefaultOpenAIGPT4Turbo",
                "sfdc_ai__DefaultOpenAIGPT4OmniMini",
                "sfdc_ai__DefaultAnthropicClaude35Sonnet",
                "sfdc_ai__DefaultAnthropicClaude35Haiku",
                "sfdc_ai__DefaultGoogleGemini25Pro",
                "sfdc_ai__DefaultGoogleGemini3Pro",
                "sfdc_ai__DefaultGoogleGemini15Flash"
            ]
            # Find index of primary model, default to 0 if not found or empty
            primary_model_index = primary_models_list.index(primary_model_value) if primary_model_value and primary_model_value in primary_models_list else 0
            
            primary_model = st.selectbox(
                "Primary Model",
                primary_models_list,
                index=primary_model_index,
                key="form_primary_model",
                format_func=lambda x: {
                    "sfdc_ai__DefaultBedrockAnthropicClaude45Sonnet": "Anthropic Claude 4.5 Sonnet",
                    "sfdc_ai__DefaultOpenAIGPT5": "OpenAI GPT-5",
                    "sfdc_ai__DefaultOpenAIGPT4": "OpenAI GPT-4",
                    "sfdc_ai__DefaultOpenAIGPT4Turbo": "OpenAI GPT-4 Turbo",
                    "sfdc_ai__DefaultOpenAIGPT4OmniMini": "OpenAI GPT-4 Omni Mini",
                    "sfdc_ai__DefaultAnthropicClaude35Sonnet": "Anthropic Claude 3.5 Sonnet",
                    "sfdc_ai__DefaultAnthropicClaude35Haiku": "Anthropic Claude 3.5 Haiku",
                    "sfdc_ai__DefaultGoogleGemini25Pro": "Google Gemini 2.5 Pro",
                    "sfdc_ai__DefaultGoogleGemini3Pro": "Google Gemini 3 Pro",
                    "sfdc_ai__DefaultGoogleGemini15Flash": "Google Gemini 1.5 Flash"
                }.get(x, x)
            )
            st.caption("Primary model used for generating responses")
            
            st.markdown('<label style="font-weight: 600; color: #262730; margin-bottom: 0.5rem; font-size: 0.875rem;">Fallback Models <span class="badge bg-secondary" style="font-size: 0.75rem; padding: 0.25rem 0.5rem; margin-left: 0.5rem;">Priority Order</span></label>', unsafe_allow_html=True)
            st.caption("Models will be tried in order if primary model fails. Drag to reorder priority.")
            
            # Fallback models container
            st.markdown('<div id="fallback-models-container" class="sortable-list">', unsafe_allow_html=True)
            fallback_models = []
            for i, model in enumerate(st.session_state.fallback_models):
                col1, col2, col3 = st.columns([1, 10, 1])
                with col1:
                    st.markdown(f'<div class="fallback-item"><div class="input-group"><span class="priority-badge drag-handle" title="Drag to reorder"><i class="bi bi-grip-vertical"></i> {i+1}</span>', unsafe_allow_html=True)
                with col2:
                    fallback_options = [
                        "",
                        "sfdc_ai__DefaultOpenAIGPT5",
                        "sfdc_ai__DefaultOpenAIGPT4",
                        "sfdc_ai__DefaultOpenAIGPT4Turbo",
                        "sfdc_ai__DefaultOpenAIGPT4OmniMini",
                        "sfdc_ai__DefaultBedrockAnthropicClaude45Sonnet",
                        "sfdc_ai__DefaultAnthropicClaude35Sonnet",
                        "sfdc_ai__DefaultAnthropicClaude35Haiku",
                        "sfdc_ai__DefaultGoogleGemini25Pro",
                        "sfdc_ai__DefaultGoogleGemini3Pro",
                        "sfdc_ai__DefaultGoogleGemini15Flash"
                    ]
                    # Initialize widget's session state key from fallback_models if not already set
                    widget_key = f"form_fallback_{i}"
                    if widget_key not in st.session_state and model:
                        st.session_state[widget_key] = model
                    # Get current value: prefer widget's session state (user selection), fallback to model from list
                    current_value = st.session_state.get(widget_key, model) if model else ""
                    fallback_index = fallback_options.index(current_value) if current_value and current_value in fallback_options else 0
                    fallback = st.selectbox(
                        f"Fallback {i+1}",
                        fallback_options,
                        index=fallback_index,
                        key=widget_key,
                        label_visibility="collapsed",
                        format_func=lambda x: {
                            "sfdc_ai__DefaultOpenAIGPT5": "OpenAI GPT-5",
                            "sfdc_ai__DefaultOpenAIGPT4": "OpenAI GPT-4",
                            "sfdc_ai__DefaultOpenAIGPT4Turbo": "OpenAI GPT-4 Turbo",
                            "sfdc_ai__DefaultOpenAIGPT4OmniMini": "OpenAI GPT-4 Omni Mini",
                            "sfdc_ai__DefaultBedrockAnthropicClaude45Sonnet": "Anthropic Claude 4.5 Sonnet",
                            "sfdc_ai__DefaultAnthropicClaude35Sonnet": "Anthropic Claude 3.5 Sonnet",
                            "sfdc_ai__DefaultAnthropicClaude35Haiku": "Anthropic Claude 3.5 Haiku",
                            "sfdc_ai__DefaultGoogleGemini25Pro": "Google Gemini 2.5 Pro",
                            "sfdc_ai__DefaultGoogleGemini3Pro": "Google Gemini 3 Pro",
                            "sfdc_ai__DefaultGoogleGemini15Flash": "Google Gemini 1.5 Flash"
                        }.get(x, x) if x else "Select fallback model..."
                    )
                    # Update session state with selected fallback (widget manages its own key, we sync to fallback_models)
                    if fallback:
                        fallback_models.append(fallback)
                        # Sync widget value to fallback_models list for form submission
                        if i < len(st.session_state.fallback_models):
                            st.session_state.fallback_models[i] = fallback
                        else:
                            st.session_state.fallback_models.append(fallback)
                with col3:
                    st.markdown('</div></div>', unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
            # Add Fallback Model button inside the previous section to avoid spacing
            st.form_submit_button(
                "➕ Add Fallback Model",
                key="btn_add_fallback",
                on_click=add_fallback
            )
            st.markdown('</div>', unsafe_allow_html=True)
            
            # 6. Test Questions
            st.markdown('<div class="config-section test-questions-section">', unsafe_allow_html=True)
            st.markdown('<h3 class="section-title test-questions-title" style="margin-bottom: 0 !important; padding-bottom: 0 !important;"><i class="bi bi-question-circle"></i> Test Questions</h3>', unsafe_allow_html=True)
            
            for i, q in enumerate(st.session_state.questions):
                q_num_key = f"form_q_num_{i}"
                q_text_key = f"form_q_text_{i}"
                q_expected_key = f"form_q_expected_{i}"
                
                # Get values: prefer widget's session state (initialized from YAML or user input), fallback to q dict
                q_num_value = st.session_state.get(q_num_key, q.get("number", ""))
                q_text_value = st.session_state.get(q_text_key, q.get("text", ""))
                q_expected_value = st.session_state.get(q_expected_key, q.get("expectedAnswer", ""))
                
                # First question starts immediately after title with no spacing
                question_item_class = "question-item" if i > 0 else "question-item first-question"
                # Remove grey background from ALL questions - no grey bars on any question
                # First question also gets no top padding/margin
                if i == 0:
                    inline_style = ' style="background: transparent !important; border: none !important; padding-top: 0 !important; margin-top: 0 !important;"'
                else:
                    inline_style = ' style="background: transparent !important; border: none !important;"'
                st.markdown(f'<div class="{question_item_class}"{inline_style}>', unsafe_allow_html=True)
                col1, col2, col3, col4 = st.columns([2, 5, 4, 1])
                with col1:
                    q_num = st.text_input("Q#", value=q_num_value, key=q_num_key)
                with col2:
                    q_text = st.text_area("Question Text", value=q_text_value, key=q_text_key, height=80)
                with col3:
                    q_expected = st.text_area("Expected Answer", value=q_expected_value, key=q_expected_key, height=80)
                with col4:
                    # Add remove button for each question (only show if more than 1 question)
                    if len(st.session_state.questions) > 1:
                        st.form_submit_button(
                            "🗑",
                            key=f"btn_remove_q_{i}",
                            on_click=remove_question,
                            args=(i,),
                            help="Remove this question"
                        )
                    else:
                        st.markdown('<br>', unsafe_allow_html=True)
                st.markdown('</div>', unsafe_allow_html=True)
            
            st.markdown('</div>', unsafe_allow_html=True)
            
            # Add Question button (HTML button placed right after questions list)
            st.form_submit_button(
                "➕ Add Question",
                key="btn_add_question",
                on_click=add_question,
                help="Add another test question"
            )
            
            # 7. Custom Instructions (Optional)
            st.markdown('<div class="config-section">', unsafe_allow_html=True)
            st.markdown('<h3 class="section-title"><i class="bi bi-pencil-square"></i> Custom Instructions (Optional)</h3>', unsafe_allow_html=True)
            
            # Get custom instructions from YAML or session state
            # Get custom instructions from YAML only (no defaults from session state)
            custom_instructions_value = st.session_state.get('form_custom_instructions', "")
            custom_instructions = st.text_area(
                "Custom Instructions",
                value=custom_instructions_value,
                key="form_custom_instructions",
                height=100,
                placeholder="Enter any custom instructions you want to add to the Gemini analysis prompt. These will be inserted into the {{CUSTOM_INSTRUCTIONS}} placeholder in the template.",
                help="Optional: Add custom instructions that will be included in the Gemini analysis prompt. Leave empty if you don't need custom instructions."
            )
            st.session_state.custom_instructions = custom_instructions
            st.markdown('</div>', unsafe_allow_html=True)
            
            # 8. Full Gemini Instructions Preview (Non-editable)
            st.markdown('<div class="config-section">', unsafe_allow_html=True)
            st.markdown('<h3 class="section-title"><i class="bi bi-file-text"></i> Full Instructions to Gemini (Preview)</h3>', unsafe_allow_html=True)
            st.markdown("**This is the complete instructions that will be sent to Gemini for analysis. It updates based on your selected refinement stage and configuration.**")
            
            # Build the preview - use yaml_for_template (uploaded or default)
            has_yaml = yaml_for_template is not None
            has_gemini_instructions = yaml_for_template and yaml_for_template.get('configuration', {}).get('geminiInstructions') if yaml_for_template else False
            has_refinement_stages = yaml_for_template and yaml_for_template.get('configuration', {}).get('refinementStages') if yaml_for_template else False
            
            if has_yaml and has_gemini_instructions and has_refinement_stages:
                # Get current form values for preview
                current_custom_instructions = custom_instructions if 'custom_instructions' in locals() else st.session_state.get('custom_instructions', '')
                current_primary_model = primary_model if 'primary_model' in locals() else ''
                current_fallback_models = fallback_models if 'fallback_models' in locals() else []
                
                preview_instructions = build_gemini_instructions_preview(
                    template=yaml_for_template['configuration']['geminiInstructions'],
                    refinement_stage=refinement_stage,
                    refinement_stages_config=yaml_for_template['configuration']['refinementStages'],
                    custom_instructions=current_custom_instructions,
                    primary_model=current_primary_model,
                    fallback_models=current_fallback_models
                )
                
                st.text_area(
                    "Full Instructions Preview (Read-Only)",
                    value=preview_instructions,
                    height=600,
                    key="full_instructions_preview",
                    disabled=True,
                    help="This shows the complete instructions that will be sent to Gemini. Values in brackets [like this] will be filled in at runtime from Salesforce or Excel."
                )
                st.caption("⚠️ Note: Values in brackets (e.g., [LLM Parser Prompt...]) will be retrieved from Salesforce or Excel at runtime. The actual instructions sent may differ slightly based on runtime data.")
            else:
                # Show helpful error message
                if not uploaded_yaml_data and not local_default_yaml:
                    st.error("⚠️ **YAML configuration required.** Please upload a YAML file above. For local testing, you can place a default YAML at `scripts/python/config/default.yaml`.")
                elif not has_gemini_instructions:
                    st.error("⚠️ **Missing `geminiInstructions` in YAML configuration.** The uploaded/default YAML file must contain `configuration.geminiInstructions`.")
                elif not has_refinement_stages:
                    st.error("⚠️ **Missing `refinementStages` in YAML configuration.** The uploaded/default YAML file must contain `configuration.refinementStages`.")
                else:
                    st.error("⚠️ **Could not load template or refinement stages.** Please check your YAML file structure.")
            
            st.markdown('</div>', unsafe_allow_html=True)
            
            # 10. Submit Buttons
            st.markdown('<div class="config-section">', unsafe_allow_html=True)
            col1, col2 = st.columns([1, 4])
            with col1:
                cancel_clicked = st.form_submit_button("Cancel", use_container_width=True)
            with col2:
                submit_clicked = st.form_submit_button("🚀 Start Optimization Workflow", type="primary", use_container_width=True)
            st.markdown('</div>', unsafe_allow_html=True)
            
            if cancel_clicked:
                st.session_state.show_create_modal = False
                st.rerun()
            
            if submit_clicked:
                # Collect questions from form
                questions_clean = []
                for i in range(len(st.session_state.questions)):
                    q_num_val = st.session_state.get(f"form_q_num_{i}", "")
                    q_text_val = st.session_state.get(f"form_q_text_{i}", "")
                    q_expected_val = st.session_state.get(f"form_q_expected_{i}", "")
                    if q_num_val and q_text_val:
                        questions_clean.append({
                            "number": q_num_val,
                            "text": q_text_val,
                            "expectedAnswer": q_expected_val
                        })
                
                # Build YAML config
                config_section = {
                    "salesforce": {
                        "username": username,
                        "password": password,
                        "instanceUrl": instance_url
                    },
                    "searchIndexId": search_index_id,
                    "promptTemplateApiName": prompt_template_api_name,
                    "refinementStage": refinement_stage,
                    "pdfDirectory": pdf_directory,
                    "geminiModel": gemini_model,
                    "headless": True,  # Always True for web app deployment
                    "takeScreenshots": False,  # Default False
                    "slowMo": 0,  # Default 0
                    "prompt_builder_models": {
                        "primary": primary_model,
                        "fallbacks": fallback_models
                    }
                }
                
                # Add geminiInstructions template from yaml_for_template (uploaded or default)
                if yaml_for_template and yaml_for_template.get('configuration', {}).get('geminiInstructions'):
                    config_section['geminiInstructions'] = yaml_for_template['configuration']['geminiInstructions']
                
                # Add refinementStages from yaml_for_template (uploaded or default)
                if yaml_for_template and yaml_for_template.get('configuration', {}).get('refinementStages'):
                    config_section['refinementStages'] = yaml_for_template['configuration']['refinementStages']
                
                # Add custom instructions if provided
                if custom_instructions and custom_instructions.strip():
                    config_section['customInstructions'] = custom_instructions.strip()
                
                # VALIDATION: PDFs are REQUIRED for Gemini analysis (Step 3)
                uploaded_pdf_files = st.session_state.get('uploaded_pdf_files', [])
                if not uploaded_pdf_files or len(uploaded_pdf_files) == 0:
                    st.error("❌ **ERROR: PDF files are REQUIRED!**")
                    st.error("The workflow requires PDF files for Gemini analysis in Step 3. Please upload at least one PDF file before creating a job.")
                    st.stop()
                
                # Validate that PDF files actually exist
                missing_pdfs = []
                for pdf_path in uploaded_pdf_files:
                    if not os.path.exists(pdf_path):
                        missing_pdfs.append(pdf_path)
                
                if missing_pdfs:
                    st.error(f"❌ **ERROR: PDF files not found:** {', '.join(missing_pdfs)}")
                    st.error("Please re-upload the PDF files.")
                    st.stop()
                
                yaml_config = {
                    "configuration": config_section,
                    "questions": questions_clean
                }
                
                # Create run entry
                import random
                run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}"
                run_data = {
                    'run_id': run_id,
                    'status': 'running',
                    'config': yaml_config,
                    'progress': {'status': 'starting', 'run_id': run_id},
                    'output_lines': [],  # Initialize output lines list
                    'started_at': datetime.now()
                }
                st.session_state.runs.append(run_data)
                st.session_state.current_run = run_data
                st.session_state.show_create_modal = False
                
                # Save run to database FIRST (so run_id exists)
                save_runs(st.session_state.runs)  # Persist to database/file
                
                # Save PDFs to database (REQUIRED - will fail if this doesn't work)
                pdf_files = uploaded_pdf_files
                if not save_pdfs_to_db(run_id, pdf_files):
                    st.error(f"❌ **CRITICAL ERROR: Failed to save PDF files to database for run {run_id}**")
                    st.error("The job cannot proceed without PDFs. Please try again.")
                    # Mark job as failed
                    run_data['status'] = 'failed'
                    run_data['error'] = 'Failed to save PDF files to database'
                    save_runs(st.session_state.runs)
                    st.stop()
                
                print(f"[APP] Saved {len(pdf_files)} PDF file(s) to database for run {run_id}", flush=True)
                
                # Mark job as queued (worker will pick it up)
                run_data['status'] = 'queued'
                save_runs(st.session_state.runs)  # Update status in database
                
                print(f"[APP] Job queued: {run_id}", flush=True)
                st.success(f"✅ Workflow queued! Run ID: `{run_id}`")
                st.info("💡 Worker will pick up the job shortly. Switch to 'Jobs' page to monitor progress.")

elif page == "Jobs":
    # Page Header
    col_header1, col_header2 = st.columns([4, 1])
    with col_header1:
        st.markdown("""
        <div style="margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 2px solid #E6E9EF;">
            <h2 style="font-size: 2rem; font-weight: 700; color: #262730; display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem;">
                <i class="bi bi-list-task"></i> Jobs
            </h2>
            <p style="font-size: 1rem; color: #666;">View and monitor all optimization workflows</p>
        </div>
        """, unsafe_allow_html=True)
    with col_header2:
        if st.button("🔄 Refresh", use_container_width=True, key="refresh_button"):
            # Force clear any cached data and reload
            if 'runs' in st.session_state:
                del st.session_state.runs
            st.rerun()
    
    # Always reload runs from file to get latest updates
    fresh_runs = load_runs()
    
    # Detect and mark dead jobs (jobs with stale heartbeat)
    dead_count = detect_and_mark_dead_jobs()
    if dead_count > 0:
        st.warning(f"⚠️ Detected {dead_count} dead job(s) and marked as failed")
        # Reload runs after marking dead jobs
        fresh_runs = load_runs()
    
    st.session_state.runs = fresh_runs
    
    # Filter options
    filter_option = st.radio(
        "Filter:",
        ["All", "Running", "Completed", "Failed"],
        horizontal=True,
        key="jobs_filter"
    )
    
    # Auto-refresh if there are active jobs
    active_count = len([r for r in fresh_runs if r.get('status') == 'running'])
    if active_count > 0 and filter_option in ["All", "Running"]:
        # Add JavaScript auto-refresh (every 5 seconds)
        st.markdown(f"""
        <div style='padding: 0.5rem; background: #E3F2FD; border-radius: 0.5rem; margin-bottom: 1rem;'>
            <small style='color: #1976D2;'>Auto-refreshing every 5 seconds... ({active_count} active job(s))</small>
        </div>
        <script>
            setTimeout(function() {{
                window.location.reload();
            }}, 5000);
        </script>
        """, unsafe_allow_html=True)
    
    st.markdown("---")
    
    # Get Excel file path helper function
    def get_excel_file_path(run_id):
        """Get Excel file path from database, run results, or state file"""
        # For running jobs on Heroku, check database FIRST (filesystem is ephemeral)
        # Try loading from database first (most reliable for Heroku)
        loaded_path = load_excel_from_db(run_id)
        if loaded_path:
            return loaded_path
        
        # Then check if it's in run data (from database)
        for run in fresh_runs:
            if run.get('run_id') == run_id:
                # Check excel_file_path from database record
                excel_file_path = run.get('excel_file_path')
                if excel_file_path:
                    # If file exists on disk, return it
                    if os.path.exists(excel_file_path):
                        return excel_file_path
                    # If not on disk but path is stored, try loading from DB again
                    # (might have been saved after we checked)
                    loaded_path = load_excel_from_db(run_id)
                    if loaded_path:
                        return loaded_path
                
                # Check results
                results = run.get('results', {})
                if results.get('excel_file'):
                    excel_file = results.get('excel_file')
                    if os.path.exists(excel_file):
                        return excel_file
                    # If file doesn't exist, try loading from database
                    loaded_path = load_excel_from_db(run_id)
                    if loaded_path:
                        return loaded_path
        
        # If not in database, check state file (for local development)
        state_file = get_app_data_dir() / "state" / f"run_{run_id}_state.json"
        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    state = json.load(f)
                    excel_file = state.get('excel_file')
                    if excel_file:
                        # If file exists on disk, return it
                        if os.path.exists(excel_file):
                            return excel_file
                        # If not on disk, try loading from DB (might have been saved)
                        loaded_path = load_excel_from_db(run_id)
                        if loaded_path:
                            return loaded_path
            except:
                pass
        return None
    
    # Filter runs based on selection
    filtered_runs = []
    for r in fresh_runs:
        if filter_option == "All":
            filtered_runs.append(r)
        elif filter_option == "Running":
            if r['status'] in ['running', 'queued', 'interrupted']:
                # Filter out likely killed jobs (only for running status)
                if r['status'] == 'running':
                    output_lines = r.get('output_lines', [])
                    progress = r.get('progress', {})
                    if not output_lines and progress.get('status') == 'cycle_start' and progress.get('step') == 0:
                        from datetime import datetime, timedelta
                        started_at = r.get('started_at')
                        if started_at:
                            if isinstance(started_at, str):
                                started_at = datetime.fromisoformat(started_at)
                            if datetime.now() - started_at > timedelta(minutes=10):
                                r['status'] = 'completed'
                                save_runs(st.session_state.runs)
                                continue
                filtered_runs.append(r)
        elif filter_option == "Completed":
            if r['status'] == 'completed':
                filtered_runs.append(r)
        elif filter_option == "Failed":
            if r['status'] == 'failed':
                filtered_runs.append(r)
    
    # Sort by started_at (newest first)
    filtered_runs.sort(key=lambda x: x.get('started_at', ''), reverse=True)
    
    if filtered_runs:
        # Helper function to extract status info for table display
        def get_table_row_data(run):
            """Extract data for table row display"""
            run_id = run['run_id']
            status = run.get('status', 'unknown')
            
            # Get config info
            config = run.get('config', {})
            configuration = config.get('configuration', {})
            search_index_id = configuration.get('searchIndexId', 'N/A')
            prompt_template_name = configuration.get('promptTemplateApiName', 'N/A')
            
            # Format timestamps
            started_at = run.get('started_at', 'N/A')
            if isinstance(started_at, datetime):
                started_at_str = started_at.strftime('%Y-%m-%d %H:%M:%S')
            elif isinstance(started_at, str):
                started_at_str = started_at
            else:
                started_at_str = 'N/A'
            
            completed_at = run.get('completed_at', 'N/A')
            if isinstance(completed_at, datetime):
                completed_at_str = completed_at.strftime('%Y-%m-%d %H:%M:%S')
            elif isinstance(completed_at, str):
                completed_at_str = completed_at
            else:
                completed_at_str = 'In Progress' if status == 'running' else 'N/A'
            
            # Status icon and label (no duplicates)
            if status == 'running':
                status_icon = "🔄"
                status_label = "Running"
            elif status == 'completed':
                status_icon = "✅"
                status_label = "Completed"
            elif status == 'failed':
                status_icon = "❌"
                status_label = "Failed"
            elif status == 'queued':
                status_icon = "⏳"
                status_label = "Queued"
            elif status == 'interrupted':
                status_icon = "⏸️"
                status_label = "Interrupted"
            else:
                status_icon = "❓"
                status_label = "Unknown"
            
            # Get current step info for running jobs
            progress = run.get('progress', {})
            output_lines = run.get('output_lines', [])
            
            # Extract step info
            def extract_status_from_logs(output_lines):
                if not output_lines:
                    return None, None, None
                recent_logs = output_lines[-20:] if len(output_lines) > 20 else output_lines
                recent_text = '\n'.join(recent_logs)
                import re
                cycle_match = re.search(r'REFINEMENT CYCLE (\d+)', recent_text)
                cycle_num = int(cycle_match.group(1)) if cycle_match else None
                step_patterns = [
                    (r'STEP 1: (.*?)(?:\n|$)', 1),
                    (r'STEP 2: (.*?)(?:\n|$)', 2),
                    (r'STEP 3: (.*?)(?:\n|$)', 3),
                ]
                step_num = None
                for pattern, step in step_patterns:
                    if re.search(pattern, recent_text, re.IGNORECASE):
                        step_num = step
                        break
                return step_num, cycle_num, None
            
            step_num, cycle_from_log, _ = extract_status_from_logs(output_lines)
            current_cycle = progress.get('cycle') or cycle_from_log or 0
            current_step = progress.get('step') or step_num or 0
            
            step_names = {
                1: 'Updating Search Index',
                2: 'Testing Index & Invoking Prompts',
                3: 'Analyzing Results with Gemini'
            }
            
            if status == 'running' and current_step > 0:
                current_step_display = f"Cycle {current_cycle} - Step {current_step}/3: {step_names.get(current_step, f'Step {current_step}')}"
            elif status == 'running' and current_cycle > 0:
                current_step_display = f"Cycle {current_cycle} - Initializing"
            elif status == 'running':
                current_step_display = "Initializing..."
            elif status in ['failed', 'completed', 'interrupted']:
                current_step_display = "—"
            else:
                current_step_display = "—"
            
            # Check for Excel file
            excel_file = get_excel_file_path(run_id)
            excel_display = "Yes" if excel_file else "No"
            
            return {
                'run_id': run_id,
                'status_icon': status_icon,
                'status_label': status_label,
                'started_at': started_at_str,
                'completed_at': completed_at_str,
                'current_step': current_step_display,
                'excel': excel_display,
                'search_index_id': search_index_id,
                'prompt_template_name': prompt_template_name,
                'run': run  # Keep reference to full run object
            }
        
        # Create table header
        table_css = "<style>.jobs-table { width: 100%; border-collapse: collapse; margin-bottom: 1rem; }.jobs-table th { background-color: #f0f2f6; padding: 0.75rem; text-align: left; font-weight: 600; border-bottom: 2px solid #d1d5db; }.jobs-table td { padding: 0.75rem; border-bottom: 1px solid #e5e7eb; }.jobs-table tr:hover { background-color: #f9fafb; }</style>"
        st.markdown(table_css, unsafe_allow_html=True)
        
        # Display table
        st.markdown("### Jobs Table")
        st.markdown("")
        
        # Table header
        col1, col2, col3, col4, col5, col6, col7, col8, col9 = st.columns([2, 1.5, 1.5, 1.5, 2, 2, 2.5, 1, 1])
        with col1:
            st.markdown("**Run ID**")
        with col2:
            st.markdown("**Status**")
        with col3:
            st.markdown("**Started**")
        with col4:
            st.markdown("**Completed**")
        with col5:
            st.markdown("**Search Index ID**")
        with col6:
            st.markdown("**Prompt Builder**")
        with col7:
            st.markdown("**Current Step**")
        with col8:
            st.markdown("**Excel**")
        with col9:
            st.markdown("**Actions**")
        
        st.markdown("---")
        
        # Display each run as a table row with expandable details
        for run in filtered_runs:
            row_data = get_table_row_data(run)
            run_id = row_data['run_id']
            job_status = run.get('status', 'unknown')  # Get actual status from run object
            
            # Table row
            col1, col2, col3, col4, col5, col6, col7, col8, col9 = st.columns([2, 1.5, 1.5, 1.5, 2, 2, 2.5, 1, 1])
            with col1:
                st.markdown(f"`{run_id}`")
            with col2:
                st.markdown(f"{row_data['status_icon']} {row_data['status_label']}")
            with col3:
                st.markdown(row_data['started_at'])
            with col4:
                st.markdown(row_data['completed_at'])
            with col5:
                st.markdown(f"`{row_data['search_index_id']}`")
            with col6:
                st.markdown(f"`{row_data['prompt_template_name']}`")
            with col7:
                st.markdown(row_data['current_step'])
            with col8:
                st.markdown(row_data['excel'])
            with col9:
                # Kill button for running/queued/interrupted jobs
                if job_status in ['running', 'queued', 'interrupted']:
                    if st.button("🛑 Kill", key=f"kill_{run_id}", use_container_width=True, type="secondary"):
                        # Kill the job
                        if kill_job(run_id):
                            st.success(f"✅ Job {run_id} killed")
                            # Reload runs to reflect the change
                            if 'runs' in st.session_state:
                                del st.session_state.runs
                            st.rerun()
                        else:
                            st.error(f"❌ Failed to kill job {run_id}")
                else:
                    st.markdown("—")
            
            # Expandable details section
            with st.expander(f"📋 View Details: {run_id}", expanded=False):
                run = row_data['run']
                run_id = row_data['run_id']
                output_lines = run.get('output_lines', [])
                # Always show last 5 lines (or all if less than 5)
                last_5_lines = output_lines[-5:] if len(output_lines) >= 5 else output_lines
                
                # Parse output lines to extract detailed status information
                def extract_status_from_logs(output_lines):
                    """Extract current step, stage, and description from log output"""
                    if not output_lines:
                        return None, None, None, None
                    
                    # Look for step information in recent logs (last 20 lines)
                    recent_logs = output_lines[-20:] if len(output_lines) > 20 else output_lines
                    recent_text = '\n'.join(recent_logs)
                    
                    step_num = None
                    step_desc = None
                    cycle_num = None
                    stage_status = None
                    
                    # Extract cycle number
                    import re
                    cycle_match = re.search(r'REFINEMENT CYCLE (\d+)', recent_text)
                    if cycle_match:
                        cycle_num = int(cycle_match.group(1))
                    
                    # Extract step information
                    step_patterns = [
                        (r'STEP 1: (.*?)(?:\n|$)', 1, 'Updating Search Index'),
                        (r'STEP 2: (.*?)(?:\n|$)', 2, 'Testing Index & Invoking Prompts'),
                        (r'STEP 3: (.*?)(?:\n|$)', 3, 'Analyzing Results with Gemini'),
                        (r'Step 1 Complete: (.*?)(?:\n|$)', 1, 'Search Index Updated'),
                        (r'Step 2 Complete: (.*?)(?:\n|$)', 2, 'Test Sheet Created'),
                        (r'Step 3 Complete: (.*?)(?:\n|$)', 3, 'Gemini Analysis Complete'),
                    ]
                    
                    for pattern, step, default_desc in step_patterns:
                        match = re.search(pattern, recent_text, re.IGNORECASE)
                        if match:
                            step_num = step
                            step_desc = match.group(1).strip() if match.groups() else default_desc
                            # Clean up description
                            if step_desc:
                                step_desc = step_desc.replace('SKIPPED', '').replace('(', '').replace(')', '').strip()
                                if not step_desc or step_desc == 'SKIPPED':
                                    step_desc = default_desc
                            break
                    
                    # Extract stage status
                    stage_match = re.search(r'Stage Status: ([\w\s]+)', recent_text)
                    if stage_match:
                        stage_status = stage_match.group(1).strip()
                    
                    return step_num, step_desc, cycle_num, stage_status
                
                # Extract detailed status from logs
                step_num, step_desc, cycle_from_log, stage_status = extract_status_from_logs(output_lines)
                
                # Display current status
                progress = run.get('progress', {})
                status_icon = "🔄"
                status_text = "Running"
                status_message = progress.get('message', '')
                
                # Get refinement stage from config
                refinement_stage = run.get('config', {}).get('configuration', {}).get('refinementStage', 'llm_parser')
                refinement_stage_names = {
                    'llm_parser': 'LLM Parser',
                    'response_prompt': 'Response Prompt',
                    'agentforce_agent': 'Agentforce Agent'
                }
                stage_name = refinement_stage_names.get(refinement_stage, 'LLM Refinement')
                
                # Use cycle from progress or logs
                current_cycle = progress.get('cycle') or cycle_from_log or 0
                
                # Get current step
                current_step = progress.get('step') or step_num or 0
                
                # Step names and descriptions
                step_names = {
                    1: 'Updating Search Index',
                    2: 'Testing Index & Invoking Prompts',
                    3: 'Analyzing Results with Gemini'
                }
                
                # Get job status early (needed for status text logic)
                job_status = run.get('status', 'unknown')
                
                # Build detailed status text
                if job_status == 'running' and progress.get('status') == 'starting':
                    status_text = "Initializing workflow..."
                elif progress.get('status') == 'cycle_start':
                    status_text = f"Cycle {current_cycle} - Starting"
                    if step_desc:
                        status_message = step_desc
                elif progress.get('status') == 'step_start':
                    # Show current step with description
                    step = current_step
                    step_name = step_names.get(step, f'Step {step}')
                    status_text = f"Step {step}/3: {step_name}"
                    status_message = f"Cycle {current_cycle} - {step_name}"
                elif progress.get('status') == 'step_complete':
                    step = current_step
                    step_name = step_names.get(step, f'Step {step}')
                    status_text = f"Step {step}/3 Complete: {step_name}"
                    status_message = f"Cycle {current_cycle} - {step_name}"
                    status_icon = "✅"
                elif progress.get('status') == 'complete':
                    status_text = "Workflow Complete!"
                    status_icon = "✅"
                    run['status'] = 'completed'
                    save_runs(st.session_state.runs)  # Persist status change
                else:
                    # Use parsed information from logs
                    if current_step > 0:
                        step_name = step_names.get(current_step, f'Step {current_step}')
                        status_text = f"Step {current_step}/3: {step_name}"
                        status_message = f"Cycle {current_cycle} - {step_name}"
                    elif current_cycle > 0:
                        status_text = f"Cycle {current_cycle} - Running"
                    else:
                        status_text = "Initializing..."
                
                # Check if job is complete
                job_complete = any('Workflow Complete' in line or 'completed' in line.lower() for line in output_lines[-3:]) if output_lines else False
                job_status = run.get('status', 'unknown')
                is_completed = job_complete or job_status == 'completed' or status_text == "Workflow Complete!"
                
                # Check if job failed
                job_status = run.get('status', 'unknown')
                is_failed = job_status == 'failed'
                error_msg = run.get('error', '')
                error_details = run.get('error_details', '')
                
                # Show run info
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown(f"**Run ID:** `{run_id}`")
                    st.markdown(f"**Job Type:** 🤖 {stage_name} Refinement Job")
                    
                    # Show error prominently if failed
                    if is_failed:
                        st.error(f"**Status:** ❌ **FAILED**")
                        if error_msg:
                            st.error(f"**Error:** {error_msg}")
                        if error_details and error_details != error_msg:
                            st.error(f"**Error Details:** {error_details}")
                        # Show which step failed if available
                        if progress.get('step'):
                            failed_step = progress.get('step')
                            step_name = step_names.get(failed_step, f'Step {failed_step}')
                            st.error(f"**Failed at:** Cycle {current_cycle} - {step_name}")
                    else:
                        st.markdown(f"**Status:** {status_icon} {status_text}")
                        if status_message:
                            st.info(f"📝 {status_message}")
                        # Only show stage status if job is not complete
                        if stage_status and not is_completed:
                            st.success(f"📊 Stage Status: {stage_status}")
                with col2:
                    if current_cycle > 0:
                        progress_value = min(current_cycle / 10, 1.0)
                        st.progress(progress_value)
                        st.caption(f"Cycle {current_cycle}")
                        if current_step > 0:
                            step_name = step_names.get(current_step, f'Step {current_step}')
                            st.caption(f"Step {current_step}/3")
                            st.caption(f"{step_name}")
                    else:
                        st.caption("Initializing...")
                
                # Show last 5 lines of output
                # Reload fresh data right before displaying to ensure we have latest output
                fresh_run_data = load_runs()
                current_run_fresh = next((r for r in fresh_run_data if r.get('run_id') == run_id), run)
                fresh_output_lines = current_run_fresh.get('output_lines', [])
                fresh_last_5_lines = fresh_output_lines[-5:] if len(fresh_output_lines) >= 5 else fresh_output_lines
                
                # Show time since last update
                from datetime import datetime, timedelta
                if fresh_output_lines:
                    last_update_time_str = fresh_output_lines[-1].split(']')[0].replace('[', '')
                    try:
                        last_update_time = datetime.strptime(last_update_time_str, '%H:%M:%S').replace(
                            year=datetime.now().year, month=datetime.now().month, day=datetime.now().day
                        )
                        time_since_update = datetime.now() - last_update_time
                        minutes_since_update = int(time_since_update.total_seconds() / 60)
                        seconds_since_update = int(time_since_update.total_seconds() % 60)
                        
                        # Check if job is complete by looking at the last output line
                        job_complete = any('Workflow Complete' in line or 'completed' in line.lower() for line in fresh_output_lines[-3:])
                        job_status = run.get('status', 'unknown')
                        is_completed = job_complete or job_status == 'completed'
                        is_running = job_status == 'running'
                        
                        # Always show time since last update (only for running jobs)
                        if not is_completed and is_running:
                            if minutes_since_update > 0:
                                time_message = f"⏱️ Last update: {minutes_since_update} minute{'s' if minutes_since_update != 1 else ''} ago"
                            else:
                                time_message = f"⏱️ Last update: {seconds_since_update} second{'s' if seconds_since_update != 1 else ''} ago"
                            st.caption(time_message)
                            
                            # Show note about index update taking up to an hour (only for Step 1)
                            current_step = progress.get('step', 0)
                            if current_step == 1:
                                st.info("ℹ️ **Note:** The index update stage (Step 1) can take up to an hour. Please be patient.")
                            
                            # Show warning if it's been too long (only for running jobs)
                            timeout_minutes = 120 if current_step == 1 else 10
                            if is_running and time_since_update > timedelta(minutes=timeout_minutes):
                                if current_step == 1:
                                    st.warning(f"⚠️ **No updates in {minutes_since_update} minutes.** Step 1 (Updating Search Index) can take up to an hour, but if it's been longer, the job may be stuck.")
                                else:
                                    st.warning(f"⚠️ **No updates in {minutes_since_update} minutes.** The job may be stuck.")
                    except:
                        pass  # If we can't parse timestamp, just continue
                
                if fresh_last_5_lines:
                    st.markdown("**📋 Live Output (Last 5 lines):**")
                    # Reload fresh data each time to ensure latest output is shown
                    with st.container():
                        st.code('\n'.join(fresh_last_5_lines), language='text')
                    
                    # Expandable section for full output
                    if len(fresh_output_lines) > 5:
                        with st.expander(f"📋 View Full Output ({len(fresh_output_lines)} total lines)", expanded=False):
                            # Reload fresh data again when expander is opened
                            fresh_run_data_expanded = load_runs()
                            current_run_expanded = next((r for r in fresh_run_data_expanded if r.get('run_id') == run_id), current_run_fresh)
                            fresh_output_lines_expanded = current_run_expanded.get('output_lines', [])
                            st.code('\n'.join(fresh_output_lines_expanded), language='text')
                else:
                    # Show current progress info even if no output lines yet
                    if job_status == 'running' and progress.get('status') == 'starting':
                        st.info("ℹ️ Workflow is initializing...")
                    elif progress.get('status') == 'cycle_start':
                        st.info(f"ℹ️ Starting Cycle {current_cycle}...")
                    elif progress.get('step'):
                        step_names = {1: 'Updating Search Index', 2: 'Testing Index', 3: 'Analyzing Results'}
                        step_name = step_names.get(progress.get('step'), f'Step {progress.get("step")}')
                        st.info(f"ℹ️ Running Cycle {current_cycle} - {step_name}...")
                    else:
                        st.info("ℹ️ Workflow is running. Output will appear here as progress updates are received.")
                
                # Show additional progress details
                if current_cycle > 0:
                    # Determine current step from progress or parsed logs
                    current_step = progress.get('step') or step_num
                    step_names_display = {1: 'Update Index', 2: 'Test Index', 3: 'Analyze Results'}
                    
                    if current_step and current_step > 0:
                        step_name_display = step_names_display.get(current_step, f'Step {current_step}')
                        st.caption(f"📍 Current: Cycle {current_cycle}, {step_name_display}")
                    else:
                        # Step not determined yet, show cycle only
                        st.caption(f"📍 Current: Cycle {current_cycle} (Initializing step...)")
                elif job_status == 'running' and progress.get('status') == 'starting':
                    st.caption("📍 Initializing workflow...")
                
                # Show Excel file if available
                excel_file = get_excel_file_path(run_id)
                if excel_file:
                    st.markdown("---")
                    st.markdown("**📊 Excel File:**")
                    st.write(f"`{excel_file}`")
                    if os.path.exists(excel_file):
                        with open(excel_file, 'rb') as f:
                            st.download_button(
                                "📥 Download Excel",
                                f,
                                file_name=Path(excel_file).name,
                                key=f"download_{run_id}"
                            )
                    else:
                        st.warning(f"⚠️ File not found at: {excel_file}")
    else:
        st.info("ℹ️ No jobs found. Click 'Create New Run' to start a workflow.")

