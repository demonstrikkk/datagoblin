"""Phase-5 metering: price table, projection, idempotent ledger, run budgets.

Unit model (credits ~= provider spend, not money):
  discover_query = 1 per planned search query (Tavily)
  fetch_page     = 1 per attempted page (bandwidth/browser time)
  extract_page   = 2 per LLM-extracted page; deterministic selector wins bill
                   0 extract (fetch still billed) — the runner splits by provider
  judge_call     = 1 per adjudicated conflict (Jev)
  export         = 1 per export call
  map            = 1 per map call

Idempotency: every charge carries charge_id "{job_id}:{stage}[:seq]". Billing
the same charge_id twice returns the original entry (billed=False) — SSE
retries and at-least-once workers can never double-charge.

Budget model (Firecrawl reserve/reconcile spirit): a run gets budget_credits
(0/None = unlimited, the default — metering records always, enforcement only
when configured). project_cost() estimates upfront; the runner clamps
max_pages to min(remaining, requested) and finalizes partial (never half-
bills) when the budget runs dry mid-run.
"""
import datetime
from typing import Any

PRICES: dict[str, int] = {
    "discover_query": 1,
    "fetch_page": 1,
    "extract_page": 2,
    "judge_call": 1,
    "export": 1,
    "map": 1,
}


def charge_id(job_id: str, stage: str, seq: int = 0) -> str:
    base = f"{job_id}:{stage}"
    return base if not seq else f"{base}:{seq}"


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def project_cost(plan: dict, max_queries: int = 8, max_pages_cap: int = 15) -> dict:
    """Upfront estimate from the plan (no network). Returns breakdown + total."""
    queries = len(plan.get("search_queries", []) or [])
    queries = max(1, min(queries, max_queries))
    pages = max(1, min(int(plan.get("max_pages", 12)), max_pages_cap))
    per_page = PRICES["fetch_page"] + PRICES["extract_page"]
    breakdown = {"discover_query": queries * PRICES["discover_query"],
                 "fetch_extract_pages": pages * per_page,
                 "judge_call": 0}  # conflicts unknowable upfront; billed actual
    return {"breakdown": breakdown, "pages": pages, "queries": queries,
            "total": breakdown["discover_query"] + breakdown["fetch_extract_pages"]}


class Ledger:
    """In-memory idempotent charge store. Persistence is the repo's job."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self._order: list[str] = []

    def bill(self, job_id: str, stage: str, units: int = 1,
             seq: int = 0) -> dict:
        """Charge units at stage price. Same charge_id twice = original entry."""
        cid = charge_id(job_id, stage, seq)
        if cid in self._entries:
            return {**self._entries[cid], "billed": False}
        entry = {"job_id": job_id, "stage": stage, "units": max(0, int(units)),
                 "credits": max(0, int(units)) * PRICES.get(stage, 1),
                 "charge_id": cid, "at": _now(), "billed": True}
        self._entries[cid] = entry
        self._order.append(cid)
        return dict(entry)

    def total(self, job_id: str) -> int:
        return sum(e["credits"] for e in self._entries.values()
                   if e["job_id"] == job_id)

    def for_job(self, job_id: str) -> list[dict]:
        return [dict(self._entries[c]) for c in self._order
                if self._entries[c]["job_id"] == job_id]


class Meter:
    """Per-run budget over a Ledger. spend() returns None when over budget."""

    def __init__(self, job_id: str, budget_credits: int = 0,
                 ledger: Ledger | None = None) -> None:
        self.job_id = job_id
        self.budget = max(0, int(budget_credits or 0))
        self.ledger = ledger or Ledger()
        self.projected = 0

    def plan(self, projected_total: int) -> None:
        self.projected = max(0, int(projected_total))

    @property
    def spent(self) -> int:
        return self.ledger.total(self.job_id)

    @property
    def unlimited(self) -> bool:
        return self.budget <= 0

    def remaining(self) -> int | None:
        return None if self.unlimited else max(0, self.budget - self.spent)

    def exhausted(self) -> bool:
        return not self.unlimited and self.spent >= self.budget

    def spend(self, stage: str, units: int = 1, seq: int = 0) -> dict | None:
        """Bill when affordable. Deterministic extract (units<=0) records 0."""
        units = max(0, int(units))
        cost = units * PRICES.get(stage, 1)
        if not self.unlimited and self.spent + cost > self.budget:
            return None
        return self.ledger.bill(self.job_id, stage, units, seq)

    def clamp_pages(self, requested_pages: int) -> int:
        """Firecrawl clamp: limit = min(remaining, requested). Unlimited passthrough."""
        requested = max(1, int(requested_pages))
        if self.unlimited:
            return requested
        per_page = PRICES["fetch_page"] + PRICES["extract_page"]
        discover = PRICES["discover_query"]  # at least one query happens
        affordable = (self.budget - self.spent - discover) // per_page
        return max(0, min(requested, affordable))

    def summary(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "budget": self.budget, "spent": self.spent,
                "remaining": self.remaining(), "projected": self.projected,
                "exhausted": self.exhausted()}
