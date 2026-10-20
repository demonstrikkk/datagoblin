"""The required-field gate must never empty a run silently.

A record is dropped when a required field has no value, which is right per
record. But when *every* extracted record is dropped for the same absent
required field, the run used to store an empty dataset and report "the pages
were read but contained no matching records" -- blaming discovery for pages that
did contain matching records. Measured: a live run extracted 92 records and
dropped all 92 for an absent `chief_executive`, then reported a no-yield reason
about near-empty pages.

Two behaviours are pinned here:

* a rescue pass keeps the judged records for review when the required field is
  not their identity, so the dataset is not silently empty;
* when nothing can be rescued (the identity itself is absent), the zero-yield
  reason names the required field instead of inventing a discovery problem.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(coro):
    return asyncio.run(coro)


def _plan(fields):
    return {"goal": "g", "entity": "Acme", "requested_count": 2, "max_results": 2,
            "fields": fields,
            "search_queries": ["Acme"], "seed_domains": [],
            "seed_urls": ["https://x.example/a"], "source_types": [],
            "traversal": {"max_pages_per_domain": 1}, "validation_rules": [],
            "dedupe_keys": ["company_name"], "allowed_sources": [], "max_pages": 2}


def _ctx(records, stored, events):
    async def _search(q, limit, include=None, exclude=None):
        return []

    async def _fetch(url, method):
        return {"url": url, "title": "Acme page",
                "html": "<html><body><p>Acme AI raises seed.</p></body></html>",
                "method": "http"}

    async def _llm(prompt, schema):
        return {"data": {"records": records, "coverage": "full"}, "provider": "test"}

    async def _store(rid, pl, rows, counts):
        stored.update(rows=rows, counts=counts)
        return "d1"

    async def _persist(s):
        pass

    async def _emit(ev):
        events.append(ev)

    return {"search": _search, "fetch": _fetch, "llm": _llm, "store": _store,
            "persist_source": _persist, "cancelled": lambda: False,
            "emit": _emit}


def _extracted(company, ceo=None):
    fields = {"company_name": company}
    evidence = [{"field": "company_name", "quote": "Acme AI",
                 "source_url": "https://x.example/a"}]
    if ceo is not None:
        fields["chief_executive"] = ceo
    return {"fields": fields, "evidence": evidence}


def test_gate_emptying_every_record_is_rescued_for_review():
    """A required enrichment absent from every page keeps the rows, not an
    empty dataset. `company_name` is the identity and is present, so the
    records are identifiable and must survive."""
    from app.services import runner as runner_svc

    plan = _plan([{"name": "company_name", "type": "string", "required": False},
                  {"name": "chief_executive", "type": "string", "required": True}])
    records = [_extracted("Acme AI"), _extracted("Zenith Robotics")]
    stored, events = {}, []
    ctx = _ctx(records, stored, events)

    result = run(runner_svc.execute_run("r1", plan, ctx, ctx["emit"]))
    assert result["status"] == "COMPLETED"
    assert result["records"] == 2, "the run must not silently yield nothing"
    assert stored["rows"], "records were kept"
    assert stored["counts"]["records"] == 2

    msgs = [e.get("message", "") for e in events]
    assert any("kept 2 record(s) for review" in m for m in msgs), msgs
    types = [e["type"] for e in events]
    assert "record.needs_review" in types
    assert "record.rejected" not in types, "rescued records are not rejections"


def test_zero_yield_reason_names_the_gate_not_discovery():
    """The reason must say which required field emptied the run, never invent a
    discovery problem for pages that did contain matching records."""
    from app.services import runner as runner_svc

    drops = [{"stage": "validate",
              "reason": "required field(s) not evidenced: chief_executive"},
             {"stage": "validate",
              "reason": "required field(s) not evidenced: chief_executive"}]
    reason = runner_svc._gate_no_yield_reason(drops)
    assert "2 record(s) dropped" in reason
    assert "chief_executive" in reason
    assert "no matching records" not in reason

    # No gate drops -> no reason, so the caller falls back to discovery.
    assert runner_svc._gate_no_yield_reason([]) == ""
    assert runner_svc._gate_drop_fields(drops) == ["chief_executive"]
