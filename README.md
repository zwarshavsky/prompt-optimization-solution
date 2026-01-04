# Prompt Optimization Solution

An automated RAG (Retrieval-Augmented Generation) optimization system that iteratively improves LLM parser prompts for Salesforce Data Cloud Search Indexes using AI-driven analysis and testing.

## Overview

This solution automates the optimization of LLM parser prompts for Data Cloud Search Indexes through an iterative refinement process:

1. **Updates** an existing Search Index with an improved LLM parser prompt
2. **Tests** the updated index by invoking prompts against test questions
3. **Analyzes** results using Google Gemini AI to identify improvements
4. **Iterates** until the prompt is optimized or maximum cycles are reached

### Current Functionality

- ✅ **LLM Parser Prompt Optimization**: Updates existing Search Index LLM parser prompts via UI automation
- ✅ **Automated Testing**: Invokes prompts against test questions and captures responses
- ✅ **AI-Powered Analysis**: Uses Gemini AI to analyze results and propose prompt improvements
- ✅ **Iterative Refinement**: Automatically cycles through improvements until optimization criteria are met
- ✅ **Persistent Job Management**: Worker dyno architecture with graceful shutdown and resume capabilities
- ✅ **Real-time Monitoring**: Streamlit web interface for job creation and monitoring

### Planned Enhancements

- 🔄 **Prompt Builder Optimization**: Optimize prompts from Prompt Builder (coming soon)
- 🔄 **Agentforce Agent Optimization**: Optimize Agentforce agent configurations (coming soon)
- 🔄 **New Search Index Creation**: Create new search indexes with optimized configurations (future)
- 🔄 **Additional Search Index Components**: Optimize other search index components beyond LLM parser (future)

## Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                         Streamlit Web App                        │
│  (app.py) - Job creation, monitoring, Excel file management     │
└────────────────────────────┬────────────────────────────────────┘
                              │
                              │ Queues jobs (status: 'queued')
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Worker Dyno Process                         │
│  (worker.py) - Polls database, executes jobs, handles shutdown   │
└────────────────────────────┬────────────────────────────────────┘
                              │
                              │ Executes workflow
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Core Workflow Engine                          │
│  (main.py) - Orchestrates iterative refinement cycles           │
└──────┬──────────────────────┬──────────────────────┬───────────┘
       │                      │                      │
       ▼                      ▼                      ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Step 1:     │    │  Step 2:     │    │  Step 3:     │
│  Update      │───▶│  Test Index  │───▶│  Analyze     │
│  Index       │    │  & Invoke    │    │  with Gemini │
│              │    │  Prompts     │    │              │
└──────┬───────┘    └──────┬───────┘    └──────┬───────┘
       │                  │                     │
       │                  │                     │
       ▼                  ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Supporting Services                           │
