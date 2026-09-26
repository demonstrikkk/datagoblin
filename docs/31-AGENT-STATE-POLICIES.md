# 31 — AGENT STATE + TOOLS + POLICIES

State (`agents/state.py`): run_id, goal, entity, fields, queries, searched_queries, candidate_urls, accepted_sources, extracted_records, rejected_records, requested_count, valid_count, iteration/max_iterations, decision, decision_reason. Explicit TypedDict, no hidden LLM state.

Graph: analyze_coverage → generate_search_strategy → execute_search → inspect_results → sufficient? FETCH : refine_search (loop ≤3) → coverage_check → FINALIZE. Post-extract: VALIDATE → coverage_check → DEDUP or bounded retry.

Tools (deterministic shims, no mutation): search_web, inspect_search_results, request_source_fetch, assess_coverage, refine_search_query, request_research_retry. Budgets: MAX_ITERATIONS 3, MAX_SEARCH_QUERIES 8, MAX_RESULTS 50, MAX_PAGES 15, MAX_RUNTIME 600s.
