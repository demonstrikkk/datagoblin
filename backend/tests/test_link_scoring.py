"""Deciding which links deserve a page budget.

The page budget is the scarcest thing a run has. On this instance 19% of every
page ever fetched (28 of 151) was a privacy page, a login page, or a bare
domain root — none of which contributed a record, and each of which took a slot
a real listing page did not get.

So these tests are mostly about *refusals*, because the failure mode is not an
exception. It is a clean run that quietly spent its budget on the wrong pages and
came back with thin data.
"""
import pytest

from app.services import crawler as crawler_svc

PLAN = {
    "entity": "company",
    "goal": "find AI companies in London with their founders",
    "dedupe_keys": ["company_name", "website"],
    "fields": [{"name": "company_name"}, {"name": "founded_year"}],
    "search_queries": ["ai startups london founders"],
}


def _s(link, anchor=""):
    return crawler_svc.score_link(link, anchor, PLAN)


# --- furniture must score negative -----------------------------------------
def test_a_login_page_scores_below_a_listing_page():
    assert _s("https://x.example/login", "Sign in") < \
        _s("https://x.example/companies/northwind", "Northwind Traders")


@pytest.mark.parametrize("path", [
    "/privacy-policy", "/terms", "/careers", "/blog", "/tag/saas",
    "/category/finance", "/cart", "/account", "/newsletter", "/sitemap.xml",
])
def test_page_furniture_scores_negative(path):
    assert _s("https://x.example" + path, "Read more") < 0, path


def test_a_bare_root_scores_negative():
    assert _s("https://x.example/", "Home") < 0


def test_pagination_scores_negative_because_it_is_not_a_new_resource():
    """`canonical_url` collapses fragments but not `?page=2`. Two pages differing
    only by that key are one resource fetched twice."""
    assert _s("https://x.example/companies?page=2", "Next") < 0
    assert _s("https://x.example/companies?sort=name", "Sort") < 0


def test_campaign_parameters_are_not_a_scoring_penalty_but_an_identity_fix():
    """They are handled by canonicalisation, not by the score.

    A replay of this instance's own pages found the same listing under
    `partner_category`/`partner_medium` labels. Those parameters name a
    campaign, not a document, so they are removed from the URL before anything
    looks at it — which also means the reuse fingerprint matches and a re-run
    recognises the page it already has. Penalising them in the score as well
    would have been two mechanisms for one idea, and the weaker one.
    """
    for bare, labelled in (
        ("https://x.example/markets/equity",
         "https://x.example/markets/equity?partner_category=index&partner_medium=web"),
        ("https://x.example/companies",
         "https://x.example/companies?utm_source=news&utm_campaign=mar"),
    ):
        assert crawler_svc.canonical_url(labelled) == bare
        assert _s(labelled, "Some page") == _s(bare, "Some page")


def test_the_tracking_key_list_covers_what_this_instance_actually_served():
    """Guards the list against being trimmed to the obvious two keys while real
    campaign parameters keep arriving."""
    for key in ("utm_source", "utm_medium", "gclid", "partner_category",
                "partner_medium", "fbclid", "msclkid"):
        assert key in crawler_svc._TRACKING_QUERY_KEYS, key


# --- content must score positive -------------------------------------------
def test_neither_an_index_nor_a_detail_page_is_starved():
    """`/companies` is where companies live, so it is worth a fetch even though
    `/companies/northwind` is the page that carries a record. Both must be
    followed; the budget decides, not the ranking, and dropping either would
    cost data."""
    index = _s("https://x.example/companies", "Companies")
    detail = _s("https://x.example/companies/northwind", "Northwind Traders")
    assert index > 0, "a listing index was treated as furniture"
    assert detail > 0, "a company detail page was treated as furniture"
    pairs = [("https://x.example/companies", "Companies"),
             ("https://x.example/companies/northwind", "Northwind Traders")]
    got = crawler_svc._rank_children(pairs, PLAN, crawler_svc.plan_terms(PLAN),
                                     set(), depth=0, budget=10, per_parent=8)
    assert set(got) == {"https://x.example/companies",
                        "https://x.example/companies/northwind"}


