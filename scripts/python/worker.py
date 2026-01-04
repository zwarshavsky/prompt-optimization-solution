#!/usr/bin/env python3
"""
Worker process that polls database for queued/interrupted jobs and executes them.
Handles graceful shutdown on SIGTERM to save checkpoint and mark job as interrupted.
"""

import os
import sys
import signal
import time
import json
from datetime import datetime
from typing import Dict, Any, Optional

# Add current directory to path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from worker_utils import (
    get_queued_jobs,
    get_interrupted_jobs,
    mark_job_as_running,
    mark_job_as_interrupted,
    mark_job_as_failed,
    mark_job_as_completed,
    update_job_progress,
    update_job_heartbeat,
    load_pdfs_from_db
)

try:
    import main as main_module
    run_full_workflow = main_module.run_full_workflow
except (ImportError, KeyError, AttributeError) as e:
    print(f"[WORKER] ERROR: Failed to import main module: {e}", flush=True)
    sys.exit(1)

# Global flag for graceful shutdown
shutdown_requested = False
current_job_id = None
current_checkpoint = None


def signal_handler(signum, frame):
    """Handle SIGTERM for graceful shutdown"""
    global shutdown_requested, current_job_id, current_checkpoint
    print(f"\n[WORKER] SIGTERM received. Initiating graceful shutdown...", flush=True)
    shutdown_requested = True
    
    # If we have a current job, save checkpoint and mark as interrupted
    if current_job_id and current_checkpoint:
        try:
            print(f"[WORKER] Saving checkpoint for job {current_job_id}...", flush=True)
            mark_job_as_interrupted(current_job_id, current_checkpoint)
            print(f"[WORKER] Job {current_job_id} marked as interrupted. Exiting...", flush=True)
        except Exception as e:
            print(f"[WORKER] ERROR: Failed to save checkpoint: {e}", flush=True)
    
    # Exit within 30 seconds (Heroku grace period)
    sys.exit(0)


def worker_progress_callback(status_dict: Dict[str, Any]):
    """Progress callback for worker - updates database instead of session state"""
    global current_job_id, current_checkpoint
    
    run_id = status_dict.get('run_id') or current_job_id
    if not run_id:
        return
    
    try:
        # Extract checkpoint info from status
        cycle = status_dict.get('cycle', 0)
        step = status_dict.get('step', 0)
        stage_status = status_dict.get('stage_status', '')
        
        # Update checkpoint info
        current_checkpoint = {
            'cycle': cycle,
            'step': step,
            'stage_status': stage_status,
            'last_updated': datetime.now().isoformat(),
            'status': status_dict.get('status', '')
        }
        
        # Update progress in database
        status = status_dict.get('status', '')
        message = status_dict.get('message', f'Status: {status}')
        
        # Generate output line
        timestamp = datetime.now().strftime('%H:%M:%S')
        output_line = f"[{timestamp}] {message}"
        
        # Update progress
        update_job_progress(run_id, status_dict, output_line)
        
        # Update heartbeat
        update_job_heartbeat(run_id)
        
        # Save Excel file to database if provided (after Step 2 or Step 3)
        excel_file_path = status_dict.get('excel_file')
        if excel_file_path and os.path.exists(excel_file_path):
            try:
                # Import save_excel_to_db from app module
                from app import save_excel_to_db
                
                if save_excel_to_db(run_id, excel_file_path):
                    print(f"[WORKER] Saved Excel file to DB: {excel_file_path}", flush=True)
                else:
                    print(f"[WORKER] Warning: Failed to save Excel file to DB: {excel_file_path}", flush=True)
            except Exception as e:
                print(f"[WORKER] Error saving Excel file to DB: {e}", flush=True)
                import traceback
                traceback.print_exc()
        
    except Exception as e:
        print(f"[WORKER] Error in progress callback: {e}", flush=True)
        import traceback
        traceback.print_exc()


