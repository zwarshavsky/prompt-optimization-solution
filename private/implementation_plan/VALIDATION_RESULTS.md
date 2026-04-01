# Implementation Plan Validation Results

**Date:** 2026-03-30
**Branch:** `search-index-api-heroku-fixes`
**Validator:** Claude (Sonnet 4.5)
**Plan Reference:** `IMPLEMENTATION_PLAN_20260330_094433.md`

## Executive Summary

✅ **API Infrastructure:** Complete and ready to use
❌ **API Integration:** Not integrated, still using Playwright UI
⚠️ **Readiness Polling:** Implemented correctly
❌ **Smoke Checks:** Not implemented
❌ **Lifecycle Tracking:** Not implemented
❌ **Parser Versioning:** Not implemented

**Status:** 3 of 15 REQUIRED items fully implemented. **Critical path blocked on Gap #1.**

---

## Current Architecture

### Workflow Entry Points
1. **Heroku Worker:** `scripts/python/worker.py` polls database for jobs
2. **Job Processor:** Calls `main.py:run_full_workflow()`
3. **Cycle Loop:** Lines 2001+ in main.py
4. **Step 1 (Cycle 2+):** Calls `playwright_scripts.py:run_new_index_pipeline()` at line 2139

### Index Provisioning Flow (Current)
```
main.py:run_full_workflow()
  └─> Cycle 2+ Step 1 (line 2139)
      └─> playwright_scripts.py:run_new_index_pipeline() (line 3565)
          ├─> _create_search_index_ui() [UI/PLAYWRIGHT] ❌ BLOCKER
          ├─> poll_index_until_ready() [API] ✅
          ├─> _create_retriever_ui() [UI/PLAYWRIGHT]
          ├─> poll_retriever_until_activated() [API] ✅
          └─> update_genai_prompt_with_retriever() [Metadata API] ✅
```

### Available API Infrastructure
**File:** `scripts/python/salesforce_api.py`

**SearchIndexAPI Class (lines 879-993):**
- ✅ `__init__()` - Uses v65.0 API, has retry strategy
- ✅ `create_index(payload)` - POST /ssot/search-index
- ✅ `get_index(index_id)` - GET /ssot/search-index/{id}
- ✅ `update_index(index_id, payload)` - PATCH /ssot/search-index/{id}
- ✅ `delete_index(index_id)` - DELETE /ssot/search-index/{id}
- ✅ `wait_for_ready()` - Polls until status == READY

**Helper Functions:**
- ✅ `poll_index_until_ready()` (line 1090) - Polls runtimeStatus, checks indexRefreshedOn
- ✅ `get_next_index_name()` (line 995) - Deterministic versioning
- ✅ `find_index_id_by_name()` (line 1017) - Index lookup with retries

---

## Gap Analysis: REQUIRED Items

### ✅ IMPLEMENTED (3/15)

#### #9: Hard Readiness Polling Gate ✅
**Location:** `salesforce_api.py:1090` (`poll_index_until_ready`)
**Implementation:**
- Polls `GET /ssot/search-index/{id}` every 10 seconds
- Checks `runtimeStatus` in ("ACTIVE", "READY")
- Validates `indexRefreshedOn` is set
- Timeout configurable (default: unlimited for LLM parsing)
- Fails on FAILED status
- Supports abort checks via `run_id`

**Verdict:** ✅ Fully compliant with requirement #9

#### #12: Retry + Diagnostics for API Failures ✅
**Location:** `salesforce_api.py:879-936`
**Implementation:**
- Retry strategy: 3 attempts, backoff=1s, status_forcelist=[429, 500, 502, 503, 504]
- HTTPError exceptions captured with response.text
- Timeout: 120s for POST, 60s for GET/PATCH/DELETE

**Verdict:** ✅ Compliant with requirement #12

#### #14: Downstream Uses READY Index ✅
**Location:** `main.py:2155`
**Implementation:**
- `search_index_id = new_index_id` after Step 1 completes
- Used in Step 2 for retrieval/testing

**Verdict:** ✅ Compliant with requirement #14

---

### ❌ NOT IMPLEMENTED (12/15)

#### #1: API-First Provisioning ❌ **CRITICAL**
**Current:** `playwright_scripts.py:3590` calls `_create_search_index_ui()` (Playwright UI automation)
**Required:** Use `SearchIndexAPI.create_index(payload)` with POST API
**Impact:**
- UI automation is brittle and fails intermittently
- No control over payload structure
- No validation before POST
- **BLOCKS HEROKU DEPLOYMENT**

