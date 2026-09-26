"""Supervisor decisions — typed, validated by code before Runner acts (spec §10)."""
from pydantic import BaseModel, Field


class SupervisorDecision(BaseModel):
    decision: str = Field(pattern=r"^(REFINE_SEARCH|FETCH|FINALIZE|RETRY)$")
    reason: str = ""
    missing_coverage: list[str] = []
    # No max_length here: construction must accept oversized LLM output so that
    # validated() — not the constructor — enforces the cap (test: budget forces FETCH).
    next_queries: list[str] = Field(default=[])
    confidence: float = 0.0

    def validated(self, budgets: dict) -> "SupervisorDecision":
        allowed = budgets.get("max_queries", 8)
        self.next_queries = self.next_queries[:3]
        if len(budgets.get("used_queries", [])) + len(self.next_queries) > allowed:
            self.next_queries = self.next_queries[: max(0, allowed - len(budgets["used_queries"]))]
            if not self.next_queries:
                self.decision = "FETCH"
                self.reason += " [budget-capped]"
        return self
