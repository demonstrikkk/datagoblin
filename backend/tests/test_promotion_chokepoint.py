"""One function may grant `verified`. This file makes that enforceable.

`verification_status` reached `"verified"` from eight places across six modules,
and only three of them had anything to do with checking evidence:

  * `validator.verify_field`  -- the gate. Legitimate.
  * `validator.judged_by_construction` -- the gate, for a structured route.
    Legitimate.
  * `validator`'s judge branch -- the gate. Legitimate.
  * `extractor._ladder_cells` -- the extraction route's name. Removed in the
    confidence fix.
  * `deduper.adjudicate_conflicts` -- a triage decision about which of two values
    is better.
  * `rejudge._upgraded` -- a judge verdict, but it never computed the locator,
    so it produced `verified` cells with `start: None`.
  * `coverage.apply_resolution` "keep" -- a button.
  * `coverage.apply_resolution` "adopt" -- a button, which additionally rebuilt
    `source` from three keys and discarded the offsets it had.

Three of those are now `validator.settle` / `validator.adopt`, and the last four
behave differently as a result. A code comment asking for one chokepoint is a
request; this is a check.

## The rule

`verified` takes two things:

  1. a **support verdict** -- does this passage establish this value
  2. a **locator** -- does the quote resolve, at a known position, against the
     exact text the store holds

Historically only (1) was ever checked, which is how a cell could be `verified`
and have no offsets at all. `settle` makes (2) a precondition and demotes when
it fails, so "verified" always means "there is a citation behind this".

## The enforcement

`test_no_other_module_may_grant_verified` parses every module under `app/` and
fails on a literal assignment to a `verification_status` of `"verified"`
anywhere outside `validator.py`. It is a real check, not a request: the
pre-change tree fails it four times.
"""
from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.evidence import locate_quote  # noqa: E402
from app.services import validator as validator_svc  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"
CHOKEPOINT = "validator.py"


# --- the enforcement -----------------------------------------------------------

