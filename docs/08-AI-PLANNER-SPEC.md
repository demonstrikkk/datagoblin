# 08 — AI PLANNER SPEC

## Output (exact shape; see contracts/workflow-plan.schema.json)

```json
{"goal":"...","entity":"company","requested_count":15,"max_results":15,
"fields":[{"name":"company_name","type":"string","description":"...","required":true}],
"search_queries":["...","...","..."],"seed_domains":[],"source_types":["company websites","public job boards","public news pages"],
"traversal":{"max_pages_per_domain":3},"validation_rules":["company_name must exist","every non-null field must have evidence"],
"dedupe_keys":["company_name","website"],"allowed_sources":["company websites","public job boards","public news pages"],"max_pages":12}
```

Field types allowed: `string, number, boolean, date, array` (+ `url` validated as string+URL rule per 11).

## Planner MUST

infer entity + requested fields + types + required flags; generate 3–5 bounded queries (not 30); generate validation requirements + dedupe keys + max_pages; produce source scope (`allowed_sources`).

## Planner MUST NOT

invent URLs; claim data exists; execute tools; write Python/code; generate arbitrary code; bypass restrictions; set progress/storage state.

Intelligence tasks only: (1) requirement understanding, (2) search strategy, (3) structured extraction schema. See prompts/planner-system.md. Via Instructor→Pydantic where available.
