"""Repository factory — one explicit choice, never a silent fallback.

The previous version inferred the adapter from whether Supabase keys happened
to be present and quietly dropped to JSONL when they were not. That is how a
fully migrated, empty Postgres sat unused while the app wrote 379KB of JSONL
that nothing else could read, with no signal anywhere that the real store was
bypassed.

`PERSISTENCE` is now explicit:

    postgres   (default when DATABASE_URL is set) — the real substrate
    local      JSONL, for tests and running with no infrastructure at all

Choosing `local` while DATABASE_URL is present is honoured rather than
overridden, and the adapter name is returned so /api/health can report it.
Anyone can therefore see which store a running system is using.
"""
from app.core.config import settings
from app.core.logging import log

#: Last chosen adapter, reported by /api/health so a silent drift is visible.
ACTIVE: dict[str, str] = {"adapter": "uninitialised", "detail": ""}


def build_repo() -> object:
    want = (settings.PERSISTENCE or "").strip().lower()

    if want == "local":
        from app.repositories.local_repo import LocalRepo
        ACTIVE.update(adapter="local",
                      detail="JSONL dev store; not production truth")
        log.warning("persistence=local (explicit) — JSONL dev store, not production truth")
        return LocalRepo()

    if want in ("", "postgres", "postgresql"):
        dsn = (settings.DATABASE_URL or "").strip()
        if not dsn:
            # No DSN and no explicit request: local is the only thing that can
            # work, so say so loudly rather than pretending postgres was chosen.
            from app.repositories.local_repo import LocalRepo
            ACTIVE.update(adapter="local",
                          detail="DATABASE_URL empty; fell back to JSONL dev store")
            log.warning("persistence=local (no DATABASE_URL) — set PERSISTENCE=local "
                        "to make this intentional, or set DATABASE_URL for postgres")
            return LocalRepo()
        from app.repositories.postgres_repo import PostgresRepo
        repo = PostgresRepo(dsn)
        ACTIVE.update(adapter="postgres", detail="Postgres via psycopg")
        log.info("persistence=postgres")
        return repo

    raise ValueError(
        f"PERSISTENCE={want!r} is not a known adapter. Use 'postgres' or 'local'.")
