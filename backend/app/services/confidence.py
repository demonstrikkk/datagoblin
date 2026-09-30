"""How a value was read, and how much that is worth.

A cell's `verification_status` answers one question: *was this value checked?* It
does not answer the one a reader actually has, which is *how did it get here?* A
value lifted straight out of a structured feed and a value inferred from prose
both come back `verified`, and a reader has no way to tell them apart.

So every cell carries a second attribute: `read_method`, the route the value
took to get in. That makes the difference between

    Proven            <- "found by AI, quote checked"
    Proven            <- "from the site's own data feed"

visible, and it is the difference between asking the reader for trust and giving
them a reason.

## Why the numbers are ordered the way they are

The ordering is the claim: a value is worth what its *route* could have got
wrong. A structured feed states the field directly, so the extraction cannot
invent anything; a regex has to interpret a convention the page did not mark up;
a language model has to read prose and decide.

    from the site's own data feed   0.99
    from the page's listing data     0.95
    read from the page              0.88
    worked out by arithmetic        0.85
    found in the text               0.80
    found by AI, quote checked      0.75
    from search results             0.70

`SEARCH` is last because a search engine's summary of a page is not the page. It
is someone's description of the page, and the reader cannot check it against the
source they came to verify.

## Derived is high on purpose

`derived` scores above regex and LLM even though it is last in the list, because
a derived value is arithmetic over values that were already read and verified.
It inherits its trustworthiness from its inputs and adds no new interpretation of
its own. Scoring it low would penalise a count that is more trustworthy than the
prose it was counted from.

## The verified gate is absolute, not a discount

`field_confidence` returns the prior or exactly `0`. A value whose evidence could
not be checked scores nothing, not "something less than nothing". A partial
score would be read as "mostly fine", and the whole point of the status is that
an unchecked value is a different kind of claim from a checked one.
"""
from __future__ import annotations

#: Every way a value can arrive. Ordered by how much the route could have got
#: wrong — with `derived` deliberately last, see the module docstring.
METHODS = (
    "api",
    "json_ld",
    "labelled",
    "dom",
    "derived",
    "regex",
    "llm",
    "search",
)

#: Prior trust per method. Deliberately not in the same order as `METHODS`, for
#: the reason given above: `derived` inherits from its inputs.
METHOD_PRIOR: dict[str, float] = {
    "api": 0.99,        # a structured feed states the field; nothing is inferred
    "json_ld": 0.95,    # schema.org in the page, same property the page publishes
    "dom": 0.92,        # read from the rendered page at a recorded locator
    "labelled": 0.90,   # the page printed `Label: value`; the quote is that line
    "derived": 0.85,    # arithmetic over values already read and verified
    "regex": 0.80,      # a convention the page did not mark up for us
    "llm": 0.75,        # prose read and interpreted, then quote-checked
    "search": 0.70,     # a search engine's summary of a page, not the page
}

#: What the reader is shown, in their words. The internal name never appears in
#: the UI; this table is the only place that mapping exists.
SOURCE_PHRASE: dict[str, str] = {
    "api": "From the site's data feed",
    "json_ld": "From the page's listing data",
    "labelled": "From a labelled line on the page",
    "dom": "Read from the page",
    "derived": "Worked out from other values",
    "regex": "Found in the text",
    "llm": "Found by AI, quote checked",
    "search": "From search results",
}

#: One-line gloss for each, used as a tooltip. Written as a statement about what
#: the route guarantees, because that is what a reader is deciding about.
SOURCE_HINT: dict[str, str] = {
    "api": "The source publishes this field directly, so nothing was inferred",
    "json_ld": "The page marks this field up as structured data",
    "labelled": "The page printed this field under its own label, and that line is the evidence",
    "dom": "Read from the rendered page at a recorded position",
    "derived": "Calculated from values that were themselves checked",
    "regex": "Matched by a pattern in the page's text",
    "llm": "Read by a language model, then checked against the page's own words",
    "search": "From a search result's summary rather than the page itself",
}

#: The three plain steps a reader sees, instead of a percentage. Thresholds are
#: placed on the priors rather than in round numbers, so a method's method
#: actually decides which step it lands on.
CERTAINTY_SURE = 0.90
CERTAINTY_LIKELY = 0.75

CERTAINTY_LABEL: dict[str, str] = {
    "sure": "Sure",
    "likely": "Likely",
    "check": "Check this",
}

CERTAINTY_HINT: dict[str, str] = {
    "sure": "Read straight from the source's own data",
    "likely": "Read from the page and checked against it",
    "check": "Found, but worth a look before you rely on it",
}

#: Map a value's route and whether its evidence checked, to a confidence.
def field_confidence(method: str, verified: bool) -> float:
    """The method's prior, or zero when the evidence could not be checked.

    An unknown method is treated as the weakest real route rather than as an
    error: a cell written by an older version of this code has no `read_method`,
    and defaulting it to `llm` would quietly raise its score.
    """
    if not verified:
        return 0.0
    return METHOD_PRIOR.get(method, METHOD_PRIOR["search"])


def certainty_of(confidence: float) -> str:
    """`sure` / `likely` / `check`, for the UI.

    Fails safe in one direction only: a missing or malformed confidence reads as
    `check`, never as `sure`.
    """
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "check"
    if c >= CERTAINTY_SURE:
        return "sure"
    if c >= CERTAINTY_LIKELY:
        return "likely"
    return "check"


def normalise_method(raw: object) -> str:
    """Coerce whatever arrived into one of `METHODS`, without raising.

    Cells written before this existed have no `read_method` at all. They were all
    produced by the language-model path, so `llm` is the honest default — but an
    unrecognised non-empty value is treated as the weakest route instead, since a
    method nobody recognises is a method nobody can vouch for.
    """
    m = str(raw or "").strip().lower()
    if m in METHOD_PRIOR:
        return m
    if not m:
        return "llm"
    return "search"


def receipt(method: str, verified: bool) -> dict:
    """Everything a reader needs about how one value was read, in one object."""
    m = normalise_method(method)
    confidence = field_confidence(m, verified)
    level = certainty_of(confidence)
    return {
        "read_method": m,
        "method_label": SOURCE_PHRASE.get(m, SOURCE_PHRASE["search"]),
        "method_hint": SOURCE_HINT.get(m, ""),
        "confidence": round(confidence, 2),
        "certainty": level,
        "certainty_label": CERTAINTY_LABEL[level],
        "certainty_hint": CERTAINTY_HINT[level],
    }
