"""The shape of a domain edition.

A profile is data, not code. That is a deliberate constraint, not a lack of
ambition: an edition that can run arbitrary Python at import time can break the
core in ways no test in this repository can see. Everything a vertical needs --
the words pages use, the sources that answer, the rules that make a value valid
-- is declared here and consumed by the core at known points.

`api_version` is the contract. Core reads it before it reads anything else; an
edition written for a different major is refused with a named reason rather than
half-applied. Adding an optional field is a minor change; changing what an
existing field means is a major one.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.plan import FieldSpec

#: What the core speaks. Bumped only when the meaning of an existing key changes
#: or a required key is removed. Editions declare the version they were written
#: against and are refused on a mismatch.
EDITION_API_VERSION = 1

#: Guidance is appended to prompts that already have their own size limits. A
#: profile is not a place to smuggle in a second system prompt.
MAX_GUIDANCE_CHARS = 2000

_SLUG = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")


class DomainProfile(BaseModel):
    """One vertical's knowledge, in a form the core can consume.

    Every key is optional except `name`, because the smallest useful edition is
    "this vertical exists and calls its row a case". A profile that sets nothing
    else must behave exactly like the generic core, which is what
    `test_generic_profile_is_a_no_op` pins.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "0.1.0"
    api_version: int = EDITION_API_VERSION
    description: str = ""

    #: What one row is. Only applied when the compiled plan has no entity of its
    #: own; a user's or planner's answer is never overwritten.
    entity: str = ""

    #: Extra columns the edition knows a page of this kind carries. Appended to
    #: the plan's fields, never replacing a field the planner already declared.
    fields: list[FieldSpec] = Field(default_factory=list)

    #: Identifier -> the words a page uses. `chief_executive` is a schema name;
    #: "chief executive" is what a search engine and a page agree on.
    field_phrases: dict[str, str] = Field(default_factory=dict)

    #: Fields a record cannot exist without. The core's `_cap_required` still
    #: narrows this to identity fields, so a profile cannot make a plan
    #: unpassable by marking every enrichment required.
    required_fields: list[str] = Field(default_factory=list)
    dedupe_keys: list[str] = Field(default_factory=list)

    #: Hard allowlist. Only used when the compiled plan has none, and always
    #: unioned with domains the user named in the prompt (see `apply_profile`).
    #: This is the one source key the crawl actually enforces.
    allowed_sources: list[str] = Field(default_factory=list)

    seed_domains: list[str] = Field(default_factory=list)

    #: Subject words this vertical treats as topical, folded into the relevance
    #: gate alongside the plan's entity and field names.
    relevance_terms: list[str] = Field(default_factory=list)

    #: Free-text rules the validator records on the plan. Kept as text because
    #: the validator is deterministic and a rule it cannot parse is a rule it
    #: must not silently pretend to enforce.
    validation_rules: list[str] = Field(default_factory=list)

    planner_guidance: str = ""
    extractor_guidance: str = ""

    #: Directory of checked-in per-host CSS schemas, relative to the manifest.
    #: Resolved to an absolute path when the edition is loaded.
    selectors_dir: str = ""

    #: Applied only where the plan has no value of its own.
    defaults: dict[str, Any] = Field(default_factory=dict)

    #: Prompts this edition is meant to answer, for the UI and for conformance.
    examples: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        value = (value or "").strip()
        if not _SLUG.match(value):
            raise ValueError("edition name must be a lowercase slug "
                             "(a-z, 0-9, _ or -), 2-41 characters")
        return value

    @field_validator("planner_guidance", "extractor_guidance")
    @classmethod
    def _bounded(cls, value: str) -> str:
        value = (value or "").strip()
        if len(value) > MAX_GUIDANCE_CHARS:
            raise ValueError(f"guidance exceeds {MAX_GUIDANCE_CHARS} characters")
        return value

    @field_validator("field_phrases")
    @classmethod
    def _phrases(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(k).strip(): str(v).strip()
                for k, v in (value or {}).items()
                if str(k).strip() and str(v).strip()}


def generic_profile() -> DomainProfile:
    """The core's own behaviour, expressed as an edition.

    It exists so the merge path is exercised even when nothing is installed. It
    deliberately sets no field, source or rule: `apply_profile` with this profile
    must be a no-op apart from the edition stamp.
    """
    return DomainProfile(name="generic", version="1", description=(
        "The domain-agnostic engine. No vocabulary, no source policy, no extra "
        "validation."))