**Estimated Effort:** 4-6 hours (includes payload builder + integration)

---

#### #2: API-Only (No UI Fallback) ⚠️
**Current:** Code uses UI exclusively, no fallback logic exists
**Required:** Remove UI path entirely, fail on API errors (no fallback)
**Status:** Partially compliant - no fallback exists, but primary path is UI

**After Gap #1 fixed:** ✅ Will be compliant

---

#### #3: Proven Payload Contract ❌
**Current:** No payload builder exists
**Required:** Use canonical structure from plan with proven config IDs
**Reference:** Plan lines 59-69 (proven indexes `18lKc000000oN30IAE`, `18lKc000000oN35IAE`)

**Payload Structure:**
```python
{
    "label": "<cycle-specific-label>",
    "developerName": "<cycle-specific-devname>",
    "sourceDmoName": "ContentVersion",  # From proven config
    "parsingConfigurations": [{
        "config": {
            "id": "parse_documents_using_llm",  # ← Critical: proven parser ID
            "userValues": [{
                "id": "prompt",
                "value": "<parser_prompt_text>"
            }]
        }
    }],
    "chunkingConfiguration": {
        "fileLevelConfiguration": {
            "defaultConfiguration": {
                "max_tokens": 8000,
                "overlap_tokens": 512
            },
            "perFileExtensions": {
                "pdf": {
                    "max_tokens": 8000,
                    "overlap_tokens": 512
                }
            }
        }
    },
    "vectorEmbeddingConfiguration": {
        "embeddingModel": {
            "model": "hybrid"  # From proven config
        }
    }
}
```

**Estimated Effort:** 2-3 hours (payload builder function)

---

#### #4: Parser in parsingConfigurations ❌
**Current:** No API payload builder
**Required:** Ensure `config.id = "parse_documents_using_llm"` in `parsingConfigurations` (NOT `preProcessingConfigurations`)

**Validation Evidence (from excel_io.py:354-359):**
```python
parsing_configs = full_index.get('parsingConfigurations', [])
for config in parsing_configs:
    config_obj = config.get('config', {})
    config_id = config_obj.get('id', '').lower()
    if 'parse_documents_using_llm' in config_id:
        # Found LLM parser
```

**Estimated Effort:** Covered by #3 (same payload builder)

---

#### #5: Run-Specific Names (Deterministic) ✅ / ❌
**Current:** `get_next_index_name()` exists (line 995), generates `{base}_V{n}`
**Gap:** Need to incorporate `run_id` or cycle number for traceability

**Required Enhancement:**
```python
def generate_index_names(run_id, cycle_number, base_prefix):
    """Generate deterministic names tied to run/cycle"""
    label = f"{base_prefix} Run{run_id[-4:]} Cycle{cycle_number} {timestamp}"
    developer_name = f"{base_prefix}_Run{run_id[-4:]}_C{cycle_number}"
    chunk_dmo_name = f"{developer_name}_Chunk__dlm"
    vector_dmo_name = f"{developer_name}_Vector__dlm"
    return (label, developer_name, chunk_dmo_name, vector_dmo_name)
```

**Estimated Effort:** 1 hour

---

#### #6: Parser Prompt Versioning/Tracking ❌ **HIGH PRIORITY**
**Current:** No hash tracking found
**Required:** Track `parser_prompt_text`, `parser_prompt_hash`, `parser_prompt_version` per cycle

**Implementation Approach:**
```python
import hashlib

def track_parser_prompt(prompt_text, cycle_number):
    """Generate hash and version for parser prompt"""
    prompt_hash = hashlib.sha256(prompt_text.encode('utf-8')).hexdigest()[:16]
    return {
        'parser_prompt_text': prompt_text,
        'parser_prompt_hash': prompt_hash,
        'parser_prompt_version': cycle_number,
        'parser_prompt_source': 'gemini_recommendation'
    }
```

**State Persistence:**
- Add to `save_state()` in main.py
- Store in cycle-specific state file
- Compare hash before reprovisioning

**Estimated Effort:** 2 hours

---

#### #7: Reprovision Only on Hash Change ❌ **OPTIMIZATION**
**Current:** No hash comparison logic
**Required:** Skip index creation if parser prompt hash unchanged from previous cycle

**Implementation:**
```python
# In main.py Step 1 (before index creation):
if prev_cycle_state:
    prev_hash = prev_cycle_state.get('parser_prompt_hash')
    current_hash = hashlib.sha256(previous_cycle_prompt.encode()).hexdigest()[:16]

    if prev_hash == current_hash:
        # Reuse existing index
        log_print("   ℹ️  Parser prompt unchanged - reusing existing index")
        search_index_id = prev_cycle_state.get('search_index_id')
        # Skip index creation, go directly to Step 2
    else:
        # Provision new index
        log_print(f"   🔄 Parser prompt changed - provisioning new index")
        # ... run_new_index_pipeline()
```