def process_job(run_id: str, resume_info: Optional[Dict[str, Any]] = None) -> bool:
    """
    Process a single job. Returns True if successful, False otherwise.
    """
    global current_job_id, current_checkpoint, shutdown_requested
    
    current_job_id = run_id
    current_checkpoint = None
    
    try:
        print(f"[WORKER] Processing job: {run_id}", flush=True)
        
        # Mark as running
        if not mark_job_as_running(run_id):
            print(f"[WORKER] ERROR: Failed to mark job {run_id} as running", flush=True)
            return False
        
        # Load job config from database
        from worker_utils import get_db_connection
        conn = get_db_connection()
        if not conn:
            print(f"[WORKER] ERROR: Could not connect to database", flush=True)
            mark_job_as_failed(run_id, "Could not connect to database")
            return False
        
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT config FROM runs WHERE run_id = %s
                """, (run_id,))
                row = cur.fetchone()
                if not row:
                    print(f"[WORKER] ERROR: Job {run_id} not found in database", flush=True)
                    mark_job_as_failed(run_id, f"Job {run_id} not found in database")
                    return False
                
                config_data = row[0]
                if not config_data:
                    print(f"[WORKER] ERROR: No config found for job {run_id}", flush=True)
                    mark_job_as_failed(run_id, "No configuration found")
                    return False
                
                # Config is stored directly as the yaml_config structure
                # Structure: {'configuration': {...}, 'questions': [...]}
                yaml_config = config_data
        finally:
            conn.close()
        
        if not yaml_config:
            print(f"[WORKER] ERROR: No YAML config found for job {run_id}", flush=True)
            mark_job_as_failed(run_id, "No YAML configuration found")
            return False
        
        # Load PDFs from database if they exist
        pdf_files_restored = []
        try:
            pdf_files_restored = load_pdfs_from_db(run_id)
            if pdf_files_restored:
                print(f"[WORKER] Loaded {len(pdf_files_restored)} PDF file(s) from database", flush=True)
                # Update pdfDirectory in config to point to restored PDFs directory
                if pdf_files_restored:
                    pdf_dir = str(Path(pdf_files_restored[0]).parent)
                    if 'configuration' in yaml_config:
                        yaml_config['configuration']['pdfDirectory'] = pdf_dir
        except Exception as e:
            print(f"[WORKER] Warning: Could not load PDFs from database: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        # Prepare resume parameters if job was interrupted
        resume_params = {}
        if resume_info:
            checkpoint = resume_info.get('checkpoint_info', {})
            if checkpoint:
                resume_params = {
                    'resume': True,
                    'resume_from_cycle': checkpoint.get('cycle'),
                    'resume_from_step': checkpoint.get('step')
                }
                print(f"[WORKER] Resuming job {run_id} from Cycle {checkpoint.get('cycle')}, Step {checkpoint.get('step')}", flush=True)
        
        # Execute workflow
        print(f"[WORKER] Starting workflow execution for job {run_id}...", flush=True)
        
        try:
            results = run_full_workflow(
                yaml_config_dict=yaml_config,
                progress_callback=worker_progress_callback,
                run_id=run_id,
                **resume_params
            )
            
            # Check if shutdown was requested during execution
            if shutdown_requested:
                print(f"[WORKER] Shutdown requested during job execution. Job will be marked as interrupted.", flush=True)
                return False
            
            # Mark as completed
            mark_job_as_completed(run_id, results)
            print(f"[WORKER] Job {run_id} completed successfully", flush=True)
            return True
            
        except Exception as e:
            error_msg = str(e)
            error_details = f"Workflow execution failed: {error_msg}"
            print(f"[WORKER] Job {run_id} failed: {error_msg}", flush=True)
            mark_job_as_failed(run_id, error_msg, error_details)
            return False
            
    except Exception as e:
        print(f"[WORKER] ERROR processing job {run_id}: {e}", flush=True)
        mark_job_as_failed(run_id, f"Worker error: {str(e)}")
        return False
    finally:
        current_job_id = None
        current_checkpoint = None


def main():
    """Main worker loop - polls for jobs and processes them"""
    global shutdown_requested
    
    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)  # Also handle Ctrl+C for local testing
    
    print("[WORKER] Worker started. Polling for jobs...", flush=True)
    
    poll_interval = int(os.environ.get('WORKER_POLL_INTERVAL', '5'))
    
    while not shutdown_requested:
        try:
            # First, check for interrupted jobs (priority - they were in progress)
            interrupted_jobs = get_interrupted_jobs()
            if interrupted_jobs:
                for job in interrupted_jobs:
                    if shutdown_requested:
                        break
                    run_id = job['run_id']
                    print(f"[WORKER] Found interrupted job: {run_id}", flush=True)
                    process_job(run_id, resume_info=job)
                    # Process one job at a time
                    break
            
            # Then check for queued jobs
            if not shutdown_requested:
                queued_jobs = get_queued_jobs()
                if queued_jobs:
                    for run_id in queued_jobs:
                        if shutdown_requested:
                            break
                        print(f"[WORKER] Found queued job: {run_id}", flush=True)
                        process_job(run_id)
                        # Process one job at a time
                        break
            
            # If no jobs found, wait before next poll
            if not interrupted_jobs and not queued_jobs:
                time.sleep(poll_interval)
            else:
                # If we processed a job, wait a bit before checking again
                time.sleep(1)
                
        except KeyboardInterrupt:
            print("\n[WORKER] Keyboard interrupt received. Shutting down...", flush=True)
            shutdown_requested = True
            break
        except Exception as e:
            print(f"[WORKER] ERROR in main loop: {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(poll_interval)  # Wait before retrying
    
    print("[WORKER] Worker shutting down...", flush=True)


if __name__ == '__main__':
    main()

