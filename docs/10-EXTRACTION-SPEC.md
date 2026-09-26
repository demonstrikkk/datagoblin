# 10 — EXTRACTION SPEC

Extractor input: WORKFLOW SCHEMA (fields) + SOURCE URL + SOURCE TITLE + CLEAN PAGE CONTENT (Markdown from reducer, never raw HTML dump).

Output (exact):

```json
{"records":[{"fields":{"company_name":"Acme AI","funding_stage":"Series A"},"evidence":[{"field":"company_name","quote":"Acme AI ...","source_url":"...","source_title":"..."}]}]}
```

Rules: never return free-form prose; one call per page/chunk; schema-typed values only; every non-null field SHOULD carry a quote (missing → `unverified`, see 11/12). Via Instructor→Pydantic. See prompts/extractor-system.md and contracts/extraction-record.schema.json.