def test_among_links_of_the_same_shape_the_anchor_that_matches_the_plan_wins():
    got = crawler_svc._rank_children(
        [("https://x.example/x/1", "Northwind founders"),
         ("https://x.example/x/2", "Read more")],
        PLAN, crawler_svc.plan_terms(PLAN), set(), 0, 10, 1)
    assert got == ["https://x.example/x/1"]


def test_a_parent_segment_does_not_credit_its_children():
    """`/companies/northwind` must not earn a slug bonus for `companies`.

    When it did, the section index and every one of its children tied, and the
    ranking had nothing to say about which was the record page.
    """
    terms = crawler_svc.plan_terms(PLAN)
    with_parent = crawler_svc.score_link("https://x.example/companies/northwind",
                                         "", PLAN, terms)
    without_parent = crawler_svc.score_link("https://x.example/other/northwind",
                                            "", PLAN, terms)
    assert with_parent == without_parent


def test_anchor_text_matching_the_plan_beats_a_generic_one():
    assert _s("https://x.example/x/1", "Northwind founders") > \
        _s("https://x.example/x/2", "Click here")


def test_a_slug_matching_the_plan_scores_even_without_anchor_text():
    """Rendered rungs return parsed links with no anchor text. Those must still
    be able to score on the URL, or every JS-heavy page would lose its children.
    """
    assert _s("https://x.example/companies/ai-startups-london", "") > 0


def test_deep_paths_are_discounted():
    shallow = _s("https://x.example/companies/acme", "")
    deep = _s("https://x.example/a/b/c/d/e/acme", "")
    assert shallow > deep


# --- plan vocabulary --------------------------------------------------------
def test_plan_terms_come_from_the_plan_not_from_the_crawler():
    terms = crawler_svc.plan_terms(PLAN)
    # Drawn from the entity, goal, dedupe keys, field names and search queries.
    for expected in ("company", "london", "founders", "startups", "website"):
        assert expected in terms, expected
    # Short words are dropped: they match almost everything and so say nothing.
    assert "ai" not in terms


def test_an_empty_plan_scores_nothing_as_a_match():
    """With no vocabulary a link can only be scored on its shape, and must not
    be rewarded for anchor text that happens to share a word with nothing.
    Shape only: the anchor earns 0.5 and the two-segment shape 0.5."""
    score = crawler_svc.score_link("https://x.example/companies/acme",
                                   "acme", {"fields": []}, set())
    assert score == pytest.approx(1.0, abs=0.01)


def test_an_empty_link_is_the_worst_possible_score():
    assert crawler_svc.score_link("", "", PLAN) < -50


def test_a_malformed_or_non_http_link_never_earns_a_bonus():
    """A bad href must be refused on shape, not scored. `ht tp://%%%` used to
    collect the anchor and segment bonuses and come out positive."""
    for bad in ("ht tp://%%%", "javascript:void(0)", "mailto:someone@example.com",
                "ftp://x.example/file"):
        assert crawler_svc.score_link(bad, "Northwind Traders", PLAN) < 0, bad


# --- ranking keeps the gate in front ---------------------------------------
def test_ranking_drops_furniture_and_keeps_the_gate():
    pairs = [
        ("https://x.example/privacy-policy", "Privacy"),
        ("https://x.example/companies/northwind", "Northwind Traders"),
        ("https://x.example/login", "Sign in"),
        ("https://x.example/companies/globex", "Globex"),
    ]
    got = crawler_svc._rank_children(pairs, PLAN, crawler_svc.plan_terms(PLAN),
                                     set(), depth=0, budget=10, per_parent=8)
    assert "https://x.example/companies/northwind" in got
    assert "https://x.example/companies/globex" in got
    assert "https://x.example/privacy-policy" not in got
    assert "https://x.example/login" not in got


def test_ranking_respects_the_per_parent_cap():
    pairs = [(f"https://x.example/companies/c{i}", f"Company {i}")
             for i in range(40)]
    got = crawler_svc._rank_children(pairs, PLAN, crawler_svc.plan_terms(PLAN),
                                     set(), depth=0, budget=100, per_parent=8)
    assert len(got) == 8


