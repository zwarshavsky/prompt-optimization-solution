# Search Index File Extension Control - API Limitation

**Status**: ⚠️ **UNDOCUMENTED LIMITATION** - Discovered through API testing  
**Date Discovered**: Through comprehensive API testing  
**Official Documentation**: None found in Salesforce Data Cloud Connect REST API documentation

## Problem

The Salesforce Data Cloud Search Index API has an **undocumented limitation** where:
- **CREATE API** rejects `perFileExtension` field (returns `JSON_PARSER_ERROR: Unrecognized field "perFileExtension"`)
- **PATCH API** also rejects `perFileExtension` field (same error)
- **UI** requires `perFileExtension` to display file extension chunking configurations
- **System** does NOT automatically populate `perFileExtension` from `fieldLevelConfigurations` or `parsingConfigurations`

## What Works

✅ **CREATE with `fieldLevelConfigurations`** - This works and creates valid chunking configuration
✅ **Chunking functionality** - The actual chunking works correctly with `fieldLevelConfigurations`
❌ **UI Display** - The UI shows blank because it reads from `perFileExtension` which is empty

## Tested Approaches

### 1. Direct CREATE with `perFileExtension`
- **Result**: ❌ Rejected by API
- **Error**: `JSON_PARSER_ERROR: Unrecognized field "perFileExtension"`

### 2. CREATE with `fieldLevelConfigurations`, then PATCH with `perFileExtension`
- **Result**: ❌ PATCH also rejects `perFileExtension`
- **Error**: Same `JSON_PARSER_ERROR`

### 3. CREATE with matching `parsingConfigurations` fileExtensions
- **Result**: ❌ System does not auto-populate `perFileExtension`
- **Status**: `perFileExtension` remains empty array `[]`

### 4. CREATE with `fieldLevelConfigurations` only
- **Result**: ✅ Creates successfully, chunking works
- **UI**: Shows blank because it reads from empty `perFileExtension`

## Current Workaround

Since the API does not support `perFileExtension` programmatically:

1. **Create index via API** with `fieldLevelConfigurations` (chunking will work)
2. **Manually edit once in UI** to set file extensions (this populates `perFileExtension`)
3. **Future updates** can be done via API on `fieldLevelConfigurations` (but UI will still show what's in `perFileExtension`)

## Alternative: Hybrid Approach

For automation scenarios:
1. Create index programmatically with `fieldLevelConfigurations`
2. Use UI automation (Selenium/Playwright) to set file extensions once
3. Or accept that UI shows blank but functionality works via `fieldLevelConfigurations`

## API Structure Comparison

### UI-Created Index (Source)
```json
{
  "chunkingConfiguration": {
    "perFileExtension": [
      {
        "fileExtension": "pdf",
        "config": {
          "id": "passage_extraction",
          "userValues": [
            {"id": "strip_html", "value": "true"},
            {"id": "max_tokens", "value": "8192"}
          ]
        },
        "decorators": []
      }
    ],
    "fieldLevelConfigurations": []
  }
}
```

### API-Created Index
```json
{
  "chunkingConfiguration": {
    "perFileExtension": [],
    "fieldLevelConfigurations": [
      {
        "sourceDmoDeveloperName": "RagFileUDMO__dlm",
        "sourceDmoFieldDeveloperName": "ResolvedFilePath__c",
        "config": {
          "id": "passage_extraction",
          "userValues": [
            {"id": "strip_html", "value": "true"},
            {"id": "max_tokens", "value": "8192"}
          ]
        },
        "decorators": []
      }
    ]
  }
}
```

## Conclusion

**The API does not provide programmatic control over `perFileExtension`**, which is required for UI display. The chunking functionality works correctly with `fieldLevelConfigurations`, but the UI will show blank until `perFileExtension` is populated (which can only be done via UI or may require Salesforce to fix the API).

## Recommendation

1. **For functionality**: Use `fieldLevelConfigurations` - it works perfectly
2. **For UI display**: Accept limitation or use UI automation for one-time setup
3. **For Salesforce**: 
   - Request API support for `perFileExtension` in CREATE/PATCH operations
   - Request documentation of this limitation
   - Request `fileExtension` field support in `fieldLevelConfigurations` for per-file-type chunking

## Additional Finding: Per-File-Type Chunking

**Question**: Can you control chunking parameters per file type (pdf, html, txt) via API?

**Answer**: ❌ **NO** - This is another undocumented limitation:
- `fieldLevelConfigurations` does NOT have a `fileExtension` or `fileExtensions` field
- You can create multiple `fieldLevelConfigurations` with different strategies/parameters
- But ALL configs apply to ALL file types - you cannot specify which file extension each config targets
- This means you cannot replicate the UI's "per file type" chunking configuration via API

**Impact**: For optimization scenarios requiring different chunking strategies per file type (e.g., PDF vs HTML), you must either:
1. Use UI-created indexes (which support `perFileExtension`)
2. Accept that all file types use the same chunking configuration
3. Create separate indexes per file type (workaround)

