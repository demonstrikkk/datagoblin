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

from app.providers.decision import jev
from app.schemas.evidence import locate_quote
from app.services.normalizer import is_placeholder

_WS = re.compile(r"\s+")
#: Currency symbols and magnitude suffixes a scraped number realistically carries.
_NUM_CLEAN = re.compile(r"[,$€£¥%\s ~]")
_NUM_SUFFIX = re.compile(r"(?i)\s*([kmbt])?\s*(illion)?$")
_NUM_RE = re.compile(r"^-?\d*\.?\d+([kmbt])?$", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def _norm(s: object) -> str:
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
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit or 0))
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.used >= self.limit

    def spend(self) -> bool:
        if self.exhausted:
            return False
        self.used += 1
        return True


#: Per-run judge budget. Replaced by execute_run via reset_judge_budget().
JUDGE_BUDGET = JudgeBudget(0)


async def verify_field(value: object, quote: str, source_text: str,
                       required: bool, ftype: str, reference_id: str = "",
                       references: str = "", budget: JudgeBudget | None = None,
                       norm_source: str | None = None) -> tuple[object | None, str]:
    """Grade one field. Returns (value_to_store, status).

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
    if _norm(quote) not in (norm_source if norm_source is not None
                            else _norm(source_text)):
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

    budget = budget if budget is not None else JUDGE_BUDGET
    if budget.exhausted:
        # The cap is spent. Keep the value - the quote genuinely is on the page
        # - but do not claim a ruling that never happened.
        return value, "judgment_unavailable"
    if not budget.spend():
        return value, "judgment_unavailable"
    verdict = await jev.evidence_verification(str(value), quote, source_text)
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
    norm_source = _norm(source_text)
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
        v, st = await verify_field(raw.get("fields", {}).get(name), quote,
                                   source_text, f.get("required", False),
                                   f.get("type", "string"), ref_id, references,
                                   budget=budget, norm_source=norm_source)
        start, end = locate_quote(quote, source_text) if st == "verified" else (None, None)
        # How the value was read, not just whether it was checked. Absent on a
        # raw extraction, so it defaults to `llm` — which is what it was, since
        # every cell that predates this went through the model.
        wrapped[name] = {"value": v, "verification_status": st,
                         # page_id makes the claim re-checkable: the offsets
                         # above address stored text rather than a string that
                         # only existed in this process's memory.
                         "source": {"url": source_url, "title": source_title,
                                    "quote": quote, "retrieved_at": ts,
                                    "reference_id": ref_id,
                                    "page_id": page_id or ev.get("page_id", ""),
                                    "content_hash": ev.get("content_hash", ""),
                                    "start": start, "end": end},
                         **_receipt_for(raw, name, st, route)}
    # Shape-drift guard: every wrapped row must satisfy the RecordRow contract.
    # Return the inner fields dict (callers expect {name: provenance-field}).
    return RecordRow.model_validate({"fields": wrapped}).model_dump()["fields"]


def _receipt_for(raw: dict, field: str, status: str, route: str = "") -> dict:
    """The `read_method` receipt for one cell, taken from how it was extracted.

    A field a deterministic rung filled carries its route on the evidence item,
    and that route is authoritative: it is how the value actually got here. Only
    a field the model produced falls back to `llm`, which is correct because
    every cell that predates this went through the model.

    The route changes where a value came from and never whether it was checked. A
    structured or labelled route is verified by construction — its quote is the
    line the page printed — and everything else carries its quote to the same gate
    the model's output always went through.
    """
    from app.services import confidence as conf_svc

    cell = (raw.get("fields") or {}).get(field)
    if isinstance(cell, dict) and cell.get("read_method"):
        return {k: cell[k] for k in ("read_method", "method_label", "method_hint",
                                     "confidence", "certainty", "certainty_label",
                                     "certainty_hint") if k in cell}
    method = conf_svc.normalise_method(route) if route else "llm"
    verified = status == "verified" or method in {"api", "json_ld", "labelled"}
    return conf_svc.receipt(method, verified=verified)
