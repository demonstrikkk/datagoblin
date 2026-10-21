# Domain editions

An edition is a vertical's knowledge — vocabulary, sources, validation — that
the core engine reads as **data**. It is not a fork, not a patch, and not a
subclass of the pipeline.

```
editions/
  <name>/
    edition.json      the profile (the only file the core reads)
    selectors/        optional per-host CSS schemas
    tests/            optional, run with the edition installed
```

`edition.json` is validated by `app.domain.profile.DomainProfile`. Unknown keys
are an error, because a key the core does not read is a promise the edition
thinks it made and the engine silently ignored.

## The contract

One direction only:

```
core  --->  app.domain  <---  editions/*
```

- Core may import `app.domain`.
- `app.domain` never imports an edition. It reads a manifest or an entry point.
- An edition that ships Python may import **only** `app.domain.api` from the
  core. `backend/tests/test_edition_isolation.py` fails the build otherwise.

`api_version` is the contract version. A manifest written for a different major
is refused with a named reason and does not load; the core keeps working.

## What the core reads, and when

| key | effect |
|---|---|
| `fields` | appended to the compiled plan before the caps, so they count toward the field budget, probes and traversal sizing |
| `required_fields` | marked required, then narrowed by the core to identity fields (a profile cannot make a plan unpassable) |
| `dedupe_keys` | used only when the plan has none |
| `allowed_sources` | hard allowlist, used when the plan has none and unioned with domains the prompt named |
| `seed_domains` | unioned into the plan |
| `field_phrases` | identifier → the words a page uses; used by probe queries and `field_phrase()` |
| `relevance_terms` | added to the relevance gate's subject terms |
| `validation_rules` | unioned into the plan's recorded rules |
| `planner_guidance` | appended to the planner's instruction (bounded) |
| `extractor_guidance` | appended to the extraction prompt (bounded) |
| `selectors_dir` | relative to the manifest, resolved to an absolute path; searched before the core's `backend/selectors/` |
| `defaults` | applied only where the plan has no value |
| `examples` | metadata for `/api/editions`-style listings and conformance |
| `entity` | used only when the plan has no entity of its own |

Nothing here overwrites a value the user or the planner chose, and applying the
same profile twice produces the same plan.

## Writing one

1. Create `editions/<name>/edition.json`.
2. Declare the fields and the words pages use for them. If a field's name is a
   schema identifier (`docket_number`), give the phrase a page would print
   (`docket number`).
3. Put curated sources in `allowed_sources` only if they are hosts
   (`courtlistener.com`), never phrases.
4. Optional: `selectors/` with `<host>.json` files in the shape
   `backend/selectors/quotes.toscrape.com.json` uses.
5. Run the conformance check in the edition's own tests:

```python
from app.domain.api import DomainProfile, check_profile

def test_the_profile_conforms():
    profile = DomainProfile.model_validate_json(
        (Path(__file__).parents[1] / "edition.json").read_text(encoding="utf-8"))
    assert check_profile(profile) == []
```

## Testing, independently

```bash
# the core, with no edition installed
backend\.venv\Scripts\python.exe -m pytest backend\tests -q

# the edition, in an environment where its manifest is discoverable
EDITIONS_PATH=editions backend\.venv\Scripts\python.exe -m pytest editions\<name>\tests -q
```

A broken edition is an `error` status in `app.domain.api.list_editions()`, never
an import failure in the API process. An engine upgrade may change internals
freely; an edition upgrade may change its own manifest freely. The isolation
test is what keeps those two statements true.
