# 23 — ENVIRONMENT

```bash
DATABASE_URL=            # OPTIONAL (Supabase Postgres; absent = local-dev JSON files)
SUPABASE_URL=            # OPTIONAL (Data API URL; app flip needs this + SUPABASE_KEY)
SUPABASE_KEY=            # OPTIONAL server secret (service_role; never log, never ship to frontend)
TAVILY_API_KEY=          # REQUIRED (discovery; without it runs find 0 sources)
GROQ_API_KEY=            # REQUIRED (main generator, openai/gpt-oss-20b)
GEMINI_API_KEY=          # REQUIRED (fallback, gemini-2.5-flash)
OPENCODE_BASE_URL=       # OPTIONAL (primary generator; default http://127.0.0.1:4096)
OPENCODE_PASSWORD=       # OPTIONAL (pairs with OPENCODE_BASE_URL; server runs with Basic auth)
OPENCODE_TIMEOUT_S=      # OPTIONAL (default 240)
TYPESAFE_API_KEY=        # OPTIONAL (Jev judge via OpenRouter; absent = judge downgrades gracefully)
JINA_API_KEY=            # OPTIONAL (r.jina.ai fallback; gated by FEATURE_JINA=false default)
VITE_API_URL=            # OPTIONAL (frontend → backend; empty = same-origin, vite proxies /api → :8000)
```

Generator chain: opencode → Groq → Gemini (first success wins, provider labeled).
Judge: Jev `~typesafe/jev-latest` via `POST https://openrouter.ai/api/alpha/decisions`
(flat `{model, state, questions}`).

Critical: start the API from the REPO ROOT (`set PYTHONPATH=backend &&
python -m uvicorn app.main:app`), because settings load `.env` from the CWD.
Starting inside `backend/` silently runs keyless (log: `startup keys missing`).

Local: `.env` (gitignored) + `.env.example` (keys only, no values). Production:
Render/Vercel env panels. Never commit real keys (see 20).
