"""Is this page about what the run was actually asked?

Added after a live run answered "sponsors for hackathons in New Delhi" with
records citing `aws.amazon.com/transform/` and `/free/offers/`. Those pages
carry a real quote about a real company, so every existing check passed and the
cells were marked `verified` -- the pipeline had no way to distinguish "this
page says the thing" from "this page is about the thing".

Evidence checking answers whether a value is faithful to its source. It cannot
answer whether the source is about the question. Those are different failures
and conflating them is how a dataset ends up full of confident, well-sourced,
completely irrelevant rows.

Deliberately a floor, not a judgement
-------------------------------------
Relevance is semantic and this is a keyword gate, so it is built to be wrong in
only one direction: it must let through anything plausibly on-topic and drop
only what is clearly off-topic. A stricter gate would reject
`aws.amazon.com/events/sponsorship` for not containing the word "hackathon" --
a genuinely relevant page -- and losing real results is worse than carrying a
few irrelevant ones, because the user can see an irrelevant row and cannot see
a missing one.

So the bar is one hit on a *core subject term*, not on every term. A page about
AWS's transform product mentions neither "hackathon" nor "sponsor" and is
dropped; a page about AWS sponsorship mentions "sponsorship" and is kept.

The entity requirement, and why "anywhere on the page" was not enough
-------------------------------------------------------------------
The floor above let one page through that cost a whole run. Asked for "European
logistics companies with their chief executive and their headquarters city", a
live run kept `digitaldefynd.com/IQ/famous-ceo-europe` -- a listicle of 50
*famous European CEOs* -- because it contains "european" and "executive". Every
CEO it yielded was for the wrong entity: LVMH, SAP, GSK, Airbus, HSBC. The one
page actually about logistics CEOs produced nothing (a separate bug), so the
dataset was 100% off-target.

`plan["entity"]` says what the rows are meant to be, and this gate ignored it
entirely. Requiring an entity term to appear *somewhere* is not enough either:
the off-topic page mentions "logistic" -- twice, in passing, at character 22,337
of 67,827 ("fast fashion logistics"). The on-topic pages mention it 80 and 149
times, starting at character 5 and 8.

So the requirement is structural, not a frequency count: when the plan names an
entity with usable terms, a page is kept only if that term appears in the page's
**URL slug or its opening** -- a page about X announces X in its title and lede.
Measured separation on the live run: 5 and 8 versus 22,337.

Every drop is reported with its reason rather than happening silently, because a
gate that quietly discards pages is indistinguishable from a gate that is
wrong, and the second one is the dangerous one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Dropped from subject terms: words that carry no topical signal, so counting
#: them would let any page pass.
_STOPWORDS = frozenset("""
a an and are as at be been being by can could do does for from find get give
have how i in into is it its list me my need needs of on or our please provide
search show tell that the their them then there these they this to want was
were what when where which who whom whose why will with would you your
about also any all both each few more most other some such only own same so
than too very can just don should now organisation organizations company
companies organisation's llm answer question details information
new together including include list lists show me
""".split())

#: Words that describe the *shape* of the answer rather than its subject. They
#: appear in almost every prompt and would make the gate trivially pass.
_SHAPE_WORDS = frozenset("""
terms conditions policy policies agreement faq details guideline guidelines
terms-and-conditions
""".split())

#: How much of the page counts as its "opening" for the entity requirement --
#: the title and lede, where a page announces its own subject. Measured on the
#: live run: the two on-topic pages name the entity at character 5 and 8, the
#: off-topic page first mentions it at 22,337 of 67,827. 1500 sits an order of
#: magnitude clear of both, so it is not a knife-edge tuned to one run.
_LEDE_CHARS = 1500


def entity_terms(plan: dict) -> list[str]:
    """The words that name *what each row is meant to be*.

    `plan["entity"]` answers "what kind of thing is a row?" -- here
    `logistics_company`. Folded the same way as subject terms, so "company" and
    "companies" fall out as stopwords and the signal word (`logistic`) survives.

    An entity that yields nothing usable (blank, or only stopwords) returns
    empty, and the gate falls back to the subject-floor rather than refusing
    every page.
    """
    return _terms_from([str(plan.get("entity") or "")])



@dataclass
class Verdict:
    """The gate's answer, and the evidence for it.

    `matched` is kept so a caller can explain the decision rather than just
    assert it. A drop with no reason is indistinguishable from a bug.
    """
    keep: bool
    reason: str = ""
    matched: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)


def _norm(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _stem(word: str) -> str:
    """Crude singularisation and suffix-folding.

    Deliberately not a real stemmer. The goal is that "sponsor", "sponsors",
    "sponsorship", "sponsored" and "partnership" all land near each other, not
    linguistics -- and a wrong stem costs a missed match, which the one-hit bar
    tolerates.

    The `ship` rule is here because its absence was a real bug: the goal yields
    `sponsor`, but the single most on-topic URL slug in the live run was
    `/events/sponsorship`. Without folding `ship`, the URL rescue could never
    fire for the page most worth rescuing, and it failed silently.
    """
    for suffix, keep in (("ship", ""), ("ations", "at"), ("ation", "at"),
                         ("ings", ""), ("ing", ""), ("ies", "y"),
                         ("ers", "er"), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) > 3:
            return word[: len(word) - len(suffix)] + keep
    return word


def subject_terms(plan: dict) -> list[str]:
    """The words that must plausibly appear on a page about this run's question.

    Taken from the **goal** first, and only topped up from the search queries
    when the goal is too thin to gate on.

    That ordering is not cosmetic. An LLM-written query carries field names --
    "hackathon sponsors New Delhi 2026 sponsorship *tiers*" -- and a field name
    is not a topic. Gating on it made `aws.amazon.com/free/` pass, because that
    page talks about the *free tier*. The goal is the question the user asked;
    the queries are the search strategy, and testing the strategy leaks the
    schema into what counts as relevant.
    """
    goal_terms = _terms_from([str(plan.get("goal") or "")])
    if len(goal_terms) >= 3:
        return goal_terms
    for t in _terms_from(str(q) for q in (plan.get("search_queries") or [])[:3]):
        if t not in goal_terms:
            goal_terms.append(t)
    return goal_terms


def _terms_from(parts) -> list[str]:
    seen: list[str] = []
    for part in parts:
        for raw in _norm(part).split():
            w = _stem(raw)
            if not w or len(w) < 3:
                continue
            if w in _STOPWORDS or raw in _STOPWORDS or w in _SHAPE_WORDS:
                continue
            # A bare year is not a topic.
            if raw.isdigit() and len(raw) == 4:
                continue
            if w not in seen:
                seen.append(w)
    return seen


def assess(plan: dict, text: object, url: str = "") -> Verdict:
    """Decide whether a page is worth spending an extraction call on.

    If the plan names an entity, the page must carry that entity in its URL slug
    or its opening -- a page about logistics companies says "logistics" up
    front. Otherwise the subject-term floor applies.

    Returns keep=True when the page passes. Empty text keeps the page: a page
    with no extractable text will fail downstream anyway, and a gate that rejects
    on emptiness just moves the failure somewhere less informative.
    """
    terms = subject_terms(plan)
    ents = entity_terms(plan)
    if not terms and not ents:
        # No usable subject means the run's question is too vague to gate on.
        # Passing everything is the honest answer; guessing would drop real rows.
        return Verdict(keep=True, reason="no-subject-terms", terms=[])

    body = _norm(text)
    if not body:
        return Verdict(keep=True, reason="empty-page", terms=terms or ents)

    slug = _stem_all(url)

    if ents:
        lede = _stem_all(body[:_LEDE_CHARS])
        ent_body = [t for t in ents if t in lede]
        ent_slug = [t for t in ents if t in slug]
        if ent_body or ent_slug:
            return Verdict(keep=True, reason="entity", matched=ent_body or ent_slug,
                           terms=ents)
        # The page is about something else. Saying so, with the entity that was
        # sought, is the difference between a traceable drop and a silent one.
        return Verdict(keep=False, reason="off-entity", matched=[], terms=ents)

    body_stems = {_stem(w) for w in body.split()}
    matched = [t for t in terms if t in body_stems]

    if matched:
        return Verdict(keep=True, reason="matched", matched=matched, terms=terms)

    # A URL is a second chance, and it is a real one: plenty of pages put the
    # subject in the slug (`/events/sponsorship`) and not in the body text we
    # reduced. Cheap, and it rescues pages the body gate would wrongly drop.
    slug_hits = [t for t in terms if t in slug]
    if slug_hits:
        return Verdict(keep=True, reason="matched-url", matched=slug_hits,
                       terms=terms)

    return Verdict(
        keep=False,
        reason="off-topic",
        matched=[],
        terms=terms,
    )


def _stem_all(text: object) -> set[str]:
    return {_stem(w) for w in _norm(text).split() if len(w) >= 3}


def report(detail: bool = False) -> dict:
    """How the gate itself is configured, for diagnostics.

    Never reports page content: a subject-term list is derived from the run's
    own plan, but a matched page's text must never end up in a diagnostic.
    """
    return {
        "stopwords": len(_STOPWORDS),
        "shape_words": sorted(_SHAPE_WORDS),
        "bar": ("entity term in url or opening, else one core subject term "
                "body or url"),
        "lede_chars": _LEDE_CHARS,
        "detail": detail,
    }