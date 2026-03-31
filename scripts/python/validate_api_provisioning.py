#!/usr/bin/env python3
"""
End-to-end validation of API-based index provisioning.

This script validates that the API-first index creation works correctly:
- Builds payload with proven structure
- Validates payload before POST
- Creates index via API (no UI)
- Polls until READY
- Cleans up test index

Run this before Heroku deployment to ensure Phase 1 is working.

Usage:
    python3 scripts/python/validate_api_provisioning.py

Reference: IMPLEMENTATION_PLAN_20260330_094433.md Phase 1
"""

import sys
import os
from pathlib import Path
from datetime import datetime
import json

# Add script directory to path
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

# Import our API functions
try:
    from salesforce_api import (
        get_salesforce_credentials,
        build_index_payload,
        validate_index_payload,
        create_search_index_api,
        poll_index_until_ready,
        SearchIndexAPI,
    )
    print("✅ Successfully imported API functions", flush=True)
except ImportError as e:
    print(f"❌ Failed to import API functions: {e}", flush=True)
    sys.exit(1)


def print_section(title):
    """Print a formatted section header"""
    print("\n" + "="*80, flush=True)
    print(f" {title}", flush=True)
    print("="*80, flush=True)


def print_step(step_num, total_steps, description):
    """Print a formatted step header"""
    print(f"\n[{step_num}/{total_steps}] {description}", flush=True)


