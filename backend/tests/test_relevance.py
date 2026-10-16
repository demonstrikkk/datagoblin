"""Tests for the page relevance gate.

The asymmetry is the whole design: losing a real page is worse than keeping an
irrelevant one, because a user can see an irrelevant row and cannot see a
missing one. So most of these tests are about not dropping things -- the AWS
sponsorship page that never says "hackathon" is the case that matters.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import relevance  # noqa: E402

PLAN = {
    "goal": ("Find companies and organisations willing to sponsor hackathons in "
             "New Delhi in 2026, together with their terms and conditions."),
    "search_queries": ["hackathon sponsors New Delhi 2026 sponsorship tiers"],
    "fields": [{"name": "sponsorship_tiers"}],
}


# -- the pages from the real run ------------------------------------------------

@pytest.mark.parametrize("url,body", [
    ("https://aws.amazon.com/events/sponsorship",
     "Become an AWS event sponsor. Sponsorship packages for conferences and "
     "hackathons."),
    ("https://unstop.com/blog/hackathon-sponsorship-models",
     "Hackathon sponsorship models explained for India."),
    ("https://unstop.com/employers/employer-branding/hackathons",
     "Reach developers by sponsoring hackathons."),
    ("https://www.hackerearth.com/hackathon/explore/city/delhi", ""),
])
def test_on_topic_pages_are_kept(url, body):
    assert relevance.assess(PLAN, body, url).keep is True, \
        f"a genuinely relevant page was dropped: {url}"


@pytest.mark.parametrize("url,body", [
    ("https://aws.amazon.com/transform/",
     "AWS Transform accelerates code modernization with machine learning."),
    ("https://aws.amazon.com/free/offers/",
     "Free tier offers and credits for new customers. Compare plans and "
     "pricing options."),
    ("https://aws.amazon.com/solutions/case-studies/",
     "Customer case studies: how organizations built solutions on AWS."),
    ("https://aws.amazon.com/free/",
     "Start building with AWS. Free tier available for new accounts."),
])
def test_off_topic_pages_are_dropped(url, body):
    """The exact junk the live run produced."""
    assert relevance.assess(PLAN, body, url).keep is False, \
        f"an off-topic page passed the gate: {url}"


# -- the trap that cost two false positives -------------------------------------

def test_a_field_name_leaked_into_a_query_is_not_a_topic():
    """`sponsorship tiers` put `tier` in scope, and every free-tier page matched.

    The goal alone must drive the terms when the goal is substantial enough;
    an LLM-written query carries schema, and schema is not subject matter.
    """
    assert "tier" not in relevance.subject_terms(PLAN), \
        "a field name leaked into the subject terms"

    plan = dict(PLAN, goal="x y z")   # deliberately too thin to gate on
    terms = relevance.subject_terms(plan)
    assert "tier" in terms, \
        "a thin goal should still fall back to the queries"


def test_an_adjective_is_not_a_subject():
    """`new` came from `New Delhi` and made every page saying 'new' pass."""
    assert "new" not in relevance.subject_terms(PLAN)


def test_morphology_is_folded_so_real_variants_match():
    for body in ("Hackathon sponsors", "hackathon sponsorship",
                 "sponsored hackathons", "hackathons sponsored"):
        assert relevance.assess(PLAN, body, "").keep is True, \
            f"variant not recognised: {body!r}"


# -- the bar is deliberately low ------------------------------------------------

def test_a_page_matching_on_one_term_is_enough():
    """A strict AND would drop real pages; the gate must not become that."""
    assert relevance.assess(PLAN, "We run hackathons.", "").keep is True


def test_the_url_can_rescue_a_page_whose_body_is_unhelpful():
    v = relevance.assess(PLAN, "Generic corporate overview page.",
                         "https://x.example/events/sponsorship")
    assert v.keep is True
    assert v.reason == "matched-url"
    assert v.matched, "a url match must say which term matched"


# -- it must never be the reason a page is lost ---------------------------------

def test_an_empty_page_keeps_so_the_failure_stays_downstream():
    """A gate that rejects on emptiness just moves the error somewhere vaguer."""
    assert relevance.assess(PLAN, "", "https://x.example/").keep is True
    assert relevance.assess(PLAN, "   ", "").keep is True


def test_a_plan_with_no_usable_subject_keeps_everything():
    """Too vague to gate on is not a reason to drop pages."""
    for goal in ("", "x y z"):
        v = relevance.assess({"goal": goal, "search_queries": []}, "anything at all", "")
        assert v.keep is True, f"dropped pages for a too-vague goal {goal!r}"


# -- the decision must be explainable -------------------------------------------

def test_every_drop_carries_a_reason_and_the_terms_it_used():
    """A silent drop is indistinguishable from a wrong drop."""
    v = relevance.assess(PLAN, "irrelevant corporate text", "https://x.example/")
    assert v.keep is False
    assert v.reason == "off-topic"
    assert v.terms, "a drop must report what it was looking for"


def test_a_keep_also_reports_what_matched():
    v = relevance.assess(PLAN, "hackathon sponsorship guide", "")
    assert v.keep is True
    assert v.matched and v.reason == "matched"


# -- robustness -----------------------------------------------------------------

@pytest.mark.parametrize("body", [None, "", 0, [], {}, "!!!", "\x00\x01"])
def test_hostile_input_does_not_raise(body):
    assert relevance.assess(PLAN, body, "https://x.example/").keep in (True, False)


def test_a_missing_plan_does_not_raise():
    assert relevance.assess({}, "some text", "").keep is True
    assert relevance.assess({"goal": None}, "text", None).keep in (True, False)


def test_report_never_includes_page_content():
    v = relevance.assess(PLAN, "SECRET PAGE TEXT", "")
    assert "SECRET" not in repr(relevance.report())
    assert "SECRET" not in repr(v) or v.keep is True


def test_verdict_is_serialisable():
    """It travels in SSE and logs; a dataclass that cannot dump breaks both."""
    import json
    v = relevance.assess(PLAN, "hackathons", "https://x.example/")
    json.dumps({"keep": v.keep, "reason": v.reason, "matched": v.matched})