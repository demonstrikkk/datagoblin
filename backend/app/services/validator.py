"""Validate: substring pre-check (cheap) -> reference gate -> Jev-B -> policy -> wrap.

Status precedence: substring-fail => unverified (never stored guess);
reference_id unknown => unverified; Jev NOT_SUPPORTED => unverified;
conflict (Jev-C) => conflicting with both provenances.

wrap_record pins each verified quote to character offsets in the preserved
source text and carries the citation reference_id — the machine-checkable
half of the proof-carrying record.
"""
import datetime
import re

from app.core.errors import AppError
from app.core.logging import log
from app.providers.decision import jev
from app.providers.llm import zen
from app.schemas.evidence import locate_quote, normalize_for_match
from app.services.normalizer import is_placeholder

_WS = re.compile(r"\s+")
#: Currency symbols and magnitude suffixes a scraped number realistically carries.
_NUM_CLEAN = re.compile(r"[,$€£¥%\s ~]")
_NUM_SUFFIX = re.compile(r"(?i)\s*([kmbt])?\s*(illion)?$")
_NUM_RE = re.compile(r"^-?\d*\.?\d+([kmbt])?$", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def _norm(s: object) -> str:
    """Whitespace/case fold for VALUE keys, not for quotes.

    Quote matching uses `evidence.normalize_for_match`, which folds inline
    markdown as well. The two are deliberately separate: a field name has no
    emphasis markers to fold, and folding them here would change what counts as
    the same key.
    """
    return _WS.sub(" ", str(s or "").strip().lower())


def type_ok(value: object, ftype: str) -> bool:
    """Does the value actually match the field's declared type?

    `required` and `type` used to be discarded on the grounds that "type
    coercion lives in normalize" - but normalize is driven by the field *name*,
    not its declared type, and it records a `normalized` sub-object rather than
    replacing the value. Nothing anywhere coerced a value to its declared type,
    so a string where a number was declared sailed through as verified. This is
    the check that was missing.

    Note that passing this check does not change the stored value: a field
    declared `number` whose extracted value is "1,200" is stored as the string
    "1,200", because the source said "1,200" and rewriting it to 1200 would
    assert a precision the page did not. The type gate is a *veto* — it rejects
    prose where a number was declared — and not a transform. Callers that need a
    number read `value` through their own parser.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return True  # emptiness is `required`'s business, not the type's
    t = (ftype or "string").strip().lower()
    if t in ("string", "text", ""):
        return True
    if t == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        # Scraped numbers arrive as "$2.4B", "1,200", "45%", "~30". Accept those
        # shapes; reject prose. An earlier version was strict enough to call
        # "$2.4B" a type error, which would have dropped real records at the
        # gate rather than catching anything.
        s = _NUM_CLEAN.sub("", str(value)).strip()
        s = re.sub(r"(?i)(illion)$", "", s)
        return bool(_NUM_RE.match(s))
    if t == "boolean":
        return isinstance(value, bool) or str(value).strip().lower() in (
            "true", "false", "yes", "no", "0", "1")
    if t == "array":
        return isinstance(value, (list, tuple))
    if t == "date":
        s = str(value).strip()
        if re.match(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?$", s):
            return True
        low = s.lower()
        if not any(m in low for m in _MONTHS):
            return False
        # A month name plus a year, with or without a day: "January 2024",
        # "Jan 2024", "January 31, 2024", "31 January 2024".
        return bool(re.search(r"\b(19|20)\d{2}\b", s))
    if t == "url":
        return bool(re.match(r"^https?://\S+$", str(value).strip(), re.I))
    return True


def normalize_key(s: object) -> str:
    s = _norm(s)
    s = re.sub(r"\b(ltd|inc|llc|pvt|private|limited|corp|co)\b", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def reference_known(reference_id: str, references: str) -> bool:
    """A claimed <n> citation must exist in the page's References map.

    An empty `reference_id` is allowed (uncited sources), and so is a non-empty
    one on a page that carries no References block at all.

    That second case is the one that matters. `references` is populated in
    exactly one place — the Crawl4AI rung of the fetcher, which is the only
    renderer that emits a `## References` map. The plain `http` and `jina`
    rungs never set it, so `page.get("references", "")` is `""` for every page
    they fetch. Failing closed on a *missing* map therefore nulled any value the
    model tagged with a `<n>` marker, on a page where no such marker could
    possibly have been checked — and a plain-text table has no citation spans at
    all, so the marker the model emitted was invented to begin with.

    In one live dataset that destroyed 53 of 78 `financial_year` cells, all of
    them from `http`-fetched URLs, while the identical page fetched by Crawl4AI
    produced none. The value was extracted, the quote was verbatim, and the cell
    was blank because a gate was checking a document that was not there.

    So the gate is only meaningful when the page actually carries the apparatus.
    With a References block present, an unknown marker is still rejected.
    """
    ref = (reference_id or "").strip()
    if not ref or not (references or "").strip():
        return True
    return ref in references


class JudgeBudget:
    """Bounds judge calls within one run.

    Verification asks a judge about every field of every extracted record, and
    the record count is whatever the extractor found - a live run hit 728, which
    is thousands of round trips and a certain budget overrun. Once the cap is
    spent the answer is JUDGMENT_UNAVAILABLE: the value is still kept, it is
    just never counted as verified, which is the honest description of a check
    that never ran.

    `limit=0` means ZERO judge calls, not unlimited.

    It used to mean unlimited, because `exhausted` was `self.limit > 0 and ...`.
    That is the worst possible reading to get wrong on a metered endpoint:
    `POST /api/datasets/{id}/rejudge` validates `judge_budget` as `0 <= n <= N`,
    so `judge_budget: 0` was a documented, accepted request meaning "judge
    nothing" - and it produced unlimited judge calls and an unbounded bill. A
    caller asking for zero checks must get zero checks.

    Genuinely unbounded callers pass `unlimited=True`, which is opt-in and
    greppable. Exactly one caller wants it: the module-level `JUDGE_BUDGET`
    fallback used by `verify_field` when no run has installed a real cap.
    """

    __slots__ = ("limit", "used", "unlimited")

    def __init__(self, limit: int, *, unlimited: bool = False) -> None:
        limit = int(limit or 0)
        if limit < 0:
            raise ValueError(f"judge budget must not be negative: {limit}")
        self.limit = limit
        self.used = 0
        self.unlimited = bool(unlimited)

    @property
    def exhausted(self) -> bool:
        if self.unlimited:
            return False
        return self.used >= self.limit

    def spend(self) -> bool:
        """Reserve one judge call. False means the cap is spent - do not call."""
        if self.exhausted:
            return False
        self.used += 1
        return True

    @property
    def remaining(self) -> int:
        if self.unlimited:
            return -1  # unbounded; -1 is the sentinel the run reports use
        return max(0, self.limit - self.used)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        bound = "unlimited" if self.unlimited else str(self.limit)
        return f"JudgeBudget({bound}, used={self.used})"


#: Per-run judge budget for callers that never install one. `execute_run` builds
#: its own from `settings.RUN_MAX_JUDGE_CALLS` and passes it explicitly, so this
#: only serves ad-hoc and test invocations of `verify_field`.
#:
#: Unbounded on purpose, and the only place that is so: a bare `verify_field`
#: call with no budget should verify, not silently return JUDGMENT_UNAVAILABLE
#: for every field. Every real path supplies a real cap.
JUDGE_BUDGET = JudgeBudget(0, unlimited=True)


#: Routes whose value is proved by the deterministic gates, not by a model.
#:
#: `json_ld`, `labelled`, `api` and `selectors` all carry the page's own line as
#: the quote, so confirming that line is on the page *is* the verification. A
#: model asked whether it agrees adds cost and latency and cannot make the answer
#: more true.
_DETERMINISTIC_ROUTES = frozenset({"json_ld", "labelled", "api", "selectors"})


async def verify_field(value: object, quote: str, source_text: str,
                       required: bool, ftype: str, reference_id: str = "",
                       references: str = "", budget: JudgeBudget | None = None,
                       norm_source: str | None = None,
                       judged_by_construction: bool = False) -> tuple[object | None, str]:
    """Grade one field. Returns (value_to_store, status).

    `judged_by_construction` short-circuits the model: the caller asserts this
    field came from a deterministic rung whose quote is the page's own line, so
    the gates below have already done the verification. It exists because the
    ladder was avoiding extraction calls but not judge calls, and that asymmetry
    is what got a real run throttled (HTTP 429) after reading every page.

    Order matters: the cheap deterministic gates come first, the judge last,
    because the judge is the only step that can be unavailable and the only one
    that costs a round trip. Statuses:

      unverified           kept, but nothing supports it — either no quote was
                           supplied, or a quote was supplied and is not on the
                           page, or the judge said NOT_SUPPORTED. Note the
                           first and second of those return the *value*: an
                           unsupported claim is not an absent one, and
                           nulling it reported certainty the system does not
                           have.
      judgment_unavailable the quote IS in the page, but nothing judged it
      verified             judged, or deduced from the quote itself

    `required` is accepted for call-site symmetry with the schema and is not
    read here. Dropping a record for a missing required field is the runner's
    decision, because it is a decision about the whole record rather than about
    one cell.
    """
    if is_placeholder(value):
        # `—`, `”`, `N/A` and friends mean "not here". Storing one produces a
        # cell that is present, unverified, and shows a punctuation mark where a
        # reader expects either a value or a dash — it reads as corruption
        # rather than as absence. Absence is already expressible, so it is what
        # gets stored. Unverified-but-real values are untouched: this is not a
        # shortcut for dropping values the judge was not reached for.
        return None, "unverified"
    if not type_ok(value, ftype):
        return None, "unverified"

    # A value with no quote is a different failure from a quote that is not on
    # the page, and they used to be fused into one condition that nulled both.
    #
    # `wrap_record` looks the quote up by exact field-name match, so a model
    # that returns a value but omits its evidence item lands here with an empty
    # quote — and the value was discarded with no judge, no fallback and no
    # status distinguishing it from a claim contradicted by the source. In one
    # live dataset that was 117 cells across `valuation` and `financial_year`,
    # every one of them an extraction that was correct and a prompt-following
    # failure rather than an absence.
    #
    # Kept as `unverified` rather than nulled, which is what already happens
    # when the judge is unreachable below: the quote genuinely is not there, so
    # the value is not proven, but "not proven" is not "absent" and the UI can
    # already say so. Nulling it was the system reporting certainty it did not
    # have — the empty cell claimed the source did not carry the value, when the
    # truth was that the model did not cite it.
    if not quote:
        return value, "unverified"

    # The normalised page is passed in when the caller has one. `_norm` runs a
    # whitespace regex plus lowercasing over the WHOLE page, and this is per
    # field, so on a 50 kB page with ten fields the same page was normalised ten
    # times before the judge was ever consulted.
    if normalize_for_match(quote) not in (
            norm_source if norm_source is not None
            else normalize_for_match(source_text)):
        # A quote that was supplied and cannot be found is a real failure: the
        # value is either hallucinated or attributed to the wrong sentence.
        return None, "unverified"
    if not reference_known(reference_id, references):
        return None, "unverified"
    # The budget is passed in, not read from the module global. As a global it
    # was shared by every concurrent run in the process: run B overwrote run
    # A's counter on startup, so one run's cap counted against another's and
    # the docstring's claim of "per-run" was simply untrue. The module-level
    # default remains only for callers that never had a budget to give.
    # The deterministic verdict is resolved *before* the budget is charged.
    #
    # It used to be charged first: `budget.spend()` ran, then
    # `evidence_verification` returned SUPPORTED from a substring test without
    # calling anything. So the per-run cap of 400 was spent on arithmetic, and a
    # real judgement could be refused as `judgment_unavailable` while the run
    # reported its budget consumed. A cap exists to bound the expensive thing.
    quick = jev.deterministic_verdict(str(value), quote, source_text)
    if quick is not None:
        # Same two outcomes the judge path would have produced, for free.
        if quick["judgment"] == "SUPPORTED":
            return value, "verified"
        return None, "unverified"

    # Every gate above has passed: the quote is on the page, it is attributable,
    # and the value is consistent with it. For a deterministic rung that is the
    # whole verification - the quote *is* the line the page printed - so the model
    # is not asked. Placed after the deterministic verdict, not before, so a rung
    # whose value is not actually in its own quote is still nulled.
    if judged_by_construction:
        return value, "verified"

    budget = budget if budget is not None else JUDGE_BUDGET
    if budget.exhausted:
        # The cap is spent. Keep the value - the quote genuinely is on the page
        # - but do not claim a ruling that never happened.
        return value, "judgment_unavailable"
    if not budget.spend():
        return value, "judgment_unavailable"
    try:
        verdict = await jev.evidence_verification(str(value), quote, source_text)
    except zen.JevRateLimited:
        # A throttled or unreachable judge must never end the run.
        #
        # `evidence_verification` raises `JevRateLimited` after its retries, and
        # that exception propagated out of here and failed the whole run - so a
        # provider 429 discarded every record in a run that had already fetched
        # and extracted all of them. Throttling is a fact about the provider, not
        # about the evidence: the value stays, unproven, and the run continues.
        return value, "rate_limited"
    except AppError as _exc:
        # A typed provider or dependency failure is NOT throttling. Reporting it
        # as `rate_limited` was the old behaviour, and it is a lie that costs
        # money twice over: the run looks like it should be retried later rather
        # than fixed now, and the real cause (a bad key, a dead endpoint, a
        # malformed request) stays invisible in the counter.
        log.error("judge call failed",
                  extra={"data": {"code": _exc.code, "error": str(_exc)[:200]}})
        return value, "judgment_unavailable"
    except Exception as _exc:  # noqa: BLE001
        # Genuinely unexpected. Same reasoning as the run must not die, but this
        # is logged loudly because it means a code path we did not anticipate.
        log.error("judge call raised unexpectedly",
                  extra={"data": {"error": f"{type(_exc).__name__}: "
                                           f"{str(_exc)[:200]}"}})
        return value, "judgment_unavailable"
    judgment = verdict.get("judgment")
    if judgment == "NOT_SUPPORTED":
        return None, "unverified"
    if judgment == "RATE_LIMITED":
        # Throttled, not absent. Kept with its value and reported as its own
        # status so a busy run is visible as throttled rather than looking like
        # the evidence was never good enough.
        return value, "rate_limited"
    if judgment == "JUDGMENT_UNAVAILABLE":
        # Keep the value - the quote genuinely is on the page - but never count
        # it as verified. Reporting this as verified is what made a dead judge
        # look like a passing one.
        return value, "judgment_unavailable"
    if judgment == "SUPPORTED":
        return value, "verified"
    # UNCERTAIN, or anything a judge returned that we cannot interpret: the
    # value stays, but an unproven claim is not a verified one.
    return value, "judgment_unavailable"


async def wrap_record(fields_spec: list[dict], raw: dict, source_text: str,
                      source_url: str, source_title: str, now: str = "",
                      references: str = "", page_id: str = "",
                      budget: JudgeBudget | None = None) -> dict:
    from app.schemas.run import RecordRow  # local import: schemas must not import services

    ts = now or (datetime.datetime.utcnow().isoformat() + "Z")
    wrapped: dict = {}
    # Normalised once for every field below. See verify_field: this used to be
    # recomputed per field, scanning the entire page each time.
    norm_source = normalize_for_match(source_text)
    for f in fields_spec:
        name = f["name"]
        ev = next((e for e in raw.get("evidence", []) if e.get("field") == name), {})
        # A field a deterministic rung filled carries its route on its evidence
        # item. It is read here so the cell records how the value actually
        # arrived, rather than being stamped `llm` because the model was still
        # asked about the record's other fields.
        route = str(ev.get("read_method") or "")
        quote = ev.get("quote", "")
        ref_id = ev.get("reference_id", "")
        # A field a deterministic rung filled is not asked of the judge.
        #
        # The ladder's whole purpose is to avoid model calls, and it was only
        # avoiding half of them: extraction was rung-first but *judging* still
        # went to the model for every field, so a page whose identity came free
        # from JSON-LD still cost a judge call per cell. That is the call pattern
        # that got a real run throttled to death (HTTP 429) after it had already
        # read every page.
        #
        # Nothing is lost by skipping it. A rung's quote *is* the line the page
        # printed, so the deterministic gate that confirms the quote is present
        # is a stronger check than asking a model whether it agrees.
        if route in _DETERMINISTIC_ROUTES:
            v, st = await verify_field(raw.get("fields", {}).get(name), quote,
                                       source_text, f.get("required", False),
                                       f.get("type", "string"), ref_id, references,
                                       budget=budget, norm_source=norm_source,
                                       judged_by_construction=True)
        else:
            v, st = await verify_field(raw.get("fields", {}).get(name), quote,
                                       source_text, f.get("required", False),
                                       f.get("type", "string"), ref_id, references,
                                       budget=budget, norm_source=norm_source)
        # Settled through the one function permitted to grant `verified`, so the
        # status and the locator can never disagree. See `settle`.
        cell = settle(
            {"value": v, "verification_status": st,
             # page_id makes the claim re-checkable: the offsets address stored
             # text rather than a string that only existed in this process.
             "source": {"url": source_url, "title": source_title,
                        "quote": quote, "retrieved_at": ts,
                        "reference_id": ref_id,
                        "page_id": page_id or ev.get("page_id", ""),
                        "content_hash": ev.get("content_hash", "")}},
            status=st, quote=quote, source_text=source_text)
        # How the value was read, not just whether it was checked. Absent on a
        # raw extraction, so it defaults to `llm` — which is what it was, since
        # every cell that predates this went through the model. Built from the
        # settled status, so a cell `settle` demoted cannot keep a confident
        # receipt.
        wrapped[name] = {**cell, **_receipt_for(
            raw, name, cell["verification_status"], route)}
    # Shape-drift guard: every wrapped row must satisfy the RecordRow contract.
    # Return the inner fields dict (callers expect {name: provenance-field}).
    return RecordRow.model_validate({"fields": wrapped}).model_dump()["fields"]


def _receipt_for(raw: dict, field: str, status: str, route: str = "") -> dict:
    """The `read_method` receipt for one cell.

    The route is authoritative for *where* the value came from — a deterministic
    rung filled it, and that is a fact about provenance the model output cannot
    supply. The route says nothing about *whether the evidence was checked*, and
    this used to let it say so twice over:

      * it preserved whatever receipt the extractor had stamped, which computed
        `verified` from the route name before the gate had run;
      * and where there was no stamped receipt it recomputed `verified` as
        `status == "verified" or method in {api, json_ld, labelled}`.

    Either way a cell the gate had just refused — quote not on the page, wrong
    type, a placeholder — kept `confidence: 0.99` and `certainty: "sure"` while
    its own `verification_status` read `unverified`. The row contradicted itself
    in the same breath, in the direction that flatters the data, and
    `field_confidence`'s own docstring calls that gate absolute: it returns the
    prior or exactly `0`.

    So `verified` is now the gate's answer and nothing else. A structured route
    that passes its gates still comes back `verified` — that promotion happens
    inside `verify_field`, after the quote and type checks — so nothing is lost.
    """
    from app.services import confidence as conf_svc

    cell = (raw.get("fields") or {}).get(field)
    method = ""
    if isinstance(cell, dict) and cell.get("read_method"):
        method = conf_svc.normalise_method(cell["read_method"])
    else:
        # No receipt on the evidence item: the model produced this one.
        method = conf_svc.normalise_method(route) if route else "llm"
    return conf_svc.receipt(method, verified=(status == "verified"))


#: Every status a cell may hold. Anything else arriving at `settle` is normalised
#: to `unverified` rather than stored, so an unrecognised value can never appear
#: in a dataset and leave a reader unable to interpret it.
_PROMOTABLE = frozenset({"verified", "unverified", "judgment_unavailable",
                         "rate_limited", "conflicting", "rejected"})


def has_locator(cell: dict) -> bool:
    """Does this cell carry offsets that resolved against stored text?

    A `source` with `start: None` is not a citation. It says a quote exists
    somewhere in a string nobody can now find, which is the state the locator's
    own docstring warns about: "a field is admitted as verified and then handed
    offsets of (None, None), which makes the citation unusable."
    """
    src = (cell or {}).get("source") or {}
    return src.get("start") is not None and src.get("end") is not None


def settle(cell: dict, *, status: str, quote: str = "",
           source_text: str = "", reason: str = "") -> dict:
    """Grant or withhold `verified`, and keep the locator and the status in step.

    **This is the only function in the codebase permitted to leave a cell
    `verified`.** `test_promotion_chokepoint.py` enforces that by parsing every
    module under `app/` and failing on a literal assignment anywhere else, so a
    new caller cannot quietly open a ninth door.

    `verified` takes two things, and historically only the first was checked:

      1. a support verdict -- does this passage establish this value
      2. a locator -- does the quote resolve, at a known position, against the
         exact text the store holds

    Passing (1) while failing (2) produces a cell that reads as proven and
    cannot be checked by anyone, including the product's own proof view. So (2)
    is now a precondition: an unlocatable quote is demoted to `unverified` and
    keeps the reason, rather than being stored as a claim with nothing behind it.

    Fails closed on every path. `settle` never raises and never returns a cell
    it could not justify.
    """
    out = dict(cell)
    src = dict(out.get("source") or {})
    src["start"], src["end"] = None, None

    if status == "verified":
        start, end = locate_quote(quote, source_text)
        if start is None or end is None:
            # The verdict stands but the citation does not. Demoting is the only
            # honest option: "verified" with no resolvable offsets is a claim no
            # reader and no later check can test.
            out["verification_status"] = "unverified"
            src["start"], src["end"] = None, None
            out["not_verified_because"] = reason or (
                "the evidence was supported but its quote does not resolve "
                "against the stored text, so there is nothing a reader could "
                "check")
        else:
            out["verification_status"] = "verified"
            src["start"], src["end"] = start, end
            # The stored quote becomes the text at the located offsets, so a
            # reader can slice the page and get exactly what is shown. The two
            # can differ: the locator's markdown pass matches a quote the model
            # de-emphasised (`Company: C.H. Robinson`) against the stored
            # `**Company:** C.H. Robinson`, and the span includes the markers.
            # Storing the model's spelling next to offsets that slice to
            # different text is the disagreement this module exists to prevent.
            # For an exact match this is a no-op.
            src["quote"] = source_text[start:end]
            out.pop("not_verified_because", None)
    else:
        # Fails closed on anything that is not a status: a missing or empty one
        # must not be stored as-is, because `Verification` is a closed set and an
        # unrecognised value would leave the cell claiming something no reader
        # can interpret. "unverified" is the least-committal true statement.
        out["verification_status"] = status if status in _PROMOTABLE else "unverified"
        if reason:
            out["not_verified_because"] = reason
    out["source"] = src
    return out


def adopt(cell: dict, other: dict, *, resolved: str = "") -> dict:
    """Take another cell's value **and its evidence**, keeping the status it earned.

    Used wherever a decision picks a rival: Jev-C's conflict triage and the
    manual resolver. Both used to write `verification_status = "verified"`
    outright, and both reasons were wrong in the same way — a decision about
    *which value is better* was recorded as *the evidence is sound*. Neither had
    re-located the quote, and neither had any support verdict for the rival.

    A decision is not evidence. So the adopted cell gets the rival's own status,
    and the rival's whole `source` verbatim, offsets included, rather than a
    three-key reconstruction that dropped them.

    A rival that carries `verified` but no resolvable locator is demoted: that
    combination is the state this whole change exists to stop being able to
    produce, so it is refused on the way in rather than discovered later.

    `resolved` records that a decision was made. It never confers status.
    """
    out = dict(cell)
    other = other or {}
    src = dict(other.get("source") or {})
    out["value"] = other.get("value")
    out["source"] = src

    status = str(other.get("verification_status") or "") or "unverified"
    if status == "verified" and not has_locator({"source": src}):
        status = "unverified"
        out["not_verified_because"] = (
            "this value was adopted from a rival marked verified, but the "
            "rival carried no offsets, so its quote cannot be checked against "
            "the stored page")
    out["verification_status"] = status
    if resolved:
        out["resolved"] = resolved
    if status == "verified":
        out.pop("not_verified_because", None)
    # The receipt described the old value's provenance; the adopted evidence has
    # its own, or none. Recomputing it is `wrap_record`'s job, which runs before
    # any of this.
    for key in ("read_method", "method_label", "method_hint", "confidence",
                "certainty", "certainty_label", "certainty_hint"):
        out.pop(key, None)
    return out
