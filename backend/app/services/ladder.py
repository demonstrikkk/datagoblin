"""Fill a record's fields from the cheapest route that can answer each one.

Extraction went through exactly one path: ask a language model, then check its
answer. That makes every field cost a model call whether or not the page states
the field outright, and it makes the extraction method invisible — a field lifted
from a structured feed and a field inferred from prose arrive looking identical.

So fields are filled by **rungs**, cheapest and most reliable first, and the
model is only asked about what is still missing:

    1. selectors    a checked-in schema, or pattern-field regexes, against the DOM
    2. json_ld      schema.org in the page, when the page publishes it
    3. llm          prose, for whatever the earlier rungs could not answer

Three rules make this safe rather than merely cheaper.

**First writer wins, and that is an invariant rather than a convention.** Every
rung is handed only the fields it is asked about, and the ladder refuses any
value for a field it already has. Without that guard a rung returning a field it
was not asked for could overwrite a cheaper route's answer with a worse one and
nothing downstream would look wrong.

**Cheap before expensive is the whole point.** The ladder stops as soon as every
field is filled, so the model is never called for a field the page answered
directly. This is where the cost actually goes.

**A rung that produces nothing is not an error.** Most pages answer most fields
for some methods and none for others. A rung returning nothing simply means the
next rung gets the field.

## Why this is the same code twice

`extractor.extract_page` already had a deterministic path, but it was
all-or-nothing: it only won when its coverage was `"full"`, so a page where the
selectors answered four of six fields fell through to the model and paid for all
six. That is the ladder's failure mode in one line, and it is why this is a
change to a gate rather than a new subsystem.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from app.services import confidence as conf_svc

#: A rung returns {field_name: {"value": ..., "read_method": ..., "quote": ...}}
#: for the fields it could fill. It is handed `fields` — the ones still missing —
#: and may return a subset. Returning a field it was not asked about is refused.
Rung = Callable[[list[dict], dict], dict[str, dict]]


def missing_fields(fields_spec: list[dict], filled: dict) -> list[dict]:
    """The fields still unfilled, in the plan's own order."""
    return [f for f in (fields_spec or [])
            if isinstance(f, dict) and f.get("name") and f.get("name") not in filled]


def accept(rung_values: dict, fields: list[dict], filled: dict) -> dict:
    """Take only the fields this rung was asked about, and only real values.

    The guard that makes first-writer-wins real. A rung is handed `fields`, so
    anything else it returns is unsolicited; without this a rung could clobber a
    cheaper route's answer and the result would still look well-formed.
    """
    wanted = {f["name"] for f in fields if isinstance(f, dict) and f.get("name")}
    out: dict = {}
    for name, value in (rung_values or {}).items():
        if name not in wanted or name in filled:
            continue
        if not isinstance(value, dict):
            continue
        if value.get("value") in (None, ""):
            continue
        out[name] = value
    return out


async def run_ladder(fields_spec: list[dict], rungs: list[tuple[str, Any]],
                     ctx: dict | None = None) -> tuple[dict, list[dict]]:
    """Fill `fields_spec` by walking `rungs`. Returns (filled, trace).

    `rungs` is a list of `(name, callable)`. Each callable may be sync or async
    and is called with `(fields, ctx)`. The walk stops the moment nothing is
    missing, so the last rung — the model — is skipped whenever the cheaper rungs
    between them covered the record.

    `ctx` is passed through untouched: a rung needs the page, and this module
    has no opinion about what is in it.
    """
    filled: dict = {}
    trace: list[dict] = []
    ctx = ctx or {}

    for name, rung in rungs:
        todo = missing_fields(fields_spec, filled)
        if not todo:
            trace.append({"rung": name, "asked": 0, "filled": 0, "skipped": "nothing missing"})
            break
        try:
            got = rung(todo, ctx)
            if asyncio.iscoroutine(got):
                got = await got
        except Exception:  # noqa: BLE001
            # A rung that cannot run is a rung that answered nothing. Letting it
            # fail the whole extraction would turn a missing selector schema
            # into a dead pipeline.
            trace.append({"rung": name, "asked": len(todo), "filled": 0,
                          "error": "this route could not run"})
            continue

        taken = accept(got or {}, todo, filled)
        filled.update(taken)
        trace.append({"rung": name, "asked": len(todo), "filled": len(taken),
                      "fields": sorted(taken)})

    return filled, trace


