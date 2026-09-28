"""Coverage / conflict / backlog derivation, and the one write path.

The recurring failure this guards against is a number that looks complete: a
coverage bar filled from defaults, a conflict counted but not addressable, or a
resolution that invents a value nothing extracted.
"""
import pytest

from app.services import coverage as cov


def _rec(rid, **fields):
    return {"record_id": rid, "fields": fields}


def _cell(value, status=None, quote="q", url="https://x.example/", **extra):
    c = {"value": value, "source": {"quote": quote, "url": url, "page_id": "p1"}}
    if status:
        c["verification_status"] = status
    c.update(extra)
    return c


# -- field_coverage ----------------------------------------------------------
def test_declared_but_never_extracted_field_is_reported_absent():
    out = cov.field_coverage(
        [_rec("a", name=_cell("Acme", "verified"))],
        [{"name": "name"}, {"name": "funding"}],
    )
    by = {f["field"]: f for f in out["fields"]}
    assert by["name"]["missing"] == 0
    assert by["funding"]["missing"] == 1
    assert by["funding"]["present"] == 0
    assert by["funding"]["coverage_pct"] == 0.0


def test_schema_entries_may_be_dicts_or_bare_strings():
    """The stored schema is objects. Treating it as strings crashed the endpoint."""
    out = cov.field_coverage(
        [_rec("a", name=_cell("Acme", "verified"))],
        [{"name": "name"}, "bare"],
    )
    assert [f["field"] for f in out["fields"]] == ["name", "bare"]


def test_value_without_a_verdict_counts_as_present_but_not_proven():
    out = cov.field_coverage([_rec("a", name=_cell("Acme"))], [{"name": "name"}])
    f = out["fields"][0]
    assert f["present"] == 1
    assert f["verified"] == 0
    assert f["unverified"] == 1
    assert f["proven_pct"] == 0.0


def test_plain_scalar_fields_are_not_mistaken_for_missing():
    """Pre-Phase-0 rows stored bare scalars. Counting those as empty would
    understate coverage on every historical dataset."""
    out = cov.field_coverage([_rec("a", name="Acme")], [{"name": "name"}])
    f = out["fields"][0]
    assert f["present"] == 1
    assert f["unverified"] == 1


# -- collect_conflicts -------------------------------------------------------
def test_conflict_carries_both_sides_with_their_evidence():
    rows = [_rec("r1", founders=_cell(
        "A", "conflicting",
        rivals=[{"value": "B", "source": {"quote": "qb", "url": "https://b.example/",
                                          "page_id": "p2"}}]))]
    cs = cov.collect_conflicts(rows)
    assert len(cs) == 1
    c = cs[0]
    assert c["record_id"] == "r1"
    assert c["field"] == "founders"
    assert c["incumbent"]["value"] == "A"
    assert c["incumbent"]["quote"] == "q"
    assert c["rivals"][0]["value"] == "B"
    assert c["rivals"][0]["page_id"] == "p2"


def test_non_conflicting_cells_are_not_reported_as_conflicts():
    rows = [_rec("r1", a=_cell("x", "verified"), b=_cell("y", "unverified"))]
    assert cov.collect_conflicts(rows) == []


def test_a_conflict_with_no_rival_is_still_listed_but_not_decidable():
    """Silent here would hide a real disagreement; listing it with zero rivals
    is honest and shows up in the backlog."""
    cs = cov.collect_conflicts([_rec("r1", a=_cell("x", "conflicting"))])
    assert len(cs) == 1 and cs[0]["rivals"] == []


# -- build_backlog -----------------------------------------------------------
def test_never_extracted_field_is_flagged_as_a_schema_problem():
    m = cov.field_coverage([_rec("a", name=_cell("Acme", "verified"))],
                           [{"name": "name"}, {"name": "funding"}])
    b = cov.build_backlog(m, [])
    item = {i["field"]: i for i in b["items"]}["funding"]
    assert "schema" in item["reason"]
    assert item["routable"] is False


def test_partially_missing_field_is_flagged_as_routable():
    rows = [_rec("a", f=_cell("1", "verified")), _rec("b", f=_cell("2", "verified")),
            _rec("c")]
    m = cov.field_coverage(rows, [{"name": "f"}])
    item = cov.build_backlog(m, [])["items"][0]
    assert item["missing"] == 1
    assert item["routable"] is True
    assert "depth" in item["reason"] or "coverage gap" in item["reason"]


def test_backlog_ranks_the_largest_gap_first():
    # x absent on 4 of 5, y absent on 1 of 5. Both are routable; x is the
    # bigger job and must lead.
    rows = [_rec("a", x=_cell("1", "verified"), y=_cell("a", "verified")),
            _rec("b", y=_cell("b", "verified")),
            _rec("c", y=_cell("c", "verified")),
            _rec("d", y=_cell("d", "verified")),
            _rec("e")]
    m = cov.field_coverage(rows, [{"name": "x"}, {"name": "y"}])
    items = cov.build_backlog(m, [])["items"]
    assert [i["field"] for i in items] == ["x", "y"]


def test_fully_covered_field_is_absent_from_the_backlog():
    rows = [_rec("a", f=_cell("1", "verified"))]
    m = cov.field_coverage(rows, [{"name": "f"}])
    assert cov.build_backlog(m, [])["items"] == []


# -- apply_resolution --------------------------------------------------------
def test_keep_marks_verified_and_records_which_move_was_made():
    cell = _cell("A", "conflicting")
    out = cov.apply_resolution(cell, "keep", None)
    assert out["value"] == "A"
    assert out["verification_status"] == "verified"
    assert out["resolved"] == "kept_incumbent"


def test_adopt_takes_the_rival_value_and_that_rivals_evidence():
    cell = _cell("A", "conflicting", quote="qa", url="https://a.example/",
                 rivals=[{"value": "B", "source": {"quote": "qb",
                                                   "url": "https://b.example/",
                                                   "page_id": "p2"}}])
    out = cov.apply_resolution(cell, "adopt", 0)
    assert out["value"] == "B"
    assert out["source"]["quote"] == "qb"
    assert out["source"]["page_id"] == "p2"
    assert out["verification_status"] == "verified"


def test_adopt_with_a_bad_rival_index_is_refused():
    cell = _cell("A", "conflicting", rivals=[{"value": "B", "source": {}}])
    with pytest.raises(ValueError):
        cov.apply_resolution(cell, "adopt", 5)


def test_unknown_choice_is_refused():
    """There is deliberately no free-text path: a dataset that claims every
    value carries evidence cannot accept one that does not."""
    with pytest.raises(ValueError):
        cov.apply_resolution(_cell("A", "conflicting"), "type-in-a-value", None)


def test_resolution_refuses_a_cell_with_no_provenance():
    with pytest.raises(ValueError):
        cov.apply_resolution("just a string", "keep", None)
