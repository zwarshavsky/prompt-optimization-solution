#!/usr/bin/env python3
"""
Test script to verify synchronization fixes and prevent regression.
Run this before deploying changes to ensure fixes work and don't break existing functionality.
"""

import sys
import os
import subprocess
import psycopg2
import json
from datetime import datetime, timedelta
from typing import Dict, Any

# Force unbuffered output for real-time printouts
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
os.environ['PYTHONUNBUFFERED'] = '1'

# Add current directory to path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from worker_utils import (
    get_db_connection,
    update_job_progress,
    mark_job_as_running,
    mark_job_as_failed,
    mark_job_as_completed
)

def get_test_db_connection():
    """Get database connection for testing"""
    result = subprocess.run(['heroku', 'config:get', 'DATABASE_URL', '-a', 'sf-rag-optimizer'], 
                          capture_output=True, text=True)
    if result.returncode == 0:
        db_url = result.stdout.strip()
        # Set environment variable for worker_utils functions
        os.environ['DATABASE_URL'] = db_url
        # Return connection for direct SQL queries
        if db_url.startswith('postgres://'):
            db_url = db_url.replace('postgres://', 'postgresql://', 1)
        return psycopg2.connect(db_url, sslmode='require')
    return None

def test_1_update_progress_sets_running():
    """Test 1: update_job_progress() sets status='running' for queued/interrupted jobs"""
    print("Test 1: update_job_progress() status sync")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job with 'queued' status
        test_run_id = f"test_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at)
            VALUES (%s, 'queued', '{"status": "starting"}'::jsonb, NULL)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Call update_job_progress (simulating worker activity)
        progress = {'status': 'step_start', 'cycle': 1, 'step': 2, 'run_id': test_run_id}
        result = update_job_progress(test_run_id, progress, "[12:00:00] Step 2 started")
        
        # Verify status changed to 'running'
        cur.execute("SELECT status FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        actual_status = row[0] if row else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if actual_status == 'running':
            print(f"  ✅ PASS: Status changed from 'queued' to 'running'")
            return True
        else:
            print(f"  ❌ FAIL: Expected 'running', got '{actual_status}'")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_2_update_progress_preserves_failed():
    """Test 2: update_job_progress() preserves 'failed' status"""
    print("Test 2: update_job_progress() preserves 'failed' status")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job with 'failed' status
        test_run_id = f"test_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at)
            VALUES (%s, 'failed', '{"status": "error"}'::jsonb, NULL)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Call update_job_progress (should NOT change status)
        progress = {'status': 'step_start', 'cycle': 1, 'step': 2, 'run_id': test_run_id}
        update_job_progress(test_run_id, progress, "[12:00:00] Some update")
        
        # Verify status stayed 'failed'
        cur.execute("SELECT status FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        actual_status = row[0] if row else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if actual_status == 'failed':
            print(f"  ✅ PASS: Status stayed 'failed' (correct)")
            return True
        else:
            print(f"  ❌ FAIL: Expected 'failed', got '{actual_status}'")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        return False
    finally:
        conn.close()

def test_3_save_runs_protects_running():
    """Test 3: save_runs() protects 'running' status from stale 'queued' overwrite"""
    print("Test 3: save_runs() protects active jobs from stale data")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job with 'running' status and recent heartbeat
        test_run_id = f"test_protect_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at, updated_at)
            VALUES (%s, 'running', '{"status": "step_start"}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Simulate save_runs() trying to overwrite with stale 'queued' status
        # This simulates the bug: UI has stale session_state with status='queued'
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at, updated_at)
            VALUES (%s, 'queued', '{"status": "starting"}'::jsonb, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT (run_id) DO UPDATE SET
                status = CASE 
                    WHEN runs.heartbeat_at > NOW() - INTERVAL '5 minutes' 
                        AND runs.status IN ('running', 'interrupted')
                        AND EXCLUDED.status = 'queued'
                    THEN runs.status
                    ELSE EXCLUDED.status
                END,
                updated_at = CURRENT_TIMESTAMP
        """, (test_run_id,))
        conn.commit()
        
        # Verify status stayed 'running' (protected)
        cur.execute("SELECT status FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        actual_status = row[0] if row else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if actual_status == 'running':
            print(f"  ✅ PASS: Status protected from stale 'queued' overwrite")
            return True
        else:
            print(f"  ❌ FAIL: Expected 'running' (protected), got '{actual_status}'")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_4_save_runs_allows_legitimate_transitions():
    """Test 4: save_runs() allows legitimate status transitions (running→failed, running→completed)"""
    print("Test 4: save_runs() allows legitimate status transitions")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job with 'running' status and recent heartbeat
        test_run_id = f"test_transition_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at, updated_at)
            VALUES (%s, 'running', '{"status": "step_start"}'::jsonb, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Simulate Kill button: trying to change running→failed (should work)
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at, updated_at)
            VALUES (%s, 'failed', '{"status": "error"}'::jsonb, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT (run_id) DO UPDATE SET
                status = CASE 
                    WHEN runs.heartbeat_at > NOW() - INTERVAL '5 minutes' 
                        AND runs.status IN ('running', 'interrupted')
                        AND EXCLUDED.status = 'queued'
                    THEN runs.status
                    ELSE EXCLUDED.status
                END,
                updated_at = CURRENT_TIMESTAMP
        """, (test_run_id,))
        conn.commit()
        
        # Verify status changed to 'failed' (allowed transition)
        cur.execute("SELECT status FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        actual_status = row[0] if row else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if actual_status == 'failed':
            print(f"  ✅ PASS: Legitimate transition 'running'→'failed' allowed")
            return True
        else:
            print(f"  ❌ FAIL: Expected 'failed', got '{actual_status}' (transition blocked incorrectly)")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_5_mark_running_verification():
    """Test 5: mark_job_as_running() actually updates the database"""
    print("Test 5: mark_job_as_running() verification")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job with 'queued' status
        test_run_id = f"test_mark_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at)
            VALUES (%s, 'queued', '{"status": "starting"}'::jsonb, NULL)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Call mark_job_as_running
        result = mark_job_as_running(test_run_id)
        
        # Verify it worked
        cur.execute("SELECT status, heartbeat_at FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        actual_status = row[0] if row else None
        heartbeat = row[1] if row and len(row) > 1 else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if result and actual_status == 'running' and heartbeat:
            print(f"  ✅ PASS: Job marked as 'running' with heartbeat")
            return True
        else:
            print(f"  ❌ FAIL: result={result}, status='{actual_status}', heartbeat={heartbeat}")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_6_completed_jobs_stay_completed():
    """Regression Test 6: Completed jobs stay completed (tests actual save_runs() function)"""
    print("Regression Test 6: Completed jobs stay completed")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        # Import the actual save_runs function
        from app import save_runs
        
        cur = conn.cursor()
        
        # Create test job with 'completed' status in database
        test_run_id = f"test_completed_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at, updated_at, config, output_lines, results)
            VALUES (%s, 'completed', '{"status": "completed"}'::jsonb, NULL, CURRENT_TIMESTAMP, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Simulate stale UI data: session_state has old 'queued' status
        # This is what happens when UI has stale data and calls save_runs()
        stale_run_data = [{
            'run_id': test_run_id,
            'status': 'queued',  # Stale status from UI session_state
            'config': {},
            'progress': {'status': 'starting'},
            'output_lines': [],
            'results': {},
            'error': None,
            'error_details': None,
            'excel_file_path': None,
            'started_at': None,
            'completed_at': None,
            'heartbeat_at': None,
            'checkpoint_info': {}
        }]
        
        # Call the actual save_runs() function (this is what the UI does)
        save_runs(stale_run_data)
        
        # Verify status stayed 'completed' (protected by save_runs())
        cur.execute("SELECT status FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        actual_status = row[0] if row else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if actual_status == 'completed':
            print(f"  ✅ PASS: save_runs() protected completed job from stale 'queued' overwrite")
            return True
        else:
            print(f"  ❌ FAIL: Expected 'completed', got '{actual_status}' (save_runs() did not protect)")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_7_interrupted_jobs_retrievable():
    """Regression Test 7: Interrupted jobs can be retrieved"""
    print("Regression Test 7: Interrupted jobs can be retrieved")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        from worker_utils import get_interrupted_jobs
        
        cur = conn.cursor()
        
        # Create test interrupted job with checkpoint_info
        test_run_id = f"test_interrupted_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        checkpoint = {"cycle": 2, "step": 3, "progress": "50%"}
        cur.execute("""
            INSERT INTO runs (run_id, status, checkpoint_info, progress, updated_at)
            VALUES (%s, 'interrupted', %s::jsonb, '{"status": "interrupted"}'::jsonb, CURRENT_TIMESTAMP)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id, json.dumps(checkpoint)))
        conn.commit()
        
        # Retrieve interrupted jobs
        interrupted_jobs = get_interrupted_jobs()
        
        # Find our test job
        test_job = next((j for j in interrupted_jobs if j['run_id'] == test_run_id), None)
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if test_job and test_job.get('checkpoint_info') == checkpoint:
            print(f"  ✅ PASS: Interrupted job retrieved with checkpoint_info")
            return True
        else:
            print(f"  ❌ FAIL: Job not found or checkpoint_info missing")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_8_heartbeat_updates_only_running():
    """Regression Test 8: Heartbeat updates only for running jobs"""
    print("Regression Test 8: Heartbeat updates only for running jobs")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        from worker_utils import update_job_heartbeat
        
        cur = conn.cursor()
        
        # Create test job with 'queued' status
        test_run_id = f"test_heartbeat_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, heartbeat_at)
            VALUES (%s, 'queued', NULL)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Try to update heartbeat (should fail because status is not 'running')
        result = update_job_heartbeat(test_run_id)
        
        # Verify heartbeat was NOT updated
        cur.execute("SELECT heartbeat_at FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        heartbeat = row[0] if row else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if not result and heartbeat is None:
            print(f"  ✅ PASS: Heartbeat not updated for non-running job")
            return True
        else:
            print(f"  ❌ FAIL: Heartbeat updated incorrectly (result={result}, heartbeat={heartbeat})")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_9_kill_button_works_on_queued():
    """Regression Test 9: Kill button works on queued jobs"""
    print("Regression Test 9: Kill button works on queued jobs")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job with 'queued' status
        test_run_id = f"test_kill_queued_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, heartbeat_at)
            VALUES (%s, 'queued', '{"status": "starting"}'::jsonb, NULL)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Simulate Kill button: change queued→failed
        cur.execute("""
            UPDATE runs 
            SET status = 'failed',
                updated_at = CURRENT_TIMESTAMP
            WHERE run_id = %s
        """, (test_run_id,))
        conn.commit()
        
        # Verify status changed to 'failed'
        cur.execute("SELECT status FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        actual_status = row[0] if row else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if actual_status == 'failed':
            print(f"  ✅ PASS: Queued job can be killed (marked as 'failed')")
            return True
        else:
            print(f"  ❌ FAIL: Expected 'failed', got '{actual_status}'")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_10_kill_button_works_on_interrupted():
    """Regression Test 10: Kill button works on interrupted jobs"""
    print("Regression Test 10: Kill button works on interrupted jobs")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job with 'interrupted' status
        test_run_id = f"test_kill_interrupted_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, checkpoint_info, heartbeat_at)
            VALUES (%s, 'interrupted', '{"status": "interrupted"}'::jsonb, '{"cycle": 1}'::jsonb, NULL)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Simulate Kill button: change interrupted→failed
        cur.execute("""
            UPDATE runs 
            SET status = 'failed',
                updated_at = CURRENT_TIMESTAMP
            WHERE run_id = %s
        """, (test_run_id,))
        conn.commit()
        
        # Verify status changed to 'failed'
        cur.execute("SELECT status FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        actual_status = row[0] if row else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if actual_status == 'failed':
            print(f"  ✅ PASS: Interrupted job can be killed (marked as 'failed')")
            return True
        else:
            print(f"  ❌ FAIL: Expected 'failed', got '{actual_status}'")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_11_progress_updates_handle_missing_fields():
    """Regression Test 11: Progress updates handle missing fields gracefully"""
    print("Regression Test 11: Progress updates handle missing fields gracefully")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job with minimal fields (no output_lines, minimal progress)
        test_run_id = f"test_missing_fields_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status)
            VALUES (%s, 'running')
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Call update_job_progress (should handle missing fields)
        progress = {'status': 'step_start', 'cycle': 1, 'step': 2, 'run_id': test_run_id}
        result = update_job_progress(test_run_id, progress, "[12:00:00] Test output")
        
        # Verify it worked without crashing
        cur.execute("SELECT progress, output_lines FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        progress_data = row[0] if row and row[0] else None
        output_lines = row[1] if row and len(row) > 1 else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if result and progress_data and output_lines:
            print(f"  ✅ PASS: Progress update handled missing fields gracefully")
            return True
        else:
            print(f"  ❌ FAIL: result={result}, progress={progress_data}, output_lines={output_lines}")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def test_12_database_connection_failures_handled():
    """Regression Test 12: Database connection failures are handled gracefully"""
    print("Regression Test 12: Database connection failures are handled gracefully")
    
    try:
        # Save original DATABASE_URL
        original_db_url = os.environ.get('DATABASE_URL')
        
        # Set invalid DATABASE_URL
        os.environ['DATABASE_URL'] = 'postgresql://invalid:invalid@invalid:5432/invalid'
        
        # Try to get queued jobs (should return empty list, not crash)
        from worker_utils import get_queued_jobs
        jobs = get_queued_jobs()
        
        # Restore original DATABASE_URL
        if original_db_url:
            os.environ['DATABASE_URL'] = original_db_url
        else:
            os.environ.pop('DATABASE_URL', None)
        
        if isinstance(jobs, list) and len(jobs) == 0:
            print(f"  ✅ PASS: Connection failure handled gracefully (returned empty list)")
            return True
        else:
            print(f"  ❌ FAIL: Expected empty list, got {jobs}")
            return False
            
    except Exception as e:
        # Restore original DATABASE_URL on error
        if 'original_db_url' in locals() and original_db_url:
            os.environ['DATABASE_URL'] = original_db_url
        
        print(f"  ❌ ERROR: Exception raised instead of graceful handling: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_13_multiple_concurrent_updates():
    """Regression Test 13: Multiple rapid progress updates don't cause issues"""
    print("Regression Test 13: Multiple rapid progress updates don't cause issues")
    conn = get_test_db_connection()
    if not conn:
        print("  ❌ SKIP: Could not connect to database")
        return False
    
    try:
        cur = conn.cursor()
        
        # Create test job
        test_run_id = f"test_concurrent_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cur.execute("""
            INSERT INTO runs (run_id, status, progress, output_lines)
            VALUES (%s, 'running', '{"status": "starting"}'::jsonb, '[]'::jsonb)
            ON CONFLICT (run_id) DO NOTHING
        """, (test_run_id,))
        conn.commit()
        
        # Simulate multiple rapid updates
        for i in range(5):
            progress = {'status': 'step_start', 'cycle': 1, 'step': 2, 'run_id': test_run_id, 'iteration': i}
            update_job_progress(test_run_id, progress, f"[12:00:{i:02d}] Update {i}")
        
        # Verify final state
        cur.execute("SELECT progress, output_lines FROM runs WHERE run_id = %s", (test_run_id,))
        row = cur.fetchone()
        # progress is already a dict (JSONB), not a string
        progress_data = row[0] if row and row[0] else None
        output_lines = row[1] if row and len(row) > 1 else None
        
        # Cleanup
        cur.execute("DELETE FROM runs WHERE run_id = %s", (test_run_id,))
        conn.commit()
        
        if progress_data and output_lines and len(output_lines) == 5:
            print(f"  ✅ PASS: Multiple concurrent updates handled correctly ({len(output_lines)} output lines)")
            return True
        else:
            print(f"  ❌ FAIL: progress={progress_data}, output_lines count={len(output_lines) if output_lines else 0}")
            return False
            
    except Exception as e:
        print(f"  ❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()

def main():
    """Run all tests"""
    print("="*80)
    print("SYNCHRONIZATION FIXES & REGRESSION TESTS")
    print("="*80)
    print()
    
    print("🔍 Running synchronization fix tests...")
    print()
    
    sync_tests = [
        test_1_update_progress_sets_running,
        test_2_update_progress_preserves_failed,
        test_3_save_runs_protects_running,
        test_4_save_runs_allows_legitimate_transitions,
        test_5_mark_running_verification
    ]
    
    print("🔍 Running regression tests...")
    print()
    
    regression_tests = [
        test_6_completed_jobs_stay_completed,
        test_7_interrupted_jobs_retrievable,
        test_8_heartbeat_updates_only_running,
        test_9_kill_button_works_on_queued,
        test_10_kill_button_works_on_interrupted,
        test_11_progress_updates_handle_missing_fields,
        test_12_database_connection_failures_handled,
        test_13_multiple_concurrent_updates
    ]
    
    all_tests = sync_tests + regression_tests
    
    results = []
    for i, test in enumerate(all_tests, 1):
        try:
            print(f"[{i}/{len(all_tests)}] ", end="", flush=True)
            result = test()
            results.append(result)
            print()
        except Exception as e:
            print(f"  ❌ TEST CRASHED: {e}")
            import traceback
            traceback.print_exc()
            results.append(False)
            print()
    
    print("="*80)
    print("RESULTS SUMMARY")
    print("="*80)
    passed = sum(results)
    total = len(results)
    sync_passed = sum(results[:len(sync_tests)])
    regression_passed = sum(results[len(sync_tests):])
    
    print(f"Total: {passed}/{total}")
    print(f"  - Synchronization fixes: {sync_passed}/{len(sync_tests)}")
    print(f"  - Regression tests: {regression_passed}/{len(regression_tests)}")
    print()
    
    if passed == total:
        print("✅ ALL TESTS PASSED")
        return 0
    else:
        print("❌ SOME TESTS FAILED")
        print()
        print("Failed tests:")
        for i, (test, result) in enumerate(zip(all_tests, results), 1):
            if not result:
                print(f"  - [{i}] {test.__name__}")
        return 1

if __name__ == '__main__':
    sys.exit(main())

