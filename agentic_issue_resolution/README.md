# Agentic Issue Resolution (POC)

FastAPI + LangGraph workflow that triages Jira issues, proposes patches, enforces approval gates, and writes final artifacts to Bitbucket, Jira, Confluence, Google Calendar, and Gmail.

## What This Project Does

1. `POST /webhook/jira` starts a workflow for a Jira ticket.
2. Manager agent gathers evidence (Jira, Bitbucket, code search, Tavily) and decides:
   - `ASK_FOR_INFO`
   - `READY_TO_PATCH`
3. If context is incomplete, workflow enters a human loop:
   - posts Jira follow-up comment
   - drafts follow-up email
   - proposes meeting slots
   - drafts Confluence notes
   - communication content is LLM-generated with deterministic fallback
4. Engineer agent generates a scoped patch artifact.
5. Auditor checks schema/scope/risk and pauses at approval gate.
6. `POST /approve` resumes:
   - approved: create PR + Jira update + Confluence draft + calendar slots + Gmail draft (LLM-enhanced comms copy)
   - rejected: add Jira rejection comment + draft human-loop comms artifacts
7. Operate module runs A/B experiments, judging, and live policy selection.

## Runtime Architecture

- Entrypoint/API: `agentic_issue_resolution/app/main.py`
- Workflow graph: `agentic_issue_resolution/graph/workflow.py`
- Policy execution: `agentic_issue_resolution/operate/`
- Integrations: `agentic_issue_resolution/tools/`
- Persistent store: `agentic_issue_resolution/storage/sqlite_store.py`
- UI: `agentic_issue_resolution/ui/`

## Current Model Strategy

- Manager:
  - policy A: Ollama local Qwen SFT (`manager_ollama_qwen25_sft_v1`)
  - policy B: Ollama local Gemma2 (`manager_ollama_gemma2_local_v1`)
- Engineer: Gemini direct API
- Judge: Gemini/OpenRouter/Groq selectable
- Manager runtime supports provider-based policies including local Ollama.

## Environment

Copy `.env.example` to `.env` and fill values.

Primary required groups:

- Provider selectors:
  - `JIRA_PROVIDER`, `BITBUCKET_PROVIDER`, `CONFLUENCE_PROVIDER`, `TAVILY_PROVIDER`, `CALENDAR_PROVIDER`, `EMAIL_PROVIDER`
- Ollama (manager):
  - `OLLAMA_BASE_URL`, `MANAGER_OLLAMA_MODEL`
- Comms LLM (human-loop drafting):
  - `COMMS_LLM_PROVIDER` (`ollama|openrouter|groq|gemini`)
  - `COMMS_OLLAMA_MODEL` (or provider-specific `COMMS_*_MODEL`)
  - `COMMS_LLM_TEMPERATURE`, `COMMS_LLM_MAX_TOKENS`
- OpenRouter:
  - `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `MANAGER_OPENROUTER_MODEL`, `JUDGE_OPENROUTER_MODEL`
- Groq:
  - `GROQ_API_KEY`, `GROQ_BASE_URL`, `MANAGER_GROQ_MODEL`, `JUDGE_GROQ_MODEL`
- Gemini:
  - `GOOGLE_API_KEY` (or `GEMINI_API_KEY`), `GEMINI_API_BASE`, `ENGINEER_GEMINI_MODEL`, `JUDGE_GEMINI_MODEL`
- Jira/Bitbucket/Confluence/Tavily real credentials
- Google:
  - `GOOGLE_CLIENT_SECRET_FILE`, `GOOGLE_TOKEN_FILE`, `GOOGLE_CALENDAR_ID`, `NOTIFY_EMAILS`
- LangSmith:
  - `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`

## Setup

```bash
cd /Users/myth/Documents/VSCode/Codetor
python3 -m venv .venv
source .venv/bin/activate
pip install -r agentic_issue_resolution/requirements.txt
python agentic_issue_resolution/scripts/preflight.py
```

Run backend:

```bash
uvicorn agentic_issue_resolution.app.main:app --reload --port 8000
```

Run UI:

```bash
cd /Users/myth/Documents/VSCode/Codetor/agentic_issue_resolution/ui
npm install
VITE_API_BASE=http://localhost:8000 npm run dev
```

## Key Endpoints

- Workflow:
  - `POST /webhook/jira`
  - `POST /approve`
  - `GET /status/{ticket_id}`
  - `GET /replay/{ticket_id}`
- Monitoring:
  - `GET /integrations/status`
  - `GET /metrics/summary`
  - `GET /metrics/model_ops`
- Operate:
  - `POST /operate/ab_run`
  - `POST /operate/judge`
  - `GET /operate/selector`
  - `GET /operate/live_decisions`

## Scripts

### 1) Generate comprehensive sample tasks

Creates full Cartesian coverage of mode/team/ticket_type/risk.

```bash
python agentic_issue_resolution/scripts/generate_sample_tasks.py \
  --output-path /Users/myth/Documents/VSCode/Codetor/agentic_issue_resolution/samples/tasks.json
```

### 2) Seed real Jira/Confluence/Bitbucket

```bash
python agentic_issue_resolution/scripts/seed_real_systems.py \
  --project-key SCRUM \
  --count 24 \
  --confluence-drafts 6
```

### 3) Run fully connected real demo batch

Seeds, syncs ticket content from task library, runs webhook/approval in batch, and writes a report.

```bash
python agentic_issue_resolution/scripts/run_real_connected_demo.py \
  --base-url http://127.0.0.1:8000 \
  --project-key SCRUM \
  --seed-count 24 \
  --process-count 20 \
  --report-path /Users/myth/Documents/VSCode/Codetor/agentic_issue_resolution/storage/real_connected_report.json
```

### 4) Run simulation batch

```bash
python agentic_issue_resolution/scripts/run_poc_simulation.py \
  --base-url http://127.0.0.1:8000 \
  --tasks-path /Users/myth/Documents/VSCode/Codetor/agentic_issue_resolution/samples/tasks.json \
  --max-tasks 40
```

## Reliability Notes

- Approval resumes are resilient to app restarts: if in-memory graph checkpoints are missing, workflow continues from persisted DB state.
- Google token scopes are unified for Calendar+Gmail so one integration does not break the other.
- OpenRouter defaults are free-model safe in current config.
