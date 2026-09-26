# 30 — AGENTIC ARCHITECTURE (lean: LangGraph moves state, Jev judges, Runner executes)

Critical sentence (also in 00): **LangGraph owns bounded agentic research decisions; the DATAGOBLIN Runner owns application execution.** Simplified loop: SEARCH → COLLECT → JEV coverage? → sufficient FETCH : REFINE → SEARCH (max 3 iterations, one iteration may fan out ≤3 queries; caps 8 queries / 15 pages / 600s).

LangGraph nodes: `search → collect → ask_jev → refine/finish` (agents/graph.py). It does NOT decide content — Jev family D (`research_continuation`) returns sufficient/insufficient/uncertain; Runner maps to FETCH/REFINE/REVIEW. Other Jev families plug at boundaries: A source screening pre-fetch, B evidence verification post-extract (Proof Drawer shows SUPPORTED 0.97), C conflict A/B/CONFLICT/INSUFFICIENT.

One supervisor graph, 6 tool shims, typed SupervisorDecision validated + budget-capped before execution. Never agentic: HTTP/DB writes, retries, timeouts, SSE, transitions, schema/type validation, L1/L2 dedupe, export, auth/RLS, budgets.