│  - playwright_scripts.py: UI automation for index updates       │
│  - salesforce_api.py: REST API calls (prompt invocation, etc.)   │
│  - gemini_client.py: Gemini AI integration                       │
│  - excel_io.py: Excel file creation and management              │
│  - worker_utils.py: Database operations for worker               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Heroku Postgres Database                      │
│  - Job status, progress, checkpoints                             │
│  - Excel file storage (BYTEA)                                    │
│  - Heartbeat tracking for dead job detection                    │
└─────────────────────────────────────────────────────────────────┘
```

### File Structure

```
prompt-optimization-solution/
├── README.md                          # This file
├── Procfile                           # Heroku process definitions (web + worker)
├── requirements.txt                   # Python dependencies
├── inputs/
│   ├── prompt_optimization_input.yaml # Main configuration file
│   ├── pdf/                           # PDF files for Gemini context
│   └── csv/                           # Historical test results
├── scripts/python/
│   ├── app.py                         # Streamlit web application
│   ├── main.py                        # Core workflow orchestration
│   ├── worker.py                      # Worker dyno process (job execution)
│   ├── worker_utils.py               # Database utilities for worker
│   ├── salesforce_api.py              # Salesforce REST API client
│   ├── playwright_scripts.py          # UI automation (Playwright)
│   ├── gemini_client.py              # Google Gemini AI client
│   ├── excel_io.py                   # Excel file operations
│   └── gemini_config.py              # Gemini configuration
└── live_outputs/                      # Generated Excel files (local)
```

### Component Relationships

**Web Application (`app.py`)**
- Creates jobs with status `'queued'`
- Monitors job progress via database
- Displays Excel files from database
- Detects and marks dead jobs

**Worker Process (`worker.py`)**
- Polls database for `'queued'` and `'interrupted'` jobs
- Executes jobs via `main.py`
- Handles graceful shutdown (SIGTERM)
- Saves checkpoints and marks jobs as `'interrupted'` on shutdown
- Automatically resumes interrupted jobs on restart

**Workflow Engine (`main.py`)**
- Orchestrates 3-step iterative cycles:
  1. **Step 1**: Update Search Index LLM parser prompt (via Playwright)
  2. **Step 2**: Test index by invoking prompts against questions
  3. **Step 3**: Analyze results with Gemini AI
- Saves state files for resume capability
- Updates heartbeat on progress callbacks
- Saves Excel files to database after Step 2 and Step 3

**Supporting Services**
- `playwright_scripts.py`: Browser automation for updating Search Index prompts
- `salesforce_api.py`: REST API calls for prompt invocation and metadata retrieval
- `gemini_client.py`: Google Gemini AI integration for analysis
- `excel_io.py`: Excel file creation, updates, and database persistence
- `worker_utils.py`: Database operations (job status, heartbeat, checkpoints)

## APIs Utilized

### Salesforce APIs

1. **SOAP API** (`/services/Soap/u/58.0`)
   - Authentication (username/password login)
   - Returns session token for REST API calls

2. **REST API - Prompt Invocation** (`/services/data/v65.0/actions/custom/generatePromptResponse/{promptName}`)
   - Invokes Prompt Builder templates with questions
   - Returns AI-generated responses
   - Supports model fallback and retry logic

3. **REST API - Metadata Retrieval** (`/services/data/v65.0/tooling/query/`)
   - Queries Prompt Builder template metadata
   - Retrieves prompt configuration details

4. **Data Cloud Connect REST API** (`/services/data/v65.0/ssot/search-index`)
   - Lists and retrieves Search Index configurations
   - Monitors index status (SUBMITTED → READY)
   - **Note**: Search Index updates are performed via UI automation (Playwright) due to API limitations

### Google Gemini API

- **Models API** (via `google-generativeai` package)
  - Text generation for prompt analysis
  - PDF context upload for document-aware analysis
  - Model: `gemini-2.5-pro` (configurable in YAML)

### Heroku Postgres

- **PostgreSQL Database**
  - Job persistence (`runs` table)
  - Excel file storage (BYTEA)
  - Checkpoint and heartbeat tracking

## Workflow

### Iterative Refinement Cycle

Each optimization run consists of multiple **refinement cycles**. Each cycle has 3 steps:

#### Cycle 1 (Baseline Test)
1. **Step 1**: SKIPPED (no previous cycle to improve upon)
2. **Step 2**: Test current/baseline index
   - Invoke prompts against all test questions
   - Capture responses in Excel file
3. **Step 3**: Analyze results with Gemini
   - Compare responses to expected answers
   - Identify failures and root causes
   - Propose improved LLM parser prompt

#### Cycle 2+ (Refinement)
1. **Step 1**: Update Search Index
   - Apply previous cycle's proposed prompt
   - Wait for index rebuild (can take up to 1 hour)
   - Verify index is READY before proceeding
2. **Step 2**: Test updated index
   - Invoke prompts against all test questions
   - Capture responses in Excel file
3. **Step 3**: Analyze results with Gemini
   - Compare to previous cycles
   - Identify improvements or regressions
   - Propose further prompt improvements (if needed)

#### Completion Criteria

The workflow stops when:
- Gemini analysis returns `stage_status: "optimized"` (prompt is good enough)
- Maximum cycles reached (default: 10, safety limit)
- Critical error occurs (workflow stops immediately)

### Job Lifecycle

```
User Creates Job
    ↓
