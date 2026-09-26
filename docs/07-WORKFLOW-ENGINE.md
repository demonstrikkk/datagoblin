# 07 — WORKFLOW ENGINE (deterministic state machine, no LangGraph in core)

## States (exact, do not invent others)

`PLANNING, DISCOVERING, FETCHING, REDUCING, EXTRACTING, VALIDATING, DEDUPLICATING, FINALIZING, COMPLETED, FAILED, CANCELLED`

## Legal transitions

- PLANNING → DISCOVERING → FETCHING → REDUCING → EXTRACTING → VALIDATING → DEDUPLICATING → FINALIZING → COMPLETED
- ANY ACTIVE STATE (all except COMPLETED/FAILED/CANCELLED) → FAILED (on unrecoverable error, with `error` recorded)
- ANY ACTIVE STATE → CANCELLED (on user cancel; preserve partial dataset + events)

## Runner semantics (conceptual)

`execute_run(run_id, plan)`: stage(DISCOVERING)→discover(urls); stage(FETCHING)→fetch(pages, per-URL retry once then alternate method then skip); stage(REDUCING)→clean markdown; stage(EXTRACTING)→records+evidence; stage(VALIDATING)→validate; stage(DEDUPLICATING)→merge; stage(FINALIZING)→save dataset+sources+events; →COMPLETED. Emit SSE on every transition + progress + source/record events. Later replaceable by Celery/Redis/Temporal without rewriting domain logic.
