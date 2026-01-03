# Prompt Optimization Solution

End-to-end automated prompt testing and optimization application using RAG on Salesforce Data Cloud.

## ⚠️ IMPORTANT: API Verification Results

**API verification has been executed!** See `docs/API_VERIFICATION_RESULTS.md` for actual findings.

**Critical Findings**:
- ❌ Search Index APIs timing out (need to investigate)
- ❌ No retriever REST API found (all endpoints return 404)
- ❌ Cannot query prompt templates via SOQL/Tooling API
- ✅ File upload works

## Overview

This solution automates the process of optimizing RAG (Retrieval-Augmented Generation) configurations by:
- Testing different search index parameters
- Testing different retriever configurations  
- Testing different prompt template variations
- Comparing results against expected answers
- Iterating until performance benchmarks are met

## Documentation

### Architecture & Planning
- **[Architecture Document](docs/PROMPT_OPTIMIZATION_ARCHITECTURE.md)** - Complete system architecture, data model, API integration, and implementation plan
- **[API Verification Plan](docs/API_VERIFICATION_PLAN.md)** - Detailed API verification scripts and checklist
- **[API Verification Findings](docs/API_VERIFICATION_FINDINGS.md)** - **CRITICAL**: What we know vs. what needs verification
- **[Implementation Summary](docs/IMPLEMENTATION_SUMMARY.md)** - High-level overview and next steps

### Scripts
- **[Comprehensive API Verification](scripts/apex/verify-all-apis.apex)** - Tests ALL APIs needed for the solution
- **[Search Index Creation Verification](scripts/apex/verify-search-index-creation.apex)** - Detailed search index creation tests

## Quick Start

### 1. ⚠️ VERIFY APIs FIRST (Required)

**Before building anything, verify what APIs actually work:**

```apex
// Execute in Anonymous Apex
// File: scripts/apex/verify-all-apis.apex
```

This will test:
- Search Index APIs (create, update, list, get)
- Retriever APIs (if they exist)
- Prompt Template APIs
- File upload APIs
- Prompt invocation APIs

**Document all results in `docs/API_VERIFICATION_FINDINGS.md`**

### 2. Review Known Limitations

**Critical Finding**: Search Index PATCH API does NOT accept `chunkingConfiguration` updates.

This means:
- ❌ Cannot update `maxTokens` or `overlapTokens` on existing indexes
- ✅ Must CREATE new indexes with different parameters
- ⚠️ Must DELETE old indexes (but fails if referenced by retriever)

See `docs/API_VERIFICATION_FINDINGS.md` for details.

### 3. Review Architecture

Read `docs/PROMPT_OPTIMIZATION_ARCHITECTURE.md` for complete system design.

**Note**: Architecture may need adjustment based on API verification results.

## Solution Components

### Data Model
- 7 custom objects for test suites, questions, runs, results, and configurations
- See architecture document for details

### Service Layer
- PDF processing service
- Search index optimization service (must account for CREATE-only limitation)
- Retriever optimization service (TBD based on API verification)
- Prompt template service (TBD based on API verification)
- Test execution service
- Performance evaluation service
- Optimization orchestrator service
- Scorecard generation service

### User Interface
- Test suite manager (LWC)
- Optimization monitor (LWC)
- Scorecard viewer (LWC)
- Configuration editor (LWC)

## Current Status

### ✅ VERIFIED & WORKING
- **Search Index APIs** - ✅ List and Get work with extended timeout (120s)
- **Prompt Templates** - ✅ Accessible via Tooling API (object name: `Prompt`)
- **File Upload** - ✅ ContentVersion insert works

### ✅ ASSET CREATION STATUS
- **Search Index CREATE** - ❌ **BLOCKED** - CREATE rejects `perFileExtension` field
  - **Finding**: CREATE does NOT accept `perFileExtension` (even though GET returns it)
  - **Hypothesis**: CREATE may only accept `fieldLevelConfigurations` (needs testing)
  - **All Options Documented**: See `docs/ALL_SEARCH_INDEX_OPTIONS.md` for complete list
  - **Test Results**: See `docs/SEARCH_INDEX_CREATE_FINDINGS.md` for detailed findings