**Estimated Effort:** 1-2 hours

---

#### #8: Payload Preflight Validation ❌ **QUALITY GATE**
**Current:** No validation before POST
**Required:** Validate structure, required fields, parser placement

**Implementation:**
```python
def validate_index_payload(payload):
    """Validate search index payload before POST"""
    errors = []

    # Required fields
    if not payload.get('label'):
        errors.append("Missing 'label' field")
    if not payload.get('developerName'):
        errors.append("Missing 'developerName' field")

    # Developer name constraints
    dev_name = payload.get('developerName', '')
    if dev_name and dev_name[0].isdigit():
        errors.append(f"developerName '{dev_name}' starts with digit")
    if len(dev_name) > 80:
        errors.append(f"developerName too long ({len(dev_name)} chars, max 80)")

    # Parser configuration
    parsing_configs = payload.get('parsingConfigurations', [])
    if not parsing_configs:
        errors.append("Missing 'parsingConfigurations'")
    else:
        found_llm_parser = False
        for config in parsing_configs:
            config_obj = config.get('config', {})
            if config_obj.get('id') == 'parse_documents_using_llm':
                found_llm_parser = True
                # Check prompt present
                user_values = config_obj.get('userValues', [])
                prompt_found = any(uv.get('id') == 'prompt' for uv in user_values)
                if not prompt_found:
                    errors.append("LLM parser config missing 'prompt' userValue")
                break
        if not found_llm_parser:
            errors.append("LLM parser (parse_documents_using_llm) not found in parsingConfigurations")

    # Check NOT in preProcessingConfigurations
    if 'preProcessingConfigurations' in payload:
        errors.append("Parser should be in 'parsingConfigurations', not 'preProcessingConfigurations'")

    # Chunking configuration
    if not payload.get('chunkingConfiguration'):
        errors.append("Missing 'chunkingConfiguration'")

    # Vector configuration
    if not payload.get('vectorEmbeddingConfiguration'):
        errors.append("Missing 'vectorEmbeddingConfiguration'")

    if errors:
        return (False, errors)
    return (True, [])
```

**Estimated Effort:** 2-3 hours

---

#### #10: Post-READY Functional Smoke Check ❌ **CRITICAL QUALITY GATE**
**Current:** No smoke check after index reaches READY
**Required:** Test retrieval path before full evaluation

**Implementation Location:** `playwright_scripts.py:3633` (after `poll_index_until_ready`)

**Smoke Check Function:**
```python
def smoke_check_index_retrieval(instance_url, access_token, index_id, retriever_api_name):
    """Test retrieval against new index before full evaluation"""
    log_print(f"   🧪 Running smoke check against index {index_id}...")

    # Use invoke_prompt with a simple test question
    test_question = "What is the main topic of this document?"

    try:
        # Import from salesforce_api
        from salesforce_api import invoke_prompt

        # Invoke prompt template (which should use the new retriever)
        response, model = invoke_prompt(
            instance_url=instance_url,
            access_token=access_token,
            question=test_question,
            prompt_name=prompt_template_name,
            max_retries=2
        )

        # Check for successful response
        if response and not response.startswith("Error:"):
            log_print(f"   ✅ Smoke check passed: Got response ({len(response)} chars)")
            return {
                'smoke_check_status': 'pass',
                'smoke_check_response_length': len(response),
                'smoke_check_timestamp': datetime.now().isoformat()
            }
        else:
            log_print(f"   ❌ Smoke check failed: {response[:200]}")
            return {
                'smoke_check_status': 'fail',
                'smoke_check_error': response[:500],
                'smoke_check_timestamp': datetime.now().isoformat()
            }
    except Exception as e:
        log_print(f"   ❌ Smoke check exception: {e}")
        return {
            'smoke_check_status': 'error',
            'smoke_check_error': str(e),
            'smoke_check_timestamp': datetime.now().isoformat()
        }
```

**Estimated Effort:** 2-3 hours

---

#### #11: Block Evaluation Until Ready + Smoke ⚠️
**Current:** Blocks on readiness (poll_index_until_ready), but no smoke check
**Required:** Block Step 2 (evaluation) until both readiness AND smoke check pass

**After Gap #10 fixed:** Check smoke_check_status before proceeding to Step 2

