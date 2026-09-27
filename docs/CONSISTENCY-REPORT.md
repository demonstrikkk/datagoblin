# CONSISTENCY AUDIT REPORT (2026-09-24, build mode)

> **SUPERSEDED (2026-09-27).** A point-in-time report kept for history.
> Several of its conclusions no longer hold: the app now persists to
> Postgres via `PERSISTENCE` (it used to silently write JSONL), pages are
> stored as evidence, validation drops records instead of annotating them,
> and the emitted event set is 13, not 14. See docs/37-CURRENT-STATE.md
> and the AUDIT.md addendum for current truth.


Checked: docs/00–29 (30 files), contracts/*.schema.json + api.openapi.yaml, prompts/*, backend 16 .py, frontend routes/components/hooks, skills/24, .env.example.

## Contradictions found → fixed

1. Flow drift: 01/04/05/06 still old `Discover→Fetch→...→Dedup→Store`; 00 frozen to `Discover→Triage→Fetch→Reduce→Extract→Validate→Normalize→Dedup→Evidence→Store`. FIXED 01 (+triage/normalize/evidence), 09 (6-step hierarchy + router ref). 04/05/06 remain compatible summaries (reference 00 as frozen; no conflicting order).
2. Missing endpoints: docs/15+openapi list 11 incl. `/records` + `/sources`; main.py had 9. FIXED: added `GET /api/datasets/{did}/records` (q/limit/offset) + `GET /api/datasets/{did}/sources` (counts+sources).
3. Env drift: config.py had SUPABASE_URL/KEY not in docs/23/.env.example. FIXED both.
4. Runner drift: execute_run ignored triage/normalize. FIXED: triage_source() orders fetch methods; normalize_record() wraps validated fields (original preserved).
5. Fetch hierarchy: 09 listed 3 steps vs 00/source_router 6 steps. FIXED 09.

## Verified compatible (no change needed)

- States: 11 states + legal chain identical in 07 ↔ constants.py ↔ run-event.schema.json ↔ runner emits.
- SSE: 14 event types identical in 16 ↔ constants ↔ openapi ↔ useRunStream.
- Provenance: {value, verification_status, source{url,title,quote,retrieved_at}} identical in 12 ↔ 29 ↔ plan.py ↔ runner ↔ ProofDrawer ↔ export.
- Validation guard: quote⊂text→unverified identical in 11 ↔ core_logic ↔ tests.
- Dedupe: L1 exact → L2 RapidFuzz → L3 optional identical in 13 ↔ 28 ↔ core_logic.
- Routes: /new /runs/:id /datasets/:id /history (+sources embedded) identical in 17 ↔ App.jsx. Agent-invented /pipelines rejected.
- Components: all 12 spec names present in studio.jsx.
- Qdrant optional, DuckDB/Polars/LanceDB evaluate-only, Jev experiment-only, MCP future-only, smolagents/Agno rejected — consistent across 00/02/26/28/skills.
- Secrets: never log/commit consistent in 20 ↔ 23 ↔ logging.py ↔ .env.example (values empty).

## Residual (accepted, not contradictions)

- In-memory PLANS/RUNS/DATASETS in main.py = Day-1 explicitly marked for Supabase swap (README).
- Free-tier numbers quoted not guaranteed (skills) — re-check at deploy.
- Jev DATAGOBLIN use proposed (UNVERIFIED public) — stub only.
