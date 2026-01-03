# Prompt Optimization Workflow

Simple Python scripts for testing prompts and analyzing results with Gemini.

## Setup

```bash
# Activate virtual environment
source venv/bin/activate

# Install dependencies (if needed)
pip install -r requirements.txt

# Set Gemini API key (for analyze mode)
export GEMINI_API_KEY='your-api-key-here'
```

## Usage

### Test Questions Through Prompt

Run questions from an Excel sheet through your prompt template:

```bash
python main.py test \
  --excel "prompt-optimization-solution/inputs/IEM POC questions  .xlsx" \
  --sheet "test prompt responses" \
  --source-sheet "V4 - SFR - Prompt V10" \
  --prompt "IEM_Questions_Copy"
```

**What it does:**
- Reads questions from `source-sheet` (column B)
- Gets expected answers from column D
- Runs each question through the prompt (async, fast)
- Saves results to `sheet` with all columns

### Analyze with Gemini

Score responses and get optimization suggestions:

```bash
python main.py analyze \
  --excel "prompt-optimization-solution/inputs/IEM POC questions  .xlsx" \
  --sheet "test prompt responses" \
  --pdf "prompt-optimization-solution/inputs/pdf/PRO-GDL-1002 CDP Design Guide_R3.1.pdf" \
  --instructions "prompt-optimization-solution/inputs/gemini_instructions.txt" \
  --model "gemini-1.5-pro"
```

**What it does:**
- Uploads PDF to Gemini
- Reads prompt instructions from template
- Sends everything to Gemini for analysis
- Fills in: Pass/Fail, Safety Score, Root Cause, Prompt Modifications

## Files

- `main.py` - Main entry point (2 modes: test, analyze)
- `utils.py` - Helper functions (credentials, HTML cleaning, API calls)
- `search_index_api.py` - Search Index API client with all available parameters documented
- `requirements.txt` - Dependencies

## Defaults

- Excel: `prompt-optimization-solution/inputs/IEM POC questions  .xlsx`
- Sheet: `test prompt responses`
- Source Sheet: `V4 - SFR - Prompt V10`
- Prompt: `IEM_Questions_Copy`
- PDF: `prompt-optimization-solution/inputs/pdf/PRO-GDL-1002 CDP Design Guide_R3.1.pdf`
- Model: `gemini-1.5-pro`
