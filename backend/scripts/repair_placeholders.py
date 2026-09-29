"""Remove placeholder values already stored in existing datasets.

Why this exists. The extraction prompt has always said "put NA if the value is
absent", and the models mostly obey it. When they do not, they emit punctuation —
an em dash, a curly quote — and that became a cell: present, unverified, and
rendered in the UI as `â€”` / `â€"`. A live 15-field schema stored mostly that
way, so a dataset that was simply *incomplete* looked *corrupt*.

New extractions cannot produce these any more: `normalizer.is_placeholder` drops
them before they reach a cell, and the validator refuses them as a last net. This
script deals with the rows that were written before that was true.

What it changes, and what it cannot. Only cells whose value carries no
information at all are touched: punctuation with no alphanumeric character, or an
explicit absence word. A value with a single real character in it is left alone,
however odd it looks. Evidence is left completely untouched — the quote stays on
the page and the cell simply becomes absent, which is what the cell was already
claiming by being empty of meaning.

Idempotent: running it twice changes nothing the second time.

    python backend/scripts/repair_placeholders.py            # dry run, the default
    python backend/scripts/repair_placeholders.py --apply

The dry run reports per-dataset counts and a sample of the exact values it would
remove, so the decision is made from evidence rather than from this file's
description of itself.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from app.services.normalizer import is_placeholder  # noqa: E402


def _cell_value(cell):
    return cell.get("value") if isinstance(cell, dict) else cell


def scan(cursor, apply: bool) -> dict:
    stats = {"datasets": 0, "rows": 0, "rows_changed": 0,
             "cells_removed": 0, "samples": []}
    cursor.execute("SELECT id, name FROM datasets ORDER BY created_at DESC")
    for ds in cursor.fetchall():
        did, name = ds[0], ds[1]
        stats["datasets"] += 1
        cursor.execute(
            """SELECT id, row_json FROM dataset_records
                WHERE dataset_id=%s ORDER BY id""", (did,))
        rows = cursor.fetchall()
        changed_rows = 0
        for rid, row_json in rows:
            stats["rows"] += 1
            fields = (row_json or {}).get("fields")
            if not isinstance(fields, dict):
                continue
            drop = [k for k, v in fields.items() if is_placeholder(_cell_value(v))]
            if not drop:
                continue
            changed_rows += 1
            stats["cells_removed"] += len(drop)
            for k in drop[:3]:
                if len(stats["samples"]) < 25:
                    stats["samples"].append({
                        "dataset": (name or did)[:44],
                        "field": k,
                        "value": repr(_cell_value(fields[k]))[:40],
                    })
            if apply:
                # The field is removed rather than emptied, so coverage counts it
                # as missing - the honest state - instead of as a present cell
                # with no value in it.
                cleaned = {k: v for k, v in fields.items() if k not in drop}
                cursor.execute(
                    """UPDATE dataset_records
                          SET row_json = jsonb_set(row_json, %s, %s::jsonb)
                        WHERE id=%s""",
                    (["fields"], _jsonb(cleaned), rid))
        stats["rows_changed"] += changed_rows
    return stats


def _jsonb(obj) -> str:
    import json
    return json.dumps(obj)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it, nothing is modified.")
    args = ap.parse_args()

    import psycopg

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("  DATABASE_URL is not set; nothing to do.")
        return 1
    # PgBouncer in transaction mode cannot hold a session prepared statement.
    with psycopg.connect(url, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            stats = scan(cur, args.apply)
            if args.apply:
                conn.commit()

    mode = "APPLIED" if args.apply else "dry run (nothing written)"
    print(f"  {mode}")
    print(f"  datasets scanned : {stats['datasets']}")
    print(f"  records scanned  : {stats['rows']}")
    print(f"  records affected : {stats['rows_changed']}")
    print(f"  cells removed    : {stats['cells_removed']}")
    if stats["samples"]:
        print("  examples:")
        for s in stats["samples"]:
            print(f"    {s['dataset'][:34]:34} {s['field'][:22]:22} = {s['value']}")
    if not args.apply and stats["cells_removed"]:
        print("  re-run with --apply to write these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
