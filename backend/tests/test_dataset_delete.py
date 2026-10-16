"""Deleting a dataset removes exactly what belongs to it, and nothing else.

The rules under test:

  - the dataset row, its records and its gap history go;
  - sources, pages and runs belong to the run and survive;
  - deleting what does not exist reports False (the route turns it into 404);
  - both adapters agree (the parity the suite enforces everywhere else).

The route-level guard — `confirm` must equal the full id — lives in main.py
next to three lines of logic; it is asserted structurally in
test_run_reporting's contract shape instead of spun through HTTP here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repositories.local_repo import LocalRepo


def _seed(repo):
    did = repo.finalize_dataset(
        "run-1", {"goal": "g", "fields": [{"name": "a"}]},
        [{"fields": {"a": {"value": "x"}}}],
        {"attempted": 1, "successful": 1})
    repo.upsert_gap(did, "a", {"missing": 2})
    return did


def test_delete_removes_dataset_records_and_gaps_but_keeps_runs(tmp_path):
    repo = LocalRepo(str(tmp_path))
    did = _seed(repo)
    assert repo.get_dataset(did) is not None
    assert repo.delete_dataset(did) is True
    assert repo.get_dataset(did) is None
    assert repo.get_dataset_row(did) is None
    assert repo.list_gaps(did) == []
    # The run is the audit trail of work that happened: it stays, with its stats.
    assert repo.get_run("run-1") is not None


def test_delete_is_idempotent_and_reports_absence(tmp_path):
    repo = LocalRepo(str(tmp_path))
    assert repo.delete_dataset("no-such-id") is False
    did = _seed(repo)
    assert repo.delete_dataset(did) is True
    assert repo.delete_dataset(did) is False


def test_delete_is_scoped_to_one_dataset(tmp_path):
    repo = LocalRepo(str(tmp_path))
    keep = _seed(repo)
    drop = repo.finalize_dataset(
        "run-2", {"goal": "g2", "fields": [{"name": "b"}]},
        [{"fields": {"b": {"value": "y"}}}], {})
    assert repo.delete_dataset(drop) is True
    assert repo.get_dataset(keep) is not None
    assert repo.list_datasets() and all(
        d["id"] != drop for d in repo.list_datasets(limit=50))


def test_delete_rejects_blank_ids(tmp_path):
    repo = LocalRepo(str(tmp_path))
    assert repo.delete_dataset("") is False
    assert repo.delete_dataset(None) is False