Status: 'queued'
    ↓
Worker Picks Up Job
    ↓
Status: 'running' (with heartbeat updates)
    ↓
[If dyno restarts during execution]
    ↓
Status: 'interrupted' (with checkpoint saved)
    ↓
Worker Restarts → Detects Interrupted Job
    ↓
Resumes from Checkpoint
    ↓
Status: 'running' (continues)
    ↓
Workflow Completes
    ↓
Status: 'completed' or 'failed'
```

## Prerequisites

### Required in Salesforce Org

1. **Search Index** (must exist)
   - A Data Cloud Search Index with an LLM parser prompt
   - Search Index ID (18-character Salesforce record ID)
   - The solution will **update** this index's LLM parser prompt

2. **Prompt Builder Template** (must exist)
   - A Prompt Builder template configured to use the Search Index
   - Prompt Template API Name (DeveloperName, e.g., `Test_RAG_Optimization_SFR_v1`)
   - Must be configured to invoke the Search Index for retrieval

3. **Salesforce Credentials**
   - Username and password for authentication
   - Instance URL (e.g., `https://yourinstance.my.salesforce.com`)

### Required Locally

1. **Python 3.8+**
2. **Virtual Environment** (recommended)
3. **Google Gemini API Key** (for analysis)
4. **Heroku Postgres** (for deployment) or local PostgreSQL (for local testing)

## Configuration: YAML Input File

The main configuration file is `inputs/prompt_optimization_input.yaml`. Key sections:

### Salesforce Configuration

```yaml
configuration:
  salesforce:
    username: "your-username@salesforce.com"
    password: "your-password"
    instanceUrl: "https://yourinstance.my.salesforce.com"
  
  # Search Index ID (18-character Salesforce record ID)
  searchIndexId: "18lHu000000CgpDIAS"
  
  # Prompt Template API Name (DeveloperName, not display name)
  promptTemplateApiName: "Test_RAG_Optimization_SFR_v1"
  
  # Refinement stage (currently only "llm_parser" supported)
  refinementStage: "llm_parser"
```

### Gemini Configuration

```yaml
configuration:
  # Gemini model for analysis
  geminiModel: "gemini-2.5-pro"  # Recommended: gemini-2.5-pro or gemini-2.5-flash
  
  # PDF directory for context (optional)
  pdfDirectory: "prompt-optimization-solution/inputs/pdf"
```

### Playwright Configuration

```yaml
configuration:
  # Browser automation settings
  headless: false        # true for servers, false for local debugging
  slowMo: 0             # Delay between actions (ms), 500 for debugging
  takeScreenshots: false # Enable screenshot capture
```

### Test Questions

```yaml
questions:
  - question: "What is the maximum capacity of the distribution section?"
    expectedAnswer: "The distribution section has a maximum capacity of 400A."
    category: "Technical Specifications"
  # ... more questions
```

### Refinement Stage Configuration

```yaml
configuration:
  refinementStages:
    llm_parser:
      description: "LLM Parser Prompt Optimization"
      focus: |
        Instructions for Gemini on what to optimize
      rootCauseGuidance: |
        How to analyze failures
      modificationGuidance: |
        How to improve the prompt
```

**See `inputs/prompt_optimization_input.yaml` for complete configuration options and examples.**

## Local Development Setup

### 1. Clone Repository

```bash
cd "/Users/zwarshavsky/Documents/Custom_LWC_Org_SDO/Custom LWC Development SDO/prompt-optimization-solution"
```