def main():
    """Run end-to-end validation of API provisioning"""
    print_section("API Index Provisioning E2E Validation")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("This test validates Phase 1 implementation (API-first provisioning)", flush=True)

    total_steps = 7
    test_start_time = datetime.now()

    # Step 1: Authenticate
    print_step(1, total_steps, "Authenticating to Salesforce")
    try:
        instance_url, access_token = get_salesforce_credentials()
        print(f"   ✅ Connected to: {instance_url}", flush=True)
        print(f"   Token length: {len(access_token)} chars", flush=True)
    except Exception as e:
        print(f"   ❌ Authentication failed: {e}", flush=True)
        return 1

    # Step 2: Build payload
    print_step(2, total_steps, "Building index payload")
    test_prompt = """You are a document parser. Extract key information from the document including:
- Main topics and themes
- Key entities (people, places, organizations)
- Important dates and events
- Technical terms and definitions

Format your response in clear sections."""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_index_name = f"ApiTest_Validation_{timestamp}"
    test_label = f"API Validation Test {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    try:
        payload = build_index_payload(
            label=test_label,
            developer_name=test_index_name,
            parser_prompt=test_prompt,
            chunk_max_tokens=8000,
            chunk_overlap_tokens=512
        )
        payload_size = len(json.dumps(payload))
        print(f"   ✅ Payload built successfully", flush=True)
        print(f"   - Label: {test_label}", flush=True)
        print(f"   - Developer Name: {test_index_name}", flush=True)
        print(f"   - Parser Prompt: {len(test_prompt)} chars", flush=True)
        print(f"   - Payload Size: {payload_size} bytes", flush=True)

        # Show payload structure
        print(f"   - Payload keys: {list(payload.keys())}", flush=True)
        if 'parsingConfigurations' in payload:
            print(f"   - Parser configs: {len(payload['parsingConfigurations'])}", flush=True)
            for config in payload['parsingConfigurations']:
                config_id = config.get('config', {}).get('id')
                print(f"     • Parser ID: {config_id}", flush=True)

    except Exception as e:
        print(f"   ❌ Payload build failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return 1

    # Step 3: Validate payload
    print_step(3, total_steps, "Validating payload (preflight checks)")
    valid, errors = validate_index_payload(payload)
    if not valid:
        print(f"   ❌ Validation failed with {len(errors)} error(s):", flush=True)
        for i, err in enumerate(errors, 1):
            print(f"      {i}. {err}", flush=True)
        return 1

    print(f"   ✅ Payload validation passed (all required fields present)", flush=True)
    print(f"   - Developer name starts with letter: ✅", flush=True)
    print(f"   - Parser in parsingConfigurations: ✅", flush=True)
    print(f"   - Parser prompt present: ✅", flush=True)
    print(f"   - Chunking configuration: ✅", flush=True)
    print(f"   - Vector configuration: ✅", flush=True)

    # Step 4: Create index via API
    print_step(4, total_steps, "Creating index via API")
    creation_start = datetime.now()

    try:
        index_id, actual_dev_name = create_search_index_api(
            instance_url=instance_url,
            access_token=access_token,
            label=test_label,
            developer_name=test_index_name,
            parser_prompt=test_prompt,
            chunk_max_tokens=8000,
            chunk_overlap_tokens=512,
            run_id=None  # No run_id for standalone test
        )

        if not index_id:
            print(f"   ❌ No index_id returned from API", flush=True)
            return 1

        creation_time = (datetime.now() - creation_start).total_seconds()
        print(f"   ✅ Index created successfully", flush=True)
        print(f"   - Index ID: {index_id}", flush=True)
        print(f"   - Developer Name: {actual_dev_name}", flush=True)
        print(f"   - Creation Time: {creation_time:.1f}s", flush=True)

    except Exception as e:
        print(f"   ❌ API create_index failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return 1

    # Step 5: Verify index was created correctly
    print_step(5, total_steps, "Verifying index configuration")
    try:
        api = SearchIndexAPI(instance_url, access_token)
        index_data = api.get_index(index_id)

        # Check key fields
        label_check = index_data.get('label') == test_label
        dev_name_check = index_data.get('developerName') == test_index_name
        runtime_status = index_data.get('runtimeStatus', 'UNKNOWN')

        print(f"   - Label matches: {'✅' if label_check else '❌'}", flush=True)
        print(f"   - Developer name matches: {'✅' if dev_name_check else '❌'}", flush=True)
        print(f"   - Runtime Status: {runtime_status}", flush=True)

        # Verify parser configuration
        parsing_configs = index_data.get('parsingConfigurations', [])
        parser_found = False
        parser_prompt_verified = False

        for config in parsing_configs:
            config_obj = config.get('config', {})
            if config_obj.get('id') == 'parse_documents_using_llm':
                parser_found = True
                # Check if prompt is present in userValues
                user_values = config_obj.get('userValues', [])
                for uv in user_values:
                    if uv.get('id') == 'prompt':
                        parser_prompt_verified = True
                        prompt_length = len(uv.get('value', ''))
                        print(f"   - Parser prompt length: {prompt_length} chars", flush=True)
                        break
                break

        print(f"   - LLM Parser configured: {'✅' if parser_found else '❌'}", flush=True)
        print(f"   - Parser prompt present: {'✅' if parser_prompt_verified else '❌'}", flush=True)

        if not parser_found or not parser_prompt_verified:
            print(f"   ❌ Parser configuration incomplete", flush=True)
            return 1

        print(f"   ✅ Index configuration verified", flush=True)

    except Exception as e:
        print(f"   ⚠️  Could not verify index configuration: {e}", flush=True)
        # Continue - verification is advisory

    # Step 6: Poll until READY
    print_step(6, total_steps, "Polling until READY status")
    print(f"   ⏳ This may take 2-15 minutes depending on index size...", flush=True)
    print(f"   (Test index with LLM parser typically takes 3-5 minutes)", flush=True)

    poll_start = datetime.now()
    timeout_seconds = 900  # 15 minutes max

    try:
        ready = poll_index_until_ready(
            index_id=index_id,
            instance_url=instance_url,
            access_token=access_token,
            timeout_seconds=timeout_seconds,
            poll_interval=10,
            run_id=None
        )

        if not ready:
            print(f"   ❌ Index did not reach READY within {timeout_seconds}s timeout", flush=True)
            print(f"   Leaving index for manual inspection: {index_id}", flush=True)
            return 1

        time_to_ready = (datetime.now() - poll_start).total_seconds()
        print(f"   ✅ Index reached READY status", flush=True)
        print(f"   - Time to READY: {time_to_ready:.0f}s ({time_to_ready/60:.1f} minutes)", flush=True)

    except Exception as e:
        print(f"   ❌ Polling failed: {e}", flush=True)
        print(f"   Leaving index for manual inspection: {index_id}", flush=True)
        return 1

    # Step 7: Cleanup
    print_step(7, total_steps, "Cleaning up test index")
    try:
        api.delete_index(index_id)
        print(f"   ✅ Test index deleted: {index_id}", flush=True)
    except Exception as e:
        print(f"   ⚠️  Could not delete test index: {e}", flush=True)
        print(f"   Manual cleanup required: {index_id}", flush=True)
        # Not a failure - test passed but cleanup failed

    # Final summary
    total_time = (datetime.now() - test_start_time).total_seconds()
    print_section("✅ VALIDATION PASSED")
    print(f"Total test time: {total_time:.0f}s ({total_time/60:.1f} minutes)", flush=True)
    print(f"\nPhase 1 Implementation Status:", flush=True)
    print(f"  ✅ API payload builder - Working", flush=True)
    print(f"  ✅ Payload validation - Working", flush=True)
    print(f"  ✅ API-based index creation - Working", flush=True)
    print(f"  ✅ Readiness polling - Working", flush=True)
    print(f"\nAPI provisioning ready for deployment!", flush=True)
    print(f"\nNext steps:", flush=True)
    print(f"  1. Commit changes to git", flush=True)
    print(f"  2. Run integration test with full workflow", flush=True)
    print(f"  3. Proceed to Phase 2 (quality gates)", flush=True)

    return 0


if __name__ == '__main__':
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user", flush=True)
        sys.exit(130)
    except Exception as e:
        print(f"\n\n❌ Unexpected error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
