# Exhaustive Execution Log - API Search Index Round

## Why this file exists
This is a standalone, non-plan run log that captures everything performed in the most recent implementation round so another engineer/agent can audit, replay, and trust the results without relying on chat history.

## High-level objective
Prove that Data Cloud Search Index creation can be done API-first (without UI), including LLM parser configuration, and preserve a repeatable working implementation artifact.

## Org and auth context
- Instance URL: `https://jamespark-250401-251-demo.my.salesforce.com`
- Auth mechanism used during execution:
  - SOAP login endpoint: `/services/Soap/u/60.0`
  - Session ID reused as Bearer token for REST calls.
- Search Index REST endpoint used for creation:
  - `POST /services/data/v64.0/ssot/search-index`
- Validation endpoint used after create:
  - `GET /services/data/v64.0/ssot/search-index/{id}`

## Core payload contract proven in this round
The successful payloads followed this contract:
- Required identity and DMO fields:
  - `label`
  - `developerName`
  - `sourceDmoDeveloperName`
  - `chunkDmoName`
  - `chunkDmoDeveloperName`
  - `vectorDmoName`
  - `vectorDmoDeveloperName`
- Parser placement (critical):
  - `parsingConfigurations` contains parser block
  - parser ID persisted as `parse_documents_using_llm`
  - parser prompt persisted in parser `userValues` as `prompt`
- Chunking structure for create:
  - `chunkingConfiguration.fileLevelConfiguration.perFileExtensions[]`
  - PDF entry includes `max_tokens` and `overlap_tokens`
- Vector configuration:
  - `vectorEmbeddingConfiguration` persisted with proven model/index/similarity
- Search mode:
  - `searchType` set explicitly to `VECTOR` or `HYBRID`

## Critical contract note discovered
- LLM parser config that worked in successful API-created indexes was persisted under `parsingConfigurations` with parser ID `parse_documents_using_llm`.
- For this validated flow, we treated `preProcessingConfigurations` parser placement as non-canonical and did not use it for working creates.

## Working reference indexes created and validated
1) API-created baseline with LLM parser persisted:
- Index ID: `18lKc000000oN30IAE`
- Label: `CoreLike 0329222116`
- DeveloperName: `CLK0329222116`
- Confirmed by GET:
  - parser ID present: `parse_documents_using_llm`
  - parser prompt persisted
  - searchType persisted

2) API-created HYBRID variant with PDF overrides:
- Index ID: `18lKc000000oN35IAE`
- Label: `Hybrid8K 0330120759`
- DeveloperName: `H8K0330120759`
- Requested/verified:
  - `searchType = HYBRID`
  - PDF `max_tokens = 8000`
  - PDF `overlap_tokens = 512`
- Runtime status check progression:
  - initially `IN_PROGRESS`
  - later `READY`

3) Additional creation using reusable script (validation run):
- Index ID: `18lKc000000oN3AIAU`
- Label: `APIWorking 0330195530`
- DeveloperName: `AW0330195530`
- Output checks passed:
  - parser ID matches
  - searchType matches
  - PDF tokens/overlap match

4) Additional creation requested immediately afterward:
- Index ID: `18lKc000000oN3UIAU`
- Label: `APIWorking 0330233438`
- DeveloperName: `AW0330233438`
- Fast validation checks:
  - created ID present: true
  - searchType matches requested HYBRID: true
  - parser ID matches `parse_documents_using_llm`: true
  - PDF max tokens (8000) matches: true
  - PDF overlap tokens (512) matches: true

## Runtime status and statistics findings
- `GET /ssot/search-index/{id}` exposes:
  - runtime and configuration metadata (`runtimeStatus`, `indexRefreshedOn`, parser/chunk/vector config)
- It does NOT directly expose:
  - chunk counts
  - vector counts
  - index size/volume metrics
- Probed likely stats subpaths and received `404 NOT_FOUND`:
  - `/stats`
  - `/metrics`
  - `/status`
  - `/summary`

## Files created/updated in this round
1) New working script (replayable implementation artifact):
- `private/implementation_plan/create_index_v64_working.py`
- Purpose:
  - SOAP auth
  - source GET
  - build create-safe payload
  - POST create
  - verify GET
  - optional READY poll

2) Plan updated for execution semantics and references:
- `private/implementation_plan/IMPLEMENTATION_PLAN_20260330_094433.md`
- Changes included:
  - REQUIRED vs SUGGESTED vs REFERENCE semantics
  - API-only failure policy (no UI fallback)
  - working reference IDs/endpoints
  - execution entry points and guidance

3) Directory reorganization completed:
- Created `private/implementation_plan` as single, non-nested plan directory
- Moved:
  - `temp` -> `private/temp`
  - old worker plan -> `private/implementation_plan/archive/WORKER_IMPLEMENTATION_PLAN.md`
  - prior archive folder -> `private/implementation_plan/archive/archive_20260202_111351`

## Exact behavior added to working script
The script now includes:
- Detailed in-file operator guidance:
  - fast vs ready validation tiers
  - expected timing profile
  - failure triage
- Validation modes:
  - `--validation-mode fast`
  - `--validation-mode ready` (same effect as enabling wait for READY)
- Structured validation output:
  - `fastValidationChecks`
  - `fastValidationPass`
  - optional `readyValidationPass`
  - `timingMs`
  - `operatorGuidance`

## Most recent fast run output summary (script)
From the run that created `18lKc000000oN3UIAU`:
- `fastValidationPass = true`
- Timing:
  - SOAP login: 313 ms
  - source GET: 703 ms
  - payload build: 0 ms
  - POST create: 30073 ms
  - verify GET: 703 ms
  - total: 31793 ms

## Operational guidance (what this proves vs what remains)
Proven in this round:
- API-first create works in this org.
- LLM parser config persists on created index.
- HYBRID + PDF 8k/512 overrides persist.
- READY status can be observed via polling.

Still required in production pipeline integration:
- Use the same payload contract in core pipeline code path.
- Enforce hard READY gate before evaluation.
- Add post-READY smoke retrieval check.
- Persist per-cycle artifact trail (parser hash/version/index ID/timestamps/status).

## Replay command (known-good script)
```bash
python3 "prompt-optimization-solution/private/implementation_plan/create_index_v64_working.py" \
  --username "<sf_username>" \
  --password "<sf_password>" \
  --instance-url "https://jamespark-250401-251-demo.my.salesforce.com" \
  --search-type HYBRID \
  --pdf-max-tokens 8000 \
  --pdf-overlap-tokens 512 \
  --validation-mode fast
```

To include readiness gate:
```bash
python3 "prompt-optimization-solution/private/implementation_plan/create_index_v64_working.py" \
  --username "<sf_username>" \
  --password "<sf_password>" \
  --instance-url "https://jamespark-250401-251-demo.my.salesforce.com" \
  --search-type HYBRID \
  --pdf-max-tokens 8000 \
  --pdf-overlap-tokens 512 \
  --validation-mode ready
```

## Final status at end of this round
- Working, replayable create script exists and was executed successfully.
- Multiple indexes were created via POST and verified by GET.
- One HYBRID index was confirmed to reach READY.
- Exhaustive execution context is now captured in this separate run log file.
