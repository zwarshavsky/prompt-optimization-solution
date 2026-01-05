#!/usr/bin/env python3
"""
Prompt Optimization Workflow - Main Script

Usage:
    # Test questions through prompt
    python main.py test --excel "path/to/file.xlsx" --sheet "Sheet Name"
    
    # Analyze with Gemini
    python main.py analyze --excel "path/to/file.xlsx" --sheet "Sheet Name" --pdf "path/to/pdf.pdf"
"""

import pandas as pd
import argparse
import sys
import os
import json
import re
import time
import yaml
import asyncio
from pathlib import Path
from datetime import datetime
from gemini_client import genai
from excel_io import create_analysis_sheet_with_prompts, update_run_summary_sheet
from salesforce_api import (
    get_salesforce_credentials,
    invoke_prompt,
    retrieve_metadata_via_api,
    SearchIndexAPI,
)
from playwright_scripts import update_search_index_prompt

# Helper function for immediate output flushing
def log_print(*args, **kwargs):
    """Print with immediate flush for real-time terminal output"""
    print(*args, **kwargs, flush=True)


# ============================================================================
# STATE MANAGEMENT: Resume/Checkpoint Support
# ============================================================================

def get_state_dir():
    """Get the state directory path, create if it doesn't exist"""
    # Use app_data/state structure (relative to script)
    app_data = Path(__file__).parent / "app_data"
    app_data.mkdir(exist_ok=True)
    state_dir = app_data / "state"
    state_dir.mkdir(exist_ok=True)
    return state_dir

def check_index_lock(search_index_id):
    """Check if an index is already being processed by another run"""
    state_dir = get_state_dir()
    lock_file = state_dir / f"index_lock_{search_index_id}.lock"
    
    if lock_file.exists():
        try:
            with open(lock_file, 'r') as f:
                lock_data = json.load(f)
                pid = lock_data.get('pid')
                timestamp = lock_data.get('timestamp', 'Unknown')
                run_id = lock_data.get('run_id', 'Unknown')
                
                # Check if process is still running
                if pid:
                    try:
                        os.kill(pid, 0)  # Signal 0 just checks if process exists
                        # Process exists - index is locked
                        return True, f"Index {search_index_id} is already being processed by run {run_id} (PID: {pid}, started: {timestamp})"
                    except ProcessLookupError:
                        # Process doesn't exist - stale lock, remove it
                        lock_file.unlink()
                        return False, None
                    except PermissionError:
                        # Process exists but we can't signal it (different user) - assume it's running
                        return True, f"Index {search_index_id} is already being processed by run {run_id} (PID: {pid}, started: {timestamp})"
                else:
                    # Lock file exists but no PID - stale, remove it
                    lock_file.unlink()
                    return False, None
        except (json.JSONDecodeError, KeyError):
            # Corrupted lock file - remove it
            lock_file.unlink()
            return False, None
    
    return False, None

def acquire_index_lock(search_index_id, run_id):
    """Acquire a lock for an index to prevent concurrent processing"""
    state_dir = get_state_dir()
    lock_file = state_dir / f"index_lock_{search_index_id}.lock"
    
    # Check if already locked
    is_locked, error_msg = check_index_lock(search_index_id)
    if is_locked:
        return False, error_msg
    
    # Create lock file
    lock_data = {
        'search_index_id': search_index_id,
        'run_id': run_id,
        'pid': os.getpid(),
        'timestamp': time.strftime("%Y-%m-%dT%H:%M:%S")
    }
    
    try:
        with open(lock_file, 'w') as f:
            json.dump(lock_data, f, indent=2)
        return True, None
    except Exception as e:
        return False, f"Failed to create lock file: {e}"

def release_index_lock(search_index_id):
    """Release the lock for an index"""
    state_dir = get_state_dir()
    lock_file = state_dir / f"index_lock_{search_index_id}.lock"
    
    if lock_file.exists():
        try:
            lock_file.unlink()
            return True
        except Exception as e:
            log_print(f"  ⚠️  Warning: Could not remove lock file: {e}")
            return False
    return True

def save_state(cycle_number, last_completed_step, sheet_name, refinement_stage,
               stage_status=None, proposed_llm_parser_prompt=None,
               proposed_response_prompt=None, stage_complete_reason=None,
               excel_file=None, run_id=None, yaml_config_snapshot=None):
    """Save workflow state to JSON file"""
    state_dir = get_state_dir()
    
    # Use run-specific state file if run_id provided
    if run_id:
        state_file = state_dir / f"run_{run_id}_state.json"
    else:
        state_file = state_dir / "current_state.json"
    
    state = {
        "run_id": run_id,
        "cycle_number": cycle_number,
        "last_completed_step": last_completed_step,
        "sheet_name": sheet_name,
        "refinement_stage": refinement_stage,
        "stage_status": stage_status,
        "proposed_llm_parser_prompt": proposed_llm_parser_prompt,
        "proposed_response_prompt": proposed_response_prompt,
        "stage_complete_reason": stage_complete_reason,
        "excel_file": excel_file,
        "yaml_config_snapshot": yaml_config_snapshot,  # Frozen YAML config
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
    }
    
    # Also save cycle-specific archive (always save, even if run_id is None)
    archive_file = state_dir / f"cycle_{cycle_number}_state.json"
    try:
        with open(archive_file, 'w') as f:
            json.dump(state, f, indent=2)
        log_print(f"  💾 Cycle-specific state saved: {archive_file.name}")
    except Exception as e:
        log_print(f"  ⚠️  Warning: Could not save cycle-specific state: {e}")
    
    # Save current/run state
    with open(state_file, 'w') as f:
        json.dump(state, f, indent=2)
    
    log_print(f"  💾 State saved: cycle {cycle_number}, step {last_completed_step}")

def load_state(resume_from_step=None, resume_from_cycle=None, run_id=None):
    """Load workflow state from JSON file"""
    state_dir = get_state_dir()
    
    # Determine which state file to load
    if run_id:
        state_file = state_dir / f"run_{run_id}_state.json"
    elif resume_from_cycle:
        # Try to find state file with matching cycle
        state_file = state_dir / f"cycle_{resume_from_cycle}_state.json"
    else:
        # Find latest run state file
        state_files = list(state_dir.glob('run_*_state.json'))
        if state_files:
            state_file = max(state_files, key=lambda p: p.stat().st_mtime)
        else:
            state_file = state_dir / "current_state.json"
    
    if not state_file.exists():
        return None
    
    try:
        with open(state_file, 'r') as f:
            state = json.load(f)
        
        # If resuming from specific step, validate it matches
        if resume_from_step and state.get('last_completed_step') != resume_from_step - 1:
            log_print(f"  ⚠️  Warning: State shows last completed step {state.get('last_completed_step')}, but resuming from step {resume_from_step}")
        
        return state
    except (json.JSONDecodeError, KeyError) as e:
        log_print(f"  ❌ ERROR: State file is corrupted: {e}")
        return None

def validate_state(state, excel_file):
    """Validate that state is consistent with Excel file"""
    if not state:
        return False, "No state to validate"
    
    # Resolve Excel file path - handle both absolute and relative paths
    excel_path = Path(excel_file)
    if not excel_path.is_absolute():
        # If relative, try resolution strategies
        if not excel_path.exists():
            # Strategy 1: Resolve from app_data/outputs (where Excel files are stored)
            app_data = Path(__file__).parent / "app_data" / "outputs"
            excel_path_attempt = app_data / Path(excel_file).name
            if excel_path_attempt.exists():
                excel_path = excel_path_attempt
            else:
                # Strategy 2: Try as relative to script location
                script_dir = Path(__file__).parent
                excel_path_attempt = script_dir / excel_file
                if excel_path_attempt.exists():
                    excel_path = excel_path_attempt
    
    if not excel_path.exists():
        return False, f"Excel file not found: {excel_file} (tried: {excel_path})"
    
    excel_file = str(excel_path)  # Update to absolute path
    
    # Check sheet exists
    try:
        xls = pd.ExcelFile(excel_file)
        sheet_name = state.get('sheet_name')
        if sheet_name not in xls.sheet_names:
            return False, f"Sheet '{sheet_name}' not found in Excel file"
    except Exception as e:
        return False, f"Error reading Excel file: {e}"
    
    # Validate step-specific requirements
    last_step = state.get('last_completed_step', 0)
    
    if last_step >= 2:
        # Step 2+ requires sheet_name (test was completed)
        if not state.get('sheet_name'):
            return False, "State shows Step 2+ completed but no sheet_name found"
    
    if last_step >= 3:
        # Step 3+ requires stage_status and proposed prompt (analysis was completed)
        if not state.get('stage_status'):
            return False, "State shows Step 3+ completed but no stage_status found"
        if state.get('refinement_stage') == 'llm_parser':
            if not state.get('proposed_llm_parser_prompt'):
                return False, "State shows Step 3+ completed but no proposed_llm_parser_prompt found"
    
    return True, "State validated successfully"

def clean_state():
    """Delete all state files"""
    state_dir = get_state_dir()
    state_file = state_dir / "current_state.json"
    
    if state_file.exists():
        state_file.unlink()
        log_print("  🗑️  Deleted current_state.json")
    
    # Optionally delete archive files too
    for archive_file in state_dir.glob("cycle_*_state.json"):
        archive_file.unlink()
        log_print(f"  🗑️  Deleted {archive_file.name}")
    
    log_print("  ✅ State files cleaned")

def show_state():
    """Display current state without resuming"""
    state = load_state()
    if not state:
        log_print("  ℹ️  No state file found")
        return
    
    log_print("="*80)
    log_print("CURRENT WORKFLOW STATE")
    log_print("="*80)
    log_print(f"  Cycle Number: {state.get('cycle_number')}")
    log_print(f"  Last Completed Step: {state.get('last_completed_step')}")
    log_print(f"  Sheet Name: {state.get('sheet_name')}")
    log_print(f"  Refinement Stage: {state.get('refinement_stage')}")
    log_print(f"  Stage Status: {state.get('stage_status', 'N/A')}")
    log_print(f"  Timestamp: {state.get('timestamp', 'N/A')}")
    log_print("="*80)


def extract_results_from_sheet(excel_file, sheet_name):
    """
    Extract results data from a cycle sheet for summary sheet update.
    
    Args:
        excel_file: Path to Excel file
        sheet_name: Name of sheet to extract from
        
    Returns:
        dict with keys: timestamp, pass_count, fail_count, total, pass_rate, 
        avg_safety, stage_status, question_results
    """
    from datetime import datetime
    import pandas as pd
    
    try:
        df = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)
        
        # Find header row
        header_row_idx = None
        for idx in range(min(20, len(df))):
            row_str = ' '.join([str(x) for x in df.iloc[idx].values if pd.notna(x)])
            if 'Pass/Fail' in row_str and 'Safety Score' in row_str:
                header_row_idx = idx
                break
        
        if header_row_idx is None:
            log_print(f"  ⚠️  Could not find header row in sheet {sheet_name}")
            return None
        
        # Read data starting from header row
        data_df = pd.read_excel(excel_file, sheet_name=sheet_name, header=header_row_idx)
        
        # Find columns
        pf_col = None
        safety_col = None
        for col in data_df.columns:
            col_str = str(col).lower()
            if 'pass/fail' in col_str:
                pf_col = col
            elif 'safety score' in col_str:
                safety_col = col
        
        if pf_col is None:
            log_print(f"  ⚠️  Could not find Pass/Fail column in sheet {sheet_name}")
            return None
        
        # Extract results
        pass_count = 0
        fail_count = 0
        safety_scores = []
        question_results = []
        
        # Find Q# column
        q_col = None
        for col in data_df.columns:
            if str(col).strip() == 'Q#':
                q_col = col
                break
        
        for idx, row in data_df.iterrows():
            pf_val = str(row[pf_col]).upper() if pd.notna(row[pf_col]) else ''
            if 'PASS' in pf_val or '✅' in pf_val:
                pass_count += 1
                status = 'PASS'
            elif 'FAIL' in pf_val or '❌' in pf_val:
                fail_count += 1
                status = 'FAIL'
            else:
                continue
            
            if safety_col and pd.notna(row[safety_col]):
                try:
                    score = float(row[safety_col])
                    safety_scores.append(score)
                except:
                    pass
            
            # Get question number
            q_number = None
            if q_col and pd.notna(row[q_col]):
                q_number = str(row[q_col]).strip()
            
            if q_number:
                question_results.append({
                    'q_number': q_number,
                    'status': status
                })
        
        total = pass_count + fail_count
        pass_rate = (pass_count / total * 100) if total > 0 else 0
        avg_safety = sum(safety_scores) / len(safety_scores) if safety_scores else 0
        
        # Get stage status from sheet metadata
        stage_status = 'needs_improvement'  # Default
        try:
            for idx in range(min(10, len(df))):
                cell_val = str(df.iloc[idx, 0]) if pd.notna(df.iloc[idx, 0]) else ''
                if 'Stage Status' in cell_val and idx + 1 < len(df):
                    status_val = df.iloc[idx, 1]
                    if pd.notna(status_val):
                        stage_status = str(status_val).strip()
                        break
        except:
            pass
        
        return {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'pass_count': pass_count,
            'fail_count': fail_count,
            'total': total,
            'pass_rate': pass_rate,
            'avg_safety': avg_safety,
            'stage_status': stage_status,
            'question_results': question_results
        }
        
    except Exception as e:
        log_print(f"  ⚠️  Error extracting results from sheet: {e}")
        import traceback
        traceback.print_exc()
        return None


