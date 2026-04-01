# Prompt Optimization Solution

Optimizes Salesforce Data Cloud search index parser prompts by running test questions, scoring results, and iterating parser updates.

## What This App Does

- Runs refinement cycles against an existing search index.
- Invokes a Prompt Builder template for each test question.
- Uses Gemini to analyze misses and suggest parser prompt edits.
- Stores run state/results in Postgres for web + worker execution.

## Deployed App

- Web UI: [https://sf-rag-optimizer-e0ec0aab3edd.herokuapp.com/](https://sf-rag-optimizer-e0ec0aab3edd.herokuapp.com/)

## Required Inputs

You need:

- Salesforce credentials (`username`, `password`, `instanceUrl`).
- Existing Data Cloud Search Index ID (`searchIndexId`).
- Prompt Builder template API name (`promptTemplateApiName`).
- Gemini API key (`GOOGLE_API_KEY`).
- YAML config file (use `inputs/prompt_optimization_input.yaml.template` as the base).

Do not commit real credentials. Keep local config files in ignored paths.

## Local Run (Developer)

From repo root:

```bash
cd scripts/python
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
export GOOGLE_API_KEY="your-key"
export DATABASE_URL="postgresql://user:pass@host:5432/db"
streamlit run app.py
```

Worker process (separate terminal):

```bash
cd scripts/python
source venv/bin/activate
export DATABASE_URL="postgresql://user:pass@host:5432/db"
python worker.py
```

Direct CLI run:

```bash
cd scripts/python
source venv/bin/activate
python main.py --yaml-input ../inputs/prompt_optimization_input.yaml
```

## Heroku Notes

- `Procfile` defines `web` and `worker`.
- Set config vars at minimum:
  - `GOOGLE_API_KEY`
  - `DATABASE_URL` (from Heroku Postgres addon)
- Deploy with your normal git push flow, then ensure worker is scaled:

```bash
heroku ps:scale worker=1 --app sf-rag-optimizer
```

## Repo Conventions

- Put one-off utilities in `temp/`.
- Keep `inputs/prompt_optimization_input.yaml` local only.
- Keep `private/` and debug artifacts out of git.

## Quick Troubleshooting

- Job stuck in `running`: check worker logs and heartbeat updates.
- Browser issues on server: verify Playwright Chromium install completed.
- No output workbook: confirm run reached test/analysis step and DB write succeeded.

