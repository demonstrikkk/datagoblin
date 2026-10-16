"""Tests for the probe-column removal script's guard.

The guard is the only thing standing between this script and deleting a
populated column from a real dataset, so it is tested directly rather than only
through its CLI.

The regression that shaped this file: a stored record is FLAT -- fields are
top-level keys of the record JSON. An earlier `_has_value` looked for a nested
`fields` key, never found it, and so reported every dataset as safe. That
version would have stripped `name` from a 142-record dataset where every record
had a value for it.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "remove_probe_col.py"


def _load():
    spec = importlib.util.spec_from_file_location("remove_probe_col", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["remove_probe_col"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _cell(value, **extra):
    """A provenance-shaped cell, which is how real values are stored."""
    return {"value": value,
            "source": {"quote": "q", "url": "https://s.example/", "page_id": "p"},
            **extra}


# -- the real shape ------------------------------------------------------------

def test_a_filled_flat_record_counts_as_data(mod):
    row = {"name": _cell("Quantexa"), "location": _cell("London")}
    assert mod._has_value(row, "name") is True


def test_a_nested_record_also_counts_as_data(mod):
    """Defensive: an adapter storing the nested shape must not be under-guarded."""
    row = {"fields": {"name": _cell("Quantexa")}}
    assert mod._has_value(row, "name") is True


# -- the residue case: declared, never valued ----------------------------------

def test_a_declared_but_absent_key_is_not_data(mod):
    assert mod._has_value({"name": _cell("x")}, "dg_probe_col") is False


def test_a_null_cell_is_not_data(mod):
    assert mod._has_value({"dg_probe_col": None}, "dg_probe_col") is False


def test_an_empty_string_cell_is_not_data(mod):
    assert mod._has_value({"dg_probe_col": ""}, "dg_probe_col") is False


def test_an_empty_provenance_cell_without_a_source_is_not_data(mod):
    """Blank value AND no quote means nothing was ever extracted."""
    assert mod._has_value({"dg_probe_col": {"value": ""}}, "dg_probe_col") is False


def test_a_blank_value_that_carries_a_quote_still_counts_as_data(mod):
    """The quote and url are the evidence; blank-plus-proven is not residue."""
    row = {"dg_probe_col": {"value": "", "source": {"quote": "n/a",
                                                    "url": "https://s.example/"}}}
    assert mod._has_value(row, "dg_probe_col") is True


# -- robustness ----------------------------------------------------------------

def test_a_non_dict_record_does_not_crash_the_guard(mod):
    for bad in (None, [], "text", 7):
        assert mod._has_value(bad, "dg_probe_col") is False


def test_a_fields_key_that_is_not_a_dict_falls_back_to_the_flat_shape(mod):
    """A malformed `fields` must not silently disable the guard."""
    row = {"fields": "broken", "name": _cell("x")}
    assert mod._has_value(row, "name") is True


def test_the_guard_would_reject_a_populated_column(mod):
    """The end-to-end property, stated as the failure it prevents."""
    populated = [{"name": _cell(f"co-{i}")} for i in range(142)]
    holding = sum(1 for r in populated if mod._has_value(r, "name"))
    assert holding == 142, \
        "a populated column was reported as removable; this is data loss"