# ============================================================================
# TEST MODE: Removed - use test_gemini.py for sequential question processing
# ============================================================================


# ============================================================================
# ANALYZE MODE: Use Gemini to score and suggest improvements
# ============================================================================

def analyze_with_gemini(excel_file, sheet_name, pdf_files=None, model_name=None, 
                        config_dict=None, cycle_number=None):
    """Analyze responses using Gemini Pro API
    
    Args:
        excel_file: Path to Excel file
        sheet_name: Name of sheet to analyze
        pdf_files: List of PDF file paths for context (or single file path for backward compatibility)
        model_name: Gemini model name
        config_dict: Frozen YAML config dictionary (REQUIRED)
        cycle_number: Current cycle number (used to load previous cycle context if available)
    """
    # Handle backward compatibility: if pdf_files is a string, convert to list
    if pdf_files and isinstance(pdf_files, (str, Path)):
        pdf_files = [Path(pdf_files)]
    elif pdf_files is None:
        pdf_files = []
    log_print("="*80)
    log_print("ANALYZE MODE: Scoring with Gemini")
    log_print("="*80)
    
    # Config dict is REQUIRED - no fallbacks
    if not config_dict:
        log_print("❌ ERROR: config_dict is REQUIRED")
        log_print("   Please provide frozen YAML config dictionary")
        sys.exit(1)
    
    yaml_config = config_dict
    
    # Check API key (try environment variable first, then config file)
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        try:
            # Try to get API key from local config first, then fall back to main config
            try:
                from gemini_config_local import GEMINI_API_KEY
            except ImportError:
                from gemini_config import GEMINI_API_KEY
            api_key = GEMINI_API_KEY
        except ImportError:
            pass
    
    if not api_key:
        log_print("❌ GEMINI_API_KEY not set. Get it from: https://aistudio.google.com/app/apikey")
        log_print("   Or set it in gemini_config.py")
        sys.exit(1)
    
    # Require sheet_name (no auto-detection)
    if not sheet_name:
        log_print("❌ Error: sheet_name is required (auto-detection removed)")
        sys.exit(1)
    
    # Resolve Excel file path (handle both absolute and relative paths)
    excel_path = Path(excel_file)
    if not excel_path.is_absolute():
        if not excel_path.exists():
            # Try app_data/outputs first (where Excel files are stored)
            app_data = Path(__file__).parent / "app_data" / "outputs"
            excel_path_attempt = app_data / Path(excel_file).name
            if excel_path_attempt.exists():
                excel_path = excel_path_attempt
            else:
                # Fallback to script directory
                script_dir = Path(__file__).parent
                excel_path_attempt = script_dir / excel_file
                if excel_path_attempt.exists():
                    excel_path = excel_path_attempt
    
    if not excel_path.exists():
        log_print(f"❌ ERROR: Excel file not found: {excel_file}")
        log_print(f"   Tried: {excel_path}")
        sys.exit(1)
    
    excel_file = str(excel_path)  # Update to absolute path
    
    # Read worksheet from provided sheet
    log_print(f"Reading: {excel_file} (sheet: {sheet_name})")
    df = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)
    
    # Find header row and column indices for analysis columns
    header_row_idx = None
    col_indices = {
        'Pass/Fail': None,
        'Safety Score': None,
        'Root Cause/Explanation': None,
        'Prompt Modification Next Version': None
    }
    
    for idx in range(min(20, len(df))):
        row_str = ' '.join([str(x) for x in df.iloc[idx].values if pd.notna(x)])
        if 'Pass/Fail' in row_str and 'Safety Score' in row_str:
            header_row_idx = idx
            log_print(f"  ✓ Found header row at index {idx}")
            # Find column indices
            for col_idx in range(len(df.columns)):
                val = str(df.iloc[idx, col_idx]).strip() if pd.notna(df.iloc[idx, col_idx]) else ''
                if 'Pass/Fail' in val:
                    col_indices['Pass/Fail'] = col_idx
                elif 'Safety Score' in val:
                    col_indices['Safety Score'] = col_idx
                elif 'Root Cause' in val or 'Explanation' in val:
                    col_indices['Root Cause/Explanation'] = col_idx
                elif 'Prompt Modification' in val:
                    col_indices['Prompt Modification Next Version'] = col_idx
            
            log_print(f"  ✓ Column indices: {col_indices}")
            break
    
    if header_row_idx is None:
        log_print("  ⚠️  Warning: Could not find header row with 'Pass/Fail' and 'Safety Score'")
        log_print("  ⚠️  Will attempt to use default column positions")
        # Default positions based on typical structure
        col_indices = {
            'Pass/Fail': 5,
            'Safety Score': 6,
            'Root Cause/Explanation': 7,
            'Prompt Modification Next Version': 8
        }
    
    # Update the same sheet (no new sheet creation)
    log_print(f"  ✓ Will update existing sheet: {sheet_name}")
    
    # Configure Gemini API with timeout settings
    GEMINI_TIMEOUT_SECONDS = 180  # 3 minutes timeout
    genai.configure(api_key=api_key)
    
    # Upload all PDF files if provided
    pdf_file_objs = []
    if pdf_files:
        log_print(f"Uploading {len(pdf_files)} PDF file(s) to Gemini...")
        for pdf_file_path in pdf_files:
            pdf_file_str = str(pdf_file_path)
            if os.path.exists(pdf_file_str):
                log_print(f"  📄 Uploading: {pdf_file_path.name}...")
                try:
                    pdf_file_obj = genai.upload_file(path=pdf_file_str, mime_type="application/pdf")
                    while genai.get_file(pdf_file_obj.name).state.name == "PROCESSING":
                        print(".", end="", flush=True)
                        time.sleep(2)
                        pdf_file_obj = genai.get_file(pdf_file_obj.name)
                    print(" ✓")
                    pdf_file_objs.append(pdf_file_obj)
                except Exception as e:
                    log_print(f"  ⚠️  Failed to upload {pdf_file_path.name}: {e}")
            else:
                log_print(f"  ⚠️  PDF file not found: {pdf_file_str}")
        
        if pdf_file_objs:
            log_print(f"  ✅ Successfully uploaded {len(pdf_file_objs)} PDF file(s)")
        else:
            log_print(f"  ⚠️  No PDF files were successfully uploaded")
    
    # Get LLM parser prompt from sheet - DYNAMIC search for label
    # Find "LLM Parser Prompt Current:" label, then get value from next row, column 1 (B)
    log_print("Getting LLM parser prompt from sheet...")
    llm_parser_prompt = ""
    
    try:
        # Search for the label dynamically
        found_label = False
        for idx in range(len(df)):
            if str(df.iloc[idx, 0]).strip() == 'LLM Parser Prompt Current:':
                found_label = True
                # Value is in the next row, column 1 (B)
                if idx + 1 < len(df):
                    prompt_val = df.iloc[idx + 1, 1]  # Column 1 = Excel column B
                    if pd.notna(prompt_val) and str(prompt_val).strip() and str(prompt_val).strip() != 'Not found':
                        llm_parser_prompt = str(prompt_val).strip()
                        log_print(f"  ✓ Found LLM parser prompt from sheet (row {idx + 1}, column 1, {len(llm_parser_prompt)} chars)")
                    else:
                        log_print(f"  ❌ ERROR: LLM parser prompt value is empty or 'Not found' at row {idx + 1}, column 1")
                        log_print(f"  ❌ Value found: '{prompt_val}'")
                        log_print("  ❌ LLM parser prompt is REQUIRED for Gemini analysis. Exiting.")
                        sys.exit(1)
                else:
                    log_print(f"  ❌ ERROR: Label found at row {idx}, but no value row exists")
                    log_print("  ❌ LLM parser prompt is REQUIRED for Gemini analysis. Exiting.")
                    sys.exit(1)
                break
        
        if not found_label:
            log_print("  ❌ ERROR: 'LLM Parser Prompt Current:' label not found in sheet")
            log_print("  ❌ LLM parser prompt is REQUIRED for Gemini analysis. Exiting.")
            sys.exit(1)
    except Exception as e:
        log_print(f"  ❌ ERROR reading LLM parser prompt from sheet: {e}")
        log_print("  ❌ LLM parser prompt is REQUIRED for Gemini analysis. Exiting.")
        sys.exit(1)
    
    # Get response prompt template from sheet - DYNAMIC search for label
    # Find "Prompt Builder Prompt:" label in BOTTOM metadata section (after questions), then get value from next row, column 1 (B)
    # Note: There are TWO "Prompt Builder Prompt:" labels - one in top metadata (row 3) and one in bottom metadata
    # We need the BOTTOM one, so search from bottom up
    log_print("Getting response prompt template from sheet...")
    response_prompt_template = ""
    response_model = ""
    
    try:
        # Find header row first to know where questions end
        header_row_idx = None
        for idx in range(len(df)):
            row_str = ' '.join([str(df.iloc[idx, col]) for col in range(min(5, len(df.columns)))])
            if 'Q#' in row_str or 'Question' in row_str:
                header_row_idx = idx
                break
        
        # Search for the label dynamically, starting from AFTER the header row (to avoid top metadata)
        # Search from bottom up to get the LAST occurrence (which is in bottom metadata)
        found_label = False
        search_start = header_row_idx + 1 if header_row_idx is not None else 0
        
        for idx in range(len(df) - 1, search_start - 1, -1):  # Search from bottom up
            if str(df.iloc[idx, 0]).strip() == 'Prompt Builder Prompt:':
                found_label = True
                # Value is in the next row, column 1 (B)
                if idx + 1 < len(df):
                    prompt_val = df.iloc[idx + 1, 1]  # Column 1 = Excel column B
                    if pd.notna(prompt_val) and str(prompt_val).strip() and str(prompt_val).strip() != 'Not found':
                        response_prompt_template = str(prompt_val).strip()
                        log_print(f"  ✓ Found response prompt template from sheet (row {idx + 1}, column 1, {len(response_prompt_template)} chars)")
                    else:
                        log_print(f"  ❌ ERROR: Response prompt template value is empty or 'Not found' at row {idx + 1}, column 1")
                        log_print(f"  ❌ Value found: '{prompt_val}'")
                        log_print("  ❌ Response prompt template is REQUIRED for Gemini analysis. Exiting.")
                        sys.exit(1)
                else:
                    log_print(f"  ❌ ERROR: Label found at row {idx}, but no value row exists")
                    log_print("  ❌ Response prompt template is REQUIRED for Gemini analysis. Exiting.")
                    sys.exit(1)
                break
        
        if not found_label:
            log_print("  ❌ ERROR: 'Prompt Builder Prompt:' label not found in sheet (bottom metadata section)")
            log_print("  ❌ Response prompt template is REQUIRED for Gemini analysis. Exiting.")
            sys.exit(1)
    except Exception as e:
        log_print(f"  ❌ ERROR reading response prompt template from sheet: {e}")
        log_print("  ❌ Response prompt template is REQUIRED for Gemini analysis. Exiting.")
        sys.exit(1)
    
    # Try to extract model from response prompt template if it contains model info
    # (This is a fallback - model might be in a different metadata row)
    if response_prompt_template and not response_model:
        # Check if there's a model metadata row (this would need to be verified in the sheet structure)
        pass
    
    # Use frozen config dict - already loaded
    try:
        config_section = yaml_config.get('configuration', {})
        
        # Required fields - throw error if missing
        gemini_instructions_template = config_section.get('geminiInstructions', '')
        if not gemini_instructions_template:
            log_print("❌ ERROR: 'geminiInstructions' is required in YAML configuration")
            sys.exit(1)
        
        refinement_stage = config_section.get('refinementStage', 'llm_parser')
        refinement_stages = config_section.get('refinementStages', {})
        if not refinement_stages:
            log_print("❌ ERROR: 'refinementStages' is required in YAML configuration")
            sys.exit(1)
        
        stage_config = refinement_stages.get(refinement_stage, {})
        if not stage_config:
            log_print(f"❌ ERROR: Refinement stage '{refinement_stage}' not found in 'refinementStages'")
            sys.exit(1)
        
        # Read all required values from YAML
        refinement_stage_description = stage_config.get('description', '')
        refinement_stage_focus = stage_config.get('focus', '')
        root_cause_guidance = stage_config.get('rootCauseGuidance', '')
        modification_guidance = stage_config.get('modificationGuidance', '')
        custom_instructions = config_section.get('customInstructions', '')  # Optional
        
        # Validate required fields
        if not refinement_stage_description:
            log_print(f"❌ ERROR: 'description' is required for refinement stage '{refinement_stage}' in YAML")
            sys.exit(1)
        if not refinement_stage_focus:
            log_print(f"❌ ERROR: 'focus' is required for refinement stage '{refinement_stage}' in YAML")
            sys.exit(1)
        if not root_cause_guidance:
            log_print(f"❌ ERROR: 'rootCauseGuidance' is required for refinement stage '{refinement_stage}' in YAML")
            sys.exit(1)
        if not modification_guidance:
            log_print(f"❌ ERROR: 'modificationGuidance' is required for refinement stage '{refinement_stage}' in YAML")
            sys.exit(1)
        
        log_print(f"  ✓ Found Gemini instructions template in YAML")
        log_print(f"  ✓ Refinement stage: {refinement_stage}")
        if custom_instructions:
            log_print(f"  ✓ Found custom instructions in YAML ({len(custom_instructions)} chars)")
        
        # Build stage-specific task and focus type based on refinement stage
        if refinement_stage == "llm_parser":
            refinement_stage_task = "LLM Parser Optimization"
            refinement_stage_focus_type = "LLM Parser improvements"
            proposed_llm_parser_description = "COMPLETE FULL TEXT of the improved LLM Parser Prompt. This must be the entire prompt text, ready to use in the search index configuration. Base it on the current prompt shown above, incorporating all improvements. If the parser is already optimal, you may return the current prompt unchanged."
            proposed_response_prompt_description = "If LLM parser is maximized, provide COMPLETE FULL TEXT of improved Response Prompt Template. Otherwise, return the current template unchanged."
        elif refinement_stage == "response_prompt":
            refinement_stage_task = "Response Prompt Template Optimization"
            refinement_stage_focus_type = "Response Prompt Template improvements"
            proposed_llm_parser_description = "Return the current LLM Parser Prompt unchanged (already optimized)."
            proposed_response_prompt_description = "COMPLETE FULL TEXT of the improved Response Prompt Template. This must be the entire template text, ready to use. Base it on the current template shown above, incorporating all improvements."
        elif refinement_stage == "agentforce_agent":
            refinement_stage_task = "Agentforce Agent Optimization"
            refinement_stage_focus_type = "Agentforce Agent improvements"
            proposed_llm_parser_description = "Return the current LLM Parser Prompt unchanged (already optimized)."
            proposed_response_prompt_description = "Return the current Response Prompt Template unchanged (already optimized)."
        else:
            log_print(f"❌ ERROR: Unknown refinement stage '{refinement_stage}'. Must be 'llm_parser', 'response_prompt', or 'agentforce_agent'")
            sys.exit(1)
            
    except Exception as e:
        log_print(f"❌ ERROR: Failed to read YAML configuration: {e}")
        sys.exit(1)
    
    # Build available models list
    available_models = [
        "- sfdc_ai__DefaultBedrockAnthropicClaude4Sonnet",
        "- sfdc_ai__DefaultOpenAIGPT4",
        "- sfdc_ai__DefaultOpenAIGPT4OmniMini",
        "- sfdc_ai__DefaultOpenAIGPT4Turbo",
        "- sfdc_ai__DefaultAnthropicClaude35Sonnet",
        "- sfdc_ai__DefaultAnthropicClaude35Haiku",
        "- sfdc_ai__DefaultGoogleGemini15Pro",
        "- sfdc_ai__DefaultGoogleGemini15Flash"
    ]
    available_models_text = '\n'.join(available_models)
    
    # Load previous cycle context if available (for Cycle 2+)
    previous_cycle_context = ""
    if cycle_number and cycle_number > 1:
        log_print(f"\n📊 Loading previous cycle context (Cycle {cycle_number - 1})...")
        try:
            # Read all sheets from Excel to find previous cycle sheets
            xls = pd.ExcelFile(excel_file)
            all_sheets = xls.sheet_names
            
            # Find previous cycle sheets (matching pattern: analysis_{stage}_cycle{N-1}_*)
            prev_cycle_sheets = []
            for sheet in all_sheets:
                if sheet.startswith('analysis_'):
                    match = re.search(r'cycle(\d+)_', sheet)
                    if match:
                        sheet_cycle = int(match.group(1))
                        if sheet_cycle == cycle_number - 1:
                            prev_cycle_sheets.append(sheet)
            
            if prev_cycle_sheets:
                # Sort by timestamp (most recent first)
                prev_cycle_sheets.sort(reverse=True)
                prev_sheet = prev_cycle_sheets[0]  # Use most recent sheet from previous cycle
                
                log_print(f"  ✓ Found previous cycle sheet: {prev_sheet}")
                
                # Read previous cycle sheet
                try:
                    prev_df = pd.read_excel(excel_file, sheet_name=prev_sheet, header=None)
                    
                    # Extract key information from previous cycle:
                    # 1. Pass/Fail results
                    # 2. Stage Status
                    # 3. Proposed LLM Parser Prompt (what was changed)
                    # 4. Root Cause/Explanation (what problems were identified)
                    
                    prev_context_parts = []
                    prev_context_parts.append(f"# PREVIOUS CYCLE (Cycle {cycle_number - 1}) RESULTS")
                    prev_context_parts.append(f"Sheet: {prev_sheet}")
                    prev_context_parts.append("")
                    
                    # Find header row in previous sheet
                    prev_header_row = None
                    for idx in range(min(20, len(prev_df))):
                        row_str = ' '.join([str(x) for x in prev_df.iloc[idx].values if pd.notna(x)])
                        if 'Pass/Fail' in row_str and 'Safety Score' in row_str:
                            prev_header_row = idx
                            break
                    
                    if prev_header_row is not None:
                        # Extract Pass/Fail results
                        prev_context_parts.append("## Previous Cycle Test Results:")
                        prev_context_parts.append("")
                        
                        # Find Pass/Fail column
                        pass_fail_col = None
                        question_col = None
                        for col_idx in range(len(prev_df.columns)):
                            val = str(prev_df.iloc[prev_header_row, col_idx]).strip() if pd.notna(prev_df.iloc[prev_header_row, col_idx]) else ''
                            if 'Pass/Fail' in val:
                                pass_fail_col = col_idx
                            if 'Q#' in val or 'Question' in val:
                                question_col = col_idx
                        
                        # Find Received Answer column
                        received_answer_col = None
                        for col_idx in range(len(prev_df.columns)):
                            val = str(prev_df.iloc[prev_header_row, col_idx]).strip() if pd.notna(prev_df.iloc[prev_header_row, col_idx]) else ''
                            if 'Received Answer' in val:
                                received_answer_col = col_idx
                                break
                        
                        if pass_fail_col is not None:
                            prev_results = []
                            for i in range(prev_header_row + 1, min(prev_header_row + 20, len(prev_df))):
                                q_num = str(prev_df.iloc[i, 0]) if pd.notna(prev_df.iloc[i, 0]) else ""
                                if q_num.startswith('Q'):
                                    pass_fail = str(prev_df.iloc[i, pass_fail_col]) if pd.notna(prev_df.iloc[i, pass_fail_col]) else ""
                                    received_answer = ""
                                    if received_answer_col is not None:
                                        received_answer_val = str(prev_df.iloc[i, received_answer_col]) if pd.notna(prev_df.iloc[i, received_answer_col]) else ""
                                        if received_answer_val and received_answer_val.strip():
                                            # Truncate long answers for context (first 200 chars)
                                            received_answer_val_clean = received_answer_val.strip()
                                            if len(received_answer_val_clean) > 200:
                                                received_answer = f" | Answer: {received_answer_val_clean[:200]}..."
                                            else:
                                                received_answer = f" | Answer: {received_answer_val_clean}"
                                    
                                    if pass_fail:
                                        prev_results.append(f"  {q_num}: {pass_fail}{received_answer}")
                            
                            if prev_results:
                                prev_context_parts.extend(prev_results)
                                prev_context_parts.append("")
                    
                    # Extract Stage Status from previous cycle
                    prev_stage_status = None
                    prev_stage_reason = None
                    for i in range(min(10, len(prev_df))):
                        for j in range(min(3, len(prev_df.columns))):
                            cell = str(prev_df.iloc[i, j]) if pd.notna(prev_df.iloc[i, j]) else ""
                            if 'Stage Status' in cell:
                                if i + 1 < len(prev_df):
                                    prev_stage_status = str(prev_df.iloc[i + 1, j]) if pd.notna(prev_df.iloc[i + 1, j]) else None
                            if 'Stage Status Reason' in cell or 'Stage Complete Reason' in cell:
                                if i + 1 < len(prev_df):
                                    prev_stage_reason = str(prev_df.iloc[i + 1, j]) if pd.notna(prev_df.iloc[i + 1, j]) else None
                    
                    if prev_stage_status:
                        prev_context_parts.append(f"## Previous Cycle Stage Status: {prev_stage_status}")
                        if prev_stage_reason:
                            prev_context_parts.append(f"Reason: {prev_stage_reason[:300]}...")
                        prev_context_parts.append("")
                    
                    # Extract Proposed LLM Parser Prompt from previous cycle (what was changed)
                    prev_proposed_prompt = None
                    for i in range(len(prev_df)):
                        cell = str(prev_df.iloc[i, 0]) if pd.notna(prev_df.iloc[i, 0]) else ""
                        if 'LLM Parser Prompt Proposed from Gemini' in cell:
                            if i + 1 < len(prev_df):
                                prev_proposed_prompt = str(prev_df.iloc[i + 1, 1]) if len(prev_df.columns) > 1 and pd.notna(prev_df.iloc[i + 1, 1]) else None
                                if prev_proposed_prompt and len(prev_proposed_prompt) > 100:
                                    # Truncate for context (first 500 chars)
                                    prev_context_parts.append("## Previous Cycle Proposed LLM Parser Prompt (applied in this cycle):")
                                    prev_context_parts.append(f"{prev_proposed_prompt[:500]}...")
                                    prev_context_parts.append("")
                                    break
                    
                    previous_cycle_context = '\n'.join(prev_context_parts)
                    log_print(f"  ✓ Loaded previous cycle context ({len(previous_cycle_context)} chars)")
                    
                except Exception as e:
                    log_print(f"  ⚠️  Warning: Could not read previous cycle sheet: {e}")
                    previous_cycle_context = ""
            else:
                log_print(f"  ℹ️  No previous cycle sheets found (this may be Cycle 1)")
        except Exception as e:
            log_print(f"  ⚠️  Warning: Could not load previous cycle context: {e}")
            previous_cycle_context = ""
    
    # Create prompt from YAML template (required, no fallback)
    # IMPORTANT: Capture worksheet text BEFORE Gemini writes to it
    # This ensures "Instructions to Gemini" shows the actual prompt sent, not including Gemini's response
    # Also, exclude analysis columns (Root Cause/Explanation, Prompt Modification) from worksheet_text
    # since those are OUTPUT columns that will be filled by Gemini, not INPUT columns
    df_for_prompt = df.copy()  # Work with a copy to avoid modifying original
    
    # Exclude analysis columns from worksheet_text (they are OUTPUT columns, not INPUT)
    # These columns will be filled by Gemini, so they shouldn't be in the prompt sent to Gemini
    if header_row_idx is not None:
        # Find analysis column indices
        root_cause_col_idx = col_indices.get('Root Cause/Explanation')
        prompt_mod_col_idx = col_indices.get('Prompt Modification Next Version')
        
        # Drop analysis columns from the copy by index (exclude them entirely from worksheet_text)
        # Since we read with header=None, columns are numeric indices
        columns_to_drop = []
        if root_cause_col_idx is not None and root_cause_col_idx < len(df_for_prompt.columns):
            columns_to_drop.append(root_cause_col_idx)
        if prompt_mod_col_idx is not None and prompt_mod_col_idx < len(df_for_prompt.columns):
            columns_to_drop.append(prompt_mod_col_idx)
        
        if columns_to_drop:
            # Drop columns by index (sort in reverse to maintain indices)
            columns_to_drop_sorted = sorted(columns_to_drop, reverse=True)
            for col_idx in columns_to_drop_sorted:
                df_for_prompt = df_for_prompt.drop(df_for_prompt.columns[col_idx], axis=1)
            log_print(f"  ✓ Excluded analysis columns from worksheet_text (indices: {columns_to_drop})")
    
    worksheet_text = df_for_prompt.to_string(index=False)
    
    if gemini_instructions_template:
        # Build the output format section based on refinement stage
        if refinement_stage == "response_prompt":
            # Include both proposed prompts in output format
            output_format_section = f"""After the array, include a separate JSON object with the proposed prompts:

{{
  "LLM_Parser_Prompt_Proposed_from_Gemini": "{proposed_llm_parser_description}",
  "Prompt_Builder_Prompt_Proposed_from_Gemini": "{proposed_response_prompt_description}",
  "StageStatus": "optimized" or "needs_improvement",
  "StageCompleteReason": "Brief explanation of why this stage is complete or needs more work"
}}"""
            output_format_important = "- The proposed Response Prompt Template should also be the complete template text, ready to use."
        elif refinement_stage == "agentforce_agent":
            # Include agent configuration in output format (structure TBD based on what agent config looks like)
            output_format_section = f"""After the array, include a separate JSON object with the proposed agent configuration:

{{
  "LLM_Parser_Prompt_Proposed_from_Gemini": "{proposed_llm_parser_description}",
  "Prompt_Builder_Prompt_Proposed_from_Gemini": "{proposed_response_prompt_description}",
  "Agentforce_Agent_Configuration_Proposed_from_Gemini": "COMPLETE configuration for the Agentforce agent including instructions, tools, and orchestration logic. Provide the full configuration ready to use.",
  "StageStatus": "optimized" or "needs_improvement",
  "StageCompleteReason": "Brief explanation of why this stage is complete or needs more work"
}}"""
            output_format_important = "- The proposed Agentforce Agent configuration should be complete and ready to use."
        else:
            # Only include LLM parser prompt in output format (llm_parser stage)
            output_format_section = f"""After the array, include a separate JSON object with the proposed prompt:

{{
  "LLM_Parser_Prompt_Proposed_from_Gemini": "{proposed_llm_parser_description}",
  "StageStatus": "optimized" or "needs_improvement",
  "StageCompleteReason": "Brief explanation of why this stage is complete or needs more work"
}}"""
            output_format_important = ""
        
        # Substitute variables in YAML template
        prompt = gemini_instructions_template.replace('{{REFINEMENT_STAGE}}', refinement_stage)
        prompt = prompt.replace('{{REFINEMENT_STAGE_DESCRIPTION}}', refinement_stage_description)
        prompt = prompt.replace('{{REFINEMENT_STAGE_FOCUS}}', refinement_stage_focus)
        prompt = prompt.replace('{{REFINEMENT_STAGE_TASK}}', refinement_stage_task)
        prompt = prompt.replace('{{REFINEMENT_STAGE_FOCUS_TYPE}}', refinement_stage_focus_type)
        prompt = prompt.replace('{{ROOT_CAUSE_GUIDANCE}}', root_cause_guidance)
        prompt = prompt.replace('{{MODIFICATION_GUIDANCE}}', modification_guidance)
        # Replace the output format section in the template
        prompt = re.sub(r'{{OUTPUT_FORMAT_SECTION}}', output_format_section, prompt)
        prompt = prompt.replace('{{OUTPUT_FORMAT_IMPORTANT}}', output_format_important)
        prompt = prompt.replace('{{LLM_PARSER_PROMPT}}', llm_parser_prompt)
        prompt = prompt.replace('{{RESPONSE_PROMPT_TEMPLATE}}', response_prompt_template)
        prompt = prompt.replace('{{RESPONSE_MODEL}}', response_model)
        prompt = prompt.replace('{{AVAILABLE_MODELS}}', available_models_text)
        
        # Add previous cycle context before worksheet text if available
        if previous_cycle_context:
            # Insert previous cycle context before TEST RESULTS section
            # Look for "# TEST RESULTS" or "{{WORKSHEET_TEXT}}" and insert before it
            if '# TEST RESULTS' in prompt:
                prompt = prompt.replace('# TEST RESULTS', f'{previous_cycle_context}\n\n# TEST RESULTS')
            elif '{{WORKSHEET_TEXT}}' in prompt:
                prompt = prompt.replace('{{WORKSHEET_TEXT}}', f'{previous_cycle_context}\n\n{{WORKSHEET_TEXT}}')
            else:
                # Fallback: add before worksheet text
                prompt = prompt.replace('{{WORKSHEET_TEXT}}', f'{previous_cycle_context}\n\n{{WORKSHEET_TEXT}}')
        
        prompt = prompt.replace('{{WORKSHEET_TEXT}}', worksheet_text)
        # Handle custom instructions - only include section if instructions exist
        if custom_instructions and custom_instructions.strip():
            # Replace placeholder with actual instructions
            prompt = prompt.replace('{{CUSTOM_INSTRUCTIONS}}', custom_instructions.strip())
        else:
            # Remove the entire CUSTOM INSTRUCTIONS section (header, content, and separator) if no instructions provided
            # Pattern matches: "# CUSTOM INSTRUCTIONS\n{{CUSTOM_INSTRUCTIONS}}\n\n---\n\n" or variations
            prompt = re.sub(r'# CUSTOM INSTRUCTIONS\s*\n\s*{{CUSTOM_INSTRUCTIONS}}\s*\n\s*---\s*\n\s*\n', '', prompt)
            # Also handle case where separator might be on same line or different spacing
            prompt = re.sub(r'# CUSTOM INSTRUCTIONS\s*\n\s*{{CUSTOM_INSTRUCTIONS}}\s*\n\s*---\s*', '', prompt)
            # Final cleanup: remove any remaining placeholder references
            prompt = prompt.replace('{{CUSTOM_INSTRUCTIONS}}', '')
    else:
        log_print("❌ ERROR: geminiInstructions template is empty in YAML")
        sys.exit(1)
    
    # Store the full prompt for later use (e.g., adding to Excel metadata)
    # This is the complete instructions sent to Gemini (with all variables substituted)
    full_gemini_instructions = str(prompt) if prompt else ""  # Ensure it's always a string
    log_print(f"  ✓ Stored full Gemini instructions ({len(full_gemini_instructions)} chars) for Excel metadata")
    
    # Write "Instructions to Gemini" to DataFrame BEFORE calling Gemini
    # This ensures the sheet shows what instructions were sent, not what came back
    try:
        if full_gemini_instructions and len(str(full_gemini_instructions).strip()) > 0:
            log_print(f"  📝 Writing Instructions to Gemini to sheet (BEFORE Gemini call)...")
            found_label = False
            for idx in range(len(df)):
                if str(df.iloc[idx, 0]).strip() == 'Instructions to Gemini:':
                    found_label = True
                    # Ensure the next row exists (where value should be written)
                    if idx + 1 >= len(df):
                        # Append an empty row if needed
                        empty_row = [''] * len(df.columns)
                        df.loc[len(df)] = empty_row
                        log_print(f"  ✓ Added row {len(df) - 1} (Excel row {len(df)}) to accommodate Instructions value")
                    
                    # Write to row idx+1, column 1 (Excel row idx+2, column B)
                    instructions_str = str(full_gemini_instructions)[:50000]  # Limit to 50k chars for Excel
                    df.iloc[idx + 1, 1] = instructions_str
                    log_print(f"  ✅ Written Instructions to Gemini to metadata row {idx + 1} (Excel row {idx + 2}, column B)")
                    log_print(f"     Written {len(instructions_str):,} characters (BEFORE Gemini call)")
                    
                    # Save to Excel IMMEDIATELY so instructions are persisted even if process stops early
                    try:
                        excel_writer = pd.ExcelWriter(excel_file, engine='openpyxl', mode='a', if_sheet_exists='replace')
                        xls = pd.ExcelFile(excel_file)
                        for existing_sheet in xls.sheet_names:
                            if existing_sheet != sheet_name:
                                pd.read_excel(excel_file, sheet_name=existing_sheet).to_excel(
                                    excel_writer, sheet_name=existing_sheet, index=False)
                        df.to_excel(excel_writer, sheet_name=sheet_name, index=False)
                        excel_writer.close()
                        log_print(f"  💾 Saved Instructions to Gemini to Excel file (before Gemini call)")
                    except Exception as save_error:
                        log_print(f"  ⚠️  Warning: Could not save Instructions to Excel immediately: {type(save_error).__name__}: {str(save_error)[:200]}")
                    
                    break
            
            if not found_label:
                log_print(f"  ⚠️  Warning: 'Instructions to Gemini:' label not found in sheet")
    except Exception as e:
        log_print(f"  ⚠️  Warning: Error writing Instructions to Gemini (before call): {type(e).__name__}: {str(e)[:200]}")
    
    # Call Gemini with retry logic for network errors
    log_print("Calling Gemini API...")
    max_retries = 5
    retry_delay = 2  # Start with 2 seconds
    response_text = None
    last_error = None
    
    for attempt in range(max_retries):
        try:
            # Use google.generativeai package
            # Fix model name for old API
            if model_name == 'gemini-1.5-pro':
                model_name = 'gemini-pro'  # Fallback for old API
            model = genai.GenerativeModel(model_name)
            # Build contents with all PDF files + prompt
            contents = list(pdf_file_objs) if pdf_file_objs else []
            contents.append(prompt)
            log_print(f"  🔄 Attempt {attempt + 1}/{max_retries} (timeout: {GEMINI_TIMEOUT_SECONDS}s)...")
            # Note: google.generativeai (old package) doesn't support timeout in generation_config or request_options
            # Timeout is handled at HTTP client level, and retry logic below will catch DeadlineExceeded errors
            response = model.generate_content(contents)
            response_text = response.text
            
            # Success - break out of retry loop
            log_print(f"  ✅ Gemini API call successful (attempt {attempt + 1})")
            break
                
        except Exception as e:
            last_error = e
            error_type = type(e).__name__
            error_msg = str(e)
            
            # Check if it's a network/connection error or timeout (retryable)
            is_retryable = any(keyword in error_msg.lower() for keyword in [
                'connection reset', 'connection refused', 'timeout', 'network',
                'readerror', 'connection error', 'broken pipe', 'connection aborted',
                'deadline exceeded', '504', 'deadlineexceeded'
            ]) or 'ReadError' in error_type or 'ConnectError' in error_type or 'DeadlineExceeded' in error_type
            
            if is_retryable and attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)  # Exponential backoff
                log_print(f"  ⚠️  Network error (attempt {attempt + 1}/{max_retries}): {error_type}")
                log_print(f"     Error: {error_msg[:200]}")
                log_print(f"     Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            else:
                # Non-retryable error or max retries reached
                log_print(f"  ❌ Gemini API call failed: {error_type}")
                log_print(f"     Error: {error_msg[:500]}")
                if attempt == max_retries - 1:
                    log_print(f"  ❌ Max retries ({max_retries}) exceeded. Giving up.")
                raise
    
    if response_text is None:
        log_print("  ❌ ERROR: Failed to get response from Gemini API after all retries")
        if last_error:
            log_print(f"     Last error: {type(last_error).__name__}: {str(last_error)[:500]}")
        raise Exception(f"Gemini API call failed: {last_error}")
    
    # Parse response - extract both the array and the proposed prompts object
    # Try to find JSON array - use a more robust pattern that handles nested structures
    json_array_match = re.search(r'\[(?:[^\[\]]+|\[[^\]]*\])*\]', response_text, re.DOTALL)
    
    # If that fails, try a simpler pattern
    if not json_array_match:
        json_array_match = re.search(r'\[.*?\]', response_text, re.DOTALL)
    
    results = []
    proposed_llm_parser_prompt = ""
    proposed_response_prompt = ""
    stage_status = ""
    stage_complete_reason = ""
    
    if json_array_match:
        try:
            results = json.loads(json_array_match.group(0))
        except json.JSONDecodeError as e:
            log_print(f"  ⚠️  JSON parsing error: {e}")
            log_print(f"  🔍 Attempting to fix JSON...")
            # Try to fix common JSON issues
            json_str = json_array_match.group(0)
            
            # Fix unescaped newlines, quotes, and other control characters in string values
            # This is a more aggressive fix that handles common JSON issues from LLM responses
            def fix_json_string(match):
                content = match.group(1)
                # Escape newlines, carriage returns, tabs
                content = content.replace('\\', '\\\\')  # Escape backslashes first
                content = content.replace('\n', '\\n')
                content = content.replace('\r', '\\r')
                content = content.replace('\t', '\\t')
                content = content.replace('"', '\\"')  # Escape quotes
                return f'"{content}"'
            
            # Try multiple fix strategies
            try:
                # Strategy 1: Use json5 or manual fixing for common issues
                # Replace problematic characters in string values
                fixed_str = json_str
                # Fix unescaped quotes within strings (basic approach)
                # This is tricky - we'll try a simpler approach: use ast.literal_eval as fallback
                import ast
                try:
                    # Try using ast.literal_eval which is more lenient
                    results = ast.literal_eval(json_str)
                    log_print(f"  ✅ Fixed JSON using ast.literal_eval")
                except:
                    # Strategy 2: Try to extract just the array content and rebuild
                    # Find the content between first [ and last ]
                    start_idx = json_str.find('[')
                    end_idx = json_str.rfind(']')
                    if start_idx >= 0 and end_idx > start_idx:
                        array_content = json_str[start_idx+1:end_idx]
                        # Try to parse as Python list literal
                        try:
                            results = ast.literal_eval('[' + array_content + ']')
                            log_print(f"  ✅ Fixed JSON by extracting array content")
                        except:
                            raise
            except Exception as fix_error:
                log_print(f"  ❌ Could not fix JSON: {fix_error}")
                log_print(f"  📄 Raw JSON string (first 1000 chars):")
                log_print(f"  {json_str[:1000]}")
                log_print(f"  📄 Full response preview (first 2000 chars):")
                log_print(f"  {response_text[:2000]}")
                log_print(f"  ❌ ERROR: Could not parse Gemini's JSON response. Exiting.")
                sys.exit(1)
    
    # Extract proposed prompts and stage status based on refinement stage
    proposed_agentforce_config = ""
    if refinement_stage == "response_prompt":
        # Look for JSON object that contains both proposed prompts
        json_object_match = re.search(r'\{[^{}]*"LLM_Parser_Prompt_Proposed_from_Gemini"[^}]*"Prompt_Builder_Prompt_Proposed_from_Gemini"[^}]*\}', response_text, re.DOTALL)
        # If not found together, try to find them separately
        if not json_object_match:
            json_object_match = re.search(r'\{[^{}]*"LLM_Parser_Prompt_Proposed_from_Gemini"[^}]*\}', response_text, re.DOTALL)
        
        if json_object_match:
            try:
                prompt_obj = json.loads(json_object_match.group(0))
                proposed_llm_parser_prompt = prompt_obj.get('LLM_Parser_Prompt_Proposed_from_Gemini', '')
                proposed_response_prompt = prompt_obj.get('Prompt_Builder_Prompt_Proposed_from_Gemini', '')
                stage_status = prompt_obj.get('StageStatus', '')
                stage_complete_reason = prompt_obj.get('StageCompleteReason', '')
            except:
                # Try to extract individually if JSON parsing fails
                llm_match = re.search(r'"LLM_Parser_Prompt_Proposed_from_Gemini"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                response_match = re.search(r'"Prompt_Builder_Prompt_Proposed_from_Gemini"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                status_match = re.search(r'"StageStatus"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                reason_match = re.search(r'"StageCompleteReason"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                if llm_match:
                    proposed_llm_parser_prompt = llm_match.group(1)
                if response_match:
                    proposed_response_prompt = response_match.group(1)
                if status_match:
                    stage_status = status_match.group(1)
                if reason_match:
                    stage_complete_reason = reason_match.group(1)
    elif refinement_stage == "agentforce_agent":
        # Look for JSON object that contains all three proposed configurations
        json_object_match = re.search(r'\{[^{}]*"LLM_Parser_Prompt_Proposed_from_Gemini"[^}]*"Prompt_Builder_Prompt_Proposed_from_Gemini"[^}]*"Agentforce_Agent_Configuration_Proposed_from_Gemini"[^}]*\}', response_text, re.DOTALL)
        # If not found together, try to find them separately
        if not json_object_match:
            json_object_match = re.search(r'\{.*?"Agentforce_Agent_Configuration_Proposed_from_Gemini".*?\}', response_text, re.DOTALL)
        if json_object_match:
            try:
                prompt_obj = json.loads(json_object_match.group(0))
                proposed_llm_parser_prompt = prompt_obj.get('LLM_Parser_Prompt_Proposed_from_Gemini', '')
                proposed_response_prompt = prompt_obj.get('Prompt_Builder_Prompt_Proposed_from_Gemini', '')
                proposed_agentforce_config = prompt_obj.get('Agentforce_Agent_Configuration_Proposed_from_Gemini', '')
                stage_status = prompt_obj.get('StageStatus', '')
                stage_complete_reason = prompt_obj.get('StageCompleteReason', '')
            except:
                # Try regex extraction if JSON parsing fails
                llm_match = re.search(r'"LLM_Parser_Prompt_Proposed_from_Gemini"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                response_match = re.search(r'"Prompt_Builder_Prompt_Proposed_from_Gemini"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                agentforce_match = re.search(r'"Agentforce_Agent_Configuration_Proposed_from_Gemini"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                status_match = re.search(r'"StageStatus"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                reason_match = re.search(r'"StageCompleteReason"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                if llm_match:
                    proposed_llm_parser_prompt = llm_match.group(1)
                if response_match:
                    proposed_response_prompt = response_match.group(1)
                if agentforce_match:
                    proposed_agentforce_config = agentforce_match.group(1)
                if status_match:
                    stage_status = status_match.group(1)
                if reason_match:
                    stage_complete_reason = reason_match.group(1)
    else:
        # Only extract LLM parser prompt for llm_parser stage
        json_object_match = re.search(r'\{[^{}]*"LLM_Parser_Prompt_Proposed_from_Gemini"[^}]*\}', response_text, re.DOTALL)
        if json_object_match:
            try:
                prompt_obj = json.loads(json_object_match.group(0))
                proposed_llm_parser_prompt = prompt_obj.get('LLM_Parser_Prompt_Proposed_from_Gemini', '')
                stage_status = prompt_obj.get('StageStatus', '')
                stage_complete_reason = prompt_obj.get('StageCompleteReason', '')
            except:
                # Try regex extraction if JSON parsing fails
                llm_match = re.search(r'"LLM_Parser_Prompt_Proposed_from_Gemini"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                status_match = re.search(r'"StageStatus"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                reason_match = re.search(r'"StageCompleteReason"\s*:\s*"([^"]+)"', response_text, re.DOTALL)
                if llm_match:
                    proposed_llm_parser_prompt = llm_match.group(1)
                if status_match:
                    stage_status = status_match.group(1)
                if reason_match:
                    stage_complete_reason = reason_match.group(1)
    
    # Update DataFrame with results
    log_print("Updating results...")
    # Find the first data row (after header row)
    data_start_row = header_row_idx + 1 if header_row_idx is not None else 7
    
    for i, result in enumerate(results):
        # Calculate the actual row index in the DataFrame
        row_idx = data_start_row + i
        if row_idx >= len(df):
            log_print(f"  ⚠️  Warning: Result {i} exceeds DataFrame length, skipping")
            break
        
        # Update using column indices
        if 'Pass/Fail' in result and col_indices['Pass/Fail'] is not None:
            df.iloc[row_idx, col_indices['Pass/Fail']] = result['Pass/Fail']
        if 'Safety Score' in result and col_indices['Safety Score'] is not None:
            df.iloc[row_idx, col_indices['Safety Score']] = result['Safety Score']
        if 'Root Cause/Explanation' in result and col_indices['Root Cause/Explanation'] is not None:
            df.iloc[row_idx, col_indices['Root Cause/Explanation']] = result['Root Cause/Explanation']
        if 'Prompt Modification Next Version' in result and col_indices['Prompt Modification Next Version'] is not None:
            df.iloc[row_idx, col_indices['Prompt Modification Next Version']] = result['Prompt Modification Next Version']
    
    log_print(f"  ✅ Updated {min(len(results), len(df) - data_start_row)} rows with analysis results")
    
    # Update Stage Status in top metadata rows (rows 3-4)
    if stage_status or stage_complete_reason:
        log_print(f"Adding Stage Status to metadata...")
        for idx in range(len(df)):
            if str(df.iloc[idx, 0]).strip() == 'Stage Status:':
                if idx + 1 < len(df) and str(df.iloc[idx + 1, 0]).strip() == 'Stage Status Reason:':
                    # Update Stage Status (row idx, column 1)
                    if stage_status:
                        df.iloc[idx, 1] = stage_status
                        log_print(f"  ✅ Updated Stage Status: {stage_status}")
                    # Update Stage Status Reason (row idx+1, column 1)
                    if stage_complete_reason:
                        df.iloc[idx + 1, 1] = stage_complete_reason[:50000]  # Limit to 50k chars for Excel
                        log_print(f"  ✅ Updated Stage Status Reason (length: {len(stage_complete_reason)} chars)")
                break
    
    # Add proposed prompts as metadata rows at the bottom
    num_cols = len(df.columns)
    
    # Find and update LLM Parser Prompt Proposed
    if proposed_llm_parser_prompt:
        log_print(f"Adding proposed LLM Parser Prompt to metadata (length: {len(proposed_llm_parser_prompt)} chars)...")
        for idx in range(len(df)):
            if str(df.iloc[idx, 0]).strip() == 'LLM Parser Prompt Proposed from Gemini:':
                if idx + 1 < len(df):
                    df.iloc[idx + 1, 1] = proposed_llm_parser_prompt[:50000]  # Limit to 50k chars for Excel
                    log_print(f"  ✅ Updated metadata row {idx + 1} with proposed LLM Parser Prompt")
                break
    
    # Find and update Prompt Builder Prompt Proposed (only if in response_prompt stage)
    if refinement_stage == "response_prompt" and proposed_response_prompt:
        log_print(f"Adding proposed Prompt Builder Prompt to metadata (length: {len(proposed_response_prompt)} chars)...")
        for idx in range(len(df)):
            if str(df.iloc[idx, 0]).strip() == 'Prompt Builder Prompt Proposed from Gemini:':
                if idx + 1 < len(df):
                    df.iloc[idx + 1, 1] = proposed_response_prompt[:50000]  # Limit to 50k chars for Excel
                    log_print(f"  ✅ Updated metadata row {idx + 1} with proposed Prompt Builder Prompt")
                break
    elif refinement_stage != "response_prompt":
        log_print(f"  ℹ️  Skipping Prompt Builder Prompt population (current stage: {refinement_stage})")
    
    # Find and update Agentforce Agent Configuration Proposed (only if in agentforce_agent stage)
    if refinement_stage == "agentforce_agent" and proposed_agentforce_config:
        log_print(f"Adding proposed Agentforce Agent Configuration to metadata (length: {len(proposed_agentforce_config)} chars)...")
        for idx in range(len(df)):
            if str(df.iloc[idx, 0]).strip() == 'Agentforce Agent Configuration Proposed from Gemini:':
                if idx + 1 < len(df):
                    df.iloc[idx + 1, 1] = proposed_agentforce_config[:50000]  # Limit to 50k chars for Excel
                    log_print(f"  ✅ Updated metadata row {idx + 1} with proposed Agentforce Agent Configuration")
                break
    elif refinement_stage != "agentforce_agent":
        log_print(f"  ℹ️  Skipping Agentforce Agent Configuration population (current stage: {refinement_stage})")
    
    # NOTE: "Instructions to Gemini" is now written BEFORE the Gemini call (see line ~1034)
    # This ensures the sheet shows what instructions were sent, not what came back
    
    # Save to Excel (update same sheet)
    # Note: excel_file is already resolved to absolute path above
    if excel_file:
        log_print(f"Saving to Excel: {excel_file} (updating sheet: {sheet_name})")
        excel_writer = pd.ExcelWriter(excel_file, engine='openpyxl', mode='a', if_sheet_exists='replace')
        xls = pd.ExcelFile(excel_file)
        for existing_sheet in xls.sheet_names:
            if existing_sheet != sheet_name:
                pd.read_excel(excel_file, sheet_name=existing_sheet).to_excel(
                    excel_writer, sheet_name=existing_sheet, index=False)
        df.to_excel(excel_writer, sheet_name=sheet_name, index=False)
        excel_writer.close()
        log_print(f"  ✅ Excel file updated: {sheet_name}")
    
    # Cleanup uploaded PDF files
    if pdf_file_objs:
        for pdf_file_obj in pdf_file_objs:
            try:
                genai.delete_file(pdf_file_obj.name)
            except:
                pass
    
    log_print("✅ Complete!")
    
    # Return proposed prompt and stage status (from Step 3: Analyze Results)
    # Note: We update the same sheet, so return the original sheet_name
    return {
        'proposed_llm_parser_prompt': proposed_llm_parser_prompt,
        'proposed_response_prompt': proposed_response_prompt,
        'stage_status': stage_status,
        'stage_complete_reason': stage_complete_reason,
        'sheet_name': sheet_name  # Same sheet, updated with analysis
    }


# ============================================================================
# MAIN
# ============================================================================

def run_full_workflow(excel_file=None, pdf_file=None, model_name=None, yaml_input=None, yaml_config_dict=None, progress_callback=None,
                     resume=False, resume_from_step=None, resume_from_cycle=None, clean_state_flag=False, show_state_flag=False, run_id=None):
    """
    Unified iterative workflow:
    - Step 1: Update Index (beginning of cycle, except Cycle 1)
    - Step 2: Test Index (create sheet, invoke prompts)
    - Step 3: Analyze Results (Gemini analysis)
    Cycle 1: Skip Step 1, start with Step 2 (test baseline), then Step 3
    Cycle 2+: Start with Step 1 (update using previous cycle's improvements), then Step 2, then Step 3
    
    Args:
        resume: If True, resume from last checkpoint
        resume_from_step: Resume from specific step (1-3)
        resume_from_cycle: Resume from specific cycle number
        clean_state_flag: If True, delete state files and start fresh
        show_state_flag: If True, display current state and exit
    """
    
    # Handle show state
    if show_state_flag:
        show_state()
        return
    
    # Handle clean state
    if clean_state_flag:
        clean_state()
        log_print("  ✅ Starting fresh workflow")
    
    log_print("="*80)
    log_print("FULL ITERATIVE WORKFLOW: Complete Optimization Cycle")
    log_print("="*80)
    
    # Read YAML configuration - accept dict or file path
    if yaml_config_dict:
        yaml_config = yaml_config_dict
        log_print("  ✅ Using YAML config from dict")
    elif yaml_input and os.path.exists(yaml_input):
        log_print(f"📋 Reading YAML configuration: {yaml_input}")
        with open(yaml_input, 'r') as f:
            yaml_config = yaml.safe_load(f)
        log_print("  ✅ YAML config frozen for this run")
    else:
        log_print("❌ ERROR: Either yaml_input file or yaml_config_dict is REQUIRED for full workflow")
        log_print(f"   Provided yaml_input: {yaml_input}")
        sys.exit(1)
    
    config = yaml_config.get('configuration', {})
    questions_config = yaml_config.get('questions', [])
    
    # Get Gemini model from YAML (override command line arg if provided)
    gemini_model = config.get('geminiModel', model_name)  # Use YAML value, fallback to command line arg
    if gemini_model != model_name:
        log_print(f"  ℹ️  Using Gemini model from YAML: {gemini_model} (overriding command line: {model_name})")
    else:
        log_print(f"  ℹ️  Using Gemini model: {gemini_model}")
    
    # Load PDFs from database first (if run_id provided and PDFs exist in DB)
    # Then fallback to filesystem if not found in DB
    pdf_files = []
    if run_id:
        try:
            # Try worker_utils first (no Streamlit dependency), fallback to app
            try:
                from worker_utils import load_pdfs_from_db
            except ImportError:
                from app import load_pdfs_from_db
            pdf_files_restored = load_pdfs_from_db(run_id)
            if pdf_files_restored:
                pdf_files = [Path(p) for p in pdf_files_restored]
                log_print(f"  📁 Loaded {len(pdf_files)} PDF file(s) from database")
                for pdf_file in pdf_files:
                    log_print(f"     - {pdf_file.name}")
        except Exception as e:
            log_print(f"  ⚠️  Could not load PDFs from database: {e}")
            # Fall through to filesystem check
    
    # If no PDFs from database, try filesystem
    if not pdf_files:
        pdf_directory = config.get('pdfDirectory', '')
        if pdf_directory:
            pdf_dir_path = Path(pdf_directory)
            if not pdf_dir_path.is_absolute():
                # Resolve relative to script location
                # First try app_data/uploads (where uploaded PDFs are stored)
                app_data = Path(__file__).parent / "app_data" / "uploads"
                if (app_data / pdf_directory).exists():
                    pdf_dir_path = app_data / pdf_directory
                elif app_data.exists() and pdf_directory.startswith("uploads/"):
                    # Handle case where path includes "uploads/" prefix
                    pdf_dir_path = app_data / pdf_directory.replace("uploads/", "")
                else:
                    # Fallback: try as relative to script
                    script_dir = Path(__file__).parent
                    pdf_dir_path = script_dir / pdf_directory
            
            if pdf_dir_path.exists() and pdf_dir_path.is_dir():
                # Find all PDF files in directory
                pdf_files = sorted(list(pdf_dir_path.glob('*.pdf')))
                if pdf_files:
                    log_print(f"  📁 Found {len(pdf_files)} PDF file(s) in directory: {pdf_directory}")
                    for pdf_file in pdf_files:
                        log_print(f"     - {pdf_file.name}")
                else:
                    log_print(f"  ⚠️  No PDF files found in directory: {pdf_directory}")
            else:
                log_print(f"  ⚠️  PDF directory not found: {pdf_directory}")
        else:
            # Fallback to single PDF file from command line if directory not specified
            if pdf_file and os.path.exists(pdf_file):
                pdf_files = [Path(pdf_file)]
                log_print(f"  📄 Using single PDF file from command line: {pdf_file}")
    
    # Call progress callback if provided
    if progress_callback:
        try:
            progress_callback({'status': 'starting', 'cycle': 0, 'step': 0, 'run_id': run_id})
        except:
            pass  # Don't fail if callback errors
    
    # Generate run_id if not provided (for backward compatibility)
    import random
    from datetime import datetime
    if not run_id:
        run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000,9999)}"
    
    # Create run-specific Excel file path in app_data/outputs directory (relative to script)
    app_data = Path(__file__).parent / "app_data"
    app_data.mkdir(exist_ok=True)
    outputs_dir = app_data / "outputs"
    outputs_dir.mkdir(exist_ok=True)
    
    run_excel_file = outputs_dir / f"IEM_POC_questions_{run_id}.xlsx"
    log_print(f"  ✅ Run ID: {run_id}")
    log_print(f"  ✅ Run-specific Excel file: {run_excel_file.name}")
    log_print(f"  📁 Output directory: {outputs_dir}")
    log_print(f"     (Will be created from scratch - no template needed)")
    
    excel_file = str(run_excel_file)
    
    # Extract configuration values
    prompt_template_name = config.get('promptTemplateApiName')  # API name (DeveloperName), not display name
    search_index_id = config.get('searchIndexId')
    refinement_stage = config.get('refinementStage', 'llm_parser')
    salesforce_config = config.get('salesforce', {})
    username = salesforce_config.get('username')
    password = salesforce_config.get('password')
    instance_url = salesforce_config.get('instanceUrl')
    take_screenshots = config.get('takeScreenshots', False)
    
    if not prompt_template_name:
        log_print("❌ ERROR: promptTemplateApiName is required in YAML configuration")
        log_print("   This must be the DeveloperName (API name) with underscores, not the display name")
        sys.exit(1)
    if not search_index_id:
        log_print("❌ ERROR: searchIndexId is required in YAML configuration")
        sys.exit(1)
    if not username or not password or not instance_url:
        log_print("❌ ERROR: Salesforce credentials (username, password, instanceUrl) are required in YAML configuration")
        sys.exit(1)
    
    # Extract questions list
    questions_list = []
    for q in questions_config:
        questions_list.append((q.get('number', ''), q.get('text', ''), q.get('expectedAnswer', '')))
    
    if not questions_list:
        log_print("❌ ERROR: No questions found in YAML configuration")
        sys.exit(1)
    
    log_print(f"  ✅ Found {len(questions_list)} questions")
    log_print(f"  ✅ Prompt Template API Name: {prompt_template_name}")
    log_print(f"  ✅ Search Index ID: {search_index_id}")
    log_print(f"  ✅ Refinement Stage: {refinement_stage}")
    
    # Extract models list from YAML
    models_list = []
    prompt_builder_models = config.get('prompt_builder_models', {})
    if prompt_builder_models:
        primary = prompt_builder_models.get('primary')
        fallbacks = prompt_builder_models.get('fallbacks', [])
        if primary:
            models_list = [primary] + fallbacks
            log_print(f"  ✅ Models: {primary} (primary) + {len(fallbacks)} fallback(s)")
    
    if not models_list:
        log_print("  ⚠️  WARNING: No prompt_builder_models found in YAML, API calls may fail")
    
    # Import required modules (now at top-level)
    
    # RESUME LOGIC: Load state if resuming
    state = None
    resume_step = None
    if resume or resume_from_step or resume_from_cycle:
        log_print("\n" + "-"*80)
        log_print("RESUME MODE: Loading checkpoint state")
        log_print("-"*80)
        
        # First, try to load from database checkpoint_info if run_id is provided
        if run_id:
            try:
                from worker_utils import get_db_connection
                try:
                    import psycopg2.extras
                except ImportError:
                    # Fallback if psycopg2.extras not available
                    import psycopg2
                    psycopg2.extras = None
                conn = get_db_connection()
                if conn:
                    try:
                        # Use RealDictCursor if available, otherwise regular cursor
                        if psycopg2.extras:
                            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                        else:
                            cur = conn.cursor()
                        try:
                            cur.execute("""
                                SELECT checkpoint_info, config, excel_file_path, progress
                                FROM runs 
                                WHERE run_id = %s
                            """, (run_id,))
                            row = cur.fetchone()
                            if row:
                                # Handle both dict (RealDictCursor) and tuple (regular cursor)
                                if isinstance(row, dict):
                                    checkpoint = row.get('checkpoint_info')
                                    excel_file_path = row.get('excel_file_path')
                                    config_data = row.get('config')
                                else:
                                    # Tuple: (checkpoint_info, config, excel_file_path, progress)
                                    checkpoint = row[0] if len(row) > 0 else None
                                    config_data = row[1] if len(row) > 1 else None
                                    excel_file_path = row[2] if len(row) > 2 else None
                                
                                if checkpoint:
                                    log_print(f"  ✅ Found checkpoint in database: Cycle {checkpoint.get('cycle')}, Step {checkpoint.get('step')}")
                                    
                                    # CRITICAL: Load Excel file from database if it exists but not on disk
                                    resolved_excel_file = excel_file_path or excel_file
                                    if excel_file_path and not Path(excel_file_path).exists():
                                        try:
                                            from app import load_excel_from_db
                                            loaded_path = load_excel_from_db(run_id)
                                            if loaded_path:
                                                resolved_excel_file = loaded_path
                                                log_print(f"  ✅ Loaded Excel file from database: {Path(loaded_path).name}")
                                        except Exception as e:
                                            log_print(f"  ⚠️  Could not load Excel from DB: {e}")
                                    
                                    # Build state-like structure from checkpoint_info
                                    state = {
                                        'cycle_number': checkpoint.get('cycle', 1),
                                        'last_completed_step': checkpoint.get('step', 0) - 1 if checkpoint.get('step', 0) > 0 else 0,
                                        'run_id': run_id,
                                        'excel_file': resolved_excel_file,
                                        'yaml_config_snapshot': config_data if config_data else yaml_config,
                                        '_from_database_checkpoint': True  # Flag to skip validation
                                    }
                                    
                                    # Override resume parameters from checkpoint
                                    if checkpoint.get('cycle'):
                                        resume_from_cycle = checkpoint.get('cycle')
                                    if checkpoint.get('step'):
                                        resume_from_step = checkpoint.get('step')
                                    
                                    log_print(f"  ✅ Resuming from Cycle {resume_from_cycle}, Step {resume_from_step}")
                            else:
                                log_print("  ℹ️  No checkpoint_info found in database, trying state files...")
                        finally:
                            cur.close()
                    finally:
                        conn.close()
            except Exception as e:
                log_print(f"  ⚠️  Warning: Could not load checkpoint from database: {e}")
                import traceback
                traceback.print_exc()
        
        # Fallback to state files if database checkpoint not found
        if not state:
            state = load_state(resume_from_step=resume_from_step, resume_from_cycle=resume_from_cycle, run_id=run_id)
        
        if not state:
            log_print("  ❌ ERROR: No checkpoint found in database or state files. Cannot resume.")
            log_print("  💡 Tip: Run without --resume to start a new workflow")
            # Don't sys.exit() - let the caller handle it (worker will mark as failed)
            raise RuntimeError("Cannot resume: No checkpoint found in database or state files")
        
        # Use frozen YAML config from state (not from file)
        if 'yaml_config_snapshot' in state:
            yaml_config = state['yaml_config_snapshot']
            log_print("  ✅ Using frozen YAML config from state")
            config = yaml_config.get('configuration', {})
            questions_config = yaml_config.get('questions', [])
        else:
            # If no snapshot in state, use the yaml_config_dict that was passed in
            log_print("  ℹ️  No frozen YAML config in state, using provided config")
            # yaml_config is already set from function parameter
        
        # Use run_id and excel_file from state (if available)
        if state.get('run_id'):
            run_id = state.get('run_id')
        state_excel_file = state.get('excel_file', excel_file)
        
        # Validate state (skip validation if loading from database checkpoint - Excel file might not exist yet)
        if state.get('_from_database_checkpoint'):
            log_print("  ℹ️  Skipping state validation (loaded from database checkpoint)")
            # Excel file path from database should be valid
            if state_excel_file and state_excel_file != excel_file:
                excel_file = state_excel_file
        else:
            is_valid, validation_msg = validate_state(state, state_excel_file)
            if not is_valid:
                log_print(f"  ❌ ERROR: State validation failed: {validation_msg}")
                log_print("  💡 Tip: Use --clean-state to start fresh")
                raise RuntimeError(f"State validation failed: {validation_msg}")
            
            # Update excel_file to use resolved path from validation
            excel_file = state_excel_file
        
        log_print(f"  ✅ State loaded: Cycle {state.get('cycle_number')}, Last step: {state.get('last_completed_step')}")
        log_print(f"  📄 Sheet: {state.get('sheet_name')}")
        log_print(f"  🆔 Run ID: {run_id}")
        
        # Determine resume step
        last_completed = state.get('last_completed_step', 0)
        # If last step was 3 (cycle completed), start next cycle from Step 1
        if last_completed == 3:
            # Cycle completed - start next cycle from Step 1
            resume_step = None  # Start fresh cycle
            log_print(f"  ℹ️  Previous cycle completed - starting next cycle from Step 1")
        else:
            # Resume within the same cycle
            resume_step = last_completed + 1
            if resume_from_step:
                resume_step = resume_from_step
        
        log_print(f"  🔄 Resuming from Step {resume_step}")
    else:
        # New run: initialize state with frozen config
        state = {}
        state['run_id'] = run_id
        state['yaml_config_snapshot'] = yaml_config
        state['excel_file'] = excel_file
    
    # ITERATIVE REFINEMENT LOOP
    # Step definitions:
    #   Step 1: Update Index (beginning of cycle, except Cycle 1)
    #   Step 2: Test Index (create sheet, invoke prompts)
    #   Step 3: Analyze Results (Gemini analysis)
    # Cycle 1: Skip Step 1, start with Step 2 (test baseline), then Step 3
    # Cycle 2+: Start with Step 1 (update using previous cycle's improvements), then Step 2, then Step 3
    max_cycles = 10  # Safety limit to prevent infinite loops
    
    # Initialize state variables
    if state:
        # Resuming: continue from the cycle we were on
        last_completed = state.get('last_completed_step', 0)
        if last_completed == 3:
            # Previous cycle completed - start next cycle
            cycle_number = state.get('cycle_number', 1) + 1
            log_print(f"  🔄 Starting Cycle {cycle_number} (previous cycle completed)")
        else:
            # Resume within the same cycle
            cycle_number = state.get('cycle_number', 1)
        new_sheet_name = state.get('sheet_name')
        proposed_llm_parser_prompt = state.get('proposed_llm_parser_prompt', '')
        stage_status = state.get('stage_status', '')
        stage_complete_reason = state.get('stage_complete_reason', '')
        is_resuming = True  # Flag to track if we're resuming
    else:
        # Starting fresh: begin at cycle 1
        cycle_number = 1
        new_sheet_name = None
        proposed_llm_parser_prompt = ''
        stage_status = ''
        stage_complete_reason = ''
        is_resuming = False
    
    # Initialize heartbeat tracking
    last_heartbeat = datetime.now()
    heartbeat_interval = 30  # Update heartbeat every 30 seconds during long operations
    
    while cycle_number <= max_cycles:
        # Clear resume flag if we were resuming
        if is_resuming:
            is_resuming = False
        
        # Update heartbeat if it's been more than heartbeat_interval seconds
        if (datetime.now() - last_heartbeat).total_seconds() > heartbeat_interval:
            if progress_callback:
                try:
                    progress_callback({
                        'status': 'heartbeat',
                        'run_id': run_id,
                        'cycle': cycle_number,
                        'step': 0,
                        'message': f'Heartbeat - Cycle {cycle_number} in progress'
                    })
                except:
                    pass
            last_heartbeat = datetime.now()
        
        log_print("\n" + "="*80)
        log_print(f"🔄 REFINEMENT CYCLE {cycle_number}")
        log_print("="*80)
        
        # Progress callback
        if progress_callback:
            try:
                progress_callback({'status': 'cycle_start', 'cycle': cycle_number, 'step': 0, 'run_id': run_id})
            except:
                pass
            last_heartbeat = datetime.now()  # Reset heartbeat on cycle start
        
        # Determine if this is Cycle 1 (baseline test, no update needed)
        # Cycle 1 is when cycle_number == 1 AND we don't have a previous cycle's proposed prompt
        # Check if previous cycle state exists
        state_dir = get_state_dir()
        prev_cycle_file = state_dir / f"cycle_{cycle_number - 1}_state.json"
        has_previous_cycle = prev_cycle_file.exists()
        is_cycle_1 = (cycle_number == 1 and not has_previous_cycle)
        
        # Step 1: Update Index (beginning of cycle, except Cycle 1)
        # Cycle 1 skips this step - it tests the baseline index
        # Cycle 2+ starts here - updates index using previous cycle's proposed prompt
        if is_cycle_1:
            log_print("\n" + "-"*80)
            log_print("STEP 1: SKIPPED (Cycle 1 - testing baseline index, no update needed)")
            log_print("-"*80)
            # For Cycle 1, we don't have a previous cycle's prompt to apply
            # We'll test the current/baseline index state
        elif resume_step and resume_step > 1:
            log_print("\n" + "-"*80)
            log_print("STEP 1: SKIPPED (Resuming from Step 2+)")
            log_print("-"*80)
            log_print(f"  ℹ️  Index update was already completed or skipped")
        else:
            # Cycle 2+: Update index using previous cycle's proposed prompt
            # Try to load from previous cycle's state file first, then fall back to main state file
            previous_cycle_prompt = None
            if prev_cycle_file.exists():
                # Load from previous cycle's state file
                try:
                    with open(prev_cycle_file, 'r') as f:
                        prev_state = json.load(f)
                        previous_cycle_prompt = prev_state.get('proposed_llm_parser_prompt', '')
                except Exception as e:
                    log_print(f"  ⚠️  Warning: Could not load previous cycle state: {e}")
                    pass
            else:
                # Fall back to main state file if cycle-specific file doesn't exist
                # When resuming after a completed cycle, the main state file has Cycle N-1's proposed prompt
                # But we need to check if the state's cycle_number matches the previous cycle
                if state:
                    state_cycle = state.get('cycle_number', 0)
                    # If state is from previous cycle (cycle_number - 1), use its proposed prompt
                    if state_cycle == cycle_number - 1:
                        previous_cycle_prompt = state.get('proposed_llm_parser_prompt', '')
                        if previous_cycle_prompt:
                            log_print(f"  ℹ️  Using proposed prompt from main state file (Cycle {cycle_number - 1})")
                    else:
                        # State is from current or different cycle - try to load from any available cycle file
                        log_print(f"  ⚠️  Main state is from Cycle {state_cycle}, but we need Cycle {cycle_number - 1}")
                        # Try to find any cycle file that might have the previous cycle's prompt
                        for try_cycle in range(cycle_number - 1, 0, -1):
                            try_file = state_dir / f"cycle_{try_cycle}_state.json"
                            if try_file.exists():
                                try:
                                    with open(try_file, 'r') as f:
                                        try_state = json.load(f)
                                        previous_cycle_prompt = try_state.get('proposed_llm_parser_prompt', '')
                                        if previous_cycle_prompt:
                                            log_print(f"  ℹ️  Found proposed prompt from Cycle {try_cycle} state file")
                                            break
                                except:
                                    pass
            
            if not previous_cycle_prompt:
                log_print("\n" + "-"*80)
                log_print("STEP 1: SKIPPED (No previous cycle prompt found)")
                log_print("-"*80)
                log_print("  ⚠️  Warning: Cannot update index without previous cycle's proposed prompt")
                log_print("  ℹ️  Proceeding to test current index state")
            elif refinement_stage == "llm_parser":
                log_print("\n" + "-"*80)
                log_print(f"STEP 1: Updating Index (applying Cycle {cycle_number - 1}'s improvements)")
                log_print("-"*80)
                
                # Progress callback - step start
                if progress_callback:
                    try:
                        progress_callback({'status': 'step_start', 'cycle': cycle_number, 'step': 1, 'run_id': run_id, 'message': f'Step 1: Updating Search Index with Cycle {cycle_number - 1} improvements'})
                    except:
                        pass
                
                # Check if index is already being processed by another run
                is_locked, error_msg = check_index_lock(search_index_id)
                if is_locked:
                    log_print(f"\n❌ ERROR: {error_msg}")
                    log_print("❌ Cannot proceed - there's already a job running on optimizing the prompt for this index.")
                    log_print("❌ Please wait for the other run to complete or stop it before starting a new one.")
                    sys.exit(1)
                
                # Acquire lock for this index
                lock_acquired, lock_error = acquire_index_lock(search_index_id, run_id)
                if not lock_acquired:
                    log_print(f"\n❌ ERROR: Failed to acquire index lock: {lock_error}")
                    sys.exit(1)
                log_print(f"   🔒 Acquired lock for index {search_index_id}")
                
                log_print(f"   Previous cycle's prompt length: {len(previous_cycle_prompt)} chars")
                
                try:
                    # Get headless and slowMo settings from YAML
                    headless_mode = config.get('headless', False)
                    slow_mo_value = config.get('slowMo', 0)
                    
                    # Call async function using asyncio.run()
                    asyncio.run(update_search_index_prompt(
                        username=username,
                        password=password,
                        instance_url=instance_url,
                        search_index_id=search_index_id,
                        new_prompt=previous_cycle_prompt,
                        capture_network=False,
                        take_screenshots=take_screenshots,
                        headless=headless_mode,
                        slow_mo=slow_mo_value
                    ))
                    log_print("\n✅ Step 1 Complete: Search Index updated and rebuilt")
                    
                    # Progress callback
                    if progress_callback:
                        try:
                            progress_callback({'status': 'step_complete', 'cycle': cycle_number, 'step': 1, 'run_id': run_id, 'message': 'Search Index updated and rebuilt'})
                        except:
                            pass
                    
                    # Release lock after Step 1 completes
                    release_index_lock(search_index_id)
                    log_print(f"   🔓 Released lock for index {search_index_id}")
                    
                    # Save state after Step 1
                    save_state(
                        cycle_number=cycle_number,
                        last_completed_step=1,
                        sheet_name=new_sheet_name,  # May be None if first step
                        refinement_stage=refinement_stage,
                        stage_status=None,  # Not analyzed yet
                        proposed_llm_parser_prompt=None,  # Will be set in Step 3
                        proposed_response_prompt=None,
                        stage_complete_reason=None,
                        excel_file=excel_file,
                        run_id=run_id,
                        yaml_config_snapshot=yaml_config
                    )
                except Exception as e:
                    # Release lock on error
                    release_index_lock(search_index_id)
                    log_print(f"   🔓 Released lock for index {search_index_id} (after error)")
                    log_print(f"\n❌ Step 1 Failed: {str(e)}")
                    
                    # Progress callback - report error
                    if progress_callback:
                        try:
                            error_msg = f"Step 1 Failed: {str(e)}"
                            if "Executable doesn't exist" in str(e) or "BrowserType.launch" in str(e):
                                error_msg = "Step 1 Failed: Playwright browser not installed. Cannot update search index. Please ensure browsers are installed."
                            elif "playwright install" in str(e).lower():
                                error_msg = "Step 1 Failed: Playwright browser installation required. Run 'playwright install chromium' to fix."
                            progress_callback({'status': 'error', 'cycle': cycle_number, 'step': 1, 'run_id': run_id, 'message': error_msg, 'error': str(e)})
                        except:
                            pass
                    
                    log_print("\n❌ CRITICAL ERROR: Step 1 (Search Index Update) failed.")
                    log_print("❌ The workflow cannot continue without successfully updating the search index.")
                    log_print("❌ This is a critical step and cannot be skipped.")
                    log_print(f"❌ Error details: {str(e)}")
                    log_print("\n💡 Possible solutions:")
                    log_print("   1. Ensure Playwright browsers are installed: 'playwright install chromium'")
                    log_print("   2. Check network connectivity to Salesforce")
                    log_print("   3. Verify search index ID and credentials are correct")
                    log_print("   4. Review the full error message above for specific issues")
                    
                    # Save error state
                    save_state(
                        cycle_number=cycle_number,
                        last_completed_step=0,  # No steps completed
                        sheet_name=None,
                        refinement_stage=refinement_stage,
                        stage_status='error',
                        proposed_llm_parser_prompt=None,
                        proposed_response_prompt=None,
                        stage_complete_reason=f"Step 1 failed: {str(e)}",
                        excel_file=excel_file,
                        run_id=run_id,
                        yaml_config_snapshot=yaml_config
                    )
                    
                    # Stop the workflow - don't continue
                    raise RuntimeError(f"Step 1 (Search Index Update) failed: {str(e)}. Workflow stopped.")
            else:
                log_print("\n" + "-"*80)
                log_print(f"STEP 1: SKIPPED (Current refinement stage is '{refinement_stage}', not 'llm_parser')")
                log_print("-"*80)
                log_print("   Step 1 (Update Index) only runs for 'llm_parser' stage")
        
        # Step 2: Test Index (create sheet, invoke prompts)
        if resume_step and resume_step > 2:
            log_print("\n" + "-"*80)
            log_print("STEP 2: SKIPPED (Resuming from Step 3+)")
            log_print("-"*80)
            log_print(f"  ℹ️  Using existing sheet: {new_sheet_name}")
        else:
            log_print("\n" + "-"*80)
            if is_cycle_1:
                log_print("STEP 2: Testing Baseline Index (Cycle 1)")
            else:
                log_print(f"STEP 2: Testing Updated Index (Cycle {cycle_number})")
            log_print("-"*80)
            
            # Progress callback - step start
            if progress_callback:
                try:
                    step_msg = 'Testing Baseline Index' if is_cycle_1 else f'Testing Updated Index (Cycle {cycle_number})'
                    progress_callback({'status': 'step_start', 'cycle': cycle_number, 'step': 2, 'run_id': run_id, 'message': f'Step 2: {step_msg}'})
                except:
                    pass
            
            try:
                # CRITICAL: Pass run_id in config_dict so excel_io.py can load from DB if needed
                yaml_config_with_run_id = yaml_config.copy()
                yaml_config_with_run_id['_run_id'] = run_id
                
                new_sheet_name = create_analysis_sheet_with_prompts(
                    excel_file=excel_file,
                    questions_list=questions_list,
                    prompt_template_name=prompt_template_name,
                    search_index_id=search_index_id,
                    models_list=models_list,
                    refinement_stage=refinement_stage,
                    cycle_number=cycle_number,
                    config_dict=yaml_config_with_run_id
                )
                
                log_print(f"\n✅ Step 2 Complete: Created sheet '{new_sheet_name}' with prompt responses")
                
                # Progress callback - include Excel file path so it can be saved to DB immediately
                if progress_callback:
                    try:
                        progress_callback({
                            'status': 'step_complete', 
                            'cycle': cycle_number, 
                            'step': 2, 
                            'run_id': run_id, 
                            'message': f'Test sheet created: {new_sheet_name}',
                            'excel_file': str(excel_file) if excel_file else None
                        })
                    except:
                        pass
                
                # Save state after Step 2
                save_state(
                    cycle_number=cycle_number,
                    last_completed_step=2,
                    sheet_name=new_sheet_name,
                    refinement_stage=refinement_stage,
                    excel_file=excel_file,
                    run_id=run_id,
                    yaml_config_snapshot=yaml_config
                )
            except Exception as e:
                log_print(f"\n❌ Step 2 Failed: {str(e)}")
                
                # Progress callback - report error
                if progress_callback:
                    try:
                        error_msg = f"Step 2 Failed: {str(e)}"
                        progress_callback({'status': 'error', 'cycle': cycle_number, 'step': 2, 'run_id': run_id, 'message': error_msg, 'error': str(e)})
                    except:
                        pass
                
                log_print("\n❌ CRITICAL ERROR: Step 2 (Testing Index & Invoking Prompts) failed.")
                log_print("❌ The workflow cannot continue without successfully testing the index and invoking prompts.")
                log_print("❌ This is a critical step and cannot be skipped.")
                log_print(f"❌ Error details: {str(e)}")
                log_print("\n💡 Possible solutions:")
                log_print("   1. Check Salesforce credentials and connectivity")
                log_print("   2. Verify search index ID and prompt template API name are correct")
                log_print("   3. Ensure test questions are properly formatted")
                log_print("   4. Review the full error message above for specific issues")
                
                # Save error state
                save_state(
                    cycle_number=cycle_number,
                    last_completed_step=1,  # Only Step 1 completed
                    sheet_name=None,
                    refinement_stage=refinement_stage,
                    stage_status='error',
                    proposed_llm_parser_prompt=None,
                    proposed_response_prompt=None,
                    stage_complete_reason=f"Step 2 failed: {str(e)}",
                    excel_file=excel_file,
                    run_id=run_id,
                    yaml_config_snapshot=yaml_config
                )
                
                # Stop the workflow - don't continue
                raise RuntimeError(f"Step 2 (Testing Index & Invoking Prompts) failed: {str(e)}. Workflow stopped.")
        
        # Step 3: Analyze Results (Gemini analysis)
        if resume_step and resume_step > 3:
            log_print("\n" + "-"*80)
            log_print("STEP 3: SKIPPED (Resuming from beyond Step 3)")
            log_print("-"*80)
            log_print(f"  ℹ️  Using existing analysis results")
            log_print(f"  📊 Stage Status: {stage_status}")
            # Use values from state
            analysis_result = {
                'proposed_llm_parser_prompt': proposed_llm_parser_prompt,
                'proposed_response_prompt': state.get('proposed_response_prompt', '') if state else '',
                'stage_status': stage_status,
                'stage_complete_reason': stage_complete_reason,
                'sheet_name': new_sheet_name
            }
        else:
            log_print("\n" + "-"*80)
            if is_cycle_1:
                log_print("STEP 3: Analyzing Baseline Results (Cycle 1)")
            else:
                log_print(f"STEP 3: Analyzing Updated Index Results (Cycle {cycle_number})")
            log_print("-"*80)
            
            # Progress callback - step start
            if progress_callback:
                try:
                    step_msg = 'Analyzing Baseline Results' if is_cycle_1 else f'Analyzing Updated Results (Cycle {cycle_number})'
                    progress_callback({'status': 'step_start', 'cycle': cycle_number, 'step': 3, 'run_id': run_id, 'message': f'Step 3: {step_msg}'})
                except:
                    pass
            
            try:
                analysis_result = analyze_with_gemini(
                    excel_file=excel_file,
                    sheet_name=new_sheet_name,
                    pdf_files=pdf_files,  # Pass list of PDF files instead of single file
                    model_name=gemini_model,  # Use model from YAML
                    config_dict=yaml_config,
                    cycle_number=cycle_number
                )
                
                # Extract results from analysis
                proposed_llm_parser_prompt = analysis_result.get('proposed_llm_parser_prompt', '')
                stage_status = analysis_result.get('stage_status', '')
                stage_complete_reason = analysis_result.get('stage_complete_reason', '')
                
                log_print(f"\n✅ Step 3 Complete: Gemini analysis finished")
                log_print(f"   Stage Status: {stage_status}")
                if stage_complete_reason:
                    log_print(f"   Reason: {stage_complete_reason[:100]}...")
                
                # Extract results for summary sheet
                results_data = extract_results_from_sheet(excel_file, new_sheet_name)
                
                # Update Running_Score sheet
                update_run_summary_sheet(
                    excel_file=excel_file,
                    run_id=run_id,
                    cycle_number=cycle_number,
                    results_data=results_data,
                    config_dict=yaml_config
                )
                
                # Progress callback - include Excel file path AFTER it's been updated with analysis results
                if progress_callback:
                    try:
                        progress_callback({
                            'status': 'step_complete', 
                            'cycle': cycle_number, 
                            'step': 3, 
                            'stage_status': stage_status, 
                            'run_id': run_id, 
                            'message': f'Gemini analysis complete (Stage Status: {stage_status})',
                            'excel_file': str(excel_file) if excel_file else None
                        })
                    except:
                        pass
                
                # Save state after Step 3
                save_state(
                    cycle_number=cycle_number,
                    last_completed_step=3,
                    sheet_name=new_sheet_name,
                    refinement_stage=refinement_stage,
                    stage_status=stage_status,
                    proposed_llm_parser_prompt=proposed_llm_parser_prompt,
                    proposed_response_prompt=analysis_result.get('proposed_response_prompt', ''),
                    stage_complete_reason=stage_complete_reason,
                    excel_file=excel_file,
                    run_id=run_id,
                    yaml_config_snapshot=yaml_config
                )
                
            except Exception as e:
                error_type = type(e).__name__
                error_msg = str(e)
                log_print(f"\n❌ Step 3 Failed: {error_type}: {error_msg[:500]}")
                
                # Progress callback - report error
                if progress_callback:
                    try:
                        error_msg_full = f"Step 3 Failed: {error_type}: {error_msg}"
                        progress_callback({'status': 'error', 'cycle': cycle_number, 'step': 3, 'run_id': run_id, 'message': error_msg_full, 'error': error_msg})
                    except:
                        pass
                
                log_print("\n❌ CRITICAL ERROR: Step 3 (Analyzing Results with Gemini) failed.")
                log_print("❌ The workflow cannot continue without successfully analyzing results.")
                log_print("❌ This is a critical step and cannot be skipped.")
                log_print(f"❌ Error details: {error_type}: {error_msg}")
                log_print("\n💡 Possible solutions:")
                log_print("   1. Check Gemini API key is set correctly")
                log_print("   2. Verify network connectivity to Gemini API")
                log_print("   3. Check that the Excel sheet was created correctly in Step 2")
                log_print("   4. Review the full error message above for specific issues")
                
                # Save error state
                save_state(
                    cycle_number=cycle_number,
                    last_completed_step=2,  # Only Step 2 completed
                    sheet_name=new_sheet_name,
                    refinement_stage=refinement_stage,
                    stage_status='error',
                    proposed_llm_parser_prompt=None,
                    proposed_response_prompt=None,
                    stage_complete_reason=f"Step 3 failed: {error_type}: {error_msg[:200]}",
                    excel_file=excel_file,
                    run_id=run_id,
                    yaml_config_snapshot=yaml_config
                )
                
                # Stop the workflow - don't continue
                raise RuntimeError(f"Step 3 (Analyzing Results with Gemini) failed: {error_type}: {error_msg}. Workflow stopped.")
        
        # Check if we should continue
        if stage_status == "optimized":
            log_print("\n" + "="*80)
            log_print("✅ REFINEMENT COMPLETE!")
            log_print("="*80)
            log_print(f"Stage '{refinement_stage}' is optimized after {cycle_number} cycle(s)")
            break
        
        # Continue to next cycle
        # The proposed prompt from this cycle will be applied at the start of the next cycle (Step 1)
        log_print(f"\n🔄 Stage Status: '{stage_status}' - Continuing to next refinement cycle...")
        log_print(f"   Proposed prompt from Cycle {cycle_number} will be applied at the start of Cycle {cycle_number + 1} (Step 1)")
        
        # Increment cycle number for next iteration
        cycle_number += 1
        
        if cycle_number <= max_cycles:
            log_print("   Waiting 5 seconds before next cycle...")
            # Update heartbeat before waiting
            if progress_callback:
                try:
                    progress_callback({
                        'status': 'heartbeat',
                        'run_id': run_id,
                        'cycle': cycle_number,
                        'step': 0,
                        'message': f'Preparing for next cycle...'
                    })
                except:
                    pass
            last_heartbeat = datetime.now()
            time.sleep(5)
        
        # Clear resume_step flag after first iteration (so next cycle runs normally)
        if resume_step:
            resume_step = None
    
    if cycle_number > max_cycles:
        log_print("\n" + "="*80)
        log_print("⚠️  MAX CYCLES REACHED")
        log_print("="*80)
        log_print(f"Reached maximum of {max_cycles} cycles. Stopping.")
        log_print("   Check Stage Status in Excel sheet to determine if optimization is complete.")
    
    log_print("\n" + "="*80)
    log_print("✅ FULL WORKFLOW COMPLETE!")
    log_print("="*80)
    log_print(f"Completed {cycle_number} refinement cycle(s)")
    if 'new_sheet_name' in locals() and new_sheet_name:
        log_print(f"Final sheet: '{new_sheet_name}'")
    log_print(f"Final Stage Status: {stage_status}")
    
    # Clean up state file on successful completion
    if not resume:
        clean_state()
        log_print("  🧹 State files cleaned (workflow complete)")
    
    # Progress callback - completion
    if progress_callback:
        try:
            progress_callback({'status': 'complete', 'cycle': cycle_number, 'stage_status': stage_status, 'excel_file': excel_file if 'excel_file' in locals() else None, 'run_id': run_id})
        except:
            pass
    
    # Return results for Streamlit
    return {
        'run_id': run_id if 'run_id' in locals() else None,
        'excel_file': excel_file if 'excel_file' in locals() else None,
        'final_sheet': new_sheet_name if 'new_sheet_name' in locals() else None,
        'stage_status': stage_status if 'stage_status' in locals() else None,
        'cycles_completed': cycle_number if 'cycle_number' in locals() else 0,
        'success': True
    }


def main():
    """Main entry point - Gemini analysis only (test mode uses test_gemini.py)"""
    parser = argparse.ArgumentParser(description='Prompt Optimization Workflow')
    parser.add_argument('--excel', default='prompt-optimization-solution/inputs/IEM POC questions  .xlsx', help='Excel file path')
    parser.add_argument('--sheet', default=None, help='Sheet name to analyze (auto-detects latest analysis sheet if not provided)')
    parser.add_argument('--pdf', default='prompt-optimization-solution/inputs/pdf/PRO-GDL-1002 CDP Design Guide_R3.1.pdf', help='PDF file path')
    parser.add_argument('--model', default='gemini-2.5-pro', help='Gemini model name (e.g., gemini-2.5-pro, gemini-2.5-flash)')
    parser.add_argument('--yaml-input', help='Path to YAML configuration file')
    parser.add_argument('--full-workflow', action='store_true', help='Run full workflow: create sheet + invoke prompts + Gemini analysis')
    parser.add_argument('--resume', action='store_true', help='Resume from last checkpoint')
    parser.add_argument('--resume-from-step', type=int, choices=[1, 2, 3, 4], help='Resume from specific step (1-4)')
    parser.add_argument('--resume-from-cycle', type=int, help='Resume from specific cycle number')
    parser.add_argument('--clean-state', action='store_true', help='Delete state files and start fresh')
    parser.add_argument('--show-state', action='store_true', help='Display current state and exit')
    
    args = parser.parse_args()
    
    if args.full_workflow:
        run_full_workflow(
            excel_file=args.excel,
            pdf_file=args.pdf,
            model_name=args.model,
            yaml_input=args.yaml_input,
            resume=args.resume,
            resume_from_step=args.resume_from_step,
            resume_from_cycle=args.resume_from_cycle,
            clean_state_flag=args.clean_state,
            show_state_flag=args.show_state,
        )
    else:
        # Standalone mode: read YAML for this single analysis
        if not args.yaml_input or not os.path.exists(args.yaml_input):
            log_print("❌ ERROR: --yaml-input is required for standalone analysis")
            sys.exit(1)
        with open(args.yaml_input, 'r') as f:
            yaml_config = yaml.safe_load(f)
        analyze_with_gemini(
            excel_file=args.excel,
            sheet_name=args.sheet,
            pdf_files=[Path(args.pdf)] if args.pdf else [],
            model_name=args.model,
            config_dict=yaml_config
        )


if __name__ == '__main__':
    main()

