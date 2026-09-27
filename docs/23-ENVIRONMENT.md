# 23 — ENVIRONMENT

```bash
# --- persistence ---
PERSISTENCE=              # postgres (default when DATABASE_URL set) | local (JSONL, explicit opt-in)
DATABASE_URL=             # REQUIRED for postgres; a real postgresql:// DSN. The app reads this.
SUPABASE_URL=             # not read by the app (REST client only; SupabaseRepo was removed)
SUPABASE_KEY=             # not read by the app
TAVILY_API_KEY=           # REQUIRED (discovery; without it runs find 0 sources)
GROQ_API_KEY=             # OPTIONAL (only reachable when OPENCODE_STRICT=false)
GEMINI_API_KEY=           # OPTIONAL (only reachable when OPENCODE_STRICT=false)
OPENCODE_BASE_URL=        # OPTIONAL (generator; default http://127.0.0.1:4096)
OPENCODE_PASSWORD=        # OPTIONAL (pairs with OPENCODE_BASE_URL; server runs with Basic auth)
OPENCODE_TIMEOUT_S=       # OPTIONAL (default 240)
OPENCODE_MODEL=           # the extraction/plan model. See the warning below.
ZEN_API_KEY=              # OPTIONAL (Zen gateway; also stored by `opencode auth login`)
ZEN_ENABLED=              # OPTIONAL (default true)
ZEN_BASE_URL=             # OPTIONAL (default https://opencode.ai/zen/v1)
ZEN_FANOUT_CONCURRENCY=   # OPTIONAL (default 4; bounds parallel calls in /api/intel/ask)
ZEN_FANOUT_MODELS=        # OPTIONAL (comma-separated override; empty = all free text models)
JEV_ZEV_MODEL=            # OPTIONAL (default jev-1.13-free; free judge tried before paid OpenRouter)
TYPESAFE_API_KEY=        # OPTIONAL (Jev judge via OpenRouter; absent = judge downgrades gracefully)
JINA_API_KEY=            # OPTIONAL (r.jina.ai fallback; gated by FEATURE_JINA=false default)
VITE_API_URL=            # OPTIONAL (frontend → backend; empty = same-origin, vite proxies /api → :8000)
```

Generator chain: opencode → Groq → Gemini (first success wins, provider labeled).
`OPENCODE_STRICT=true` makes opencode THE only rung — the cloud fallbacks
(`GROQ_API_KEY`, `GEMINI_API_KEY`) are then **never called**, so they are
optional, not required.

### `OPENCODE_MODEL` is a performance decision, not a detail

Measured on a realistic 21.5k-character extraction prompt:

| model | latency |
|---|---|
| **big-pickle** | **4.9s** |
| muse-spark-1.3-contributor-free | 29.2s |
| nemotron-3.5-lightning-free | 162.1s |
| mimo-v2.6-flash-free | 170.2s (timed out) |

This model drives planning, extraction and the supervisor, so its latency *is*
the run's latency. `muse-spark-1.3-contributor-free` was configured and cost 6×
`big-pickle`; against `EXTRACT_PAGE_TIMEOUT_S=150` the slower models simply
time out and the page returns zero records. Currently set to
`opencode/big-pickle`. Re-measure before changing it — a faster model extracts
more records, which moves cost into validation (see `RUN_MAX_JUDGE_CALLS`).

The requested model always wins over this setting; it is only the fallback.
A reply naming a *different* model is reported as `model_mismatch` rather than
relabelled.

### Session economy

Sessions are **pooled with exclusive checkout**, not shared. A single warm
session handed to concurrent callers was measured serving every request from
whichever model that session was pinned to, with all callers receiving the same
reply — two run workers would have recorded each other's extractions. A session
is now owned by exactly one in-flight call and recycled afterwards
(pool size 4, max 6 uses / 300s per session).

Pacing: `EXTRACT_PAGE_SPACING_S=10` between pages (free-tier quotas are
per-minute), `EXTRACT_PAGE_TIMEOUT_S=150` per page,
`EXTRACT_PAGE_CONCURRENCY=3` pages at once, `RUN_MAX_JUDGE_CALLS=400` per run,
`RUN_MAX_RUNTIME_S` overall (checked per stage **and** per page).

Judge: Jev `~typesafe/jev-latest` via `POST https://openrouter.ai/api/alpha/decisions`
(flat `{model, state, questions}`).

## Zen free tier: one key, two transports

`ZEN_API_KEY` (or `opencode auth login`, which stores the same key under the
`opencode` provider) opens `https://opencode.ai/zen/v1`. What it does **not** do
is make every free model callable from application code. Measured:

| Transport | Models | Notes |
|---|---|---|
| direct REST | `space-bunny-free`, `jev-1.13-free` | works from this process |
| OpenCode server | the other 9 free text models | `403 FreeTierError` on direct REST by policy |
| systemone | `jev-1.13-free` | typed decisions, not prose |

So `opencode serve` is a prerequisite for the free tier, not an optional
fallback: without it, 9 of 10 free text models are simply unavailable.
`GET /api/models/free` reports this live rather than letting you discover it as
a 403. The free set is curated in `backend/app/providers/llm/zen.py` on purpose
— `GET /zen/v1/models` returns 82 models with null `cost`, so "free" cannot be
derived from the catalogue, and availability rotates (the same model answered
correctly in one run and returned an upstream 500 in the next). Fan-out therefore
isolates failures per model and reports agreement, never a single merged answer.

### The OpenCode password must be set on BOTH sides

The server enforces Basic auth only when *its own* `OPENCODE_SERVER_PASSWORD`
is set. Measured against a server started without it:

| Request | Result |
|---|---|
| no `Authorization` header | `200` |
| Basic, **wrong** password | `200` |
| Basic, correct password | `200` |

So `OPENCODE_PASSWORD` in `.env` protects nothing while the server runs bare,
and a typo in that value can never surface as an error — everything just
succeeds. Always launch the server with the matching value:

```bash
set OPENCODE_SERVER_PASSWORD=<same value as OPENCODE_PASSWORD in .env>
opencode serve --port 4096 --hostname 127.0.0.1
```

`start-dev.ps1` reads `OPENCODE_PASSWORD` out of `.env` and forwards it as
`OPENCODE_SERVER_PASSWORD`, and states on startup whether auth is on. With it
set, no header and a wrong password both return `401`.

Agreement is also compared semantically, not textually: `{"a": 1}` and `{"a":1}`
are one answer. Three models that all said `{"capital": "Paris"}` were once
reported as a 2-1 "majority" purely because one omitted a space after the colon.


Critical: start the API from the REPO ROOT (`set PYTHONPATH=backend &&
python -m uvicorn app.main:app`), because settings load `.env` from the CWD.
Starting inside `backend/` silently runs keyless (log: `startup keys missing`).

Local: `.env` (gitignored) + `.env.example` (keys only, no values). Production:
Render/Vercel env panels. Never commit real keys (see 20).
