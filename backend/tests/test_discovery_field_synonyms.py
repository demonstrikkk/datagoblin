"""A page is ranked by what it says, not by the identifier the plan uses.

The plan asks for `chief_executive_name`; the page says "Leadership". Before the
synonym lookup, a company's team page and an unrelated page scored the same on
that field, so the page holding the answer had no reason to outrank the one that
did not. Measured: identity columns 100%, `chief_executive_name` 2/17.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.graph import _plan_relevance  # noqa: E402

CEO_PLAN = {
    "entity": "company",
    "goal": "",
    "queries": ["logistics companies europe"],
    "fields": [{"name": "company_name"},
               {"name": "chief_executive_name"}],
}

SITE_PLAN = {
    "entity": "company",
    "goal": "",
    "queries": ["logistics companies europe"],
    "fields": [{"name": "company_name"}, {"name": "website"}],
}


def test_a_leadership_page_outranks_an_unrelated_page():
    team = {"url": "https://acme.example/leadership", "title": "Leadership"}
    other = {"url": "https://acme.example/press", "title": "Press"}
    assert _plan_relevance(team, CEO_PLAN) > _plan_relevance(other, CEO_PLAN)


def test_an_official_page_outranks_an_unrelated_page():
    official = {"url": "https://acme.example/official", "title": "Official site"}
    other = {"url": "https://acme.example/press", "title": "Press"}
    assert _plan_relevance(official, SITE_PLAN) > _plan_relevance(other, SITE_PLAN)


def test_the_synonym_never_outranks_the_field_word_itself():
    """A page that says "website" is better evidence than one that says
    "official", so the weaker signal must not overtake the stronger."""
    exact = {"url": "https://acme.example/website", "title": "Website"}
    syn = {"url": "https://acme.example/official", "title": "Official"}
    assert _plan_relevance(exact, SITE_PLAN) > _plan_relevance(syn, SITE_PLAN)
