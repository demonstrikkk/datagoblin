"""Entity gate: headlines are dropped with reasons, real names are kept.

Every rule names a headline marker, so the suite doubles as the false-positive
contract: ordinary names ("The Boring Company", "D9Tech Resources, LLC") carry
none of the markers and must survive. A rule that eats a real name is a worse
failure than the headlines it catches, because a dropped record is invisible
while a kept headline is at least reviewable.
"""
from app.services import entity_gate as gate


def _rec(**fields):
    return {"record_id": "r1",
            "fields": {k: {"value": v, "verification_status": "verified",
                           "source": {}} for k, v in fields.items()}}


_SCHEMA = [{"name": "company_name"}, {"name": "location"}]


def test_headlines_are_dropped_with_reasons():
    recs = [_rec(company_name="The Largest AI Startup Funding Deals Of 2024",
                 location="London")]
    kept, drops = gate.apply(recs, _SCHEMA)
    assert kept == []
    assert len(drops) == 2  # the value and the emptied record
    assert drops[0]["field"] == "company_name"
    assert drops[1]["field"] is None


def test_question_headlines_and_prefixes_are_dropped():
    for bad in ["European AI Funding Is Growing. Will That Boost The Region?",
                "Exclusive: No More Side Hustles For Startup Staff",
                "The Week's 10 Biggest Funding Rounds This Year"]:
        kept, drops = gate.apply([_rec(company_name=bad)], _SCHEMA)
        assert kept == [], bad
        assert drops[0]["reason"], bad


def test_real_names_survive():
    for good in ["The Boring Company", "Airbnb", "D9Tech Resources, LLC",
                 "FSS Government Solutions", "Omnea", "Yann LeCun"]:
        kept, drops = gate.apply([_rec(company_name=good)], _SCHEMA)
        assert len(kept) == 1, good
        assert drops == [], good


def test_urls_are_not_names():
    kept, drops = gate.apply(
        [_rec(company_name="https://news.crunchbase.com/venture/x")], _SCHEMA)
    assert kept == [] and drops[0]["reason"].startswith("value is a URL")


def test_non_entity_fields_are_untouched():
    recs = [_rec(company_name="Acme",
                 location="The Largest AI Startup Funding Deals Of 2024")]
    kept, drops = gate.apply(recs, _SCHEMA)
    assert len(kept) == 1
    assert kept[0]["fields"]["location"]["value"].startswith("The Largest")
    assert drops == []


def test_a_dropped_non_primary_field_keeps_its_record():
    schema = [{"name": "company_name"}, {"name": "founder_names"}]
    recs = [_rec(company_name="Acme", founder_names="Top 10 Founders To Watch")]
    kept, drops = gate.apply(recs, schema)
    assert len(kept) == 1
    assert kept[0]["fields"]["company_name"]["value"] == "Acme"
    assert kept[0]["fields"]["founder_names"]["value"] is None
    assert any(d["field"] == "founder_names" for d in drops)


def test_emptied_cells_keep_their_shape():
    kept, drops = gate.apply(
        [_rec(company_name="Acme",
              founder_names="Massive AI Deals Drive $189B Startup Funding Records")],
        [{"name": "company_name"}, {"name": "founder_names"}])
    cell = kept[0]["fields"]["founder_names"]
    assert cell["value"] is None
    assert cell["verification_status"] == "rejected"
