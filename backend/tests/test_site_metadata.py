"""Site-level metadata must not become a record.

Found by running the pipeline, not by reading it. A live run against a listing
page produced:

    company_name = "Latest News | TechCrunch"          [verified]
    company_name = "Startup Battlefield | TechCrunch"  [verified]

Both are TechCrunch's own `Organization`, reached because `jsonld.extract` walks
documents in document order and a listing page declares the publisher before its
contents. They were marked **verified**, because a structured route is verified
by construction and the quote equals the value.

That is the worst failure shape available: a value that is confident, proven and
wrong. Nothing downstream re-checks a verified cell, so the error is permanent
and invisible — which is exactly what a scraper must never produce.

A property field is not affected. `industry` describing the site is legitimate,
and a company page that publishes its own `Organization` must keep working.
"""
from __future__ import annotations

import json

import pytest

from app.schemas.plan import is_identity_field
from app.services import ladder

FIELDS = [{"name": "company_name"}, {"name": "industry"}, {"name": "founders"}]


def _page(entity: dict, title: str, body: str = "listing rows here") -> dict:
    ld = json.dumps({"@context": "https://schema.org", **entity})
    return {"html": f'<html><head><title>{title}</title>'
                    f'<script type="application/ld+json">{ld}</script></head>'
                    f'<body>{body}</body></html>',
            "title": title}


LISTING = _page({"@type": "NewsMediaOrganization", "name": "TechCrunch",
                 "url": "https://techcrunch.com"},
                "Latest News | TechCrunch")

SUBJECT = _page({"@type": "Organization", "name": "Acme Inc",
                 "hasCredential": {"@type": "EducationalOccupationalCredential"},
                 "address": {"@type": "PostalAddress"}},
                "Acme Inc")

WEBSITE = _page({"@type": "WebSite", "name": "Acme",
                 "publisher": {"@type": "Organization", "name": "Acme Media"}},
                "Acme")

BARE_ORG = _page({"@type": "Organization", "name": "TechCrunch"},
                 "TechCrunch")


# ------------------------------------------------------------ what is identity


@pytest.mark.parametrize("name", [
    "company_name", "companyName", "founders", "founder_name", "author",
    "startup_name", "product_title", "employee_names",
])
def test_identity_fields_are_recognised(name: str):
    assert is_identity_field(name)


@pytest.mark.parametrize("name", [
    "industry", "funding_amount", "location", "website", "founded", "hq",
])
def test_property_fields_are_not_identity(name: str):
    assert not is_identity_field(name), (
        f"{name} describes the subject's properties, so page-level data is "
        f"legitimate for it and must not be refused")


# ------------------------------------------------------- the publisher refused


@pytest.mark.parametrize("page,label", [
    (LISTING, "NewsMediaOrganization"),
    (WEBSITE, "WebSite"),
    (BARE_ORG, "bare Organization"),
])
def test_a_site_entity_never_fills_an_identity_field(page, label):
    out = ladder.json_ld_rung(FIELDS, {"page": page})
    assert "company_name" not in out, (
        f"a {label} was stored as the record's company name and verified by "
        f"construction: a confident, proven, wrong value")


def test_the_refusal_lets_the_model_rung_still_be_asked():
    """Empty from this rung is the point — the model reads the listing rows."""
    out = ladder.json_ld_rung(FIELDS, {"page": LISTING})
    assert out == {}, "the rung must decline, not fill with page furniture"


def test_a_property_field_is_unaffected_by_the_site_entity():
    """`industry` describing the site is legitimate; refusing it loses data."""
    page = _page({"@type": "NewsMediaOrganization", "name": "TechCrunch",
                  "industry": "Technology News"}, "TechCrunch")
    out = ladder.json_ld_rung([{"name": "industry"}], {"page": page})
    assert out["industry"]["value"] == "Technology News"


# ------------------------------------------------------ the subject still works


def test_a_companys_own_about_page_still_yields_its_name():
    """The case this rung exists for — refusing it would be a regression.

    The About page is titled with the company name and publishes an
    `Organization` that *is* the subject. A "does the value match the title"
    heuristic would refuse exactly this; entity typing must not.
    """
    out = ladder.json_ld_rung(FIELDS, {"page": SUBJECT})
    assert out["company_name"]["value"] == "Acme Inc"
    assert out["company_name"]["read_method"] == "json_ld"


def test_site_name_detection_distinguishes_the_two():
    assert ladder._site_entity_names(LISTING["html"]) == {"techcrunch"}
    assert ladder._site_entity_names(WEBSITE["html"]) == {"acme"}
    assert ladder._site_entity_names(SUBJECT["html"]) == set()


# ------------------------------------------------------------- the quote holds


def test_an_accepted_value_is_still_quoted_and_verifiable():
    out = ladder.json_ld_rung([{"name": "company_name"}], {"page": SUBJECT})
    cell = out["company_name"]
    assert cell["quote"] in SUBJECT["html"], (
        "a structured value must be a substring of the stored page, or the "
        "proof view cannot show where it came from")
    assert cell["value"] == cell["quote"]