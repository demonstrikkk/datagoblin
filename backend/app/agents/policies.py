"""Supervisor policies — hard budgets. Agent reasons/decides; Runner executes (spec §§4-7)."""
POLICIES = {
    "MAX_ITERATIONS": 3,       # refine loops max
    "MAX_SEARCH_QUERIES": 8,   # total incl. plan queries
    "MAX_RESULTS": 50,
    "MAX_PAGES": 15,
    "MAX_RUNTIME_S": 600,
    "ALLOWED_TOOLS": ["search_web", "inspect_search_results", "request_source_fetch",
                      "assess_coverage", "refine_search_query", "request_research_retry"],
    # NEVER agentic: http/db writes, retries, timeouts, SSE, transitions, schema/type validation,
    # L1/L2 dedupe, export, auth, RLS, rate/budget limits (code-owned).
}

AGENTIC_SCOPE = ["research strategy", "search refinement", "source relevance",
                 "coverage assessment", "recovery strategy", "conflict investigation"]