def _grants_verified(path: Path) -> list[tuple[int, str]]:
    """Lines that write a literal `"verified"` into a `verification_status`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    found: list[tuple[int, str]] = []

    def _is_verified(node: ast.AST) -> bool:
        return (isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == "verified")

    def _is_status_key(node: ast.AST) -> bool:
        return (isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "verification_status" in node.value)

    for node in ast.walk(tree):
        # `return {**cell, "verification_status": "verified"}` is the same bypass
        # as an assignment, in the syntax the rejudge path actually used. It is
        # not a rarer edge case; it is one of the four sites this file exists to
        # catch, and an assignment-only guard missed it.
        if isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if _is_status_key(key) and _is_verified(val):
                    found.append((node.lineno, "dict literal"))
            continue

        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for tgt in targets:
            if not isinstance(tgt, ast.Subscript):
                continue
            sl = tgt.slice
            if isinstance(sl, ast.Index):  # pragma: no cover - py<3.9 shape
                sl = sl.value  # type: ignore[attr-defined]
            if not _is_status_key(sl):
                continue
            value = getattr(node, "value", None)
            if value is None:
                continue
            if _is_verified(value):
                found.append((node.lineno, "assignment"))
    return found


def test_no_other_module_may_grant_verified():
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path.name == CHOKEPOINT:
            continue
        for lineno, kind in _grants_verified(path):
            rel = path.relative_to(APP).as_posix()
            offenders.append(f"{rel}:{lineno} ({kind})")
    assert not offenders, (
        "these write verification_status = \"verified\" outside validator.settle, "
        "so the evidence contract can be bypassed. Route the promotion through "
        "validator.settle() or validator.adopt():\n  "
        + "\n  ".join(offenders))


def test_the_chokepoint_is_the_one_that_survives():
    """The guard must have something to point at.

    If `settle` were ever deleted, every other module would be passing by
    default and this file would be guarding nothing.
    """
    assert hasattr(validator_svc, "settle")
    assert hasattr(validator_svc, "adopt")
    assert hasattr(validator_svc, "has_locator")


def test_the_guard_catches_both_syntactic_shapes():
    """A guard that only understands one way of writing the mistake is a half guard.

    Two of the four sites were assignments and two were dict literals returned
    from a function. The dict-literal case was added after the first version of
    this guard let it through.
    """
    src = (
        'def a(cell):\n'
        '    cell["verification_status"] = "verified"\n'
        'def b(cell):\n'
        '    return {**cell, "verification_status": "verified"}\n'
        'def c(cell):\n'
        '    cell["verification_status"] = "unverified"\n'
        'def d(cell):\n'
        '    return {"other": "verified"}\n'
    )
    import tempfile

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        name = fh.name
    hits = _grants_verified(Path(name))
    assert [kind for _ln, kind in hits] == ["assignment", "dict literal"], (
        "both shapes of the same bypass must be caught, and only those")
    """The allowlist is a fixed count, not a blank cheque.

    `validator.py` legitimately writes `"verified"` in `verify_field` and in
    `settle`. A third would be a new bypass inside the module the bypass guard
    exempts.
    """
    src = APP / "services" / CHOKEPOINT
    grants = _grants_verified(src)
    assert len(grants) <= 3, (
        "validator.py gained a new literal grant of `verified`; if that is "
        f"deliberate, this bound has to move with it: {grants}")


# --- settle: the locator is a precondition, not a footnote --------------------

PAGE = "Acme Corp was founded in 2011 by Jane Roe. Jane Roe is the CEO."
QUOTE = "Jane Roe is the CEO"


def _cell(**kw):
    cell = {"value": "Jane Roe", "verification_status": "unverified",
            "source": {"url": "https://x.example/", "quote": QUOTE,
                       "page_id": "p1", "content_hash": "h1"}}
    cell.update(kw)
    return cell


def test_validator_itself_has_a_bounded_number_of_grants():
    """The allowlist is a fixed count, not a blank cheque.

    `validator.py` legitimately writes `"verified"` in `verify_field` and inside
    `settle`. A third would be a new bypass inside the one module the bypass
    guard exempts, so the bound has to move deliberately rather than by accident.
    """
    grants = _grants_verified(APP / "services" / CHOKEPOINT)
    assert len(grants) <= 3, (
        "validator.py gained a new literal grant of `verified`; if that is "
        f"deliberate, this bound has to move with it: {grants}")


def test_supported_and_locatable_is_verified_with_offsets():
    out = validator_svc.settle(_cell(), status="verified", quote=QUOTE,
                               source_text=PAGE)
    assert out["verification_status"] == "verified"
    start, end = out["source"]["start"], out["source"]["end"]
    assert start is not None and end is not None
    assert PAGE[start:end] == QUOTE, (
        "the offsets must address the quote in the stored text, which is the "
        "only thing that makes them checkable")


def test_supported_but_unlocatable_is_not_verified():
    """The case that made the whole thing necessary.

    A verdict that the passage supports the value is not sufficient on its own.
    If the quote cannot be found in the stored text there is no citation, and a
    reader -- or any later check -- has nothing to verify against.
    """
    out = validator_svc.settle(_cell(), status="verified", quote=QUOTE,
                               source_text="A completely different page.")
    assert out["verification_status"] == "unverified"
    assert out["source"]["start"] is None and out["source"]["end"] is None
    assert "does not resolve" in (out.get("not_verified_because") or "")


def test_a_verified_cell_can_never_carry_null_offsets():
    """The invariant, asserted over the whole surface rather than one case."""
    for text in (PAGE, "", None, "unrelated"):
        out = validator_svc.settle(_cell(), status="verified", quote=QUOTE,
                                   source_text=text)
        if out["verification_status"] == "verified":
            assert out["source"]["start"] is not None
            assert out["source"]["end"] is not None
        else:
            assert out["source"]["start"] is None


def test_non_verified_statuses_never_carry_offsets():
    for status in ("unverified", "judgment_unavailable", "rate_limited",
                   "conflicting"):
        out = validator_svc.settle(_cell(), status=status, quote=QUOTE,
                                   source_text=PAGE)
        assert out["verification_status"] == status
        assert out["source"]["start"] is None, (
            f"{status} must not present a locator; offsets are only ever "
            "meaningful for a claim that passed")


def test_settle_clears_a_stale_reason_on_a_later_success():
    out = validator_svc.settle(_cell(not_verified_because="earlier problem"),
                               status="verified", quote=QUOTE, source_text=PAGE)
    assert "not_verified_because" not in out


def test_settle_never_raises_on_junk():
    """It runs inside the item pipeline; a raise here loses the whole record."""
    for status in ("verified", "unverified", "", None):
        out = validator_svc.settle({}, status=status, quote=None,
                                   source_text=None)
        assert out["verification_status"] == "unverified"


# --- adopt: a decision is not evidence ---------------------------------------

def _rival(status, **src):
    return {"value": "B", "verification_status": status,
            "source": {"quote": "qb", "url": "https://b.example/",
                       "page_id": "p2", **src}}


def test_adopting_a_verified_rival_keeps_verified_and_its_offsets():
    out = validator_svc.adopt(_cell(verification_status="conflicting"),
                              _rival("verified", start=9, end=11))
    assert out["verification_status"] == "verified"
    assert out["source"]["start"] == 9 and out["source"]["end"] == 11
    assert out.get("resolved", "") == "", (
        "adoption is not itself a resolution unless the caller names one")


def test_adopting_a_rival_that_claimed_verified_without_offsets_is_refused():
    """The exact state `rejudge` used to manufacture.

    Refused on the way in rather than discovered later, because a cell reading
    as verified with no resolvable citation is indistinguishable from a real one
    once it is in the database.
    """
    out = validator_svc.adopt(_cell(verification_status="conflicting"),
                              _rival("verified"))
    assert out["verification_status"] == "unverified"
    assert "no offsets" in (out.get("not_verified_because") or "")


def test_adopting_an_unverified_rival_stays_unverified():
    out = validator_svc.adopt(_cell(verification_status="conflicting"),
                              _rival("unverified"))
    assert out["verification_status"] == "unverified"


def test_adopt_takes_the_whole_source_not_three_keys():
    """What the manual resolver used to discard.

    `content_hash` and `retrieved_at` are how a reader tells one fetch from
    another, and the offsets are the citation itself.
    """
    out = validator_svc.adopt(
        _cell(verification_status="conflicting"),
        _rival("verified", start=1, end=3, content_hash="h2", retrieved_at="then"))
    assert out["source"] == {"quote": "qb", "url": "https://b.example/",
                             "page_id": "p2", "start": 1, "end": 3,
                             "content_hash": "h2", "retrieved_at": "then"}


def test_adopt_drops_a_receipt_that_described_the_old_value():
    out = validator_svc.adopt(
        _cell(verification_status="conflicting", read_method="llm",
              confidence=0.75, certainty="likely", method_label="Found by AI"),
        _rival("verified", start=0, end=2))
    for key in ("read_method", "confidence", "certainty", "method_label"):
        assert key not in out, (
            f"{key} described the value that was replaced; carrying it over "
            "would label the rival's evidence with the incumbent's route")


def test_has_locator_is_the_test_the_locator_agrees_with():
    """Both paths must use the same definition, or one of them is theatre."""
    good = {"source": {"start": 3, "end": 9}}
    assert validator_svc.has_locator(good) is True
    for bad in ({"source": {"start": None, "end": None}},
                {"source": {"start": 0, "end": None}},
                {"source": {"end": 4}}, {"source": {}}, {}):
        assert validator_svc.has_locator(bad) is False


# --- rejudge: the promotion that forgot its own locator -----------------------

def test_rejudge_upgrade_carries_the_locator_it_promises():
    """Rejudge exists to close `judgment_unavailable` cells.

    Those cells have a quote and a `page_id` and no offsets, because offsets are
    only computed for a verified cell. Promoting one without recomputing them
    produced `verified` with `start: None` -- the failure this whole change is
    about, produced by the module meant to be the cure.
    """
    from app.services import rejudge as rejudge_svc

    cell = {"value": "Jane Roe", "verification_status": "judgment_unavailable",
            "source": {"quote": QUOTE, "url": "https://x.example/", "page_id": "p1",
                       "start": None, "end": None}}
    out = rejudge_svc._upgraded(cell, "SUPPORTED", 0.9, text=PAGE)
    assert out["verification_status"] == "verified"
    assert out["source"]["start"] is not None
    assert out["source"]["end"] is not None
    start, end = out["source"]["start"], out["source"]["end"]
    assert PAGE[start:end] == QUOTE
    assert out["judgment_confidence"] == 0.9


def test_rejudge_cannot_promote_a_quote_the_page_does_not_contain():
    from app.services import rejudge as rejudge_svc

    cell = {"value": "Jane Roe", "verification_status": "judgment_unavailable",
            "source": {"quote": QUOTE, "url": "u", "page_id": "p1"}}
    out = rejudge_svc._upgraded(cell, "SUPPORTED", 0.9, text="other page")
    assert out["verification_status"] == "unverified"
    assert out["source"]["start"] is None


def test_rejudge_leaves_a_cell_it_cannot_judge_alone():
    from app.services import rejudge as rejudge_svc

    cell = {"value": "x", "verification_status": "judgment_unavailable",
            "source": {"quote": QUOTE}}
    assert rejudge_svc._upgraded(cell, "", 0.0, text=PAGE) is cell
    assert rejudge_svc._upgraded(cell, "UNCERTAIN", 0.5, text=PAGE) is cell


# --- the gate and the locator agree ------------------------------------------

def test_settle_and_locate_quote_agree_on_what_is_findable():
    """Two different notions of "present" would let one of them be wrong.

    `settle` demotes on a failed locate, so if the two disagreed there would be
    cells admitted as verified whose own proof view could not highlight them.
    """
    for quote in ("Jane Roe is the CEO", "jane roe is the ceo",
                  "Jane  Roe   is the CEO", "not on the page at all"):
        start, end = locate_quote(quote, PAGE)
        out = validator_svc.settle(_cell(), status="verified", quote=quote,
                                   source_text=PAGE)
        assert (start is not None) == (out["verification_status"] == "verified"), (
            f"quote {quote!r}: locator says {start}, settle says "
            f"{out['verification_status']}")