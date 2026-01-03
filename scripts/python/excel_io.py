"""
Excel I/O helpers.

create_analysis_sheet_with_prompts is moved here from test_gemini.py to centralize
Excel-related logic.
"""

import sys
import json
from pathlib import Path
import pandas as pd
from salesforce_api import get_salesforce_credentials, invoke_prompt, retrieve_metadata_via_api, SearchIndexAPI
import xml.etree.ElementTree as ET
import xml.etree.ElementTree as ET

def log_print(*args, **kwargs):
    """Print with immediate flush for real-time output"""
    print(*args, **kwargs, flush=True)


def create_analysis_sheet_with_prompts(excel_file, questions_list=None,
                                      prompt_template_name=None, search_index_id=None, models_list=None,
                                      refinement_stage=None, cycle_number=None, config_dict=None):
    """
    Create a new timestamped sheet with columns A-D, get prompts/metadata, and fetch answers from Salesforce
    """
    # #region agent log
    with open('/Users/zwarshavsky/Documents/Custom_LWC_Org_SDO/Custom LWC Development SDO/.cursor/debug.log', 'a') as f:
        f.write(json.dumps({"sessionId":"debug-session","runId":"run1","hypothesisId":"ALL","location":"excel_io.py:create_analysis_sheet_with_prompts","message":"ENTRY","data":{"excel_file":str(excel_file),"has_questions_list":questions_list is not None,"questions_count":len(questions_list) if questions_list else 0,"prompt_template":prompt_template_name},"timestamp":int(__import__('time').time()*1000)}) + '\n')
    # #endregion

    if not prompt_template_name:
        log_print("❌ Error: --prompt-template is required")
        sys.exit(1)
    if not search_index_id:
        log_print("❌ Error: --search-index-id is required")
        sys.exit(1)
    if pd is None:
        log_print("❌ pandas is required for Excel file processing")
        sys.exit(1)

    # Resolve Excel file path - handle both absolute and relative paths
    excel_path = Path(excel_file)
    if not excel_path.is_absolute():
        if not excel_path.exists():
            script_dir = Path(__file__).parent.parent
            excel_path_attempt = script_dir / excel_file
            if excel_path_attempt.exists():
                excel_path = excel_path_attempt
            else:
                if "prompt-optimization-solution" in excel_file:
                    relative_part = excel_file.replace("prompt-optimization-solution/", "")
                    excel_path_attempt = script_dir / relative_part
                    if excel_path_attempt.exists():
                        excel_path = excel_path_attempt
                    else:
                        inputs_dir = script_dir / "inputs"
                        excel_path_attempt = inputs_dir / Path(excel_file).name
                        if excel_path_attempt.exists():
                            excel_path = excel_path_attempt

    # For new runs, the Excel file may not exist yet - that's OK, we'll create it
    if not excel_path.exists():
        if "run_" in str(excel_path) and excel_path.suffix == '.xlsx':
            log_print(f"   ℹ️  New Excel file will be created: {excel_path.name}")
        else:
            log_print(f"⚠️  Excel file not found: {excel_file}")
            log_print(f"   Tried: {excel_path}")
            log_print(f"   Will attempt to create if this is a new run")

    excel_file = str(excel_path)  # Update to absolute path

    log_print("="*80)
    log_print("Creating New Analysis Sheet with Prompts")
    log_print("="*80)
    log_print(f"📄 Excel File: {excel_file}")
    log_print(f"🔮 Prompt Template: {prompt_template_name}")
    log_print(f"🔍 Search Index ID: {search_index_id}")
    log_print()

    # Get questions list - must be provided
    if questions_list is None or len(questions_list) == 0:
        log_print("❌ Error: questions_list must be provided")
        sys.exit(1)

    log_print(f"📋 Using provided questions list: {len(questions_list)} questions")
    # Normalize format - ensure tuples have 3 elements (q_num, q_text, expected_answer)
    normalized_questions = []
    for q in questions_list:
        if len(q) == 2:
            normalized_questions.append((q[0], q[1], ''))
        elif len(q) == 3:
            normalized_questions.append(q)
        else:
            log_print(f"   ⚠️  Skipping invalid question format: {q}")
    questions_list = normalized_questions

    # Build DataFrame structure from questions list (self-contained)
    header_data = [['Q#', 'Question', 'Received Answer', 'Expected Answer', 'Model Used', 'Pass/Fail', 'Safety Score', 'Root Cause/Explanation', 'Prompt Modification Next Version']]
    question_data = [[q_num, q_text, '', expected_answer, '', '', '', '', ''] for q_num, q_text, expected_answer in questions_list]
    df_new = pd.DataFrame(header_data + question_data, columns=['Q#', 'Question', 'Received Answer', 'Expected Answer', 'Model Used', 'Pass/Fail', 'Safety Score', 'Root Cause/Explanation', 'Prompt Modification Next Version'])

    # Metadata rows at top (matching V7 format)
    log_print("   🔑 Getting Salesforce credentials for metadata...")
    instance_url, access_token = get_salesforce_credentials()
    log_print(f"   ✅ Connected: {instance_url}")
    org_username = config_dict.get('configuration', {}).get('salesforce', {}).get('username', 'Unknown') if config_dict else "Unknown"
    search_index_label = "Unknown"
    try:
        log_print(f"   🔍 Retrieving search index label for ID: {search_index_id}...")
        search_api = SearchIndexAPI(instance_url, access_token)
        full_index = search_api.get_index(search_index_id)
        if full_index:
            search_index_label = full_index.get('label', 'Unknown')
            log_print(f"   ✅ Found index label: {search_index_label}")
        else:
            log_print(f"   ⚠️  Index not found")
    except Exception as e:
        log_print(f"   ⚠️  Could not retrieve index label: {e}")
        pass

    num_cols = len(df_new.columns)
    metadata_rows = [
        ['Environment:', instance_url, org_username, ''] + [''] * (num_cols - 4),
        ['Search Index:', search_index_label] + [''] * (num_cols - 2),
        ['Prompt Builder Prompt:', prompt_template_name] + [''] * (num_cols - 2),
        ['Stage Status:', ''] + [''] * (num_cols - 2),
        ['Stage Status Reason:', ''] + [''] * (num_cols - 2),
        [''] * num_cols,
        [''] * num_cols,
    ]
    metadata_header = pd.DataFrame(metadata_rows, columns=df_new.columns)

    log_print(f"\n🔮 Invoking Salesforce prompt '{prompt_template_name}' (API name) for each question...")
    log_print("   🔑 Getting Salesforce credentials...")
    instance_url, access_token = get_salesforce_credentials()
    log_print(f"   ✅ Connected to: {instance_url}")

    # Model from prompt template (display only)
    response_model = ""
    try:
        log_print(f"   📋 Retrieving prompt template metadata for: {prompt_template_name}...")
        dev_name = prompt_template_name
        xml_content = retrieve_metadata_via_api(instance_url, access_token, "GenAiPromptTemplate", dev_name)
        if xml_content:
            root = ET.fromstring(xml_content)
            ns = {'met': 'http://soap.sforce.com/2006/04/metadata'}
            active_version = root.find('.//met:activeVersionIdentifier', ns)
            if active_version is not None:
                active_id = active_version.text
                for version in root.findall('.//met:templateVersions', ns):
                    version_id_elem = version.find('met:versionIdentifier', ns)
                    if version_id_elem is not None and version_id_elem.text == active_id:
                        model_elem = version.find('met:primaryModel', ns)
                        if model_elem is not None:
                            response_model = model_elem.text or ''
                            log_print(f"   ✅ Found model in prompt template: {response_model} (for display only)")
                            break
    except Exception as e:
        log_print(f"   ⚠️  Could not retrieve model: {e}")
        response_model = "Unknown"

    if not models_list or len(models_list) == 0:
        log_print(f"   ⚠️  WARNING: No models_list provided from YAML config! API calls may fail.")
    else:
        log_print(f"   ✅ Using models from YAML config: {models_list[0]} (primary) + {len(models_list)-1} fallback(s)")

    log_print(f"   📝 Processing {len(questions_list)} questions...")
    for q_num, q_text, _ in questions_list:
        log_print(f"   ✓ {q_num}: {q_text[:60]}...")

    prompt_dev_name = prompt_template_name
    log_print(f"   🔧 Using prompt API name (DeveloperName): {prompt_dev_name}")

    total = len(questions_list)
    all_responses = []
    log_print(f"\n   🚀 Starting prompt invocations (sequential, {total} questions)...")
    for idx, (q_num, q_text, _) in enumerate(questions_list):
        log_print(f"\n   🔄 [{idx+1}/{total}] Invoking prompt for {q_num}...")
        log_print(f"      Question: {q_text[:80]}...")
        try:
            result, model_used = invoke_prompt(instance_url, access_token, q_text, prompt_dev_name, max_retries=3, model_used=None, models_list=models_list)
            log_print(f"      ✅ Response received (model: {model_used}, length: {len(result)} chars)")
            if 'Error' in result or 'API Error' in result:
                log_print(f"   ⚠️  {q_num}: Error - {result[:100]}")
            else:
                log_print(f"   ✅ {q_num}: Success")
        except Exception as e:
            result = f"Error invoking prompt: {e}"
            model_used = "Unknown"
            log_print(f"   ⚠️  {q_num}: Exception - {e}")
        all_responses.append((q_num, result, model_used))

    log_print("   📝 Updating DataFrame with answers...")
    df_answers = df_new.copy()
    for i, (q_num, answer_text, model_used) in enumerate(all_responses):
        if i < len(df_answers):
            df_answers.iloc[i + 1, 2] = answer_text  # Received Answer
            df_answers.iloc[i + 1, 4] = model_used  # Model Used

    # Build final DataFrame: metadata + answers
    final_df = pd.concat([metadata_header, df_answers], ignore_index=True)

    # Get LLM parser prompt from search index
    llm_parser_prompt_current = ""
    try:
        log_print(f"   🔍 Retrieving LLM parser prompt from search index...")
        search_api = SearchIndexAPI(instance_url, access_token)
        full_index = search_api.get_index(search_index_id)
        if full_index:
            parsing_configs = full_index.get('parsingConfigurations', [])
            for config in parsing_configs:
                config_obj = config.get('config', {})
                config_id = config_obj.get('id', '').lower()
                if 'llm' in config_id or 'parse_documents_using_llm' in config_id:
                    user_values = config_obj.get('userValues', [])
                    for uv in user_values:
                        if uv.get('id') == 'prompt':
                            llm_parser_prompt_current = uv.get('value', '')
                            if llm_parser_prompt_current:
                                log_print(f"   ✅ Found LLM parser prompt ({len(llm_parser_prompt_current)} chars)")
                                break
                    if llm_parser_prompt_current:
                        break
            if not llm_parser_prompt_current:
                log_print(f"   ⚠️  LLM parser prompt not found in index")
        else:
            log_print(f"   ⚠️  Search index not found")
    except Exception as e:
        log_print(f"   ⚠️  Could not retrieve LLM parser prompt: {e}")

    # Get Prompt Builder Prompt from prompt template metadata
    prompt_builder_prompt_current = ""
    try:
        log_print(f"   🔍 Retrieving Prompt Builder Prompt from template metadata...")
        xml_content = retrieve_metadata_via_api(instance_url, access_token, "GenAiPromptTemplate", prompt_template_name)
        if xml_content:
            root = ET.fromstring(xml_content)
            ns = {'met': 'http://soap.sforce.com/2006/04/metadata'}
            active_version = root.find('.//met:activeVersionIdentifier', ns)
            if active_version is not None:
                active_id = active_version.text
                for version in root.findall('.//met:templateVersions', ns):
                    version_id_elem = version.find('met:versionIdentifier', ns)
                    if version_id_elem is not None and version_id_elem.text == active_id:
                        content_elem = version.find('met:content', ns)
                        if content_elem is not None and content_elem.text:
                            prompt_builder_prompt_current = content_elem.text.strip()
                            # Unescape HTML entities
                            prompt_builder_prompt_current = prompt_builder_prompt_current.replace('&quot;', '"').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                            log_print(f"   ✅ Found Prompt Builder Prompt ({len(prompt_builder_prompt_current)} chars)")
                            break
            if not prompt_builder_prompt_current:
                log_print(f"   ⚠️  Prompt Builder Prompt not found in template")
        else:
            log_print(f"   ⚠️  Could not retrieve template metadata")
    except Exception as e:
        log_print(f"   ⚠️  Could not retrieve Prompt Builder Prompt: {e}")

    # Add bottom metadata (parser/prompt, etc.) - match old working structure
    # Each label has its value in the NEXT row, column 1 (B)
    num_cols = len(final_df.columns)
    
    # Determine stage-specific values
    if refinement_stage == "agentforce_agent":
        agentforce_config_current_value = 'Not found'  # TODO: retrieve if needed
        agentforce_config_proposed_value = ''  # Empty, will be filled by Gemini
    else:
        agentforce_config_current_value = 'not the current stage'
        agentforce_config_proposed_value = 'not the current stage'
    
    if refinement_stage == "response_prompt":
        prompt_builder_proposed_value = ''  # Empty, will be filled by Gemini
    else:
        prompt_builder_proposed_value = 'not the current stage'
    
    bottom_rows = [
        [''] * num_cols,
        ['LLM Parser Prompt Current:', ''] + [''] * (num_cols - 2),
        ['', llm_parser_prompt_current[:50000] if llm_parser_prompt_current else 'Not found'] + [''] * (num_cols - 2),  # Value in next row, column 1
        [''] * num_cols,
        ['LLM Parser Prompt Proposed from Gemini:', ''] + [''] * (num_cols - 2),
        ['', ''] + [''] * (num_cols - 2),  # Empty, will be filled by Gemini
        [''] * num_cols,
        ['Prompt Builder Prompt:', ''] + [''] * (num_cols - 2),
        ['', prompt_builder_prompt_current[:50000] if prompt_builder_prompt_current else 'Not found'] + [''] * (num_cols - 2),  # Always populated with current
        [''] * num_cols,
        ['Prompt Builder Prompt Proposed from Gemini:', ''] + [''] * (num_cols - 2),
        ['', prompt_builder_proposed_value] + [''] * (num_cols - 2),  # "not the current stage" or empty
        [''] * num_cols,
        ['Agentforce Agent Configuration Current:', ''] + [''] * (num_cols - 2),
        ['', agentforce_config_current_value] + [''] * (num_cols - 2),
        [''] * num_cols,
        ['Agentforce Agent Configuration Proposed from Gemini:', ''] + [''] * (num_cols - 2),
        ['', agentforce_config_proposed_value] + [''] * (num_cols - 2),
        [''] * num_cols,
        ['Instructions to Gemini:', ''] + [''] * (num_cols - 2),
        ['', ''] + [''] * (num_cols - 2)  # Empty initially, will be filled with full instructions after Gemini analysis
    ]
    bottom_df = pd.DataFrame(bottom_rows, columns=final_df.columns)

    final_df = pd.concat([final_df, bottom_df], ignore_index=True)

    # Write to Excel
    from openpyxl import load_workbook
    excel_file_exists = Path(excel_file).exists()
    try:
        if excel_file_exists:
            with pd.ExcelWriter(excel_file, engine='openpyxl', mode='a', if_sheet_exists='new') as writer:
                final_df.to_excel(writer, sheet_name=f"analysis_{refinement_stage}_cycle{cycle_number}_{Path(excel_file).stem}", index=False, header=False)
        else:
            with pd.ExcelWriter(excel_file, engine='openpyxl', mode='w') as writer:
                final_df.to_excel(writer, sheet_name=f"analysis_{refinement_stage}_cycle{cycle_number}_{Path(excel_file).stem}", index=False, header=False)
    except Exception as e:
        log_print(f"❌ Error writing to Excel: {e}")
        sys.exit(1)

    # Ensure Running_Score sheet exists (create if new file or if missing)
    # Match old working structure exactly
    try:
        import openpyxl
        wb = openpyxl.load_workbook(excel_file)
        if 'Running_Score' not in wb.sheetnames:
            # Create new sheet at position 0 (first tab)
            ws = wb.create_sheet('Running_Score', 0)
            questions = config_dict.get('questions', []) if config_dict else []
            
            # Build initial structure - match old code exactly
            rows = []
            # Row 1: Headers
            header_row = ['Metric', ''] + ['Run 1']
            rows.append(header_row)
            # Rows 2-10: Summary metrics
            rows.append(['Run ID', ''])
            rows.append(['Timestamp', ''])
            rows.append(['Cycle', ''])
            rows.append(['Pass', ''])
            rows.append(['Fail', ''])
            rows.append(['Total', ''])
            rows.append(['Pass Rate', ''])
            rows.append(['Avg Safety', ''])
            rows.append(['Stage Status', ''])
            rows.append([''])  # Empty separator
            # Row 12: Question headers (column 3 should be empty, header is only in row 1)
            question_header = ['Q#', 'Question', '']
            rows.append(question_header)
            # Rows 13+: Questions
            for q in questions:
                q_row = [q.get('number', ''), q.get('text', '')]
                rows.append(q_row)
            
            # Write initial structure
            for row_idx, row_data in enumerate(rows, start=1):
                for col_idx, value in enumerate(row_data, start=1):
                    ws.cell(row=row_idx, column=col_idx, value=value)
            
            wb.save(excel_file)
            wb.close()
            log_print(f"   ✅ Created Running_Score sheet")
        else:
            wb.close()
    except Exception as e:
        log_print(f"   ⚠️  Could not create/verify Running_Score sheet: {e}")

    sheet_name = f"analysis_{refinement_stage}_cycle{cycle_number}_{Path(excel_file).stem}"
    log_print(f"   ✅ Created new sheet: {sheet_name}")
    return sheet_name


