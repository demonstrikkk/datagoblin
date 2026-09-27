# 29 — EVIDENCE LAYER (the substrate everything else stands on)

```
Crawl -> STORE the page -> process the stored page -> extract -> validate -> store records
```

Not:

```
Crawl -> RAM -> extract -> discard the page
```

That second shape is what this project used to do, and it is why nothing
downstream could be trusted. A page was fetched, reduced in RAM, extracted from,
and thrown away; only a SHA-256 and a character count survived. Every evidence
quote carried `start`/`end` offsets into text that no longer existed, so no
record could ever be re-checked, and `content_hash` could never be confirmed
against anything.

## What is stored

One `pages` row per settled page (see docs/14), written at settle time inside
the crawler:

| column | why it exists |
|---|---|
| `markdown` | the exact reduced text the LLM was shown — quotes and offsets resolve against **this** |
| `raw_html` | the snapshot, for re-checking `content_hash` and anything the reducer dropped |
| `content_hash` | re-fetch and confirm the page has not changed underneath a claim |
| `parent_url`, `depth` | the crawl tree is reconstructable |
| `retrieved_at` | when this evidence was collected |
| `id` | **cited by records** |

## The one shared definition

`reducer.page_evidence_text(page)` is the single source of truth for "what text
on this page can be quoted". It is called by **both** the store and the
extractor. They used to disagree — the extractor appended media alt-text and
JSON-LD to what it read, while the stored copy was built by a separate
expression — and any difference between the two makes a verified quote
unverifiable against the stored page, which defeats the point of storing it.

A test asserts the round trip: take a record, take its cited `page_id`, slice the
stored `markdown` at the cited offsets, and require it to equal the quote.

## Provenance shape

```json
{"value": "Acme Corp",
 "verification_status": "verified",
 "source": {"url": "https://example.com/list",
            "title": "Top AI startups",
            "quote": "Acme Corp — enterprise AI platform",
            "retrieved_at": "2026-09-27T16:20:00Z",
            "reference_id": "",
            "start": 4182, "end": 4212,
            "page_id": "9f7c23da-...",
            "content_hash": "eaaa667ddcf7"}}
```

`page_id` is what turns `start`/`end` from a memory address into a citation.
Empty only when no store was supplied (unit tests, the `/api/map` probe).

## Invariants

- A stored page's text is a **superset** of what the LLM saw, so every quote
  locates.
- A record that cites a page can be re-verified without re-crawling.
- Storage failure is **not** silently swallowed: the crawler counts the page as
  failed rather than keeping an unverifiable success.
- Unverified is never upgraded to verified by anything but a judge or a
  deduction from the quote itself (see docs/11).
