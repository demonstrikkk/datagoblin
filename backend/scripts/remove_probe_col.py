"""Remove the `dg_probe_col` residue left by an endpoint sweep.

`scripts/sweep_endpoints.py` posts `{"name": "dg_probe_col"}` to
`POST /api/datasets/{id}/columns` to prove that route stopped raising NameError.
It succeeded, on two real datasets, and left a declared-but-empty field behind
on each. That residue is not cosmetic:

  - it appears in the dataset's schema, so coverage and profile count a column
    that no record ever carried -- reported as absent, which is honest, but it
    is a column a person did not ask for;
  - `field_coverage` folds it into the backlog, so it occupies a rank in the
    "what is missing" list;
  - it is a real write to real data, performed by a diagnostic script.

Dry-run by default. This edits stored schemas, so it prints what it would do and
exits unless `--apply` is passed.

Two guards, because the field is user-visible data even though it is empty:

  1. A dataset whose records actually hold values for the field is SKIPPED, not
     cleaned. Refused rather than asked about: the field may have been filled
     since the sweep, and dropping a filled column destroys evidence. Checked
     per dataset, not globally -- one dataset having values does not license
     editing another.
  2. The previous schema is printed in full before anything is written, and the
     original value is returned per dataset, so a wrong call is recoverable
     without a backup.

Usage:
    python scripts/remove_probe_col.py                 # dry run, prints the plan
    python scripts/remove_probe_col.py --apply         # perform it
    python scripts/remove_probe_col.py --field dg_probe_col
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402


def _connect():
    import psycopg
    return psycopg.connect(settings.DATABASE_URL, connect_timeout=30)


def _holding_values(cur, dataset_id: str, field: str) -> tuple[int, list]:
    """How many stored records actually carry a value for `field`.

    Records live in `dataset_records.row_json`, not a `fields` column, so this
    inspects the row JSON rather than assuming a shape.
    """
    cur.execute(
        "SELECT row_json FROM dataset_records WHERE dataset_id = %s",
        (dataset_id,))
    rows = cur.fetchall()
    holding = [r[0] for r in rows if _has_value(r[0], field)]
    return len(holding), holding


def _has_value(row_json, field: str) -> bool:
    """True only if the record carries a non-empty value, not just the key.

    A declared key holding null or empty string is the residue case and does
    not count as data. This is the difference between safe to remove and data
    loss, so it is deliberately strict about empties.

    Shape note, learned the hard way: a stored record is FLAT -- fields are
    top-level keys of `row_json`, not nested under a `fields` key. An earlier
    version of this function looked for `row_json["fields"]`, never found it,
    and therefore reported every dataset as cleanable -- including one with 142
    records whose `name` was filled on every row. A guard that cannot fail is
    worse than no guard, so the nested shape is still accepted in case an
    adapter ever stores it, but the flat shape is the one that is real.
    """
    if not isinstance(row_json, dict):
        return False
    fields = row_json.get("fields")
    if not isinstance(fields, dict):
        fields = row_json
    if field not in fields:
        return False
    cell = fields[field]
    if cell is None:
        return False
    if isinstance(cell, dict):
        # A provenance-shaped cell is data even when the value is blank,
        # because the quote and url are the evidence.
        return bool(cell.get("value") not in (None, "") or cell.get("source"))
    return cell not in ("", None)


def plan(cur, field: str) -> tuple[list, list]:
    """Split candidates into those safe to clean and those that must be left."""
    cur.execute(
        """SELECT id, name, created_at, schema_json, record_count
           FROM datasets WHERE schema_json::text ILIKE %s ORDER BY created_at""",
        (f"%{field}%",))
    cleanable, blocked = [], []
    for did, name, created, schema_json, record_count in cur.fetchall():
        names = [f.get("name") for f in (schema_json or []) if isinstance(f, dict)]
        if field not in names:
            continue
        holding, _samples = _holding_values(cur, str(did), field)
        entry = {"id": str(did), "name": name, "created_at": str(created),
                 "record_count": record_count, "records_holding_field": holding,
                 "schema_before": schema_json}
        (blocked if holding else cleanable).append(entry)
    return cleanable, blocked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Omitted means print and exit.")
    ap.add_argument("--field", default="dg_probe_col",
                    help="field name to remove (default: dg_probe_col)")
    args = ap.parse_args()

    with _connect() as conn:
        with conn.cursor() as cur:
            cleanable, blocked = plan(cur, args.field)

            print(f"field: {args.field}")
            print(f"datasets declaring it: {len(cleanable) + len(blocked)}")
            print(f"  cleanable (no records hold a value): {len(cleanable)}")
            print(f"  BLOCKED   (records hold a value)     : {len(blocked)}")
            print()

            for e in cleanable:
                print(f"  CLEAN  {e['id']}  records={e['record_count']}")
                print(f"        {e['name']}")
            for e in blocked:
                print(f"  BLOCK  {e['id']}  records={e['record_count']}  "
                      f"holding={e['records_holding_field']}")
                print(f"        {e['name']}")
                print("        left untouched: dropping a filled column "
                      "destroys evidence")

            if not cleanable and not blocked:
                print("nothing to do.")
                return 0

            print("\nschema before:")
            for e in cleanable + blocked:
                names = [f.get("name") for f in (e["schema_before"] or [])
                         if isinstance(f, dict)]
                print(f"  {e['id']}: {names}")

            if not args.apply:
                print("\nDRY RUN. Re-run with --apply to write.")
                return 0

            if blocked:
                print("\nnot applying: at least one dataset holds real values "
                      "for this field. Resolve those by hand, then re-run.")

            removed = []
            for e in cleanable:
                kept = [f for f in (e["schema_before"] or [])
                        if not (isinstance(f, dict) and f.get("name") == args.field)]
                cur.execute(
                    "UPDATE datasets SET schema_json = %s WHERE id = %s",
                    (json.dumps(kept), e["id"]))
                removed.append(e["id"])
            conn.commit()

            print(f"\napplied: removed {args.field} from {len(removed)} dataset(s)")
            for did in removed:
                print(f"  {did}")
            print("\nnote: field_freshness rows, if any, are left in place -- "
                  "they are keyed by name and report zero, which is accurate.")
            print("Caches are not invalidated here; restart the API to pick "
                  "this up, or let the TTL lapse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())