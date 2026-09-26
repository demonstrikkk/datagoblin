"""DB probe: version + public tables + row counts. Password never printed."""
import sys

sys.path.insert(0, "backend")

from app.core.config import settings  # noqa: E402

import psycopg  # noqa: E402

WANT = ["workflows", "runs", "sources", "run_events", "datasets",
        "dataset_records", "exports", "usage_ledger", "seen_fingerprints"]


def main() -> None:
    if not settings.DATABASE_URL:
        print("DATABASE_URL unset; aborting")
        raise SystemExit(1)
    with psycopg.connect(settings.DATABASE_URL, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute("select version()")
            print("server:", str(cur.fetchone()[0]).split(",")[0])
            cur.execute(
                "select tablename from pg_tables where schemaname = 'public' order by 1")
            tables = [r[0] for r in cur.fetchall()]
            print(f"public tables ({len(tables)}):", tables)
            for t in WANT:
                if t in tables:
                    cur.execute(f'select count(*) from "{t}"')
                    print(f"  {t}: {cur.fetchone()[0]} rows")
                else:
                    print(f"  {t}: MISSING")


if __name__ == "__main__":
    main()