def selectors_rung(fields: list[dict], ctx: dict) -> dict[str, dict]:
    """Rung 1 — a checked-in schema or a pattern-field regex, against the DOM.

    Reads `selectors.deterministic_extract`'s per-field debug map, which already
    records which route answered each field, and re-shapes it into ladder values.
    The method it reports becomes the cell's `read_method`, so a field answered
    by CSS is labelled `dom` and a field answered by a pattern is `regex`.
    """
    plan = ctx.get("plan") or {}
    page = ctx.get("page") or {}
    from app.services import selectors as selectors_svc

    records, _coverage, debug = selectors_svc.deterministic_extract(plan, page)
    if not records:
        return {}

    # One record per container on a listing page, one record for a single-item
    # page. The ladder fills a single record's worth of fields, so the first
    # record is the one the later rungs will be asked to complete.
    first = records[0] or {}
    out: dict[str, dict] = {}
    for name, cell in (first.get("fields") or {}).items():
        if not isinstance(cell, dict) or cell.get("value") in (None, ""):
            continue
        route = (debug.get(name) or {}).get("method") or ""
        out[name] = {
            "value": cell.get("value"),
            "quote": cell.get("source", {}).get("quote", "") if isinstance(cell.get("source"), dict) else "",
            "read_method": "regex" if "regex" in str(route).lower() else "dom",
        }
    return out


def json_ld_rung(fields: list[dict], ctx: dict) -> dict[str, dict]:
    """Rung 2 — schema.org, when the page publishes it.

    Structured data is a page stating its own facts in a machine-readable form,
    so it outranks everything inferred and needs no quote check: there is no
    interpretation to verify. Returns nothing when the page carries no usable
    JSON-LD, which is the common case and not a failure.
    """
    page = ctx.get("page") or {}
    html = page.get("html") or page.get("rendered_html") or ""
    if not html:
        return {}
    from app.services import jsonld as jsonld_svc
    if not jsonld_svc.present(html):
        return {}

    out: dict[str, dict] = {}
    for f in fields:
        name = str(f.get("name") or "")
        if not name:
            continue
        text, prop = jsonld_svc.extract(html, name)
        if text:
            # The page's own text for the value, so the evidence quote locates
            # against the stored page like every other route's does.
            out[name] = {"value": text, "quote": text, "read_method": "json_ld",
                         "jsonld_path": prop}
    return out


def summarise(trace: list[dict], filled: dict) -> dict:
    """What the ladder did, for the run's stats and the run view.

    `model_avoided` is the number worth showing: fields the model was never asked
    about because a cheaper route already answered them. It is the direct
    consequence of the ladder and the reason to keep it.
    """
    asked = sum(int(t.get("asked") or 0) for t in trace)
    by_rung = {t["rung"]: int(t.get("filled") or 0) for t in trace}
    return {
        "fields_filled": len(filled),
        "field_asks": asked,
        "model_avoided": by_rung.get("selectors", 0) + by_rung.get("json_ld", 0),
        "by_rung": by_rung,
        "trace": trace,
    }


def attach_receipt(cell: dict, method: str) -> dict:
    """Stamp a filled cell with how it was read, and nothing else.

    The value, the quote and the page are untouched. This only adds the receipt,
    so a cell can never be made *more* trustworthy by being routed through a
    higher rung's label.
    """
    m = conf_svc.normalise_method(method)
    return {**cell, **conf_svc.receipt(m, verified=bool(cell.get("verified", False)))}
