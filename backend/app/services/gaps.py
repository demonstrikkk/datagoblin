"""What is missing from a dataset, what has been tried, and what may be tried next.

`coverage.build_backlog` answers "what is missing right now" by reading every
record. It is correct and it is cheap to be wrong about, because the next read
recomputes it. This module answers a different question — "what have we already
tried, how far did we get, and is there anything left to try" — and the answer
has to survive between requests, because otherwise the system redoes work it has
already proved is fruitless.

Three categories, because the remedy differs and conflating them is what made
the old `routable` flag unusable:

* **`schema_gap`** — no record carries the field. Either the sources genuinely do
  not publish it, or the extractor never looked in the right place. Re-reading
  the stored pages is the cheap test; if that finds nothing, the field is a
  schema problem and searching the web for it is a proposal, not a default.
* **`depth_gap`** — some records carry the field and some do not. The crawl
  reached some entities and not others. This is the category a second phase can
  genuinely close, because there is a known identity to search for.
* **`evidence_gap`** — a value exists but carries no verdict, or two sources
  disagree. Nothing is missing; something is unproven. Search does not help.
  This needs the judge or a human, and saying so is the useful output.

The state machine is deliberately small and strictly forward-moving:

    open ──▶ resolved    nothing outstanding against the field
      │
      ├──▶ exhausted    every phase was actually attempted; sources do not answer
      └──▶ refused      unsafe to attempt (no identity to match against)

`exhausted` is a finding, not a failure. It is terminal on purpose: a gap that
has been through phase 1 and phase 2 and still has no value is not going to get
one from the same approach, and re-running it is how a system burns a search
budget on a question already answered. `refused` is kept separate precisely
because nothing was tried — collapsing it into `exhausted` would report a
conclusion from an attempt that never happened.

`phase` advances only from a completed attempt. This is the single most
important invariant in the module: the UI must never be able to show "we
searched the web" for a gap that was only re-read, or "nothing found" for a gap
nobody looked at.
"""
from __future__ import annotations

from typing import Any

#: The furthest phase a gap may have reached.
PHASE_NONE = 0
PHASE_STORED = 1
PHASE_SEARCH = 2

#: Gap categories, in the order they should be reported when several apply.
CATEGORIES = ("schema_gap", "depth_gap", "evidence_gap")

#: States a gap can be in. `open` is the only one the scheduler may act on.
STATES = ("open", "resolved", "exhausted", "refused")

#: A gap may not be attempted more than this many times. The bound exists
#: because an unbounded retry loop against a public search API is a bill, and
#: because a field that has failed twice rarely succeeds on the third attempt
#: against the same sources.
MAX_ATTEMPTS = 3


def classify(field_coverage: dict) -> str | None:
    """Which category one field's gap falls into, or None if it is not a gap.

    Ordered by how much the distinction changes the remedy. `evidence_gap` is
    checked first even though a field can be both partially filled and
    unproven, because a value that carries no verdict needs judging before
    anything else is worth attempting — no amount of fetching fixes it.
    """
    present = int(field_coverage.get("present") or 0)
    missing = int(field_coverage.get("missing") or 0)
    unverified = int(field_coverage.get("unverified") or 0)
    conflicting = int(field_coverage.get("conflicting") or 0)

    if not (missing or unverified or conflicting):
        return None
    if unverified or conflicting:
        return "evidence_gap"
    if present == 0:
        return "schema_gap"
    return "depth_gap"


def category_reason(category: str, field_coverage: dict) -> str:
    """A sentence explaining the category, in the terms the numbers support.

    Every clause here is checkable against the counts that produced it. The
    previous wording in `build_backlog` said "a depth or source-coverage gap"
    for a field missing on 12 of 88 records, which named two different problems
    and settled for neither.
    """
    present = int(field_coverage.get("present") or 0)
    missing = int(field_coverage.get("missing") or 0)
    unverified = int(field_coverage.get("unverified") or 0)
    conflicting = int(field_coverage.get("conflicting") or 0)
    total = int(field_coverage.get("records") or 0)

    if category == "evidence_gap":
        if conflicting and unverified:
            return (f"{conflicting} record(s) disagree between sources and "
                    f"{unverified} value(s) carry no verdict — nothing is missing, "
                    f"so fetching more cannot help; these need judging or a decision")
        if conflicting:
            return (f"{conflicting} record(s) disagree between sources — nothing is "
                    f"missing, so fetching more cannot help; these need a decision")
        return (f"{unverified} value(s) carry no verdict — nothing is missing, so "
                f"fetching more cannot help; these need judging")
    if category == "schema_gap":
        return (f"no record of {total} carries this field, so the pages already "
                f"fetched do not answer it; re-reading them may still find it, but "
                f"searching the web for it is a separate decision")
    return (f"present on {present} of {total} records, so the crawl reached some of "
            f"these entities and not others — this is the gap a second pass can close")


