"""The published surface of the core, for domain editions.

Editions are data in this version, so nothing here is imported by a vertical at
runtime. This module is still the contract: it names exactly what an edition may
rely on, and `test_edition_isolation.py` allows an edition module to import from
`app.domain.api` and nothing else under `app`.

Keeping the surface small is the point. Every name here is a promise that a core
upgrade will not break a vertical; everything else under `app` is free to move.
"""
from __future__ import annotations

from app.core.errors import validation
from app.domain.apply import apply_profile
from app.domain.conformance import check as check_profile
from app.domain.profile import EDITION_API_VERSION, DomainProfile, generic_profile
from app.domain.registry import (Edition, get, list_editions, register, reset)

__all__ = [
    "EDITION_API_VERSION", "DomainProfile", "Edition", "apply_profile", "check_profile",
    "generic_profile", "get", "list_editions", "register", "require", "reset",
]


def require(name: str) -> DomainProfile:
    """The profile for `name`, or a validation error naming what is installed.

    Called at compile time, where a bad edition name is the caller's mistake and
    deserves a 422 with the alternatives, not a stack trace.
    """
    key = str(name or "").strip()
    edition = get(key)
    if edition is None:
        known = ", ".join(sorted(e["name"] for e in list_editions()))
        raise validation(f"Unknown edition {key!r}; installed: {known or 'none'}")
    if not edition.usable:
        raise validation(f"Edition {key!r} is not usable: {edition.error}")
    assert edition.profile is not None
    return edition.profile
