"""Budgets must mean what they say, especially on metered paths.

Two defects, both on the path that spends money:

1. `JudgeBudget(0)` meant *unlimited*. `exhausted` was `self.limit > 0 and ...`,
   so a zero cap never tripped. `POST /api/datasets/{id}/rejudge` accepts
   `judge_budget: 0` as valid and documents it, meaning "judge nothing" — and it
   produced unlimited judge calls and an unbounded bill. A caller asking for zero
   checks must get zero checks.

2. `Ledger` was constructed per request by `/map`, `/export` and `/intel/ask`, so
   the idempotency key that the module promises ("SSE retries and at-least-once
   workers can never double-charge") never applied to any of them. A retried
   request started from an empty cache and billed again.

Also covered: a judge call that fails for a reason *other* than throttling must
not be reported as `rate_limited`, because that tells the operator to retry
later instead of fixing the actual fault.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from app.providers.llm import zen
from app.core.errors import provider_fatal, provider_transient
from app.services import metering, validator
from app.services.validator import JudgeBudget


# --------------------------------------------------------------- JudgeBudget


def test_zero_budget_means_zero_calls():
    """The headline regression: asking for no judge calls must buy none."""
    budget = JudgeBudget(0)
    assert budget.exhausted is True, (
        "JudgeBudget(0) reported itself unexhausted, which is how zero came to "
        "mean unlimited on a metered endpoint")
    assert budget.spend() is False
    assert budget.used == 0


def test_unlimited_is_opt_in_and_explicit():
    budget = JudgeBudget(0, unlimited=True)
    assert budget.exhausted is False
    assert [budget.spend() for _ in range(50)] == [True] * 50
    assert budget.remaining == -1, "-1 is the sentinel callers read as unbounded"


def test_a_budget_stops_exactly_at_its_limit():
    budget = JudgeBudget(3)
    assert [budget.spend() for _ in range(5)] == [True, True, True, False, False]
    assert budget.used == 3, "a refused spend must not increment the counter"
    assert budget.exhausted is True
    assert budget.remaining == 0


def test_a_negative_budget_is_rejected_not_clamped():
    with pytest.raises(ValueError):
        JudgeBudget(-1)


def test_remaining_counts_down():
    budget = JudgeBudget(4)
    assert budget.remaining == 4
    budget.spend()
    assert budget.remaining == 3


def test_the_module_default_is_unbounded_and_says_so():
    """The one caller that genuinely wants no cap must ask for it explicitly."""
    assert validator.JUDGE_BUDGET.unlimited is True, (
        "the fallback budget is no longer unbounded; a bare verify_field() call "
        "would now return JUDGMENT_UNAVAILABLE for every field")
    assert JudgeBudget(0).unlimited is False


def test_repr_states_the_bound():
    assert "unlimited" in repr(JudgeBudget(0, unlimited=True))
    assert "5" in repr(JudgeBudget(5))


# --------------------------------------------------------------- Ledger


def test_the_shared_ledger_deduplicates_across_calls():
    ledger = metering.SHARED_LEDGER
    job = "test-shared-ledger-job"
    first = ledger.bill(job, "map", 1)
    repeat = ledger.bill(job, "map", 1)
    assert repeat["billed"] is False, (
        "the shared ledger re-billed a repeated charge_id")
    assert repeat["credits"] == first["credits"]
    assert ledger.total(job) == first["credits"], (
        "a duplicate charge inflated the job total")


def test_different_jobs_do_not_collide():
    ledger = metering.SHARED_LEDGER
    ledger.bill("job-a", "export", 1)
    ledger.bill("job-b", "export", 1)
    assert ledger.total("job-a") == 1
    assert ledger.total("job-b") == 1


def test_seq_distinguishes_repeat_calls_within_one_job():
    """Export bills once per call, so `seq` is what separates two exports."""
    ledger = metering.SHARED_LEDGER
    job = "test-seq-job"
    a = ledger.bill(job, "export", 1, seq=1)
    b = ledger.bill(job, "export", 1, seq=2)
    assert a["billed"] and b["billed"]
    assert ledger.total(job) == 2


def test_concurrent_bills_of_one_charge_id_bill_once():
    """The check-then-insert pair must be atomic across threads."""
    ledger = metering.Ledger()
    results: list[dict] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        results.append(ledger.bill("race", "judge_call", 1))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r["billed"]) == 1, (
        "concurrent identical charge_ids double-billed")
    assert ledger.total("race") == 1


def test_for_job_lists_only_that_job():
    ledger = metering.Ledger()
    ledger.bill("x", "map", 2)
    ledger.bill("y", "map", 1)
    rows = ledger.for_job("x")
    assert len(rows) == 1
    assert rows[0]["units"] == 2


def test_a_meter_is_isolated_from_the_shared_ledger():
    """A Meter is per-run; the shared cache exists for the non-run endpoints."""
    meter = metering.Meter("m-isolated", budget_credits=0)
    assert meter.ledger is not metering.SHARED_LEDGER
    assert isinstance(meter.ledger, metering.Ledger)


def test_meter_accepts_an_explicit_ledger_for_isolation():
    private = metering.Ledger()
    meter = metering.Meter("m-private", ledger=private)
    assert meter.ledger is private
    meter.spend("map", 1)
    assert private.total("m-private") == 1
    assert metering.SHARED_LEDGER.total("m-private") == 0


# ---------------------------------------------- judge failure classification


def _judge_call(monkeypatch, side_effect):
    async def fake(value, quote, text):
        if isinstance(side_effect, BaseException):
            raise side_effect
        return side_effect
    monkeypatch.setattr(validator.jev, "evidence_verification", fake)


def _passes_every_gate() -> dict:
    """A record that reaches the judge, and nothing further.

    Two conditions have to hold or the call never gets that far:

    * the quote must be present in `source_text`, or `deterministic_verdict`
      answers NOT_SUPPORTED and never asks the model;
    * the value must NOT appear inside the quote, or `deterministic_verdict`
      answers SUPPORTED and settles it for free.

    So the quote carries the number and the claim carries the formatted version.
    They differ ("$4.2 million" vs "4,200,000"), which is exactly the
    interpretation a judge is for.
    """
    quote = "Annual revenue reached USD 4.2 million during the period."
    value = "4,200,000"
    return {"value": value, "quote": quote,
            "source_text": f"Company profile. {quote} Figures unaudited."}


def _verify(budget: JudgeBudget) -> tuple[object | None, str]:
    """Run verify_field on a record that clears every gate up to the judge."""
    gate = _passes_every_gate()
    return asyncio.run(validator.verify_field(
        required=True, ftype="string", budget=budget, **gate))


def test_a_rate_limited_judge_reports_rate_limited(monkeypatch):
    _judge_call(monkeypatch, zen.JevRateLimited("429"))
    budget = JudgeBudget(10)
    value, status = _verify(budget)
    assert status == "rate_limited"
    assert value is not None, "a throttled judge must not discard a real value"


@pytest.mark.parametrize("exc", [
    provider_fatal("judge key rejected"),
    provider_transient("judge endpoint unreachable"),
])
def test_a_non_throttling_failure_is_not_reported_as_throttling(monkeypatch, exc):
    """A bad key is not a busy server, and reporting it as one costs real time."""
    _judge_call(monkeypatch, exc)
    budget = JudgeBudget(10)
    value, status = _verify(budget)
    assert status == "judgment_unavailable", (
        "a provider failure was reported as rate_limited, which tells the "
        "operator to retry later instead of fixing the key or the endpoint")
    assert value is not None


def test_an_unexpected_exception_does_not_kill_the_run(monkeypatch):
    _judge_call(monkeypatch, TypeError("a bug in our own code"))
    budget = JudgeBudget(10)
    value, status = _verify(budget)
    assert status == "judgment_unavailable"
    assert value is not None, (
        "an unexpected error must not discard the record; the run keeps going")


def test_an_exhausted_budget_is_reported_as_judgment_unavailable(monkeypatch):
    calls: list[str] = []

    async def counting(value, quote, text):
        calls.append(value)
        return {"judgment": "SUPPORTED", "confidence": 0.9}

    monkeypatch.setattr(validator.jev, "evidence_verification", counting)
    budget = JudgeBudget(0)
    value, status = _verify(budget)
    assert calls == [], "a zero budget still called the judge"
    assert status == "judgment_unavailable"
    assert value is not None, "the value survives a check that never ran"