"""Apply backend/migrations/*.sql in filename order (idempotent files).

Reads DATABASE_URL from settings (never argv/env echo — the password stays in
.env). Prints per-file table counts after applying. Safe to re-run.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import settings  # noqa: E402

import psycopg  # noqa: E402


def main() -> None:
    if not settings.DATABASE_URL:
        print("DATABASE_URL unset; aborting")
        raise SystemExit(1)
    mig_dir = Path(__file__).resolve().parents[1] / "migrations"
    files = sorted(mig_dir.glob("*.sql"))
    if not files:
        print("no migration files; aborting")
        raise SystemExit(1)
    with psycopg.connect(settings.DATABASE_URL, connect_timeout=30,
                         autocommit=True) as conn:
        for path in files:
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "select count(*) from pg_tables where schemaname = 'public'")
                total = cur.fetchone()[0]
            print(f"applied {path.name} (public tables now: {total})")
    print("migrate complete")


if __name__ == "__main__":
    main()
