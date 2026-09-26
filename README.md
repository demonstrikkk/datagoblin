# DATAGOBLIN — ask real questions, get verified answers

Deterministic + bounded-agentic web-scraping backend with a proof-first rule:
every non-null field carries its source URL, retrieval time, and evidence quote —
or the field stays null. Empty results are honest, never fabricated.

Pipeline: GENERATE (opencode → Groq → Gemini) → ORCHESTRATE (LangGraph
SEARCH → SCREEN → DECIDE → REFINE/FETCH, ≤3 iterations) → JUDGE (Jev, 4
decision families) → EXECUTE (4-worker fetch pool: static HTTP → allowlisted
impersonation → Crawl4AI rendered fallback; robots.txt honored, no proxies,
no bypass) → PROVE (evidence gates + Markdown/CSV/JSON/JSONL export).

## Quickstart

Backend (from the **repo root** — the server reads `.env` from the CWD):
```bash
cd backend
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
copy ..\..env.example ..\..env   # fill keys (see docs/23-ENVIRONMENT.md)
cd C:\Users\asus\Downloads\datagoblin
set PYTHONPATH=backend && python -m uvicorn app.main:app --port 8000
```

Frontend:
```bash
cd frontend
npm install
npm run dev   # http://localhost:5173 (`/api` proxied to :8000)
```

Flow: ask → compile → plan preview → POST /runs → SSE `/api/runs/:id/stream`
(14 event types) → dataset table → proof inspector → export (csv/json/jsonl/md).
Tests: `cd backend && python -m pytest -q` (171 passed; fixture-only, no credits).

## Docs

`docs/00-PROJECT-CONSTITUTION.md` through `docs/36-FILE-E2E.md` are the build
specs; `docs/05-SYSTEM-ARCHITECTURE.md` + `docs/33-BACKEND-ARCHITECTURE.md` are
the maps. Start with `docs/23-ENVIRONMENT.md` (keys) and `docs/17-FRONTEND-SPEC.md`.
`AUDIT.md` tracks dead-code deletion candidates. No `TODO`s ship as done work.

## Security

`.env` holds real keys and is gitignored — never commit it. The frontend never
receives server secrets. Scraping posture: permitted sources only, robots.txt
+ per-host throttle enforced, no proxies, no CAPTCHA/challenge solving.