### 2. Set Up Python Virtual Environment

```bash
cd scripts/python
python3 -m venv venv
source venv/bin/activate  # On macOS/Linux
# OR
venv\Scripts\activate  # On Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

**Note**: This installs:
- `streamlit` - Web framework
- `playwright` - Browser automation
- `google-generativeai` - Gemini AI client
- `pandas`, `openpyxl` - Excel file handling
- `psycopg2-binary` - PostgreSQL adapter
- `requests`, `PyYAML` - HTTP and YAML parsing

### 4. Install Playwright Browsers

```bash
playwright install chromium
```

### 5. Configure Environment

**Option A: Use Heroku Postgres (Recommended for Testing)**

```bash
# Get DATABASE_URL from Heroku
heroku config:get DATABASE_URL --app sf-rag-optimizer

# Set locally
export DATABASE_URL="postgresql://..."
```

**Option B: Use Local PostgreSQL**

```bash
# Create local database and set DATABASE_URL
export DATABASE_URL="postgresql://user:password@localhost:5432/dbname"
```

### 6. Configure Gemini API Key

Set environment variable or configure in `gemini_config.py`:

```bash
export GOOGLE_API_KEY="your-api-key"
```

### 7. Update YAML Configuration

Edit `inputs/prompt_optimization_input.yaml`:
- Set Salesforce credentials
- Set Search Index ID
- Set Prompt Template API Name
- Configure test questions
- Set Gemini model

### 8. Run Locally

**Option A: Run Web Application**

```bash
cd scripts/python
source venv/bin/activate
streamlit run app.py
```

Access at `http://localhost:8501`

**Option B: Run Worker Process**

```bash
cd scripts/python
source venv/bin/activate
export DATABASE_URL="your-database-url"
python worker.py
```

**Option C: Run Workflow Directly (CLI)**

```bash
cd scripts/python
source venv/bin/activate
python main.py --yaml-input ../inputs/prompt_optimization_input.yaml
```

## Deployment to Heroku

### 1. Prerequisites

- Heroku account
- Heroku CLI installed
- Git repository initialized

### 2. Create Heroku App

```bash
heroku create your-app-name
```

### 3. Add Heroku Postgres

```bash
heroku addons:create heroku-postgresql:essential-0
```

### 4. Set Environment Variables

```bash
heroku config:set GOOGLE_API_KEY="your-gemini-api-key"
```

### 5. Deploy

```bash
git push heroku main
```

### 6. Scale Worker Dyno

```bash
heroku ps:scale worker=1
```

### 7. Monitor

```bash
# View web logs
heroku logs --tail --dyno web

# View worker logs
heroku logs --tail --dyno worker

# Check dyno status
heroku ps
```

## How It Works

### Step 1: Update Search Index (Cycle 2+)

- Uses **Playwright** to automate browser interaction
- Navigates to Search Index record page
- Updates LLM parser prompt textarea
- Submits changes and waits for index rebuild
- Polls API until index status is `READY` (can take up to 1 hour)

