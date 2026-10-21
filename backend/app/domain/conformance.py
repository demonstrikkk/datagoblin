"""Checks an edition runs in its own test suite.

The model in `profile.py` validates shape. This validates the things shape
cannot: that `required_fields` and `dedupe_keys` actually name fields the
edition declares, that an allowlist entry is a host rather than a sentence, and
that a `selectors_dir` points at a directory. A profile that fails these is not
"wrong" in the way a typo is -- it is a profile that will quietly do less than
its author believes, which is the failure mode this repository keeps finding.

Editions call this; the core suite calls it on the generic profile.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from app.domain.profile import EDITION_API_VERSION, DomainProfile

_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_FIELD = re.compile(r"^[a-z0-9_]+$")


def _host_of(entry: str) -> str:
    raw = str(entry or "").strip().lower()
    if "://" in raw:
        return (urlparse(raw).hostname or "").strip(".")
    return raw.strip(".")


def check(profile: DomainProfile) -> list[str]:
    """Every problem found, in the order a reader would want to fix them."""
    problems: list[str] = []

    if int(profile.api_version) != EDITION_API_VERSION:
        problems.append(f"api_version {profile.api_version} != core API "
                        f"{EDITION_API_VERSION}")

    names = [f.name for f in profile.fields]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        problems.append(f"duplicate field names: {', '.join(duplicates)}")
    bad_names = [n for n in names if not _FIELD.match(n)]
    if bad_names:
        problems.append(f"field names must be snake_case: {', '.join(bad_names)}")

    declared = set(names)
    for label, values in (("required_fields", profile.required_fields),
                          ("dedupe_keys", profile.dedupe_keys)):
        missing = [v for v in values if v not in declared]
        if missing:
            problems.append(f"{label} names fields the profile does not declare: "
                            f"{', '.join(missing)}")

    # `generic` is the baseline the core ships with: an edition that adds
    # nothing is a mistake for a vertical, but it is the definition of this one.
    if (profile.name != "generic" and not declared
            and not (profile.field_phrases or profile.allowed_sources
                     or profile.validation_rules)):
        problems.append("the profile declares no fields, phrases, sources or "
                        "rules, so it cannot change what a plan does")

    for entry in profile.allowed_sources:
        host = _host_of(entry)
        if not host or " " in host or "." not in host or not _HOST.match(host):
            problems.append(f"allowed_sources entry is not a host: {entry!r}")

    if profile.selectors_dir and not Path(profile.selectors_dir).is_dir():
        problems.append(f"selectors_dir does not exist: {profile.selectors_dir}")

    for name in profile.field_phrases:
        if not _FIELD.match(name):
            problems.append(f"field_phrases key is not a field identifier: {name!r}")

    for prompt in profile.examples:
        if not str(prompt).strip():
            problems.append("examples contains a blank prompt")

    return problems
