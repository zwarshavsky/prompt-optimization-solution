# API Index Provisioning Implementation Plan

## Objective
Migrate the core pipeline from UI-driven search index creation to API-driven provisioning with strict readiness and traceability gates before Heroku deployment.

## Requirement vs Suggestion (Execution Semantics)
- **REQUIRED**: Must be implemented and pass. If a required item fails, stop the run and mark as infrastructure failure.
- **SUGGESTED**: Recommended optimization. Agent may proceed without it if all required gates pass.
- **REFERENCE**: Context/example only. Do not treat as implementation work by itself.

When another agent executes this plan:
1. Execute all **REQUIRED** items in order.
2. Use **SUGGESTED** items only after required gates are green.
3. Never skip a required gate to continue the pipeline.

## Execution Steps
1. **REQUIRED** Replace UI-based index creation with API-first provisioning using `POST /services/data/v64.0/ssot/search-index`.
2. **REQUIRED** Use API-only provisioning (no UI fallback). On failure, apply retry logic for transient API errors; if retries are exhausted, fail the cycle/task as infrastructure failure.
3. **REQUIRED** Use the proven payload contract shape that successfully created and validated an index in-org.
4. **REQUIRED** Keep parser configuration in `parsingConfigurations` with `config.id = parse_documents_using_llm`.
5. **REQUIRED** Generate run-specific names (`label`, `developerName`, chunk/vector DMO names) deterministically per cycle.
6. **REQUIRED** Track parser prompt as a versioned artifact per cycle:
   - `parser_prompt_text`
   - `parser_prompt_hash`
   - `parser_prompt_version`
   - source recommendation reference
7. **REQUIRED** Reprovision index only when parser prompt hash changes; otherwise reuse existing index.
8. **REQUIRED** Add payload preflight validation before POST:
   - required fields and shape
   - parser block placement and prompt presence
   - developer name constraints
9. **REQUIRED** Add hard readiness polling gate after create/update:
   - poll `GET /ssot/search-index/{id}` until `runtimeStatus == READY`
   - bounded timeout + backoff
   - fail cycle on timeout/FAILED
10. **REQUIRED** Add post-READY functional smoke check against retrieval path tied to the new index.
11. **REQUIRED** Block full scoring/evaluation until readiness and smoke checks both pass.
12. **REQUIRED** Add retry and diagnostics for transient API failures; classify infra failures separately from model/parser quality failures.
13. **REQUIRED** Persist lifecycle evidence per cycle:
   - created index id + api version
   - create timestamp
   - ready timestamp
   - time-to-ready
   - final runtime status
   - smoke-check result
14. **REQUIRED** Ensure downstream retrieval/testing always uses the just-provisioned READY index id.
15. **REQUIRED** Add pre-Heroku release gates requiring a minimum of one full successful local E2E run:
   - create/update via API
   - readiness polling to READY
   - smoke retrieval passes
   - full Gemini-guided cycle completes

## Rollout Notes
- **SUGGESTED** Start with a controlled rollout via feature flag.
- **SUGGESTED** Run side-by-side validation during initial cycles.
- **REQUIRED** Keep API-only behavior; do not add UI fallback path.

## Working Request Shape References
- **REFERENCE** Primary create endpoint: `POST /services/data/v64.0/ssot/search-index`
- **REFERENCE** Validation endpoint: `GET /services/data/v64.0/ssot/search-index/{id}`
- **REFERENCE** Proven API-created index with LLM parser (`parse_documents_using_llm`) persisted:
  - `18lKc000000oN30IAE` (`CoreLike 0329222116`)
- **REFERENCE** Proven HYBRID + PDF chunking override (`max_tokens=8000`, `overlap_tokens=512`):
  - `18lKc000000oN35IAE` (`Hybrid8K 0330120759`)
- **REFERENCE** Canonical payload pattern to mirror:
  - parser in `parsingConfigurations` (not `preProcessingConfigurations`)
  - chunking under `chunkingConfiguration.fileLevelConfiguration.perFileExtensions`
  - vector settings under `vectorEmbeddingConfiguration`

## Agent Execution Entry Points (Code)
- **REFERENCE** Primary client module: `scripts/python/salesforce_api.py`
- **REFERENCE** Current POST call site: `SearchIndexAPI.create_index(payload)` (entry point, not a guarantee of production-ready behavior)
- **REFERENCE** Current GET call site: `SearchIndexAPI.get_index(index_id)`
- **REFERENCE** Readiness polling helper: `poll_index_until_ready(...)` (uses `runtimeStatus` + `indexRefreshedOn`)
- **REQUIRED** Align runtime API version with proven runs (`v64.0`) unless a newer version is explicitly revalidated.

## How Another Agent Should Use This Plan
1. Read this file first and execute all items labeled **REQUIRED** in order.
2. Use `scripts/python/salesforce_api.py` as the implementation anchor for API calls.
3. Build payload from the canonical structure in this plan, then call `create_index`.
4. Immediately call `get_index` and verify parser/chunking/vector fields match expected values.
5. Run readiness polling until `READY`; fail on timeout/FAILED (no UI fallback).
6. Run post-READY smoke retrieval check before full scoring/evaluation cycle.
7. Persist lifecycle artifacts (index id, timestamps, status, smoke result, parser hash/version).
8. Apply **SUGGESTED** items only after required gates pass.
