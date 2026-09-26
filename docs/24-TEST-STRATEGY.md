# 24 — TEST STRATEGY (fixtures first, no live-credit burn)

Fixtures (checked in):
`fixtures/webpages/{startup-a,startup-b,jobs-page,malformed}.html`,
`fixtures/search-results/*.json`, `fixtures/extracted-records/*.json`,
`fixtures/expected-datasets/{startup-dataset,jobs-dataset}.json`.

Levels: reducer unit (HTML→Markdown keeps main, drops nav/ads); validator unit (types, required, evidence-substring guard); dedupe unit (exact/fuzzy fixtures); extractor contract test (fixture HTML → records match expected + quotes are substrings); runner partial-failure test (1 bad URL → skipped, run completes partial); SSE ordering test.

No live Tavily/Gemini in unit tests (mock providers). One opt-in live smoke covers 3 demo scenarios.