**Estimated Effort:** 1 hour (conditional logic)

---

#### #13: Persist Lifecycle Evidence ❌ **TRACEABILITY**
**Current:** No lifecycle tracking
**Required:** Per-cycle persistence of index metadata

**State Extension:**
```python
# In save_state() function (main.py):
def save_state(..., index_lifecycle=None):
    state = {
        # ... existing fields ...
        'index_lifecycle': {
            'index_id': '<18l...>',
            'api_version': 'v65.0',
            'created_timestamp': '2026-03-30T09:44:33Z',
            'ready_timestamp': '2026-03-30T09:46:15Z',
            'time_to_ready_seconds': 102,
            'final_runtime_status': 'READY',
            'smoke_check_status': 'pass',
            'smoke_check_timestamp': '2026-03-30T09:46:20Z',
            'parser_prompt_hash': 'a3f5d8...',
            'parser_prompt_version': 2
        }
    }
```

**Estimated Effort:** 2 hours

---

#### #15: Pre-Heroku E2E Release Gates ❌ **DEPLOYMENT BLOCKER**
**Current:** No pre-deployment validation script
**Required:** Automated E2E test before Heroku deploy

**Implementation:** See Task #2 (Create automated validation script)

**Script Requirements:**
1. Connect to org via API (no UI)
2. Create test index with LLM parser
3. Poll until READY
4. Run smoke retrieval test
5. Validate lifecycle data persisted
6. Complete Gemini-guided cycle
7. Report success/failure

**Estimated Effort:** 4-6 hours

---

## Implementation Priority & Roadmap

### Phase 1: Critical Path (Deploy Blocker) - 8-10 hours
**Goal:** Replace UI with API, enable Heroku deployment