- **Prompt Template CREATE** - ⚠️ Needs Metadata field (query works, create needs investigation)
- **Retriever CREATE** - ❌ **NO API EXISTS** - Need alternative approach

### ❌ UPDATE STATUS - NO METHODS WORK
- **Search Index Updates** - ❌ **NO UPDATE METHODS WORK**
  - PATCH: ❌ Fails (INVALID_INPUT error)
  - PUT: ❌ Not allowed (405 METHOD_NOT_ALLOWED)
  - POST: ❌ Not allowed (405 METHOD_NOT_ALLOWED)
  - Actions API: ❌ No action exists
  - Tooling API: ❌ Resource doesn't exist
  - **Conclusion**: Cannot update existing indexes at all - must DELETE and CREATE new ones
  - See `docs/ALL_UPDATE_METHODS_TEST_RESULTS.md` for complete test results
- **Prompt Template GET/UPDATE** - Via Tooling API (query confirmed working)
- **Prompt Invocation** - Need to find correct endpoint

### ❌ CONFIRMED MISSING
- **Retriever REST API** - Does NOT exist (tested 12 endpoint variations)
- **Prompt Invocation** - `/actions/standard/aiPrompt` returns 404
- **Data Stream API** - Endpoint not found

### 📋 Key Discoveries
- **Prompt Templates**: Object name is `Prompt` (not `PromptTemplate__c`), accessible via Tooling API
- **Search Indexes**: Work with extended timeout, can see full chunking configurations
- **Retrievers**: No REST API found - may need UI automation or Metadata API

## Next Steps

1. **API Verification** (Priority 1 - DO THIS FIRST)
   - Run `scripts/apex/verify-all-apis.apex`
   - Document all findings in `docs/API_VERIFICATION_FINDINGS.md`
   - Update architecture based on results

2. **Test Search Index Creation** (Priority 1)
   - Create test indexes with different parameters
   - Verify POST payload structure
   - Test parameter combinations

3. **Test Retriever Behavior** (Priority 1)
   - Verify if retrievers are auto-created
   - Find retriever API (if exists)
   - Test retriever update capabilities

4. **Architecture Refinement** (Priority 2)
   - Adjust based on API verification results
   - Document workarounds for missing APIs
   - Finalize technical specifications

5. **Proof of Concept** (Priority 3)
   - Manual end-to-end flow
   - Validate approach
   - Document process

6. **Implementation** (Weeks 4-8)
   - Follow phased approach
   - Build components incrementally

## Critical Questions to Answer

1. **Can we create search indexes programmatically with different chunking parameters?**
   - Test: POST with various `maxTokens` and `overlapTokens` values
   - Document: Exact payload structure that works

2. **Are retrievers automatically created with search indexes?**
   - Test: Create index, check if retriever appears
   - Document: Retriever creation behavior

3. **Can we update retriever parameters programmatically?**
   - Test: Find retriever API, test update operations
   - Document: What parameters can be updated

4. **Can we create/update prompt templates programmatically?**
   - Test: SOQL, Tooling API, Metadata API
   - Document: Which approach works

5. **How do we trigger Data Cloud file ingestion automatically?**
   - Test: Data stream refresh endpoint
   - Document: Ingestion process and timing

## References

- [Data Cloud Connect REST API](https://developer.salesforce.com/docs/data/connectapi/overview)
- [Einstein Prompt Builder Guide](https://developer.salesforce.com/docs/einstein/genai/guide/get-started-prompt-builder.html)
- [Models API Documentation](https://developer.salesforce.com/docs/einstein/genai/references/about/about-genai-api.html)
- [Data Cloud Search Index Reference](https://help.salesforce.com/s/articleView?id=sf.c360_a_search_index_reference.htm)

## Important Notes

- **All APIs need verification before implementation**
- **Architecture may change based on API capabilities**
- **Workarounds may be needed for missing APIs**
- **See `docs/API_VERIFICATION_FINDINGS.md` for current knowledge**