**Why Playwright?** The Search Index LLM parser prompt cannot be updated via REST API (PATCH/PUT methods don't work). UI automation is the only reliable method.

### Step 2: Test Index & Invoke Prompts

- Reads test questions from YAML configuration
- For each question:
  - Invokes Prompt Builder template via REST API
  - Captures AI-generated response
  - Stores question, response, and expected answer in Excel
- Saves Excel file to database immediately after creation

### Step 3: Analyze Results with Gemini

- Uploads Excel file and PDF context to Gemini
- Asks Gemini to:
  - Compare responses to expected answers
  - Identify failures and root causes
  - Propose improved LLM parser prompt
- Updates Excel with analysis results
- Determines if prompt is `optimized` or `needs_improvement`

### Iteration Logic

- If `needs_improvement`: Start next cycle with proposed prompt
- If `optimized`: Stop workflow, mark as completed
- If error: Stop workflow, mark as failed

## Database Schema

The `runs` table stores:

- `run_id`: Unique job identifier
- `status`: `queued`, `running`, `interrupted`, `completed`, `failed`
- `config`: YAML configuration (JSONB)
- `progress`: Current cycle, step, status (JSONB)
- `output_lines`: Live output log (JSONB array)
- `results`: Final results (JSONB)
- `excel_file_path`: Path to Excel file
- `excel_file_content`: Excel file binary data (BYTEA)
- `heartbeat_at`: Last activity timestamp
- `checkpoint_info`: Resume checkpoint (cycle, step) (JSONB)
- `started_at`, `completed_at`: Timestamps
- `error`, `error_details`: Error information

## Worker Dyno Architecture

### Why Worker Dynos?

Heroku dynos restart at least once every 24 hours. Long-running jobs (hours) need:
- **Separation**: Web UI separate from job execution
- **Resilience**: Jobs survive web dyno restarts
- **Graceful Shutdown**: Save state before restart
- **Automatic Resume**: Continue from checkpoint after restart

### How It Works

1. **Job Creation**: Web app creates job with status `'queued'`
2. **Worker Polling**: Worker polls database every 5 seconds
3. **Job Execution**: Worker picks up job, marks as `'running'`, executes workflow
4. **Heartbeat Updates**: Workflow updates heartbeat every 30 seconds
5. **Graceful Shutdown**: On SIGTERM, worker saves checkpoint and marks as `'interrupted'`
6. **Automatic Resume**: On restart, worker detects `'interrupted'` jobs and resumes from checkpoint

### Dead Job Detection

- Jobs with status `'running'` and no heartbeat for > 2 minutes are marked as `'failed'`
- **Note**: This threshold may need adjustment for very long operations (e.g., Step 1 index rebuild)

## Troubleshooting

### Job Stuck in "Running" Status

- Check worker logs: `heroku logs --tail --dyno worker`
- Verify worker dyno is running: `heroku ps`
- Check database heartbeat: Query `runs` table for `heartbeat_at`

### Excel File Not Showing

- Excel files are saved to database after Step 2 and Step 3
- Check database: `excel_file_content` column should have data
- Verify job completed Step 2 or Step 3

### Resume Not Working

- Resume requires state files (stored in `app_data/state/`)
- State files are on ephemeral filesystem (lost on dyno restart)
- **Future**: Store state files in database for true resume capability

### Playwright Browser Not Found

- Ensure `.profile` script installs browsers on startup
- Check Heroku logs for browser installation messages
- Verify `playwright install chromium` runs successfully

## Future Enhancements

### Immediate Roadmap

1. **Prompt Builder Optimization**
   - Optimize prompts from Prompt Builder (not just LLM parser)
   - Similar iterative refinement process

2. **Agentforce Agent Optimization**
   - Optimize Agentforce agent configurations
   - Test agent behavior and refine

### Future Enhancements

1. **New Search Index Creation**
   - Create new search indexes with optimized configurations
   - Test different chunking parameters

2. **Additional Search Index Components**
   - Optimize other search index settings beyond LLM parser
   - Test different field configurations

3. **State File Persistence**
   - Store state files in database for true resume capability
   - Enable resume even after dyno restarts

4. **One-Off Dyno Architecture**
   - Use one-off dynos instead of persistent worker
   - Each job gets its own dyno (no forced restarts)

## References

- [Salesforce Data Cloud Connect REST API](https://developer.salesforce.com/docs/data/connectapi/overview)
- [Einstein Prompt Builder Guide](https://developer.salesforce.com/docs/einstein/genai/guide/get-started-prompt-builder.html)
- [Google Gemini API](https://ai.google.dev/docs)
- [Heroku Postgres](https://devcenter.heroku.com/articles/heroku-postgresql)
- [Playwright Documentation](https://playwright.dev/python/)

## License

[Add your license information here]
