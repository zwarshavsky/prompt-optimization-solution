# Undocumented API Limitations - Data Cloud Search Index

**Status**: ⚠️ **UNDOCUMENTED** - Discovered through comprehensive API testing  
**Last Updated**: Based on testing results  
**Official Documentation**: None found in Salesforce Data Cloud Connect REST API documentation

## Summary

These limitations were discovered through actual API testing and are **NOT documented** in official Salesforce documentation. They represent gaps between what the API returns (GET) and what it accepts (POST/PATCH).

## 1. Per-File-Extension Chunking Configuration

### Limitation
- **CREATE API**: Rejects `perFileExtension` field (`JSON_PARSER_ERROR: Unrecognized field "perFileExtension"`)
- **PATCH API**: Also rejects `perFileExtension` field (same error)
- **GET API**: Returns `perFileExtension` in response
- **UI**: Requires `perFileExtension` to display file extension configurations

### Impact
- Cannot programmatically set chunking configuration per file type (pdf, html, txt, etc.)
- UI shows blank "Select Files to Chunk" section for API-created indexes
- Chunking functionality works via `fieldLevelConfigurations`, but UI cannot display it

### Workaround
1. Create index via API with `fieldLevelConfigurations` (functionality works)
2. Manually edit once in UI to populate `perFileExtension` (for UI display only)

## 2. Per-File-Type Chunking in fieldLevelConfigurations

### Limitation
- `fieldLevelConfigurations` does NOT have `fileExtension` or `fileExtensions` field
- Cannot specify which file type (pdf, html, txt) each chunking config applies to
- Multiple `fieldLevelConfigurations` can be created with different strategies/parameters
- But ALL configs apply to ALL file types - no way to target specific extensions

### Impact
- Cannot replicate UI's "per file type" chunking configuration via API
- All file types in the source field use the same chunking configuration
- For optimization requiring different strategies per file type, must use UI-created indexes

### Workaround
1. Use UI-created indexes (which support `perFileExtension`)
2. Accept that all file types use the same chunking configuration
3. Create separate indexes per file type (if acceptable for use case)

## 3. Search Index Update (PATCH)

### Limitation
- **PATCH API**: Does not work for any fields (`label`, `description`, `chunkingConfiguration`, `vectorEmbeddingConfiguration`)
- **Error**: `INVALID_INPUT - __MISSING LABEL__ PropertyFile - val NoSemanticSearchConfigProvided not found`
- **All Update Methods Tested**: PATCH, PUT, POST, Actions API, Tooling API, Composite API, Bulk API, GraphQL, Metadata API - all failed

### Impact
- Cannot update search indexes programmatically
- Must DELETE and CREATE new index for any changes
- Cannot update chunking parameters without recreating entire index

### Workaround
1. DELETE existing index (if not referenced by retriever)
2. CREATE new index with updated parameters

## Documentation Status

| Limitation | Official Documentation | Status |
|------------|----------------------|--------|
| `perFileExtension` rejected in CREATE/PATCH | ❌ None found | Undocumented |
| `fieldLevelConfigurations` lacks `fileExtension` field | ❌ None found | Undocumented |
| PATCH does not work for any fields | ❌ None found | Undocumented |
| Cannot control chunking per file type via API | ❌ None found | Undocumented |

## Recommendations

1. **For Salesforce**: 
   - Document these limitations in Data Cloud Connect REST API documentation
   - Add `fileExtension` field support to `fieldLevelConfigurations`
   - Fix PATCH API to support updates
   - Add `perFileExtension` support to CREATE/PATCH operations

2. **For Development**:
   - Use UI-created indexes when per-file-type chunking is required
   - Use API-created indexes with `fieldLevelConfigurations` when single config is acceptable
   - Plan for DELETE/CREATE workflow instead of UPDATE
   - Consider UI automation for one-time `perFileExtension` setup

## Testing Evidence

All limitations documented here were discovered through:
- Comprehensive API testing with actual HTTP requests
- Analysis of GET response structures vs POST/PATCH payload requirements
- Testing of all possible update methods (REST, Tooling, Metadata, etc.)
- Comparison of UI-created vs API-created index structures

See detailed test results in:
- `SEARCH_INDEX_FILE_EXTENSION_CONTROL.md`
- `PATCH_TEST_RESULTS.md`
- `ALL_UPDATE_METHODS_TEST_RESULTS.md`
- `COMPREHENSIVE_UPDATE_METHODS_TEST.md`



