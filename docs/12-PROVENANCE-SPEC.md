# 12 — PROVENANCE SPEC (cell-level, signature feature)

Every stored field:

```json
{"value":"$12.5M","verification_status":"verified|unverified|conflicting","source":{"url":"https://example.com/article","title":"Acme raises Series A","quote":"Acme AI raised $12.5 million in Series A funding.","retrieved_at":"2026-09-24T16:48:00Z"}}
```

Requirements: quote must be verbatim substring (see 11 guard); `retrieved_at` ISO-8601; every non-null field has ≥1 source; export must preserve url+status+timestamp (see 21). Proof Drawer shows value, VERIFIED badge, source name/URL, evidence quote, collected time, [Open source]. See contracts/provenance.schema.json.
