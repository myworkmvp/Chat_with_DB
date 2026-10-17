# Database-driven agentic chatbot (local Ollama)

Flow: intent router -> find intent -> similarity search (NULL-column) -> if nothing matches or the question is unclear,
ASK THE USER, then continue -> schema fetch -> plan (precise question, steps, actions) -> SQL from the plan -> SELECT-only validation
-> read-only execution (1 retry) -> chart if planned -> natural-tone explanation.
CONFIRM_BAND (default 0.05): matches within this margin above SIM_THRESHOLD are confirmed with the user first.
The React and Streamlit UIs both show every step live.

## Quick start (Docker, demo data)
    ollama pull qwen2.5-coder:7b && ollama pull llama3.1:8b && ollama pull nomic-embed-text

## Manual start (your own database)
    cd backend && pip install -r requirements.txt           
    uvicorn main:app --port 8000
    python mcp_server.py             # Select the Database MCP (mcp_server_mariadb or mcp_server_postgres)

    cd frontend && npm i && npm run dev        

## Logs (full, structured JSON)
Every execution gets a `run_id` (shown in the UI). Files in `backend/logs/` (`./logs` with Docker), rotated at 20 MB x 5:
- `runs.jsonl`: one record per execution: `request`, `config` (models, thresholds), `outcome`/`reason`, `response`
  (answer, sql, columns, row_count, rows_sample, clarify), `totals` (steps, errors, llm_calls, llm_ms, db_ms) and `steps`.
- `events.jsonl`: one line per step, the same step record as in `runs.jsonl`.
- Step record: `seq, step, title, status, offset_ms, duration_ms, input, output, llm_calls[], error`.
  `llm_calls[]` holds the full prompt messages, raw model response, model, options and duration of each LLM or embedding call in that step.
  Retries are separate step records. Durations are measured around the work itself.
- API: `GET /api/logs?limit=50`, `GET /api/logs/{run_id}`. Console prints one compact line per step.
Result rows are logged as a masked sample (`LOG_ROWS`, default 20). Strings over 20000 chars / lists over 500 items are cut with an explicit marker.
Logs contain questions, prompts and data samples: protect the folder. `LOG_FULL=0` keeps only timings and outcomes.
