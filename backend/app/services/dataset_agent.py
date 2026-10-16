"""The dataset agent — questions answered with tools, not with SQL.

WHY THIS EXISTS

The Ask panel used to send a question to a model, get back a SQL string,
validate it against a grammar, and run it. That worked, and it had one virtue:
the query was visible. But it also had three costs the product kept paying:

  1. Every question needed a model that writes valid SQL for an unfamiliar
     schema, with a retry loop when it did not.
  2. The only questions answerable were the ones expressible as one SELECT.
     "Which fields are thin, and why?" is not.
  3. The model touched the data through a string it authored, which is the
     widest possible interface to hand an LLM next to a data store.

The agent inverts that: the model never touches the data at all. It picks from
a fixed menu of read tools, the server executes them against stored records
with bounded arguments, and the model only ever sees the results. There is no
query language to inject into, because there is no query language.

WHAT THE MODEL CAN AND CANNOT DO

  Read tools run freely. They read stored records and derived aggregates, and
  every one of them is capped: a value count returns at most 12 rows, a record
  search at most 10 records with truncated values, a full scan never exceeds
  AGENT_MAX_ROWS.

  Nothing here writes. A question that asks for a change — fill a gap,
  re-judge a field, adopt a rival, re-fetch a source — comes back as a
  *proposal* naming the existing endpoint that carries it out, and the UI runs
  that endpoint through its own approve button. There is no write tool, so
  there is nothing to mis-approve, and no new write surface was added to make
  this feature exist.

THE LOOP, BOUNDED

  1. The intent classifier (`proposals.classify`) decides whether the sentence
     is a question about the data or a request to change something. A change
     request skips the model entirely and returns the existing proposal.
  2. A question goes to the model with the schema and the tool menu. It replies
     with a plan: up to MAX_TOOL_CALLS tool calls, as JSON.
  3. The server validates every call — known tool, field in the schema, limits
     clamped — and drops anything else. A plan that is not JSON, or that names
     no valid tool, falls back to a default plan (facts + coverage + the most
     populated field's distribution), so a model failure degrades to a useful
     answer rather than an error.
  4. The results go back to the model, which composes the answer: prose, the
     figures it cites, and optionally one chart spec drawn from a distribution
     the tools actually returned.

Two model calls at most, both through `structured_generate`, so the provider
chain (OpenCode -> Groq -> Gemini) and its timeouts apply unchanged.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.core.errors import AppError, validation
from app.core.logging import log
from app.services.normalizer import is_placeholder

#: Never scan more than this many records for one question. The coverage and
#: profile paths already read in bounded prefixes; the agent matches them.
AGENT_MAX_ROWS = 2000

#: How many tool calls one plan may name. Enough for "distribution, then the
#: records behind its biggest slice, then their sources" — which is the longest
#: honest chain a question needs — and small enough that a confused model
#: cannot turn one question into a scan of the whole store.
MAX_TOOL_CALLS = 4

#: Result caps per tool. Small on purpose: the model cites figures, it does not
#: ingest the dataset.
_VALUE_LIMIT = 12
_SEARCH_LIMIT = 10
_CONFLICT_LIMIT = 10
_GAP_LIMIT = 12


def _cell_value(cell: Any) -> Any:
    if isinstance(cell, dict) and "value" in cell:
        return cell.get("value")
    return cell


def _cell_text(cell: Any, limit: int = 160) -> str:
    v = _cell_value(cell)
    if v is None:
        return ""
    s = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _schema_fields(schema: Any) -> list[str]:
    out = []
    for f in schema or []:
        name = f.get("name") if isinstance(f, dict) else f
        if name:
            out.append(str(name))
    return out


# ------------------------------------------------------------------ tools

def tool_facts(ctx: dict, args: dict) -> dict:
    """What this dataset is, in one dict. Always runs first."""
    return {
        "records": ctx["total"],
        "fields": ctx["fields"],
        "record_count_note": (
            f"{ctx['total']} stored records"
            + (f" (tools read the first {ctx['read']} for distributions)"
               if ctx.get("sampled") else "")
        ),
    }


def tool_value_counts(ctx: dict, args: dict) -> dict:
    """Top values of one field, biggest first. The ranking, filtering and
    distribution primitive the model's questions reduce to."""
    field = str(args.get("field") or "")
    if field not in ctx["fields"]:
        raise validation(f"unknown field '{field}'")
    limit = max(1, min(int(args.get("limit") or _VALUE_LIMIT), _VALUE_LIMIT))
    only = args.get("filter") if isinstance(args.get("filter"), dict) else {}
    f_field = str(only.get("field") or "")
    f_value = str(only.get("value") or "")
    counts: dict[str, int] = {}
    for rec in ctx["records"]:
        fields = rec.get("fields") or {}
        if f_field and f_field in ctx["fields"]:
            if _cell_text(fields.get(f_field)).strip().casefold() != f_value.strip().casefold():
                continue
        v = _cell_text(fields.get(field)).strip()
        if not v:
            continue
        counts[v] = counts.get(v, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return {
        "field": field,
        "distinct": len(counts),
        "values": [{"value": v, "n": n} for v, n in top],
        "truncated": len(counts) > limit,
        "filter": only or None,
    }


def tool_search_records(ctx: dict, args: dict) -> dict:
    """Records containing a text, values truncated. For "show me the rows
    behind that figure" — the evidence half of every distribution answer."""
    q = str(args.get("q") or "").strip().lower()
    if not q:
        raise validation("search needs a text")
    limit = max(1, min(int(args.get("limit") or _SEARCH_LIMIT), _SEARCH_LIMIT))
    hits = []
    for rec in ctx["records"]:
        fields = rec.get("fields") or {}
        flat = {k: _cell_text(v) for k, v in fields.items()}
        if q in json.dumps(flat, default=str).lower():
            hits.append({
                "record_id": str(rec.get("record_id") or ""),
                "fields": flat,
            })
            if len(hits) >= limit:
                break
    return {"q": args.get("q"), "matched": len(hits), "records": hits}


def tool_coverage(ctx: dict, args: dict) -> dict:
    """Per-field fill and verdict counts. For "which fields are thin" and
    "how much of this dataset is proven"."""
    matrix = ctx.get("matrix") or {}
    out = []
    for f in matrix.get("fields") or []:
        out.append({k: f.get(k) for k in
                    ("field", "records", "present", "missing", "verified",
                     "unverified", "conflicting", "coverage_pct", "proven_pct")})
    return {"fields": out}


def tool_conflicts(ctx: dict, args: dict) -> dict:
    """Open disagreements with both sides' values. Capped — the Conflicts
    section is where all of them live."""
    out = []
    for c in (ctx.get("conflicts") or [])[:_CONFLICT_LIMIT]:
        out.append({
            "record_id": str(c.get("record_id") or ""),
            "field": c.get("field"),
            "incumbent": (c.get("incumbent") or {}).get("value"),
            "rivals": [r.get("value") for r in (c.get("rivals") or [])[:3]],
        })
    return {"open": len(ctx.get("conflicts") or []),
            "shown": len(out), "conflicts": out}


def tool_gaps(ctx: dict, args: dict) -> dict:
    """What is missing and whether a stored page could fill it."""
    out = []
    for g in (ctx.get("gaps") or [])[:_GAP_LIMIT]:
        out.append({k: g.get(k) for k in
                    ("field", "category", "missing", "outstanding",
                     "attemptable", "state")})
    return {"tracked": len(ctx.get("gaps") or []), "gaps": out}


def tool_sources(ctx: dict, args: dict) -> dict:
    """Which hosts supplied the evidence, ranked by proven values."""
    hosts = (ctx.get("yield_map") or {}).get("hosts") or {}
    ranked = sorted(hosts.items(),
                    key=lambda kv: (-(kv[1].get("verified") or 0),
                                    -(kv[1].get("records") or 0)))[:_VALUE_LIMIT]
    return {"hosts": [{"host": h,
                       "verified": r.get("verified") or 0,
                       "records": r.get("records") or 0}
                      for h, r in ranked]}


#: Money-ish magnitudes, longest suffix first so `billion` is not read as `b`.
_MAGNITUDES = (
    ("trillion", 1e12), ("tn", 1e12),
    ("billion", 1e9), ("bn", 1e9), ("b", 1e9),
    ("million", 1e6), ("mn", 1e6), ("m", 1e6),
    ("thousand", 1e3), ("k", 1e3),
)


def _numeric(text: str) -> float | None:
    """A number out of stored text, or None.

    Handles `1,100,000,000`, `$1.1b`, `USD 2.5 million`, `12.5%`. Returns None
    rather than guessing on anything else, because a comparator that silently
    treats an unparseable value as zero will answer "above one million" with
    every row in the dataset.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    low = raw.lower().replace("usd", "").replace("gbp", "").replace("eur", "")
    factor = 1.0
    for suffix, mult in _MAGNITUDES:
        if suffix in low:
            factor = mult
            low = low.replace(suffix, " ")
            break
    pct = "%" in low
    if pct:
        low = low.replace("%", " ")
    cleaned = re.sub(r"[^0-9.\-]", "", low)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value * factor


def _currency(text: str) -> str:
    """The currency symbol on a stored value, or `$` when none is stated."""
    raw = str(text or "")
    for sym in ("£", "€", "¥", "₹", "A$", "C$"):
        if sym in raw:
            return sym
    return "$" if "$" in raw or "usd" in raw.lower() else ""


def tool_records_where(ctx: dict, args: dict) -> dict:
    """Rows matching conditions on their own field values. The filter primitive.

    Without this, "the five companies whose founders column is filled" has no
    honest route: the model reached for `search_records` and searched for
    founder *names it had just read out of `value_counts`*, which is circular —
    each such call found exactly the record it started from.

    Numbers are compared as plain magnitudes, which is honest arithmetic but not
    a currency conversion: `£6m` is greater than `1000000` as a number and the
    dataset does not say what it is worth in dollars. Any non-dollar currency
    seen in a numeric comparison is reported so the answer cannot quietly mix
    them.
    """
    conditions = args.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise validation("records_where needs at least one condition")
    if len(conditions) > 4:
        raise validation("records_where takes at most four conditions")
    parsed: list[tuple[str, str, Any]] = []
    for cond in conditions:
        if not isinstance(cond, dict):
            raise validation("each condition must be an object")
        field = str(cond.get("field") or "")
        if field not in ctx["fields"]:
            raise validation(f"unknown field '{field}'")
        op = str(cond.get("op") or "equals").lower()
        if op not in _CONDITION_OPS:
            raise validation(f"unsupported op '{op}'; use one of "
                             f"{', '.join(sorted(_CONDITION_OPS))}")
        parsed.append((field, op, cond.get("value")))

    limit = max(1, min(int(args.get("limit") or _SEARCH_LIMIT), _SEARCH_LIMIT))
    matched: list[dict] = []
    total = 0
    # A numeric comparator that cannot read a value must say so rather than
    # quietly treating it as zero, which would match every row.
    unread: dict[str, int] = {}
    currencies: set[str] = set()
    for rec in ctx["records"]:
        fields = rec.get("fields") or {}
        keep = True
        for field, op, want in parsed:
            text = _cell_text(fields.get(field)).strip()
            if op == "filled":
                ok = bool(text) and not is_placeholder(text)
            elif op == "empty":
                ok = not text or is_placeholder(text)
            elif op == "equals":
                ok = text.casefold() == str(want or "").strip().casefold()
            elif op == "contains":
                ok = str(want or "").strip().casefold() in text.casefold()
            else:
                mine, theirs = _numeric(text), _numeric(str(want or ""))
                if mine is None:
                    # An empty cell is absent, not unparseable. Calling it
                    # "unreadable" made a live answer report "7 values
                    # unreadable" when those 7 records simply carry no figure.
                    if text and not is_placeholder(text):
                        unread[field] = unread.get(field, 0) + 1
                    ok = False
                elif theirs is None:
                    raise validation(
                        f"cannot compare '{want}' as a number for field '{field}'")
                else:
                    ok = mine > theirs if op == "gt" else mine < theirs
                    if mine is not None and text:
                        cur = _currency(text)
                        if cur and cur != "$":
                            currencies.add(cur)
            if not ok:
                keep = False
                break
        if not keep:
            continue
        total += 1
        if len(matched) < limit:
            matched.append({
                "record_id": str(rec.get("record_id") or ""),
                "fields": {k: _cell_text(v) for k, v in fields.items()},
                "verified": sum(1 for v in fields.values()
                                if isinstance(v, dict)
                                and v.get("verification_status") == "verified"),
            })
    out = {
        "conditions": [{"field": f, "op": o, "value": v} for f, o, v in parsed],
        "matched": total,
        "shown": len(matched),
        "truncated": total > len(matched),
        "records": matched,
    }
    if currencies:
        out["currencies"] = sorted(currencies)
        out["note"] = ("compared as plain magnitudes: values in "
                       + ", ".join(sorted(currencies))
                       + " were not converted, so this mixes currencies")
    if unread:
        out["unreadable"] = unread
        out["note"] = ("values that are not numbers were skipped rather than "
                       "treated as zero, so this undercounts")
    return out


_CONDITION_OPS = {"filled", "empty", "equals", "contains", "gt", "lt"}


TOOLS = {
    "dataset_facts": (tool_facts, "records, fields, what the tools can see"),
    "value_counts": (tool_value_counts, "top values of a field: {field, limit?, filter?: {field, value}}"),
    "records_where": (tool_records_where,
                      "rows matching conditions: {conditions: [{field, op, value}], limit?} "
                      "where op is filled|empty|equals|contains|gt|lt"),
    "search_records": (tool_search_records, "records containing a text: {q, limit?}"),
    "coverage": (tool_coverage, "per-field fill and verdict counts: {}"),
    "conflicts": (tool_conflicts, "open disagreements with both sides: {}"),
    "gaps": (tool_gaps, "what is missing and whether it is fillable: {}"),
    "sources": (tool_sources, "hosts ranked by proven values: {}"),
}

_TOOL_MENU = "\n".join(f"- {name}: {desc}" for name, (_, desc) in TOOLS.items())


# ------------------------------------------------------------------- loop

def _default_plan(ctx: dict) -> list[dict]:
    """Facts, coverage, and the distribution of the most populated field.

    The plan a question gets when the model does not return a usable one.
    Deliberately the most informative cheap read, not an error: a model that
    stumbles on planning should still leave the reader with an answer shaped
    like the dataset.
    """
    plan: list[dict] = [{"tool": "dataset_facts", "args": {}},
                        {"tool": "coverage", "args": {}}]
    counts: dict[str, int] = {}
    for rec in ctx["records"][:500]:
        for k, v in (rec.get("fields") or {}).items():
            if _cell_text(v).strip():
                counts[k] = counts.get(k, 0) + 1
    if counts:
        best = max(counts, key=lambda k: counts[k])
        plan.append({"tool": "value_counts",
                     "args": {"field": best, "limit": 8}})
    return plan


def _validate_plan(raw: Any, fields: list[str]) -> list[dict]:
    """Keep what is executable, drop the rest. A plan is untrusted input — it
    names tools and arguments, and either half can be wrong."""
    calls = []
    items = raw.get("tool_calls") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    for item in items[:MAX_TOOL_CALLS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("tool") or "")
        if name not in TOOLS:
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        if name == "value_counts" and str(args.get("field") or "") not in fields:
            continue
        filt = args.get("filter")
        if isinstance(filt, dict) and str(filt.get("field") or "") not in fields + [""]:
            continue
        if name == "records_where":
            # Same rule as value_counts: a call naming a field this dataset does
            # not have will be refused at the door, and it would have consumed a
            # plan slot out of a four-call budget to be refused.
            conds = args.get("conditions")
            if not isinstance(conds, list) or not conds:
                continue
            if any(not isinstance(c, dict)
                   or str(c.get("field") or "") not in fields
                   or str(c.get("op") or "").lower() not in _CONDITION_OPS
                   for c in conds):
                continue
        calls.append({"tool": name, "args": args})
    # Facts first when the plan forgot them: an answer that cites figures
    # without saying what dataset they came from is unanchored.
    if calls and all(c["tool"] != "dataset_facts" for c in calls):
        calls.insert(0, {"tool": "dataset_facts", "args": {}})
    return calls[:MAX_TOOL_CALLS]


def _plan_prompt(question: str, fields: list[str], total: int) -> str:
    return (
        "You answer questions about a scraped dataset by picking read tools. "
        "Reply with a single JSON object only: "
        '{"tool_calls": [{"tool": "<name>", "args": {...}}]}. '
        "No prose, no backticks.\n\n"
        f"Dataset: {total} records. Fields: {', '.join(fields) or '(none)'}.\n\n"
        "Tools (read-only, results are capped):\n"
        f"{_TOOL_MENU}\n\n"
        "Rules: at most "
        f"{MAX_TOOL_CALLS} calls. Prefer value_counts for 'which/most/how many' "
        "questions, coverage for 'thin/missing/proven' questions, conflicts and "
        "gaps for 'disputed/missing' questions, sources for 'where from' "
        "questions, search_records to show the rows behind a figure. "
        "A `filter` narrows value_counts to records where another field equals "
        "a value.\n"
        "Use records_where for any question that names rows to return: "
        "'the five companies whose founders column is filled' is "
        '{"conditions": [{"field": "founders", "op": "filled"}], "limit": 5}, '
        "and 'companies above $1M' is a `gt` condition on the amount field. "
        "Do NOT reach for search_records with a value you already read out of "
        "value_counts — that finds only the row you started from. `filled` and "
        "`empty` ignore cells holding a placeholder like NA.\n\n"
        f"Question: {question}"
    )


def _answer_prompt(question: str, results: list[dict]) -> str:
    return (
        "Answer the question from these tool results. Reply with a single JSON "
        "object only, no prose, no backticks: "
        '{"answer": "<2-6 sentences, every figure traceable to the results>", '
        '"figures": [{"label": "<what>", "value": "<number or share>"}], '
        '"chart": null or {"title": "<...>", "items": [{"label": "<...>", '
        '"n": <count>}]}}. '
        "Rules: cite exact counts from the results, never round a cited figure "
        "without saying so, never mention a value no tool returned, and set "
        "chart to null unless a value_counts result with 2+ values answers the "
        "question better as bars. The chart items must be copied from that "
        "result, not invented.\n\n"
        f"Question: {question}\n\n"
        f"Tool results:\n{json.dumps(results, default=str)[:12000]}"
    )


async def answer(store, dataset_id: str, question: str, llm) -> dict:
    """Run the loop. `llm` is `structured_generate` (or a test double)."""
    from app.services import coverage as coverage_svc
    from app.services import gaps as gaps_svc
    from app.services import proposals as proposals_svc
    from app.services import yieldmap as yieldmap_svc

    schema = await asyncio.to_thread(store.get_dataset_schema, dataset_id)
    if schema is None:
        raise validation("dataset not found")
    fields = _schema_fields(schema)

    # A change request still gets its proposal. But it is *also* answered.
    #
    # This used to return early on any non-query intent, which meant one
    # misclassification cost the reader the entire answer: "founders with
    # company valuation above 1M" was read as a backfill (a bare field noun, no
    # verb) and came back as a proposal card with nothing answering it. A
    # stray card is a nuisance the reader can ignore; a missing answer is the
    # feature failing. The tools below are read-only, so answering a question
    # that also suggests a change is always safe.
    intent, confidence = proposals_svc.classify(question)
    proposal: dict | None = None
    if intent != "query":
        try:
            proposal = proposals_svc.propose(store, question, dataset_id)
        except Exception:  # noqa: BLE001 (a failed proposal must not lose the answer)
            log.warning("agent proposal failed for dataset %s", dataset_id)
            proposal = None

    got = await asyncio.to_thread(
        store.get_records, dataset_id, "", AGENT_MAX_ROWS, 0) or {}
    records = list(got.get("records") or [])
    total = int(got.get("total", len(records)) or 0)
    matrix = coverage_svc.field_coverage(records, schema)
    try:
        conflicts = coverage_svc.collect_conflicts(records)
    except Exception:  # noqa: BLE001 (conflicts are additive, not load-bearing)
        log.warning("agent conflicts read failed for dataset %s", dataset_id)
        conflicts = []
    try:
        gap_rows = gaps_svc.derive(matrix)
    except Exception:  # noqa: BLE001
        gap_rows = []
    try:
        yield_map = yieldmap_svc.build(store, dataset_id, records, light=True)
    except Exception:  # noqa: BLE001
        yield_map = {"hosts": {}}

    ctx = {"records": records, "total": total, "fields": fields,
           "read": len(records), "sampled": total > len(records),
           "matrix": matrix, "conflicts": conflicts, "gaps": gap_rows,
           "yield_map": yield_map}

    # 1. Plan.
    plan: list[dict] = []
    provider = model = ""
    try:
        raw = await llm(_plan_prompt(question, fields, total), {"type": "object"})
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        provider = raw.get("provider", "") if isinstance(raw, dict) else ""
        model = raw.get("model", "") if isinstance(raw, dict) else ""
        plan = _validate_plan(data, fields)
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 (planning is best-effort)
        log.warning("agent planning failed, using the default plan: %s", exc)
    if not plan:
        plan = _default_plan(ctx)

    # 2. Execute. Every call validated again at the door, because the plan
    # above is model output and model output is untrusted input.
    results = []
    for call in plan:
        name, args = call["tool"], call["args"]
        fn, _ = TOOLS[name]
        try:
            out = fn(ctx, args)
            summary = _summarise(name, out)
        except AppError as e:
            out, summary = {"error": e.message}, f"tool refused: {e.message}"
        except Exception as e:  # noqa: BLE001 (one bad tool must not kill the answer)
            log.warning("agent tool %s failed: %s", name, e)
            out, summary = {"error": "tool failed"}, "tool failed"
        results.append({"tool": name, "args": args,
                        "result": out, "summary": summary})

    # 3. Compose.
    try:
        raw = await llm(_answer_prompt(question, results), {"type": "object"})
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        provider = raw.get("provider", provider) if isinstance(raw, dict) else provider
        model = raw.get("model", model) if isinstance(raw, dict) else model
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("agent composing failed: %s", exc)
        data = {}
    if not isinstance(data, dict) or not str(data.get("answer") or "").strip():
        data = _fallback_answer(question, results)

    chart = _validate_chart(data.get("chart"), results)
    figures = _validate_figures(
        data.get("figures") if isinstance(data.get("figures"), list) else [],
        results)
    return {
        # The answer is always the answer. `intent` records what the classifier
        # thought, and the proposal rides alongside when there is one, but a
        # write reading never replaces the reply.
        "intent": intent,
        "confidence": confidence if intent != "query" else 1.0,
        "answer": str(data.get("answer")),
        "figures": figures,
        "chart": chart,
        "trace": [{"tool": r["tool"], "args": r["args"], "summary": r["summary"]}
                  for r in results],
        "proposals": [proposal] if proposal else [],
        "provider": provider,
        "model": model,
        "facts": {"records": total, "fields": fields},
    }


def _summarise(name: str, out: dict) -> str:
    """One line per tool call, for the "how it found out" trace."""
    if "error" in out:
        return str(out["error"])
    if name == "dataset_facts":
        return f"{out.get('records')} records, {len(out.get('fields') or [])} fields"
    if name == "value_counts":
        top = (out.get("values") or [{}])[0]
        return (f"{out.get('distinct')} distinct values in {out.get('field')}; "
                f"top is {top.get('value')} ({top.get('n')})")
    if name == "search_records":
        return f"{out.get('matched')} records contain the text"
    if name == "records_where":
        conds = ", ".join(
            f"{c.get('field')} {c.get('op')}"
            + (f" {c.get('value')}" if c.get("value") not in (None, "") else "")
            for c in (out.get("conditions") or []))
        line = f"{out.get('matched')} records match {conds or 'the conditions'}"
        if out.get("unreadable"):
            # The count is an undercount and the summary must say so, or the
            # composing model will report it as the whole population.
            line += f" ({sum(out['unreadable'].values())} value(s) unreadable)"
        return line
    if name == "coverage":
        fields = out.get("fields") or []
        thin = min(fields, key=lambda f: (f.get("present") or 0)) if fields else {}
        return (f"{len(fields)} fields; thinnest is {thin.get('field')} "
                f"({thin.get('present')}/{thin.get('records')})")
    if name == "conflicts":
        return f"{out.get('open')} open disagreements"
    if name == "gaps":
        return f"{out.get('tracked')} gaps tracked"
    if name == "sources":
        hosts = out.get("hosts") or []
        top = hosts[0] if hosts else {}
        return (f"{len(hosts)} hosts; led by {top.get('host')} "
                f"({top.get('verified')} proven)")
    return name


def _fallback_answer(question: str, results: list[dict]) -> dict:
    """The composing call failed, but the tools did not. Say what was measured
    rather than failing the whole question."""
    lines = [r["summary"] for r in results if "error" not in r["result"]]
    return {
        "answer": ("The data was read but the summary could not be composed. "
                   + ("Measured: " + "; ".join(lines) + "." if lines else "")),
        "figures": [],
        "chart": None,
    }


def _validate_figures(figures: list, results: list[dict]) -> list:
    """A cited figure must be traceable to the tool results — same rule as
    charts, extended to derived shares.

    Every integer in the figure must appear in the serialized results, with one
    exception: a trailing percentage, which must satisfy
    |pct - round(a/b*100)| <= 1 for some ordered pair (a, b) drawn from the other
    numbers in the *same* figure. A figure that fails either check is dropped,
    not fixed — inventing the correction would be the same sin one level down. An
    empty figure list is a thinner answer, never an error.

    All ordered pairs are tried, not just adjacent ones. `zip(others, others[1:])`
    pairs each number only with its neighbour in the order the model happened to
    write them, so "12 of 88 records, 13.6% covered, out of 100 candidates" had
    `others = [12, 88, 100]` and only ever checked `88/100`. A percentage
    derived from `12/100` — arithmetically correct and genuinely cited — was
    silently dropped for being correct.
    """
    import re
    pool = set(re.findall(r"\d[\d,]*", json.dumps(results, default=str)))
    pool_nums = set()
    for p in pool:
        try:
            pool_nums.add(int(p.replace(",", "")))
        except ValueError:
            continue

    # A percentage is read as one token *including its decimals*, and is removed
    # from the list of bare integers before they are checked against the pool.
    # `\d[\d,]*` on "13.6%" yields "13" and "6": the figure then claimed to cite
    # the number 13, which is nowhere in the results, and was dropped for being
    # correct. Percentages are also floats here, so a derived share can be
    # compared at 13.6 rather than being rounded to a whole number first.
    _PCT = re.compile(r"(\d[\d,]*\.?\d*)\s*%")
    _INT = re.compile(r"\d[\d,]*")

    out = []
    for f in figures:
        if not isinstance(f, dict):
            continue
        value = str(f.get("value") or "")
        if not value:
            continue

        pct_match = _PCT.search(value)
        pct: float | None = None
        if pct_match:
            try:
                pct = float(pct_match.group(1).replace(",", ""))
            except ValueError:
                pct = None

        # Bare integers only: the percentage is removed first so its digits are
        # never treated as a separately cited figure.
        bare = value
        if pct_match:
            bare = value[:pct_match.start()] + value[pct_match.end():]
        nums = [int(n.replace(",", "")) for n in _INT.findall(bare)]
        if not nums and pct is None:
            continue

        pct_ok = True
        if pct is not None:
            others = sorted({n for n in nums if n != pct})
            if len(others) >= 2:
                pct_ok = any(
                    abs(pct - round(n / d * 100, 1)) <= 1.0
                    for n in others for d in others
                    if d and n != d
                )
            else:
                # Not enough numbers for a ratio, so the percentage has to have
                # been quoted straight from the results.
                pct_ok = pct in pool_nums or round(pct) in pool_nums
            check = nums
        else:
            check = nums

        if all(n in pool_nums for n in check) and pct_ok:
            out.append({"label": str(f.get("label") or "")[:120],
                        "value": value[:120]})
    return out


def _validate_chart(chart: Any, results: list[dict]) -> dict | None:
    """A chart must be copied from a distribution a tool returned — same labels,
    same counts. Anything else is a picture of nothing."""
    if not isinstance(chart, dict):
        return None
    items = chart.get("items")
    if not isinstance(items, list) or len(items) < 2:
        return None
    wanted = [(str(i.get("label") or ""), i.get("n")) for i in items
              if isinstance(i, dict)]
    if not wanted or any(not isinstance(n, (int, float)) for _, n in wanted):
        return None
    for r in results:
        got = (r.get("result") or {}).get("values")
        if not isinstance(got, list):
            continue
        pool = {(str(g.get("value") or ""), g.get("n")) for g in got
                if isinstance(g, dict)}
        if all((label, n) in pool for label, n in wanted):
            return {"title": str(chart.get("title") or "Distribution")[:80],
                    "items": [{"label": label, "n": n} for label, n in wanted][:12]}
    return None
