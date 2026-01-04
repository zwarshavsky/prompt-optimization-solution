# Worker Dyno Implementation Plan

## Overview
Implement a worker dyno architecture to handle long-running jobs independently from the web dyno, preventing job failures due to web dyno restarts. Includes dead job detection via heartbeat mechanism.

---

## New Files to Create

### 1. `scripts/python/worker.py`
**Purpose**: Main worker process that polls database for queued jobs and executes them.

**Key Functions**:
- `main()`: Entry point that runs infinite polling loop
- `poll_for_jobs()`: Queries database for jobs with `status = 'queued'`
- `process_job(run_id)`: Executes the workflow for a single job
- `update_job_status(run_id, status, **kwargs)`: Updates job status in database
- `handle_job_error(run_id, error)`: Marks job as failed with error details

**Structure**:
```python
- Imports (app utilities, main module, database functions)
- Worker-specific progress callback (updates DB, not session state)
- Main polling loop (every 5-10 seconds)
- Job processing logic
- Error handling and logging
- Graceful shutdown on SIGTERM
```

**Dependencies**:
- Uses `get_db_connection()` from `app.py`
- Uses `run_full_workflow()` from `main.py`
- Uses `load_runs()`, `save_runs()` from `app.py` (or direct DB access)

---

### 2. `scripts/python/worker_utils.py`
**Purpose**: Shared utilities for worker process.

**Key Functions**:
- `get_db_connection()`: Database connection (reuse from app.py or standalone)
- `update_job_heartbeat(run_id)`: Updates heartbeat timestamp
- `mark_job_as_queued(run_id)`: Sets job status to queued
- `mark_job_as_running(run_id)`: Sets job status to running
- `mark_job_as_failed(run_id, error, error_details)`: Marks job as failed
- `mark_job_as_completed(run_id, results)`: Marks job as completed
- `get_queued_jobs()`: Returns list of queued job IDs
- `get_running_jobs()`: Returns list of running job IDs (for recovery)

**Database Operations**:
- Direct SQL queries (more efficient than loading all runs)
- Updates `status`, `heartbeat_at`, `updated_at`, `progress`, `output_lines`
- Handles JSONB serialization for progress/output_lines

---

## Files to Modify

### 1. `Procfile`
**Changes**:
```
web: python -u -m streamlit run scripts/python/app.py --server.port=$PORT --server.address=0.0.0.0
worker: python -u scripts/python/worker.py
```

---

### 2. `scripts/python/app.py`

#### A. Database Schema Updates
**Location**: `init_database()` function (around line 77-122)

**Changes**:
- Add `heartbeat_at TIMESTAMP` column to `runs` table
- Add index on `heartbeat_at` for faster dead job queries
- Add index on `status` + `heartbeat_at` for worker polling

**SQL**:
```sql
ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_runs_status_heartbeat ON runs(status, heartbeat_at);
```

#### B. New Function: `detect_and_mark_dead_jobs()`
**Location**: After `load_runs()` function (around line 270)

**Purpose**: Detects jobs that haven't updated heartbeat in > 2 minutes

**Logic**:
1. Query database for jobs with `status = 'running'`
2. Check if `heartbeat_at` is NULL or older than 2 minutes
3. Mark as `'failed'` with error: "Job appears to have stopped (no heartbeat detected for > 2 minutes). Possible dyno restart or process crash."
4. Set `completed_at` to last heartbeat time (or `updated_at` if heartbeat is NULL)
5. Return count of dead jobs marked

**Parameters**: `stale_threshold_minutes=2` (configurable)

#### C. Modify `load_runs()` function
**Location**: Around line 215-270

**Changes**:
- Add `heartbeat_at` to SELECT query
- Include `heartbeat_at` in returned run dictionary
- Deserialize `heartbeat_at` timestamp

#### D. Modify `save_runs()` function
**Location**: Around line 272-330

**Changes**:
- Include `heartbeat_at` in INSERT/UPDATE
- Update `heartbeat_at` when provided in run dict

#### E. Modify `progress_callback()` function
**Location**: Around line 762-926

**Changes**:
1. Add heartbeat update on every callback:
   - Set `heartbeat_at = datetime.now()` in run dict
   - Update database with heartbeat timestamp
2. Ensure `updated_at` is also updated (already handled by DB)

