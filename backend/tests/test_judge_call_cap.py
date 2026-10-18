"""`RUN_MAX_JUDGE_CALLS = 0` means "no cap", and the run path has to say so.

The setting is documented `0 = unlimited`. `JudgeBudget` reads 0 as ZERO calls,
deliberately, because the rejudge route takes `judge_budget` from a request body
where 0 must mean "judge nothing" -- an unbounded bill is not an acceptable
reading of "no limit" from an API caller.

Those are both correct. They were simply not reconciled: the runner passed the
setting straight into `JudgeBudget`, so `0` bought the opposite of what the
config promised.

The consequence is the part worth pinning. It is not a crash and not a warning.
Verification stops happening, values are still kept, and every field reports
`judgment_unavailable` -- which is the same status used for "the judge was
unreachable". An operator who set the variable to *lift* the cap had removed the
feature, and the dataset was indistinguishable from one that had never had
evidence.

`test_a_zero_budget_means_zero_calls_not_unlimited` in test_validation_gate.py
already pins the request-body half. This pins the config half.
"""
import asyncio
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.providers.decision import jev  # noqa: E402
from app.services import runner  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


#: A value that is NOT a substring of its quote, so the deterministic path cannot
#: settle it and a judge is genuinely required. "Acme" in "Acme Corp was founded"
#: would be settled by arithmetic and prove nothing about the budget.
PAGE = "Acme Corp was founded in 2011 and employs roughly 240 people."
QUOTE = "employs roughly 240 people"


@pytest.fixture()
def yes_judge(monkeypatch):
    """A reachable judge that supports the claim, counting its calls."""
    calls = {"n": 0}

    async def _yes(prompt, schema, **kw):
        calls["n"] += 1
        return {"support": {"type": "noul", "noul": 0.9}}

    monkeypatch.setattr(jev, "_jev_call", _yes)
    return calls


def _set_cap(monkeypatch, value):
    monkeypatch.setattr(settings, "RUN_MAX_JUDGE_CALLS", value)


def test_zero_means_no_cap_on_the_run_path(monkeypatch):
    """The bug. This asserted `unlimited is False` before, pinning it in place."""
    _set_cap(monkeypatch, 0)
    budget = runner._judge_budget_for_run()
    assert budget.unlimited is True, (
        "RUN_MAX_JUDGE_CALLS=0 is documented as unlimited; a zero-call budget "
        "here means setting it to 0 silently stops all verification"
    )
    assert budget.spend() is True


def test_a_positive_cap_still_caps(monkeypatch):
    _set_cap(monkeypatch, 400)
    budget = runner._judge_budget_for_run()
    assert budget.unlimited is False
    assert budget.limit == 400
    for _ in range(400):
        assert budget.spend() is True
    assert budget.spend() is False, "a positive cap must actually cap"


def test_lifting_the_cap_restores_verification(yes_judge, monkeypatch):
    """The consequence, not the mechanism.

    A field that needs a judge comes back `verified` when the cap is lifted, and
    `judgment_unavailable` when the setting is read the other way. That is the
    whole failure: a dataset that stops proving things, with no error anywhere.
    """
    _set_cap(monkeypatch, 0)
    fixed = runner._judge_budget_for_run()
    _, status = run(validator_svc.verify_field(
        "firm-0", QUOTE, PAGE, False, "string", budget=fixed))
    assert yes_judge["n"] == 1, "the judge was never asked, so nothing was proved"
    assert status == "verified"

    # What the runner used to build from the same setting.
    yes_judge["n"] = 0
    _, status = run(validator_svc.verify_field(
        "firm-0", QUOTE, PAGE, False, "string",
        budget=validator_svc.JudgeBudget(settings.RUN_MAX_JUDGE_CALLS)))
    assert yes_judge["n"] == 0, "a zero cap must not reach the provider"
    assert status == "judgment_unavailable"


def test_the_runner_does_not_pass_the_setting_straight_through():
    """Static guard, on the parsed tree rather than the text.

    The defect was a one-argument call that looked reasonable and read clearly
    at the call site -- which is why it survived review. Reconstructing it would
    reintroduce the split-brain silently.

    Parsed rather than grepped because this file's own comments have to be able
    to name the mistake in order to explain it, and a text match would then fail
    on the explanation.
    """
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "JudgeBudget":
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Attribute) and first.attr == "RUN_MAX_JUDGE_CALLS":
            offenders.append(first.lineno)
    assert not offenders, (
        f"runner.py passes RUN_MAX_JUDGE_CALLS straight to JudgeBudget at line(s) "
        f"{offenders}; 0 then means zero judge calls instead of no cap. "
        f"Go through _judge_budget_for_run."
    )


def test_the_request_surface_still_fails_safe():
    """The other half, restated so a future edit cannot quietly relax it.

    `JudgeBudget(0)` on its own must stay ZERO. Only the run path translates.
    """
    budget = validator_svc.JudgeBudget(0)
    assert budget.unlimited is False
    assert budget.exhausted is True
    assert budget.spend() is False