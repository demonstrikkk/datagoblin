"""Merge a profile into a compiled plan without ever overwriting intent.

Two rules decide every line here:

1. **Additive.** The plan the user and the planner agreed on is the plan. A
   profile fills what is missing; it does not replace a field the planner
   declared, a source the prompt named, or a rule already recorded.
2. **Idempotent.** Applying a profile twice produces the same plan. Plan
   compilation is retried and resumed, and a merge that appends on every pass
   would turn a retry into schema drift.

The merge runs *before* the planner's caps and probes (`planner._finalize`), so
an edition's fields participate in `_cap_total_fields`, in the probe queries and
in traversal sizing exactly like fields the model wrote.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable

from app.domain.profile import DomainProfile


def _union(existing: Any, extra: Iterable[Any]) -> list[str]:
    """Order-preserving union of two string lists, tolerant of junk."""
    out: list[str] = []
    for value in list(existing or []) + list(extra or []):
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def apply_profile(plan: dict, profile: DomainProfile) -> dict:
    """Return a new plan with `profile` applied. The input is not modified."""
    out = copy.deepcopy(plan or {})

    entity = str(out.get("entity") or "").strip()
    if not entity and profile.entity:
        out["entity"] = profile.entity

    fields = [f for f in (out.get("fields") or []) if isinstance(f, dict)]
    have = {str(f.get("name") or "") for f in fields}
    for spec in profile.fields:
        if spec.name in have:
            continue
        fields.append(spec.model_dump())
        have.add(spec.name)
    if profile.required_fields:
        required = set(profile.required_fields)
        for f in fields:
            if str(f.get("name") or "") in required:
                f["required"] = True
    out["fields"] = fields

    if not out.get("dedupe_keys") and profile.dedupe_keys:
        out["dedupe_keys"] = list(profile.dedupe_keys)

    if profile.allowed_sources:
        # Recorded separately because `planner._requested_allowed_sources` keeps
        # only the domains the *prompt* named. An edition's allowlist is curated,
        # not invented by the planner, so it is unioned back in after that filter
        # rather than being silently deleted by it.
        out["edition_allowed_sources"] = list(profile.allowed_sources)
        if not out.get("allowed_sources"):
            out["allowed_sources"] = list(profile.allowed_sources)

    out["seed_domains"] = _union(out.get("seed_domains"), profile.seed_domains)
    out["relevance_terms"] = _union(out.get("relevance_terms"), profile.relevance_terms)
    out["validation_rules"] = _union(out.get("validation_rules"), profile.validation_rules)

    phrases = dict(profile.field_phrases)
    phrases.update({str(k): str(v) for k, v in (out.get("field_phrases") or {}).items()})
    if phrases:
        out["field_phrases"] = phrases

    if profile.planner_guidance and not out.get("planner_guidance"):
        out["planner_guidance"] = profile.planner_guidance
    if profile.extractor_guidance and not out.get("extractor_guidance"):
        out["extractor_guidance"] = profile.extractor_guidance
    if profile.selectors_dir and not out.get("selectors_dir"):
        out["selectors_dir"] = profile.selectors_dir

    for key, value in (profile.defaults or {}).items():
        if key not in out or out[key] in (None, "", [], {}):
            out[key] = copy.deepcopy(value)

    out["edition"] = profile.name
    out["edition_version"] = profile.version
    return out
