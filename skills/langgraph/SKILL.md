# LangGraph — bounded Research Supervisor subgraph ONLY

Role: analyze→decide→(refine loop ≤3)→coverage inside Runner. Runner owns fetch/DB/validation/export/budgets.

Verified 2026-09-24: https://github.com/langchain-ai/langgraph (42.2k★) + https://docs.langchain.com/oss/python/langgraph/ (use-graph-api, persistence, streaming, interrupts)
```bash
pip install -U langgraph  # py 3.10+
```
```python
from langgraph.graph import StateGraph, START, END
builder = StateGraph(State)
builder.add_node("a", a)
builder.add_conditional_edges("a", route)
graph = builder.compile()  # checkpointer=InMemorySaver + thread_id for persistence; interrupt_before/after + Command(resume) for HITL
```
Code: agents/graph.py (lazy import → None fallback = deterministic path). Limits: orchestration only (no sandbox/cost guard), MemorySaver volatile (prod PostgresSaver), recursion limit, checkpoint growth. Never LangGraph-everywhere, never 10 agents.