def test_ranking_still_honours_traversal_allowed():
    """Scoring reorders. It must never admit something the gate refused.

    A plan with an allowlist must not crawl a host the allowlist excludes,
    however well the link scores.
    """
    plan = {**PLAN, "allowed_sources": ["allowed.example"]}
    pairs = [("https://other.example/companies/northwind", "Northwind Traders")]
    got = crawler_svc._rank_children(pairs, plan, crawler_svc.plan_terms(plan),
                                     set(), depth=0, budget=10, per_parent=8)
    assert got == []


def test_ranking_skips_links_already_seen():
    pairs = [("https://x.example/companies/northwind", "Northwind")]
    got = crawler_svc._rank_children(pairs, PLAN, crawler_svc.plan_terms(PLAN),
                                     {"https://x.example/companies/northwind"},
                                     depth=0, budget=10, per_parent=8)
    assert got == []


def test_ranking_is_deterministic_for_equal_scores():
    pairs = [("https://x.example/companies/b", "B"),
             ("https://x.example/companies/a", "A")]
    terms = crawler_svc.plan_terms(PLAN)
    first = crawler_svc._rank_children(pairs, PLAN, terms, set(), 0, 10, 8)
    second = crawler_svc._rank_children(list(reversed(pairs)), PLAN, terms, set(), 0, 10, 8)
    assert first == second


def test_ranking_does_not_starve_a_page_whose_candidates_all_score():
    """The cap is generous so a listing page with many real candidates is not
    trimmed to nothing. Two equal, positive links must both survive."""
    pairs = [("https://x.example/companies/a", "A"),
             ("https://x.example/companies/b", "B")]
    got = crawler_svc._rank_children(pairs, PLAN, crawler_svc.plan_terms(PLAN),
                                     set(), depth=0, budget=10, per_parent=2)
    assert len(got) == 2


# --- anchor extraction ------------------------------------------------------
def test_child_links_still_returns_urls_only():
    """Kept for callers that never needed text. Changing its return type would
    have broken `_media_counts` and anything else that treats it as URLs."""
    page = {"url": "https://x.example/",
            "html": "<a href='/a'>A</a><a href='/b'>B</a>"}
    assert crawler_svc._child_links(page) == ["https://x.example/a",
                                              "https://x.example/b"]


def test_child_links_with_text_pairs_each_url_with_its_anchor():
    page = {"url": "https://x.example/",
            "html": "<a href='/a'>Northwind</a><a href='/b'>Privacy</a>"}
    pairs = crawler_svc._child_links_with_text(page)
    assert ("https://x.example/a", "Northwind") in pairs
    assert ("https://x.example/b", "Privacy") in pairs


def test_a_rendered_pages_parsed_links_still_expand_with_empty_anchors():
    """The rendered rungs return `links` and no `html`. They must not lose their
    children — that was a bug once, and the fix must survive the scoring change.
    """
    page = {"url": "https://x.example/", "links": ["https://x.example/companies/a"]}
    pairs = crawler_svc._child_links_with_text(page)
    assert pairs == [("https://x.example/companies/a", "")]
    got = crawler_svc._rank_children(pairs, PLAN, crawler_svc.plan_terms(PLAN),
                                     set(), depth=0, budget=10, per_parent=8)
    assert got == ["https://x.example/companies/a"]


# --- canonicalisation --------------------------------------------------------
def test_campaign_parameters_are_dropped_because_the_document_is_the_same():
    """A replay of this instance's own pages found the same listing under
    `partner_category`/`partner_medium` labels. Each is the same response, so
    each would have taken a page budget of its own."""
    bare = crawler_svc.canonical_url("https://x.example/companies")
    assert crawler_svc.canonical_url(
        "https://x.example/companies?utm_source=news") == bare
    assert crawler_svc.canonical_url(
        "https://x.example/companies?gclid=abc&partner_medium=web") == bare


def test_canonicalisation_keeps_query_strings_that_select_content():
    assert crawler_svc.canonical_url("https://x.example/companies?page=2") != \
        crawler_svc.canonical_url("https://x.example/companies?page=3")
    assert "page=2" in crawler_svc.canonical_url("https://x.example/companies?page=2")


def test_canonicalisation_still_survives_a_bare_question_mark():
    assert crawler_svc.canonical_url("https://x.example/companies?utm_source=x") \
        == "https://x.example/companies"
