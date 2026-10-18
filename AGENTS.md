# DataGoblin — working agreement

Read this before changing anything. It exists because the answers were in five
places that disagreed with each other.

## What this is

A question in plain English goes in; a dataset comes out where every value
carries a quote that can be located in the exact stored text the quote came from,
or the cell is not trusted. FastAPI backend (`backend/`), React/Vite console
(`frontend/`), Postgres or JSONL behind one switch.

## The quality gate

Run this before committing. It is the whole gate.

```bash
# backend -- the full suite, no network, no database, no provider credits
backend\.venv\Scripts\python.exe -m pytest backend\tests -q

# frontend
cd frontend && npm test && npm run build && cd ..
```

If the change touched a route or a response shape, regenerate the contract
before the suite so `test_run_reporting` compares against the new one:

```bash
backend\.venv\Scripts\python.exe backend\scripts\gen_contract.py --write
```

### Markers

`backend/pyproject.toml` registers four. `--strict-markers` is on, so an
unregistered one is an error.

| marker | meaning |
|---|---|
| `hermetic` | offline, no DB, no provider. The default for everything here. |
| `postgres` | needs `DATABASE_URL` **exported in the process environment** |
| `live` | real network or a real provider; costs money |
| `slow` | over about a second |

```bash
pytest backend/tests -q -m "not postgres"   # skip the live-database file
```

`postgres` tests read `DATABASE_URL` from the environment, **not** from `.env`:
`conftest._no_env_file` rebuilds the settings singleton from
`Settings(_env_file=None)` so the developer's real `.env` cannot leak into a test
run. Export it before invoking pytest, or those tests will skip.

## Test counts

Do not write a test count into a document. Four of them were, they disagreed with
each other (143 / 389 / 1313 / 1620), and every one of them was wrong by the time
it was read. The suite's size is the output of the command, not a fact to be
maintained. Write the command; the number comes from running it.

## The things that are actually load-bearing

### One function may grant `verified`

`validator.settle` is the only place a cell may leave `verified`. A cell needs
two things: a support verdict, **and** a locator that resolves against the
stored text. `test_promotion_chokepoint.py` parses every module under `app/` and
fails on a literal `"verified"` written into a `verification_status` anywhere
outside `validator.py`, in both syntactic shapes. If you need to promote, route
through `settle` or `adopt`.

A decision is not evidence. A click, a merge verdict and a triage answer resolve a
*conflict*; they do not make a quote locatable.

### A `page_id` addresses one text, forever

`pages` is append-only per content change (`009_page_revisions.sql`). Identical
`content_hash` reuses the id; different content gets a new id and the next
`revision`. Never update a `pages` row in place — that is what let recorded
offsets drift silently.

### Store before you extract

A quote has to be able to cite a row that exists. If you add a path that extracts,
persist the page first and put the returned id on the page dict — `gapfill` built
the id map for years and never read it.

Persist the **reduced** text (`reducer.page_evidence_text(page)`), not the raw
fetch. Every offset indexes into that.

### No silent failure

Every fetch attempt gets an outcome: `render_attempted`, `render_status`,
`render_error_code` from the closed taxonomy, `fallback_used`,
`final_content_source`. A render that failed and was served over HTTP is
`degraded`, not `ok`, and it says so on the event stream as well as in the row.
Never discard an exception to make a path look clean — `crawler._one` used to drop
`last_err` and that is how 55% of a page's content vanished without a trace.

### The gate before the spend

Robots is checked before a judge call is spent, not only at fetch time. Keep it
that way; a dead source should cost a lookup, not a model call.

### Nothing reaches the network unvalidated

`guard_url` for the HTTP path, the `on_page_context_created` egress hook for
Chromium (`test_egress_boundaries.py`). `robots_allowed` validates its own target
because it runs ahead of the fetch-time guard on raw search results.

## Migrations

`backend/migrations/*.sql`, applied in filename order by
`backend/scripts/db_migrate.py`. Idempotent, additive, re-runnable. Every new
column needs a default. If you write a column, something must read it — a column
nobody reads is the same anti-pattern as a dead function.

## Before you claim something works

Say what you ran and what it printed. "The tests pass" is a claim about a
command; quote the number. A live run is only claimed if a live run happened.

`test_name_binding.py` and `test_event_contract.py` are static guards over the
whole source tree: unbound names, and events declared vs emitted. They exist
because both bugs shipped and were invisible in review.

## Known, deliberately not done

- `locate_quote` runs at write time only. Nothing re-checks a stored citation.
- The status vocabulary disagrees with itself in three places: `schemas/plan.py`
  declares five values, `contracts/provenance.schema.json` three, stored data has
  had six. `settle` refuses to write a seventh; that contains it, not fixes it.
- `_check_hops` is detection after the fact. Closing the DNS-rebinding window
  needs pinning — resolve, dial the resolved address, re-check each hop.
- No CI. `.github/` does not exist. The gate above is manual.