"""Repository factory — Supabase when keys present, else explicit local-dev adapter.

The choice is logged at startup; local mode refuses production use via _adapter tags.
"""
from app.core.config import settings
from app.core.logging import log


def build_repo() -> object:
    if settings.SUPABASE_URL and settings.SUPABASE_KEY:
        from app.repositories.supabase_repo import SupabaseRepo
        log.info("persistence=supabase")
        return SupabaseRepo(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    from app.repositories.local_repo import LocalRepo
    log.info("persistence=local-dev (Supabase keys absent)")
    return LocalRepo()
