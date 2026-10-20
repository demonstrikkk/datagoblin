"""Offline coverage scorecard for one stored dataset. Read-only, no network.

    python scripts/coverage_scorecard.py [dataset_id] [--json]

Answers "did coverage actually get better, and why" without spending a search or
a provider call. For each field it reports how many stored records carry a value,
how many of those are `verified`, and how many carry no value at all. It also
reports the crawl's truncation causes from the run counters, because a thin
column is only actionable if the run can say whether it ran out of page budget,
hit a per-domain cap, or never went past its seeds.

`live_scrape_check.py` is the live counterpart: it runs a prompt end to end.
This one reads a dataset that already exists, which makes it the right baseline
tool before and after a change.

Omit the dataset id to use the most recent dataset in the store.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repositories.factory import build_repo  # noqa: E402
from app.services import coverage as coverage_svc  # noqa: E402
from app.services import gaps as gaps_svc  # noqa: E402

#: Enough for any dataset a single run produces. `get_records` pages in SQL, so
#: this is a ceiling on the read, not a scan of the table.
_PAGE = 1000
_MAX_RECORDS = 20000


def _field_names(schema: list) -> list[str]:
    return [coverage_svc._field_name(f) for f in (schema or [])]


def _records(repo, dataset_id: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while len(out) < _MAX_RECORDS:
        batch = (repo.get_records(dataset_id, "", _PAGE, offset) or {}).get("records") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < _PAGE:
            break
        offset += len(batch)
    return out


def _latest_dataset(repo) -> str:
    rows = repo.list_datasets(25) or []
    for row in rows:
        if row.get("id"):
            return str(row["id"])
    return ""


def _report(repo, dataset_id: str) -> dict:
    dataset = repo.get_dataset_row(dataset_id) or {}
    schema = dataset.get("schema") or []
    records = _records(repo, dataset_id)
    coverage = coverage_svc.field_coverage(records, schema)
    derived = gaps_svc.derive(coverage)
    return {
        "dataset_id": dataset_id,
        "run_id": dataset.get("run_id", ""),
        "name": dataset.get("name", ""),
        "records": coverage.get("records", len(records)),
        "schema": _field_names(schema),
        "fields": coverage.get("fields", []),
        "gaps": derived,
        "counts": dataset.get("counts") or {},
    }


def _print(report: dict) -> None:
    print(f"dataset : {report['dataset_id']}")
    if report.get("name"):
        print(f"name    : {report['name']}")
    print(f"run     : {report.get('run_id') or '(unknown)'}")
    print(f"records : {report['records']}")
    print()
    print(f"{'field':<28} {'present':>7} {'verified':>8} {'missing':>8} "
          f"{'cover%':>7} {'proven%':>8}")
    for f in report["fields"]:
        print(f"{str(f['field']):<28} {f['present']:>7} {f['verified']:>8} "
              f"{f['missing']:>8} {f['coverage_pct']:>7.1f} {f['proven_pct']:>8.1f}")
    if report["gaps"]:
        print()
        print("open gaps (by outstanding cells):")
        for g in report["gaps"]:
            print(f"  {str(g['field']):<28} {g['category']:<14} "
                  f"missing={g['missing']} unverified={g['unverified']} "
                  f"conflicting={g['conflicting']}")
    traversal = (report.get("counts") or {}).get("traversal") or {}
    if traversal:
        print()
        print("crawl:")
        for key in ("budget", "seeds_queued", "seeds_dropped", "traversed_pages",
                    "depth_reached", "max_depth", "per_domain_cap",
                    "blocked_by_domain_cap", "blocked_by_budget"):
            if key in traversal:
                print(f"  {key:<22} {traversal[key]}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    want_json = "--json" in sys.argv[1:]
    repo = build_repo()
    try:
        dataset_id = args[0] if args else _latest_dataset(repo)
        if not dataset_id:
            print("no dataset id given and the store has no datasets")
            return 2
        report = _report(repo, dataset_id)
    finally:
        closer = getattr(repo, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
    if want_json:
        print(json.dumps(report, indent=1, default=str))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
