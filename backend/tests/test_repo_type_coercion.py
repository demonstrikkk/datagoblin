"""Every value the repository returns must be JSON-safe.

psycopg hands back real `UUID` and `datetime` objects for `uuid` and
`timestamptz` columns. The REST adapter this replaced returned JSON strings, so
every typed view downstream was written against strings and nothing noticed -
until a `DatasetView` was handed a `UUID` and pydantic raised `string_type`,
i.e. HTTP 500 on a dataset the user had just finished creating.

The fix is at the single read choke point, not per endpoint, so the next typed
view added anywhere downstream is already safe.
"""
import datetime
import decimal
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repositories.postgres_repo import _plain  # noqa: E402


def test_uuid_becomes_a_string():
    u = uuid.uuid4()
    assert _plain(u) == str(u)
    assert isinstance(_plain(u), str)


def test_datetime_becomes_iso_z():
    """The wire format the rest of the app already uses for timestamps."""
    naive = datetime.datetime(2026, 9, 27, 12, 39, 17, 822044)
    out = _plain(naive)
    assert isinstance(out, str)
    assert out == "2026-09-27T12:39:17.822044Z", "naive is treated as UTC"
    assert not out.endswith("+00:00"), "the app's format is a Z suffix"


def test_aware_datetime_is_converted_to_utc():
    tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    aware = datetime.datetime(2026, 9, 27, 18, 9, 17, tzinfo=tz)
    assert _plain(aware) == "2026-09-27T12:39:17Z"


def test_date_and_time_become_strings():
    assert _plain(datetime.date(2026, 9, 27)) == "2026-09-27"
    assert isinstance(_plain(datetime.time(12, 30)), str)


def test_containers_are_coerced_recursively():
    """jsonb columns arrive as dicts/lists that may themselves hold native
    types, so the walk has to recurse rather than only touch the top level."""
    out = _plain({"id": uuid.uuid4(), "when": datetime.datetime(2026, 1, 1),
                  "nested": {"page": uuid.uuid4()},
                  "list": [uuid.uuid4(), 1, "x"]})
    assert isinstance(out["id"], str)
    assert isinstance(out["when"], str)
    assert isinstance(out["nested"]["page"], str)
    assert isinstance(out["list"][0], str)
    assert out["list"][1] == 1 and out["list"][2] == "x"


def test_scalars_pass_through_untouched():
    for v in (None, "s", 1, 1.5, True, False):
        assert _plain(v) is v or _plain(v) == v


def test_decimal_and_bytes_become_serialisable():
    assert _plain(decimal.Decimal("2.50")) == 2.5
    assert _plain(b"hi") == "hi"
    assert _plain(memoryview(b"hi")) == "hi"


def test_unknown_types_fall_back_to_str():
    class Weird:
        def __str__(self):
            return "weird"
    assert _plain(Weird()) == "weird"


# --- the read path is where it matters --------------------------------------

def test_rows_are_normalised_by_the_single_choke_point():
    """Every SELECT goes through `_rows`, so normalising there covers all of
    them. Assert the helper is actually wired into it."""
    src = (Path(__file__).resolve().parents[1] /
           "app" / "repositories" / "postgres_repo.py").read_text(encoding="utf-8")
    body = src.split("def _rows(")[1].split("return self._run")[0]
    assert "_plain(" in body, (
        "_rows must normalise; doing it per endpoint is what let a typed view "
        "break on a bare UUID")


def test_dataset_view_accepts_what_the_repository_returns():
    """The exact failure the user hit, as a regression test."""
    from app.schemas.run import DatasetView
    view = DatasetView(
        id=_plain(uuid.uuid4()),
        run_id=_plain(uuid.uuid4()),
        name="q",
        schema=[],
        record_count=3,
        created_at=_plain(datetime.datetime(2026, 9, 27, 12, 39, 17, 822044)),
    )
    assert isinstance(view.id, str)
    assert isinstance(view.created_at, str)
    assert view.created_at.endswith("Z")
