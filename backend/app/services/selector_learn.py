"""Propose CSS selectors for a domain, then prove each one works.

The offline `gen_selectors` script asked a model for selectors, wrote whatever
came back to disk, and told a human to "review before use". Nothing ever
checked that a selector matched anything, so a plausible-looking `.price-tag`
that hits nothing on the real page was indistinguishable from a working one
until extraction silently came back empty.

The difference here is one rule: **a selector is only offered if running it
against a real page returns real text.** Every proposal carries the text it
actually produced, so the review is reading a sample rather than guessing at a
CSS string. Selectors that match nothing are reported as rejected, with the
reason, instead of being quietly dropped or quietly saved.

This never invents a value. It only learns where on a page the text sits, and
the extraction path that consumes these is unchanged.
"""
from __future__ import annotations

import datetime
import json
from typing import Any

from app.services import selectors as selectors_svc

#: Above this, a selector is matching a container rather than the value, and
#: the field will come back as a whole article.
_MAX_SAMPLE = 220


def _shape_validate(data: Any) -> dict:
    """Keep the shape `load_schema` accepts. Rejects rather than repairs."""
    if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
        raise ValueError("proposal lacks fields{}")
    fields: dict[str, dict] = {}
    for name, spec in data["fields"].items():
        if not isinstance(spec, dict):
            continue
        sels = [s for s in (spec.get("selectors") or []) if isinstance(s, str) and s.strip()]
        if not sels:
            continue
        snap = spec.get("snapshot") if isinstance(spec.get("snapshot"), dict) else {}
        fields[str(name)] = {"selectors": sels[:5], "snapshot": snap}
    if not fields:
        raise ValueError("proposal has no usable field selectors")
    return fields


async def propose(url: str, fields_spec: list[dict], *,
                  html: str = "", page_id: str = "") -> dict:
    """Propose selectors for `url`'s domain and verify each against real HTML.

    `html` short-circuits the fetch, which is how a stored page is reused: the
    run already paid for that page, and re-fetching it to design a selector
    would both cost more and risk learning from a different rendering than the
    one extraction will see.
    """
    from app.providers.crawl import fetcher
    from app.providers.llm import generate as llm_mod

    domain = selectors_svc.domain_of(url)
    if not domain:
        raise ValueError(f"cannot determine a domain from {url!r}")
    if not fields_spec:
        raise ValueError("no fields to learn selectors for")

    source = "stored page" if html else "live fetch"
    if not html:
        page = await fetcher.http_fetch(url)
        html = page.get("html", "") or ""
        if not html:
            raise ValueError(f"no HTML available from {url} "
                             f"({page.get('skipped', 'empty response')})")

    prompt = selectors_svc.build_schema_prompt(fields_spec, html, url)
    out = await llm_mod.structured_generate(prompt, {"type": "object"})
    proposal = _shape_validate(out.get("data", out))

    verified: dict[str, dict] = {}
    rejected: list[dict] = []
    for name, spec in proposal.items():
        working: list[str] = []
        samples: dict[str, str] = {}
        for sel in spec["selectors"]:
            try:
                text, hit = selectors_svc.css_extract(html, [sel])
            except Exception:
                rejected.append({"field": name, "selector": sel,
                                 "reason": "not a valid CSS selector"})
                continue
            if not text or not hit:
                continue
            text = " ".join(text.split())
            if len(text) > _MAX_SAMPLE:
                rejected.append({"field": name, "selector": sel,
                                 "reason": f"matched {len(text)}+ chars — likely a container"})
                continue
            working.append(sel)
            samples[hit] = text
        if not working:
            # Name every selector that was tried. "It did not work" with nothing
            # else leaves the reviewer with no way to tell a bad proposal from
            # a good one aimed at the wrong element, which is the only question
            # they would be able to act on.
            rejected.append({"field": name,
                             "selectors": list(spec["selectors"]),
                             "reason": "no proposed selector returned any text from this page"})
            continue
        verified[name] = {"selectors": working,
                          "snapshot": spec.get("snapshot") or {},
                          "samples": samples,
                          "multiple": _matches_multiple(html, working[0])}

    return {
        "domain": domain,
        "page_id": page_id,
        "page_url": url,
        "html_source": source,
        "provider": str(out.get("provider", "llm")),
        "fields": verified,
        "rejected": rejected,
        # A proposal that verified nothing is a failure, not a partial success.
        "usable": bool(verified),
    }


def _matches_multiple(html: str, selector: str) -> bool:
    try:
        from bs4 import BeautifulSoup
        return len(BeautifulSoup(html or "", "html.parser").select(selector)) > 1
    except Exception:
        return False


def build_document(draft: dict) -> dict:
    """The on-disk shape, plus what was verified and against what."""
    return {
        "domain": draft.get("domain", ""),
        "generated_by": draft.get("provider", "llm"),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "verified_against": draft.get("page_url", ""),
        "fields": {name: {"selectors": spec["selectors"],
                          "snapshot": spec.get("snapshot") or {},
                          "multiple": bool(spec.get("multiple"))}
                   for name, spec in (draft.get("fields") or {}).items()},
    }


def save(draft: dict, *, overwrite: bool = False) -> dict:
    """Persist a verified draft. Refuses to clobber silently.

    Overwriting a checked-in schema without asking is how a working domain
    quietly regresses: a bad proposal lands, extraction starts returning
    nothing, and there is no record that a good selector used to be there.
    """
    domain = selectors_svc.domain_of(str(draft.get("page_url") or draft.get("domain") or ""))
    if not domain:
        raise ValueError("draft has no domain")
    fields = draft.get("fields") or {}
    if not isinstance(fields, dict) or not fields:
        raise ValueError("draft has no verified fields to save")
    if not any(spec.get("selectors") for spec in fields.values() if isinstance(spec, dict)):
        raise ValueError("draft has no usable selectors to save")

    existing = selectors_svc.load_schema(domain)
    if existing and not overwrite:
        raise ValueError(f"{domain} already has a checked-in schema; "
                         f"resend with overwrite=true to replace it")

    path = selectors_svc._schema_path(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = build_document({**draft, "domain": domain})
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    # Read it back through the loader rather than trusting the write: a schema
    # that does not survive shape-validation would fail at extraction time,
    # in a run, far from here.
    reloaded = selectors_svc.load_schema(domain)
    return {"path": str(path), "domain": domain,
            "fields": len(reloaded["fields"]) if reloaded else 0,
            "replaced": bool(existing)}


def list_schemas() -> list[dict]:
    """Every schema on disk, with the verdict it was saved under."""
    out = []
    directory = selectors_svc.selectors_dir()
    if not directory.exists():
        return out
    for file in sorted(directory.glob("*.json")):
        schema = selectors_svc.load_schema(file.stem)
        out.append({
            "domain": file.stem,
            "valid": schema is not None,
            "fields": sorted(schema["fields"]) if schema else [],
            "field_count": len(schema["fields"]) if schema else 0,
            "generated_by": schema.get("generated_by", "") if schema else "",
            "generated_at": schema.get("generated_at", "") if schema else "",
            "item_selector": schema.get("item", "") if schema else "",
        })
    return out
