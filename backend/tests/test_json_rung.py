"""The JSON ladder rung: reading a structured payload without a model.

The point of this rung is that it costs nothing and cannot misread. A language
model asked to read `{"revenue": 4200000}` will sometimes answer `$4.2M`,
sometimes `4,200,000`, sometimes `approximately 4.2 million`. A parser answers
`4200000`, every time.

These tests assert three things, in order of how badly they fail:

1. the shape contract — a rung returns `{value, quote, read_method}`, flat,
   because a nested cell dict is stored as the *value* and silently discarded
   downstream;
2. the refusals — a payload that is not a record list must produce nothing, so
   the model rung is still asked, rather than a confident wrong answer;
3. the whole path — through `extract_page`, with the reducer and the store's
   evidence text, because the rung can fill correctly and still produce no
   records if a quote cannot be located in the text the store persists.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services import extractor as extractor_svc
from app.services import jsonrung as J

PAYLOAD = {"results": [
    {"name": "Acme Inc", "revenue": 4200000, "employees": 120,
     "founded_year": 2011, "industry": "AI", "hq_city": "Berlin",
     "website": "https://acme.example", "revenue_forecast": 9999999},
    {"name": "Globex LLC", "revenue": 1500000, "employees": 40,
     "founded_year": 2015, "industry": "Robotics"},
]}
TEXT = json.dumps(PAYLOAD)


def fields(*names: str) -> list[dict]:
    return [{"name": n, "type": "string", "required": False,
             "description": n.replace("_", " ")} for n in names]


# ------------------------------------------------------------ shape contract


def test_a_rung_returns_a_flat_cell():
    """The contract every other rung follows. Nesting here loses the record."""
    out = J.rung(fields("company_name"), {"json_text": TEXT})
    cell = out["company_name"]
    assert set(cell) == {"value", "quote", "read_method"}, (
        f"a rung must return a flat cell; extra keys or nesting cause the value "
        f"to be stored as a dict: {sorted(cell)}")
    assert cell["value"] == "Acme Inc", (
        "the stored value was the whole rung dict, not the value in it")
    assert cell["read_method"] == "api"


def test_the_quote_is_the_value_as_it_appears_in_the_payload():
    """A quote must be a substring of the stored document or nothing resolves."""
    out = J.rung(fields("revenue"), {"json_text": TEXT})
    quote = out["revenue"]["quote"]
    assert quote in TEXT, "the quote is not verbatim text from the payload"
    # And it round-trips to the same value the cell stores, so a reader checking
    # the proof sees exactly the number that was written.
    assert json.loads(quote) == out["revenue"]["value"] == 4200000


def test_numbers_keep_their_type():
    """Not stringified. A number field holding a string is a type mismatch."""
    out = J.rung(fields("revenue", "headcount"), {"json_text": TEXT})
    assert out["revenue"]["value"] == 4200000
    assert isinstance(out["revenue"]["value"], int)


# -------------------------------------------------------------- field mapping


@pytest.mark.parametrize("field,key", [
    ("company_name", "name"),
    ("revenue", "revenue"),
    ("headcount", "employees"),
    ("founded", "founded_year"),
    ("location", "hq_city"),
    ("industry", "industry"),
    ("website", "website"),
])
def test_a_field_maps_to_its_key(field: str, key: str):
    assert J._key_matches(field, key), f"{field} should match {key}"


def test_a_qualifier_defeats_the_match():
    """`revenue_forecast` is a projection, not the revenue that was asked for.

    Filling `revenue` from it is a confident wrong answer, which is the one
    outcome a deterministic rung exists to avoid. The model rung can still try.
    """
    assert not J._key_matches("revenue", "revenue_forecast")
    assert not J._key_matches("company_name", "registry_code")


def test_an_absent_key_is_left_missing_rather_than_guessed():
    out = J.rung(fields("ceo"), {"json_text": TEXT})
    assert out == {}, "a field the payload does not contain must stay missing"


def test_the_first_record_is_the_one_used():
    """The ladder fills one record; chunking handles the rest."""
    out = J.rung(fields("company_name"), {"json_text": TEXT})
    assert out["company_name"]["value"] == "Acme Inc"


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize("body,why", [
    ('{"error": "not found"}', "an error envelope is not a record"),
    ('{"errors": ["bad key"]}', "plural error envelope"),
    ("[]", "an empty array is nothing to read"),
    ("{}", "an empty object is nothing to read"),
    ("null", "null is nothing to read"),
    ('"just a string"', "a bare scalar is not a record list"),
    ("[1, 2, 3]", "an array of scalars is a column, not rows"),
    ("not json at all", "not JSON"),
    ("<html><body>hi</body></html>", "not JSON"),
])
def test_a_non_record_payload_yields_nothing(body: str, why: str):
    assert J.parse_records(body) == [], why
    assert J.rung(fields("company_name"), {"json_text": body}) == {}


def test_an_oversize_payload_is_refused():
    big = json.dumps({"results": [{"name": "x" * 50}] * 40000})
    assert len(big) > J.MAX_BYTES
    assert J.parse_records(big) == []


def test_more_rows_than_the_cap_is_truncated_not_dropped():
    body = json.dumps([{"name": f"c{i}"} for i in range(50)])
    assert len(J.parse_records(body, max_records=10)) == 10


def test_a_wrapper_key_is_unwrapped():
    for key in ("results", "data", "items", "records", "rows"):
        body = json.dumps({key: [{"name": "Acme"}]})
        assert len(J.parse_records(body)) == 1, f"{key} wrapper not unwrapped"


def test_deeply_nested_values_are_not_turned_into_strings():
    """`{"a":{"b":{"c":{"d":{"e":1}}}}}` has no leaf worth storing as a cell."""
    body = json.dumps({"a": {"b": {"c": {"d": {"e": 1}}}}})
    assert J.rung(fields("a"), {"json_text": body}) == {}


def test_a_long_list_of_objects_is_not_a_cell():
    body = json.dumps({"tags": [{"x": 1}] * 30})
    assert J.rung(fields("tags"), {"json_text": body}) == {}


def test_a_short_list_of_scalars_is_one_cell():
    body = json.dumps({"founders": ["Ada Lovelace", "Alan Turing"]})
    out = J.rung(fields("ceo"), {"json_text": body})
    assert out["ceo"]["value"] == "Ada Lovelace, Alan Turing"


# ------------------------------------------------------------- the whole path


def test_extract_page_reads_json_without_calling_the_model():
    """The actual claim: zero model calls, and the values survive verification."""
    calls: list[str] = []

    async def llm(prompt, schema=None):
        calls.append(prompt)
        return {"data": {}, "provider": "llm"}

    page = {"url": "https://api.example.com/v2/companies", "json_text": TEXT,
            "page_id": "pg1", "title": "Companies"}
    plan = {"fields": fields("company_name", "revenue", "headcount")}

    records, provider = asyncio.run(extractor_svc.extract_page(plan, page, llm))

    assert records, "the rung filled every field and the record still vanished"
    assert calls == [], f"a deterministic rung paid for {len(calls)} model call(s)"
    assert provider.startswith("ladder:")
    assert "json" in provider

    got = {k: v.get("value") for k, v in records[0]["fields"].items()}
    assert got["company_name"] == "Acme Inc"
    assert got["revenue"] == 4200000
    assert got["headcount"] == 120


def test_the_stored_evidence_text_contains_the_quotes():
    """The store and the extractor must agree on what the document says.

    The rung quoted `4200000` from the payload; if the reducer's evidence text
    does not contain it, `locate_quote` fails and the cell is dropped at
    verification even though the rung was right. This is the seam where a
    deterministic rung silently loses everything.
    """
    from app.services.reducer import page_evidence_text

    page = {"url": "u", "json_text": TEXT}
    text = page_evidence_text(page)
    assert text, "a JSON page reduced to no evidence text at all"
    out = J.rung(fields("revenue", "company_name"), {"json_text": TEXT})
    for name, cell in out.items():
        assert cell["quote"] in text, (
            f"the quote for {name} is not in the stored evidence text, so the "
            f"cell cannot be located and will be discarded at verification")


def test_a_json_page_with_no_html_still_reaches_the_rung():
    """A JSON response has neither `html` nor `markdown`.

    The early return that guarded against an empty page tested both, so a pure
    JSON page returned `([], "none")` before any rung ran.
    """
    async def llm(prompt, schema=None):
        raise AssertionError("the model must not be called")

    body = json.dumps({"results": [{"name": "Acme"}]})
    plan = {"fields": fields("company_name")}
    page = {"url": "u", "json_text": body}
    records, _ = asyncio.run(extractor_svc.extract_page(plan, page, llm))
    assert records


def test_an_html_page_is_unaffected_by_the_json_rung():
    """The rung must not fire on markup, and must not change existing behaviour."""
    calls: list[str] = []

    async def llm(prompt, schema=None):
        calls.append(prompt)
        return {"data": {"records": []}, "provider": "llm"}

    page = {"url": "https://example.test/x", "html": "<html><body>hello</body></html>",
            "markdown": "hello"}
    records, provider = asyncio.run(extractor_svc.extract_page(
        {"fields": fields("company_name")}, page, llm))
    assert "json" not in provider


def test_describe_explains_a_payload_for_run_reporting():
    """A zero-yield run needs to say *why*, not just report zero."""
    info = J.describe(TEXT)
    assert info["is_json"] is True
    assert info["records"] == 2
    assert "revenue" in info["keys"]

    assert J.describe("<html>").get("records") == 0