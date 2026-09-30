"""schema.org JSON-LD, read out of a page.

A page that publishes JSON-LD is stating its own facts in a machine-readable
form: a job board marks up `JobPosting` with `title`, `hiringOrganization`,
`jobLocation` and `baseSalary`; a product page marks up `Product`. Nothing is
inferred, so a value read this way needs no quote check and outranks every route
that has to interpret prose.

It is the cheapest rung and the most trustworthy one, and it is on the page for
free. Leaving it unused means asking a language model to read a sentence that the
page already published as structured data.

Two shapes are handled, because pages use both:

* a single object;
* an array, or a `@graph` wrapper around several.

Only the first record is used. This is the single-item rung: the ladder asks it
for the fields of the record it is completing, and a page carrying several
entities is better served by the container rung, which knows how to tell them
apart.
"""
from __future__ import annotations

import json
import re
from typing import Any

#: `application/ld+json` blocks. Non-greedy and case-insensitive so a
#: mis-declared charset attribute does not lose the block.
_LD_BLOCK = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)

#: schema.org properties worth reading, mapped to the field name they answer.
#: Longest-first at lookup time, so `jobLocation` is tried before `jobTitle` is
#: considered a candidate for a field called `location` — handled by the
#: per-field ordered key list in `_candidates`.
_PROPERTY_HINTS: dict[str, tuple[str, ...]] = {
    "company_name": ("hiringorganization", "employer", "brand", "publisher", "author", "name"),
    "company": ("hiringorganization", "employer", "brand", "publisher", "author", "name"),
    "title": ("title", "name", "headline"),
    "job_title": ("title", "name"),
    "role": ("title", "name"),
    "location": ("joblocation", "location", "address", "worklocation"),
    "hq_country": ("joblocation", "addresscountry", "country", "location"),
    "city": ("addresslocality", "joblocation", "address"),
    "website": ("url", "sameas", "mainentityofpage"),
    "industry": ("industry", "about", "description"),
    "description": ("description", "about", "abstract"),
    "founded": ("foundingdate", "foundingdate", "datecreated"),
    "date": ("dateposted", "validthrough", "foundingdate"),
    "salary": ("basesalary", "estimatedsalary"),
    "salary_range": ("basesalary", "estimatedsalary"),
    "employment_type": ("employmenttype", "basalary", "joblocation"),
    "open_roles": ("title", "name"),
    "valuation": ("description", "about"),
}


def _blocks(html: str) -> list[str]:
    return [m.group(1) for m in _LD_BLOCK.finditer(html or "")]


def _walk(node: Any, out: list[dict]) -> None:
    """Flatten a JSON-LD document into the objects it declares.

    Handles the three shapes pages actually use: an object, an array, and a
    `@graph` wrapper whose members are the objects of interest.
    """
    if isinstance(node, list):
        for item in node:
            _walk(item, out)
        return
    if not isinstance(node, dict):
        return
    graph = node.get("@graph")
    if isinstance(graph, (list, dict)):
        _walk(graph, out)
        return
    out.append(node)


def documents(html: str) -> list[dict]:
    """Every JSON-LD object the page declares, in document order."""
    out: list[dict] = []
    for raw in _blocks(html):
        try:
            _walk(json.loads(raw.strip()), out)
        except (ValueError, TypeError):
            # A malformed block is common and says nothing about the others.
            continue
    return out


def _as_text(value: Any) -> str:
    """Flatten a schema.org value to readable text.

    schema.org allows a bare string, an object, or a list of either, at any depth
    for almost any property, so `title` can legitimately arrive as
    `{"@value": "x"}` or `["a", "b"]`. Anything the page did not state as text
    yields "" rather than a Python repr.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        parts = [_as_text(v) for v in value]
        return "; ".join(p for p in parts if p).strip()
    if isinstance(value, dict):
        for key in ("@value", "name", "value", "text", "streetAddress",
                    "addressLocality", "addressCountry"):
            if key in value:
                return _as_text(value[key])
        return ""
    return ""


def _candidates(node: dict, field: str) -> list[str]:
    """Property names for `field`, best first.

    An exact case-insensitive match on the field name always wins, so a plan
    asking for `industry` reads a page's `industry` rather than falling through
    to a hint. Only then are the hints tried, in their declared order.
    """
    wanted = field.replace("_", "").lower()
    lowered = {k.lower(): k for k in node}
    out: list[str] = []
    if wanted in lowered:
        out.append(lowered[wanted])
    for hint in _PROPERTY_HINTS.get(field.lower(), ()):
        if hint in lowered and lowered[hint] not in out:
            out.append(lowered[hint])
    return out


def extract(html: str, field: str) -> tuple[str, str]:
    """The first declared object that states `field`. Returns (value, property).

    "First" is document order, which is the order the page chose to present its
    entities. A page carrying both an `Organization` and a `JobPosting` will
    usually declare the organisation first, so the ladder gets whatever the page
    led with — which is a reason to treat this rung as a fast path rather than an
    authority, and it is why the value still passes the field's own type check
    downstream.
    """
    for node in documents(html):
        for prop in _candidates(node, field):
            text = _as_text(node.get(prop))
            if text:
                return text, prop
    return "", ""


def extract_all(html: str) -> dict[str, str]:
    """Every hinted field the first declared object states, as a flat mapping."""
    node = None
    for candidate in documents(html):
        node = candidate
        break
    if not node:
        return {}
    out: dict[str, str] = {}
    for field in _PROPERTY_HINTS:
        for prop in _candidates(node, field):
            text = _as_text(node.get(prop))
            if text:
                out[field] = text
                break
    return out


def present(html: str) -> bool:
    """Whether the page publishes any JSON-LD at all. Cheap gate for the rung."""
    return bool(_blocks(html or ""))
