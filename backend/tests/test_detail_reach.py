"""A company's "about" page is furniture on a listing and the answer on a detail page.

`_NAV_SEGMENTS` penalises `about`, `about-us`, `contact` and `contact-us` by
-5.0, and `_rank_children` drops anything scoring below zero. That is correct
for an identity-only plan -- a list of names has no use for a contact page -- and
backwards for a plan that asks for a CEO: the company's `/leadership` or
`/about` page is exactly where that value lives.

Measured: a live entity run fetched its listing seeds, queued 49 same-host
children, and spent its whole 10-page budget before opening any of them. Its
identity columns were 100% filled and its per-entity columns were nearly empty.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import crawler as crawler_svc  # noqa: E402

IDENTITY = {
    "dedupe_keys": ["company_name"],
    "fields": [{"name": "company_name"}],
}

ENRICHMENT = {
    "dedupe_keys": ["company_name"],
    "fields": [{"name": "company_name"}, {"name": "chief_executive_name"}],
}


def _s(link, anchor, plan):
    return crawler_svc.score_link(link, anchor, plan)


def test_about_is_furniture_for_a_name_list():
    assert _s("https://x.example/about", "About us", IDENTITY) < 0


def test_about_is_a_candidate_when_a_property_is_wanted():
    assert _s("https://x.example/about", "About us", ENRICHMENT) > 0


def test_leadership_team_and_contact_survive_for_a_property_plan():
    for path in ("/team", "/leadership", "/management", "/contact"):
        assert _s("https://x.example" + path, "Our team", ENRICHMENT) > 0, path


def test_other_furniture_stays_penalised_even_for_a_property_plan():
    for path in ("/login", "/privacy-policy", "/cart", "/careers"):
        assert _s("https://x.example" + path, "Read more", ENRICHMENT) < 0, path


def test_the_detail_exemption_does_not_bypass_the_domain_gate():
    """Scoring reorders; it must never admit a host the allowlist refused."""
    plan = {**ENRICHMENT, "allowed_sources": ["allowed.example"]}
    pairs = [("https://other.example/about", "About us")]
    got = crawler_svc._rank_children(pairs, plan, crawler_svc.plan_terms(plan),
                                     set(), depth=0, budget=10, per_parent=8)
    assert got == []