def outstanding(field_coverage: dict) -> int:
    """How many cells of work this field represents.

    This is the number the queue is ordered by, and it is deliberately cells and
    not fields: a field present on 76 of 88 records is 12 cells of work, and
    ranking it beside a field missing on all 88 as "1 field" understates it by an
    order of magnitude.
    """
    return (int(field_coverage.get("missing") or 0)
            + int(field_coverage.get("unverified") or 0)
            + int(field_coverage.get("conflicting") or 0))


def derive(coverage: dict, conflicts: list[dict] | None = None) -> list[dict]:
    """The gaps a dataset currently has, ordered by outstanding cells.

    Pure and read-only — it writes nothing. `sync` is what persists this, and
    keeping the two apart is what lets the derived view be tested without a
    database and the persisted state be tested without re-deriving it.
    """
    rows: list[dict] = []
    for f in coverage.get("fields") or []:
        name = str(f.get("field") or "")
        if not name:
            continue
        category = classify(f)
        if category is None:
            continue
        rows.append({
            "field": name,
            "category": category,
            "reason": category_reason(category, f),
            "records": int(f.get("records") or 0),
            "present": int(f.get("present") or 0),
            "missing": int(f.get("missing") or 0),
            "unverified": int(f.get("unverified") or 0),
            "conflicting": int(f.get("conflicting") or 0),
            "outstanding": outstanding(f),
            "coverage_pct": f.get("coverage_pct", 0.0),
            "proven_pct": f.get("proven_pct", 0.0),
        })
    rows.sort(key=lambda r: (-r["outstanding"], r["field"]))
    return rows


def can_attempt(gap: dict, *, phase: int) -> tuple[bool, str]:
    """Whether `phase` may be attempted on this gap, and why not if it may not.

    Returns (allowed, reason). The reason is empty when allowed, and is a
    user-facing sentence otherwise — every refusal names its cause, because
    "nothing happened" is the single most confusing possible response to a
    request someone made deliberately.
    """
    state = str(gap.get("state") or "open")
    if state == "refused":
        return False, (gap.get("reason")
                       or "this gap was refused as unsafe to attempt, and refusing is final")
    if state == "resolved":
        return False, (gap.get("reason")
                       or "this gap is already closed; nothing outstanding against the field")
    if state == "exhausted":
        return False, (gap.get("reason")
                       or "this gap has already been through every phase and the "
                          "sources do not answer it")
    if int(gap.get("attempts") or 0) >= MAX_ATTEMPTS:
        return False, (f"this gap has already been attempted "
                       f"{MAX_ATTEMPTS} times, which is the limit")
    reached = int(gap.get("phase") or 0)
    if phase <= reached:
        return False, (f"this gap has already been attempted through phase {reached}; "
                       f"phase {phase} would repeat it")
    return True, ""


def record_attempt(gap: dict, *, phase: int, stats: dict | None = None,
                   error: str = "") -> dict:
    """A gap after an attempt at `phase` finished, whatever the outcome.

    `phase` advances whenever the phase actually *ran*, not whenever it found
    something. A phase-2 attempt that searched for four pages and extracted
    nothing still reached phase 2 — that is what makes a repeat unnecessary, and
    recording it as phase 1 would invite the same search forever.

    `error` is kept out of the state decision on purpose: a network failure is
    not evidence that the sources do not answer, so it advances the phase but
    leaves the gap `open` with the error recorded for the next attempt to weigh.
    Collapsing "we could not reach the internet" into "this field does not exist"
    is the failure mode that would make the whole table lie.
    """
    reached = max(int(gap.get("phase") or 0), int(phase))
    merged = {**(gap.get("stats") or {}), **(stats or {})}
    out = {
        **gap,
        "phase": reached,
        "attempts": int(gap.get("attempts") or 0) + 1,
        "stats": merged,
        "last_error": error or "",
    }
    if error:
        out["state"] = "open"
        out["reason"] = f"the last attempt failed: {error}"
        return out

    filled = int(merged.get("cells_written") or 0)
    out["state"] = "resolved" if filled else "exhausted"
    out["reason"] = (
        "" if filled else
        f"phase {reached} ran and found no value for this field; the sources "
        f"available do not answer it"
    )
    if filled:
        out["resolved_at"] = "now"
    return out


def refusal(gap: dict, reason: str) -> dict:
    """A gap marked unsafe to attempt. Terminal, and no attempt is recorded.

    Note that `attempts` and `phase` are untouched: refusing is not trying, and
    a row that said otherwise would report work that never happened.
    """
    return {**gap, "state": "refused", "reason": reason, "last_error": ""}


def next_phase(gap: dict) -> int | None:
    """The next phase worth attempting on this gap, or None if there is none.

    Phase 0 means nothing has been tried, so phase 1 comes first — it costs no
    external requests and can close a gap on its own. Only a gap that survived
    phase 1 is a candidate for searching the web, which is why this is a
    strict ladder rather than a preference.
    """
    reached = int(gap.get("phase") or 0)
    # The ladder is strict: phase 1 before phase 2, always. Only a gap that
    # survived re-reading the stored pages is a candidate for searching the web.
    nxt = PHASE_SEARCH if reached >= PHASE_STORED else PHASE_STORED
    allowed, _reason = can_attempt(gap, phase=nxt)
    return nxt if allowed else None


