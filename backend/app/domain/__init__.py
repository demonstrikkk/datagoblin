"""Domain profiles: what a vertical knows, expressed without touching the core.

The core engine (discover, fetch, extract, verify, store) is domain-agnostic. A
vertical such as legal or finance differs in vocabulary, source policy and
validation, not in machinery. This package is the seam between the two.

The rule that makes the separation hold is one-directional:

    core  --->  app.domain  <---  editions/*

Core modules may import `app.domain`. `app.domain` never imports an edition: it
reads a declarative manifest from disk or a registered entry point. An edition
may import only `app.domain.api` from core; `test_edition_isolation.py` fails the
build if either direction is broken.
"""
from app.domain.api import (EDITION_API_VERSION, DomainProfile, Edition,
                            apply_profile, get, list_editions, register, reset)

__all__ = ["EDITION_API_VERSION", "DomainProfile", "Edition", "apply_profile",
           "get", "list_editions", "register", "reset"]