def update_run_summary_sheet(excel_file, run_id, cycle_number, results_data, config_dict):
    """
    Update or create Running_Score sheet with cycle results.
    This sheet tracks all cycles across all runs, with each run/cycle as a column.
    
    Args:
        excel_file: Path to run-specific Excel file
        run_id: Unique run identifier
        cycle_number: Current cycle number
        results_data: Dict with cycle results containing:
            - timestamp: str
            - pass_count: int
            - fail_count: int
            - total: int
            - pass_rate: float
            - avg_safety: float
            - stage_status: str
            - question_results: list of dicts with 'q_number' and 'status'
        config_dict: Frozen YAML config (for questions list)
    """
    import openpyxl
    from pathlib import Path
    
    sheet_name = 'Running_Score'
    excel_path = Path(excel_file)
    
    if not excel_path.exists():
        log_print(f"  ⚠️  Excel file not found: {excel_file}")
        return
    
    try:
        # Load workbook
        wb = openpyxl.load_workbook(excel_file)
        
        # Get questions from config
        questions = config_dict.get('questions', [])
        
        # Check if Running_Score sheet exists
        if sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            existing_runs = True
        else:
            # Create new sheet at position 0 (first tab)
            ws = wb.create_sheet(sheet_name, 0)
            existing_runs = False
        
        # Determine column index for new run/cycle
        if existing_runs:
            # Find last data column (after Q# and Question)
            # Check for actual DATA (row 2 = Run ID), not just headers (row 1)
            # This is important because the sheet might have "Run 1" header but no data yet
            last_col = 3  # Start at column 3 (first run column)
            while ws.cell(row=2, column=last_col).value is not None and str(ws.cell(row=2, column=last_col).value).strip():
                last_col += 1
            new_col_index = last_col  # Next available column (or column 3 if empty)
        else:
            # First run - create structure
            new_col_index = 3  # After Q# (col 1) and Question (col 2)
            
            # Build initial structure
            rows = []
            # Row 0: Headers
            header_row = ['Metric', ''] + [f"Run 1"]
            rows.append(header_row)
            # Rows 1-9: Summary metrics
            rows.append(['Run ID', ''])
            rows.append(['Timestamp', ''])
            rows.append(['Cycle', ''])
            rows.append(['Pass', ''])
            rows.append(['Fail', ''])
            rows.append(['Total', ''])
            rows.append(['Pass Rate', ''])
            rows.append(['Avg Safety', ''])
            rows.append(['Stage Status', ''])
            rows.append([''])  # Empty separator
            # Row 12: Question headers (column 3 should be empty, header is only in row 1)
            question_header = ['Q#', 'Question', '']
            rows.append(question_header)
            # Rows 11+: Questions
            for q in questions:
                q_row = [q.get('number', ''), q.get('text', '')]
                rows.append(q_row)
            
            # Write initial structure
            for row_idx, row_data in enumerate(rows, start=1):
                for col_idx, value in enumerate(row_data, start=1):
                    ws.cell(row=row_idx, column=col_idx, value=value)
        
        # Update header for this column
        run_label = f"Run {new_col_index - 2}"  # Run 1, Run 2, etc.
        ws.cell(row=1, column=new_col_index, value=run_label)
        
        # Populate summary metrics (rows 2-10)
        ws.cell(row=2, column=new_col_index, value=run_id)
        ws.cell(row=3, column=new_col_index, value=results_data['timestamp'])
        ws.cell(row=4, column=new_col_index, value=cycle_number)
        ws.cell(row=5, column=new_col_index, value=results_data['pass_count'])
        ws.cell(row=6, column=new_col_index, value=results_data['fail_count'])
        ws.cell(row=7, column=new_col_index, value=results_data['total'])
        ws.cell(row=8, column=new_col_index, value=f"{results_data['pass_rate']:.1f}%")
        ws.cell(row=9, column=new_col_index, value=results_data['avg_safety'])
        ws.cell(row=10, column=new_col_index, value=results_data['stage_status'])
        
        # Populate question results (starting at row 12, which is index 11)
        question_start_row = 12
        for q_result in results_data['question_results']:
            q_number = q_result['q_number']
            status = q_result['status']
            
            # Find row for this question
            for row_idx in range(question_start_row, ws.max_row + 1):
                if ws.cell(row=row_idx, column=1).value == q_number:
                    # Set Pass/Fail
                    cell_value = '✅ PASS' if status == 'PASS' else '❌ FAIL'
                    ws.cell(row=row_idx, column=new_col_index, value=cell_value)
                    break
        
        # Ensure Running_Score is first sheet
        if wb.sheetnames[0] != sheet_name:
            wb.move_sheet(wb[sheet_name], offset=-len(wb.sheetnames))
        
        # Save workbook
        wb.save(excel_file)
        wb.close()
        
        log_print(f"  ✅ Updated Running_Score sheet with Run {new_col_index - 2} (Cycle {cycle_number})")
        
    except Exception as e:
        log_print(f"  ⚠️  Error updating Running_Score sheet: {e}")
        import traceback
        traceback.print_exc()


__all__ = ["create_analysis_sheet_with_prompts", "update_run_summary_sheet"]