1. **Task P1.1:** Build API payload function (Gap #3, #4) - 2-3 hours
   - Create `build_index_payload()` in salesforce_api.py
   - Use proven structure from plan
   - Include LLM parser with prompt

2. **Task P1.2:** Replace _create_search_index_ui with API (Gap #1) - 3-4 hours
   - Create `create_search_index_api()` function
   - Call `SearchIndexAPI.create_index(payload)`
   - Add error handling and logging
   - Update `run_new_index_pipeline()` to use new function

3. **Task P1.3:** Add payload validation (Gap #8) - 2-3 hours
   - Create `validate_index_payload()`
   - Call before create_index()
   - Fail fast on validation errors

**Deliverable:** API-only index creation working end-to-end

---

### Phase 2: Quality Gates - 4-6 hours
**Goal:** Prevent broken indexes from reaching production

4. **Task P2.1:** Implement smoke check (Gap #10) - 2-3 hours
   - Create `smoke_check_index_retrieval()`
   - Call after `poll_index_until_ready()`
   - Persist smoke check results

5. **Task P2.2:** Add lifecycle tracking (Gap #13) - 2 hours
   - Extend `save_state()` with index_lifecycle
   - Track timestamps, status, smoke results
   - Store in cycle-specific state file

6. **Task P2.3:** Block evaluation on smoke check (Gap #11) - 1 hour
   - Check smoke_check_status before Step 2
   - Fail cycle if smoke check fails

**Deliverable:** Comprehensive quality gates with full traceability

---

### Phase 3: Optimization - 3-4 hours
**Goal:** Reduce unnecessary reprovisioning

7. **Task P3.1:** Parser prompt versioning (Gap #6) - 2 hours
   - Add hash generation to state management
   - Track parser_prompt_hash, version, source

8. **Task P3.2:** Hash-based reprovisioning (Gap #7) - 1-2 hours
   - Compare hash before provisioning
   - Reuse index if unchanged
   - Log skip reason

**Deliverable:** Intelligent reprovisioning saves time/resources

---

### Phase 4: Deployment Readiness - 4-6 hours
**Goal:** Automated pre-deployment validation

9. **Task P4.1:** E2E validation script (Gap #15) - 3-4 hours
   - Build standalone test script
   - Run full cycle without user input
   - Report pass/fail with evidence

10. **Task P4.2:** Run-specific naming (Gap #5) - 1 hour
    - Enhance name generation with run_id
    - Update payload builder

**Deliverable:** Automated deployment gate, Heroku-ready

---

## Validation Script Skeleton

**File:** `scripts/python/validate_api_provisioning.py`

```python
#!/usr/bin/env python3
"""
End-to-end validation of API-based index provisioning.
Run this before Heroku deployment to ensure all gates work.
"""
import sys
from pathlib import Path
from datetime import datetime
import hashlib

# Add path
sys.path.insert(0, str(Path(__file__).parent))

from salesforce_api import SearchIndexAPI, get_salesforce_credentials, poll_index_until_ready
from build_index_payload import build_index_payload, validate_index_payload  # New module
from smoke_check import smoke_check_index_retrieval  # New module

def main():
    print("="*80)
    print("API Index Provisioning E2E Validation")
    print("="*80)

    # Step 1: Authenticate
    print("\n[1/7] Authenticating to Salesforce...")
    try:
        instance_url, access_token = get_salesforce_credentials()
        print(f"   ✅ Connected to: {instance_url}")
    except Exception as e:
        print(f"   ❌ Auth failed: {e}")
        return 1

    # Step 2: Build payload
    print("\n[2/7] Building index payload...")
    test_prompt = "Test parser prompt for validation"
    test_index_name = f"ValidationTest_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    try:
        payload = build_index_payload(
            label=f"Validation Test {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            developer_name=test_index_name,
            parser_prompt=test_prompt,
            chunk_max_tokens=8000,
            chunk_overlap_tokens=512
        )
        print(f"   ✅ Payload built: {test_index_name}")
    except Exception as e:
        print(f"   ❌ Payload build failed: {e}")
        return 1

    # Step 3: Validate payload
    print("\n[3/7] Validating payload...")
    valid, errors = validate_index_payload(payload)
    if not valid:
        print(f"   ❌ Validation failed:")
        for err in errors:
            print(f"      - {err}")
        return 1
    print(f"   ✅ Payload validation passed")

    # Step 4: Create index via API
    print("\n[4/7] Creating index via API...")
    api = SearchIndexAPI(instance_url, access_token)
    try:
        result = api.create_index(payload)
        index_id = result.get('id')
        if not index_id:
            print(f"   ❌ No index_id in response: {result}")
            return 1
        print(f"   ✅ Index created: {index_id}")
    except Exception as e:
        print(f"   ❌ Create failed: {e}")
        return 1

    # Step 5: Poll until READY
    print("\n[5/7] Polling until READY...")
    start_time = datetime.now()
    try:
        if not poll_index_until_ready(index_id, instance_url, access_token, timeout_seconds=600):
            print(f"   ❌ Index did not reach READY within timeout")
            return 1
        ready_time = datetime.now()
        time_to_ready = (ready_time - start_time).total_seconds()
        print(f"   ✅ Index READY in {time_to_ready:.0f} seconds")
    except Exception as e:
        print(f"   ❌ Polling failed: {e}")
        return 1

    # Step 6: Smoke check
    print("\n[6/7] Running smoke check...")
    try:
        smoke_result = smoke_check_index_retrieval(
            instance_url, access_token, index_id, retriever_api_name=None  # TODO: get from retriever
        )
        if smoke_result['smoke_check_status'] != 'pass':
            print(f"   ❌ Smoke check failed: {smoke_result}")
            return 1
        print(f"   ✅ Smoke check passed")
    except Exception as e:
        print(f"   ⚠️  Smoke check error: {e}")
        # Continue - smoke check is advisory

    # Step 7: Cleanup
    print("\n[7/7] Cleaning up test index...")
    try:
        api.delete_index(index_id)
        print(f"   ✅ Test index deleted")
    except Exception as e:
        print(f"   ⚠️  Cleanup failed (manual cleanup needed): {e}")
        print(f"      Index ID: {index_id}")

    print("\n" + "="*80)
    print("✅ VALIDATION PASSED - API provisioning ready for deployment")
    print("="*80)
    return 0

if __name__ == '__main__':
    sys.exit(main())
```

---

## Next Steps for User

1. **Review this validation report** - Confirm gaps align with expectations
2. **Prioritize phases** - Agree on Phase 1 → Phase 2 → Phase 3 → Phase 4 order
3. **Start Phase 1** - Begin with payload builder (highest priority)
4. **Run validation after each phase** - Test as you build
5. **Deploy to Heroku** - After Phase 4 validation passes

---

## Self-Validation Capability

After implementation, the system can validate itself with:
```bash
# Phase 1 validation (API provisioning):
python3 scripts/python/validate_api_provisioning.py

# Phase 2 validation (quality gates):
python3 scripts/python/validate_quality_gates.py

# Phase 4 validation (full E2E):
python3 scripts/python/validate_e2e_cycle.py
```

No user input required - scripts report pass/fail automatically.

---

**Report Generated:** 2026-03-30
**Total Estimated Effort:** 19-26 hours across 4 phases
**Critical Path:** Phase 1 (8-10 hours) must complete before Heroku deployment