def sync(store, dataset_id: str, coverage: dict) -> list[dict]:
    """Reconcile a dataset's derived gaps against the persisted ones.

    The derived view is the truth about what is missing *now*; the persisted rows
    are the truth about what has been *tried*. Reconciling them has to respect
    both, and the interesting case is where they disagree.

    * A field that is no longer a gap becomes `resolved` — but only if it was
      already resolved or attempted. A gap that simply vanished because nobody had
      scanned it yet must not be recorded as fixed.
    * A field that is a gap again keeps its `phase` and `attempts`. Re-deriving
      counts must not reset the record of past work, or a dataset that is
      re-scanned hourly would forget it had already tried a field and would run
      the same search forever.
    * A gap that reappears after being `resolved` returns to `open` at the phase
      it had reached. The evidence that once filled it is gone — a field can lose
      its value when a record is replaced — and reporting it as closed would hide
      real missing data.

    Returns the reconciled rows, each carrying the derived counts merged over the
    persisted attempt history.
    """
    existing = {}
    lister = getattr(store, "list_gaps", None)
    if callable(lister):
        for row in lister(dataset_id) or []:
            name = str(row.get("field") or "")
            if name:
                existing[name] = row

    derived = {g["field"]: g for g in derive(coverage)}
    out: list[dict] = []

    for name, gap in derived.items():
        prev = existing.get(name) or {}
        was_resolved = str(prev.get("state") or "") == "resolved"
        merged = {
            "id": prev.get("id") or "",
            **gap,
            "phase": int(prev.get("phase") or 0),
            "attempts": int(prev.get("attempts") or 0),
            "stats": dict(prev.get("stats") or {}),
            "last_error": str(prev.get("last_error") or ""),
            "resolved_at": prev.get("resolved_at") or "",
            # A gap that reappears is open again, whatever it was before. Keeping
            # `resolved` here would report a field as closed while it is empty.
            "state": "open" if (was_resolved or not prev) else str(prev.get("state") or "open"),
        }
        # A recorded phase only stays true if the gap has not been fixed since. If
        # it was filled and has gone empty again, the old attempts describe a
        # different state of the data.
        if was_resolved:
            merged["phase"] = 0
            merged["attempts"] = 0
            merged["stats"] = {}
        out.append(merged)

    for name, prev in existing.items():
        if name in derived:
            continue
        # No longer a gap. Only mark it resolved if something had actually been
        # attempted; otherwise this is the first time anyone looked, and closing
        # it would credit a fix that never happened.
        if int(prev.get("attempts") or 0) > 0 and str(prev.get("state") or "") == "open":
            closed = {**prev, "state": "resolved", "reason": "",
                      "resolved_at": prev.get("resolved_at") or "now"}
            _write(store, dataset_id, name, closed)
            out.append(closed)
        else:
            out.append(prev)

    out.sort(key=lambda r: (-int(r.get("outstanding") or 0), str(r.get("field") or "")))
    for row in out:
        _write(store, dataset_id, str(row.get("field") or ""), row)
    return out


def _write(store, dataset_id: str, field: str, gap: dict) -> None:
    """Persist one gap, tolerating an adapter without the gap surface.

    The persistence test asserts both adapters expose the same methods, so this
    guard is belt-and-braces: a third adapter, or a test double that predates
    007, should degrade to "gaps are derived but not tracked" rather than take
    down a coverage read. Losing the history is a real loss and the caller can
    see it — `tracked` says so — but it is better than a 500.
    """
    writer = getattr(store, "upsert_gap", None)
    if callable(writer):
        writer(dataset_id, field, gap)


def tracked(store) -> bool:
    """Whether this adapter can persist gap history at all."""
    return callable(getattr(store, "upsert_gap", None))


def summary(gaps: list[dict]) -> dict:
    """Counts by category and state, for the coverage view's header.

    Carries `attemptable` explicitly — the number a person actually wants, which
    is "how much of this can still be worked on" rather than "how many gaps
    exist". A dataset with forty exhausted gaps has no queue, and reporting "40"
    would say the opposite.
    """
    by_category = {c: 0 for c in CATEGORIES}
    by_state = {s: 0 for s in STATES}
    attemptable = 0
    cells = 0
    for g in gaps:
        cat = str(g.get("category") or "")
        state = str(g.get("state") or "open")
        if cat in by_category:
            by_category[cat] += 1
        if state in by_state:
            by_state[state] += 1
        if state == "open" and int(g.get("attempts") or 0) < MAX_ATTEMPTS:
            attemptable += 1
        cells += int(g.get("outstanding") or 0)
    return {
        "gaps": len(gaps),
        "by_category": by_category,
        "by_state": by_state,
        "attemptable": attemptable,
        "outstanding_cells": cells,
        "max_attempts": MAX_ATTEMPTS,
    }
