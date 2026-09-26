# 01 — PRODUCT REQUIREMENTS

## Core user story

As a business user, I describe the information I need in natural language.

The platform (frozen flow — matches 00):
1. understands the requirement
2. proposes a collection plan
3. discovers permitted sources
3b. triages sources (web-http / readable / document-docling / js-crawl4ai / difficult-jina / browser-future)
4. collects information (fetch hierarchy cheapest-first, browser last resort)
5. structures it
6. validates it
6b. normalizes it (currency/date/URL/company, original preserved)
7. removes duplicates (L1 exact → L2 RapidFuzz; L3 embeddings later)
8. attaches evidence (Evidence layer: URL + quote span + retrieval meta, docs/29)
9. presents a dataset
10. lets me inspect and export it

## Product shape

```
USER → Natural Language Prompt → AI PLAN COMPILER (understand requirement,
infer fields, search strategy, validation rules, dedupe keys) → WORKFLOW PLAN
(Search→Discover URLs→Triage→Fetch→Reduce→Extract→Validate→Normalize→Deduplicate→Evidence→Save provenance)
→ Search (Tavily) + Source Router (HTTP/Trafilatura/Docling/Crawl4AI/Jina/Browser) → Clean Markdown
→ Structured LLM extraction → Validation+Cleanup → DATASET → Table Studio /
Sources-Proof / Workflow Trace → CSV/JSON export
```

## Must demonstrate end-to-end

Natural-language requirement becoming a clean, structured, source-backed dataset
through a managed workflow — with every non-null field carrying evidence.

## Non-requirements (summary)

No autonomous open-ended browsing, no auth/paywall/CAPTCHA circumvention,
no private data access. See `03-NON-GOALS.md`.
