"""Verify migrated schema: transactional round-trip (rolled back) + RLS/policies.

Touches every table SupabaseRepo uses, then rolls back — zero pollution.
"""
import sys
import uuid

sys.path.insert(0, "backend")

from app.core.config import settings  # noqa: E402

import psycopg  # noqa: E402


def main() -> None:
    wid, rid, did = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    with psycopg.connect(settings.DATABASE_URL, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute("insert into workflows (id, prompt, plan_json) values (%s, %s, %s)",
                        (wid, "probe", '{"goal": "probe"}'))
            cur.execute("insert into runs (id, workflow_id, status) values (%s, %s, %s)",
                        (rid, wid, "DISCOVERING"))
            cur.execute("insert into sources (run_id, url, title) values (%s, %s, %s)",
                        (rid, "https://probe.example/a", "Probe"))
            cur.execute("insert into run_events (run_id, stage, message) values (%s, %s, %s)",
                        (rid, "DISCOVERING", "probe event"))
            cur.execute("insert into datasets (id, run_id, name) values (%s, %s, %s)",
                        (did, wid and rid, "probe-ds"))
            cur.execute("insert into dataset_records (dataset_id, row_json) values (%s, %s)",
                        (did, '{"fields": {}}'))
            cur.execute("insert into exports (id, dataset_id, format, byte_size) values (%s, %s, %s, %s)",
                        (str(uuid.uuid4()), did, "json", 8))
            cur.execute("insert into usage_ledger (charge_id, run_id, stage, units, credits) "
                        "values (%s, %s, %s, %s, %s)",
                        (f"{rid}:fetch_page", rid, "fetch_page", 2, 2))
            cur.execute("insert into seen_fingerprints (fp, run_id, url) values (%s, %s, %s)",
                        ("abc123", rid, "https://probe.example/a"))
            cur.execute("insert into sources (run_id, url) values (%s, %s) "
                        "on conflict (run_id, url) do update set title = excluded.title",
                        (rid, "https://probe.example/a"))
            cur.execute("insert into usage_ledger (charge_id, run_id, stage, units, credits) "
                        "values (%s, %s, %s, %s, %s) "
                        "on conflict (charge_id) do nothing",
                        (f"{rid}:fetch_page", rid, "fetch_page", 2, 2))
            cur.execute("""select (select count(*) from datasets d join runs r on r.id = d.run_id),
                                  (select count(*) from dataset_records),
                                  (select sum(credits) from usage_ledger)""")
            print("round-trip reads:", cur.fetchone())
            cur.execute("""select tablename from pg_tables where schemaname = 'public'
                           and rowsecurity""")
            rls = sorted(r[0] for r in cur.fetchall())
            print(f"RLS enabled ({len(rls)}/9):", rls)
            cur.execute("select count(*) from pg_policies where schemaname = 'public'")
            print("policies:", cur.fetchone()[0])
            # GRANT ALL expands to per-privilege rows; require CRUD on every table.
            cur.execute("""select count(*) from (
                             select table_name from information_schema.role_table_grants
                             where grantee = 'anon'
                             and privilege_type in ('SELECT','INSERT','UPDATE','DELETE')
                             group by table_name having count(*) = 4) t""")
            print("tables with full anon CRUD:", cur.fetchone()[0])
        conn.rollback()
    print("verify complete (rolled back, tables untouched)")


if __name__ == "__main__":
    main()
