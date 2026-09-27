# 31 — AGENT STATE + TOOLS + POLICIES

## State

`agents/state.py` — explicit TypedDict, no hidden LLM state:

```
run_id, goal, entity, fields, queries,
searched_queries, candidate_urls, screened_urls, accepted_sources,
extracted_records, rejected_records,
requested_count, valid_count, missing_fields,
iteration, max_iterations, last_searched, attempted,
decision, decision_reason, evidence_summary
```

`evidence_summary` was added because the coverage judge was reading
`valid_count` and `missing_fields`, **neither of which any node ever wrote** — so
it was always asked about "0 valid out of 20" while holding no information at
all. It now carries what discovery actually knows (rounds, queries, candidates,
accepted sources and their titles).

> `valid_count` and `missing_fields` are still never written. They remain in the
> TypedDict because the graph and prompts read them, but the judge's reasoning
> comes from `evidence_summary`, not from those counters.

## The graph

Three nodes, compiled when LangGraph is installed and hand-stepped otherwise:

```
START -> search -> screen -> decide -> {refine: back to search | fetch: END}
```

This doc previously described a seven-node graph
(`analyze_coverage -> generate_search_strategy -> execute_search ->
inspect_results -> ... -> FINALIZE`). That was never what the code did. The
actual graph is a bounded search/screen/decide loop, which is the right size —
the goal is a working vertical slice, not a node count.

`decide_node` short-circuits: no fresh queries to run → `FETCH`, because
refining with nothing to search is waste.

## Tools

`agents/tools.py` (`search_web`, `apply_decision`) is **dead code** — no
production module imports it. The graph calls providers directly.

## Policies

`POLICIES` is 1-of-6 read: only `MAX_SEARCH_QUERIES` is used, to validate the
supervisor's proposal. The rest (`MAX_ITERATIONS`, `MAX_RESULTS`, `MAX_PAGES`,
`MAX_RUNTIME_S`, `ALLOWED_TOOLS`) are documentation-only; the real limits are
`settings`.

| control | live setting |
|---|---|
| iterations | `SUPERVISOR_MAX_ITERATIONS` |
| queries | `SUPERVISOR_MAX_QUERIES` |
| results per query | `RUN_MAX_RESULTS_PER_QUERY` |
| pages | `RUN_MAX_PAGES` |
| wall clock | `RUN_MAX_RUNTIME_S` (per stage and per page, not one wrapper) |