**Key Addition**:
```python
# Update heartbeat on every progress update
run['heartbeat_at'] = datetime.now()
```

#### F. Modify Job Creation (Workflow Start)
**Location**: Around line 1870-1925 (in the workflow thread)

**Changes**:
1. **Remove**: Direct `run_full_workflow()` call in background thread
2. **Change**: Set job status to `'queued'` instead of `'running'`
3. **Remove**: Background thread execution (worker will pick it up)
4. **Keep**: Job creation and initial status save

**Before**:
```python
thread = threading.Thread(target=run_workflow, daemon=True)
thread.start()
```

**After**:
```python
# Just mark as queued - worker will pick it up
runs = load_runs()
for run in runs:
    if run.get('run_id') == run_id:
        run['status'] = 'queued'
        save_runs(runs)
        break
```

#### G. Jobs Page - Call Dead Job Detection
**Location**: Around line 1948-1950 (after `load_runs()`)

**Changes**:
- Call `detect_and_mark_dead_jobs()` after loading runs
- Display message if dead jobs were detected: "⚠️ Detected X dead job(s) and marked as failed"

**Code**:
```python
fresh_runs = load_runs()
dead_count = detect_and_mark_dead_jobs()
if dead_count > 0:
    st.warning(f"⚠️ Detected {dead_count} dead job(s) and marked as failed")
st.session_state.runs = fresh_runs
```

#### H. Add Helper Function: `update_job_heartbeat()`
**Location**: Near other database helper functions

**Purpose**: Update heartbeat for a specific job (called by worker)

**Function**:
```python
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
        print(f"Error updating heartbeat: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()
```

---

### 3. `scripts/python/main.py`

#### A. Add Heartbeat Updates in Workflow Loop
**Location**: In `run_full_workflow()` function, main loop (around line 1600-2150)

**Changes**:
1. Add heartbeat update every 30-60 seconds during long operations
2. Update heartbeat while waiting for external APIs (Salesforce index build, etc.)
3. Ensure heartbeat updates even when no progress callback is called

**Implementation**:
- Add `last_heartbeat = datetime.now()` at start of workflow
- In main loop, check if `(datetime.now() - last_heartbeat).seconds > 30`
- If so, call progress callback with heartbeat-only update:
  ```python
  if progress_callback:
      progress_callback({
          'status': 'heartbeat',
          'run_id': run_id,
          'cycle': cycle_number,
          'step': current_step
      })
  last_heartbeat = datetime.now()
  ```

#### B. Update Progress Callback Calls
**Location**: Throughout workflow (Step 1, 2, 3)

**Changes**:
- Ensure all `progress_callback()` calls include `'run_id'`
- Ensure heartbeat is updated on every callback (handled in app.py)

---

## Database Schema Changes

### New Column: `heartbeat_at`
- **Type**: `TIMESTAMP`
- **Nullable**: `YES` (NULL for queued jobs, set when running)
- **Default**: `NULL`
- **Purpose**: Track last activity time for running jobs

### New Indexes:
1. `idx_runs_status_heartbeat`: Composite index on `(status, heartbeat_at)`
   - Speeds up worker polling for queued jobs
   - Speeds up dead job detection queries

2. `idx_runs_heartbeat`: Index on `heartbeat_at` (if needed for dead job queries)

---

## Implementation Order

### Phase 1: Database Schema & Dead Job Detection
1. Update `init_database()` to add `heartbeat_at` column
2. Update `load_runs()` to include `heartbeat_at`
3. Update `save_runs()` to handle `heartbeat_at`
4. Create `detect_and_mark_dead_jobs()` function
5. Add heartbeat updates to `progress_callback()`
6. Call dead job detection on Jobs page
7. **Test**: Verify dead jobs are detected and marked

### Phase 2: Worker Utilities
1. Create `worker_utils.py` with database helper functions
2. Implement `get_queued_jobs()`, `update_job_heartbeat()`, etc.
3. **Test**: Verify worker utilities can connect and query database

### Phase 3: Worker Process
1. Create `worker.py` with polling loop
2. Implement job processing logic
3. Add error handling and logging
4. **Test**: Worker picks up queued jobs and processes them

