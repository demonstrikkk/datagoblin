"""The gate must drop pages about the wrong entity, not just the wrong topic.

A live run asked for "European logistics companies with their chief executive and
their headquarters city". It returned 139 records and not one of them was a
logistics company: LVMH, SAP, GSK, Airbus, HSBC. The culprit was a single page,
`digitaldefynd.com/IQ/famous-ceo-europe` -- "50 Famous CEOs in Europe".

That page passed the old gate honestly. The gate tested one core subject term
drawn from the goal, and the page contains "european" and "executive". Topic
relevance was never the page's problem; *entity* was. `plan["entity"]` says
exactly what a row should be, and it was never consulted.

Requiring the entity term to appear anywhere would not have fixed it: the page
mentions "logistic" twice, in passing, the first time at character 22,337 of
67,827. The genuinely on-topic pages mention it 80 and 149 times from character
5 and 8. So the rule is structural -- URL slug or opening -- and the fixtures
below reproduce those measured offsets so the test cannot pass by accident.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import relevance  # noqa: E402
from app.services.relevance import (  # noqa: E402
    _LEDE_CHARS,
    assess,
    entity_terms,
    subject_terms,
)

PLAN = {
    "goal": ("European logistics companies with their chief executive and their "
             "headquarters city"),
    "entity": "logistics_company",
    "search_queries": ["top logistics companies europe ceo headquarters"],
}

#: The real off-topic page, in miniature. The opening is about famous CEOs; the
#: entity is mentioned once, far past any plausible lede.
OFF_TOPIC = ("# 50 Famous CEOs in Europe [2026]\n"
             + "Leadership is the cornerstone of global progress. " * 90
             + "She is known for mastering fast fashion logistics.\n")

#: The real on-topic page, in miniature: the entity is the second word.
ON_TOPIC = "# Top 10: Logistics CEOs\nWe rank the biggest 3PLs in Europe.\n"

OFF_TOPIC_URL = "https://digitaldefynd.com/IQ/famous-ceo-europe/"
ON_TOPIC_URL = "https://supplychaindigital.com/top10/top-10-logistics-ceos"


def test_the_fixtures_reproduce_the_measured_offsets():
    """Guard the premise. If a fixture drifts, this fails before the gate does."""
    assert OFF_TOPIC.lower().index("logistic") > _LEDE_CHARS, "off-topic mentions late"
    assert ON_TOPIC.lower().index("logistic") < _LEDE_CHARS, "on-topic mentions early"


def test_entity_terms_keep_the_signal_word_and_drop_the_generic_noun():
    assert entity_terms(PLAN) == ["logistic"]


def test_an_entity_that_is_only_a_generic_noun_yields_nothing():
    # "company" is a stopword: it names no entity, so the gate must not pretend
    # it can constrain anything.
    assert entity_terms({"entity": "company"}) == []


def test_the_famous_ceo_page_is_dropped_as_off_entity():
    v = assess(PLAN, OFF_TOPIC, OFF_TOPIC_URL)
    assert v.keep is False
    assert v.reason == "off-entity"
    assert v.terms == ["logistic"]


def test_the_entity_rule_is_what_drops_it_not_the_subject_floor():
    """Same page, same text -- only the entity changes the outcome.

    With the entity removed the old subject floor keeps the page (it contains
    "european" and "executive"). That is the bug, reproduced; the entity rule
    is the fix.
    """
    without_entity = {"goal": PLAN["goal"], "search_queries": PLAN["search_queries"]}
    assert assess(without_entity, OFF_TOPIC, OFF_TOPIC_URL).keep is True
    assert assess(PLAN, OFF_TOPIC, OFF_TOPIC_URL).keep is False


def test_the_logistics_page_is_kept_on_an_entity_hit():
    v = assess(PLAN, ON_TOPIC, ON_TOPIC_URL)
    assert v.keep is True
    assert v.reason == "entity"
    assert "logistic" in v.matched


def test_an_entity_hit_does_not_also_demand_a_subject_term():
    # A page that names the entity but never says "Europe" is still a page about
    # logistics companies. The requirement is one entity hit, not both bars.
    v = assess(PLAN, "# Logistics quarterly\nThe 3PL market grew.\n",
               "https://example.org/report")
    assert v.keep is True and v.reason == "entity"


def test_the_url_slug_can_carry_the_entity_the_body_omits():
    body = "The biggest providers by revenue, ranked.\n"
    assert assess(PLAN, body, ON_TOPIC_URL).keep is True
    # Same body, no entity in the slug -> dropped. The slug is load-bearing.
    assert assess(PLAN, body, "https://example.org/report").keep is False


def test_without_an_entity_the_subject_floor_still_applies():
    no_entity = {"goal": PLAN["goal"], "search_queries": PLAN["search_queries"]}
    kept = assess(no_entity, "European firms and their executive teams.\n", "")
    assert kept.keep is True and kept.reason == "matched"
    dropped = assess(no_entity, "A page about gardening and soil.\n", "")
    assert dropped.keep is False and dropped.reason == "off-topic"


def test_a_vague_plan_passes_everything():
    # No usable subject term (all stopwords) and no usable entity: the plan is
    # too vague to gate on, so guessing would drop real rows.
    v = assess({"goal": "of and the", "entity": "company"}, "anything", "")
    assert v.keep is True and v.reason == "no-subject-terms"


def test_an_empty_page_is_kept_rather_than_rejected_on_emptiness():
    v = assess(PLAN, "", "")
    assert v.keep is True and v.reason == "empty-page"


def test_the_entity_terms_are_reported_so_a_drop_can_be_explained():
    assert subject_terms(PLAN)  # the goal still yields subject terms
    v = assess(PLAN, OFF_TOPIC, OFF_TOPIC_URL)
    assert v.terms == ["logistic"], "the drop names the entity that was sought"


def test_report_states_the_entity_bar():
    r = relevance.report()
    assert "entity" in r["bar"]
    assert r["lede_chars"] == _LEDE_CHARS