### Phase 4: Job Creation Changes
1. Modify job creation to set status `'queued'` instead of `'running'`
2. Remove background thread execution
3. **Test**: Jobs are queued and picked up by worker

### Phase 5: Heartbeat in Workflow
1. Add heartbeat updates in `main.py` workflow loop
2. Add heartbeat during long waits (Salesforce API polling)
3. **Test**: Heartbeat updates every 30-60 seconds during job execution

### Phase 6: Procfile Update
1. Add worker process to `Procfile`
2. **Test**: Both web and worker can run simultaneously locally

---

## Testing Plan

### Local Testing (Before Heroku Deployment)

#### Test 1: Database Connection
- Verify `get_db_connection()` works with Heroku Postgres URL
- Verify schema updates apply correctly

#### Test 2: Dead Job Detection
- Create a test job with old `heartbeat_at`
- Call `detect_and_mark_dead_jobs()`
- Verify job is marked as failed

#### Test 3: Worker Utilities
- Test `get_queued_jobs()` returns correct jobs
- Test `update_job_heartbeat()` updates timestamp
- Test status update functions

#### Test 4: Worker Process
- Start worker in separate terminal
- Create job from UI (should be queued)
- Verify worker picks it up
- Verify job processes correctly
- Verify progress updates in UI

#### Test 5: Process Isolation
- Stop worker → verify job stays in database
- Restart worker → verify job resumes
- Stop web → verify worker continues
- Restart web → verify UI shows updated progress

#### Test 6: Heartbeat Mechanism
- Start long-running job
- Monitor database: `SELECT heartbeat_at FROM runs WHERE run_id = '...'`
- Verify `heartbeat_at` updates every 30-60 seconds

#### Test 7: Multiple Jobs
- Queue 3 jobs
- Verify worker processes them sequentially
- Verify all complete successfully

#### Test 8: Error Handling
- Create job with invalid config
- Verify worker marks it as failed
- Verify error message appears in UI

---

## Configuration

### Environment Variables
- `DATABASE_URL`: Already used, no changes needed
- `WORKER_POLL_INTERVAL`: Optional, default 5 seconds
- `HEARTBEAT_INTERVAL`: Optional, default 30 seconds
- `DEAD_JOB_THRESHOLD_MINUTES`: Optional, default 2 minutes

### Worker Settings
- Poll interval: 5-10 seconds (configurable)
- Heartbeat update: Every 30-60 seconds during workflow
- Dead job threshold: 2 minutes of no heartbeat

---

## Error Handling

### Worker Errors
- Job processing fails → Mark as `'failed'` with error message
- Database connection fails → Log error, retry after delay
- Workflow exception → Catch, log, mark job as failed

### Web App Errors
- Dead job detection fails → Log error, continue (don't crash page)
- Database connection fails → Fall back to JSON file (if available)

---

## Logging

### Worker Logs
- Job found: `[WORKER] Found queued job: {run_id}`
- Job started: `[WORKER] Processing job: {run_id}`
- Job completed: `[WORKER] Job completed: {run_id}`
- Job failed: `[WORKER] Job failed: {run_id} - {error}`
- Heartbeat: `[WORKER] Heartbeat updated for job: {run_id}`

### Web App Logs
- Dead jobs detected: `[APP] Detected {count} dead job(s)`
- Job queued: `[APP] Job queued: {run_id}`

---

## Rollback Plan

If issues arise:
1. Remove worker process from `Procfile`
2. Revert job creation to use background threads
3. Keep dead job detection (it's safe)
4. Keep heartbeat mechanism (it's safe)

Archive location: `archive_pre_worker_20260104_121826/`

---

## Success Criteria

✅ Worker dyno processes queued jobs successfully  
✅ Jobs survive web dyno restarts  
✅ Dead jobs are automatically detected and marked as failed  
✅ Heartbeat updates every 30-60 seconds during job execution  
✅ Multiple jobs can be queued and processed  
✅ Error handling works correctly  
✅ Local testing passes all test cases  

---

## Notes

- Worker and web share the same database (Heroku Postgres)
- No need for message queue (Redis, etc.) - database acts as queue
- Worker processes jobs sequentially (can be parallelized later if needed)
- Heartbeat threshold of 2 minutes is conservative (jobs can run for hours)
- Dead job detection runs on page load (can be moved to background task later)

