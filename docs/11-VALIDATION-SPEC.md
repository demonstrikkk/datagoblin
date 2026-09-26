# 11 — VALIDATION SPEC (deterministic, outside LLM)

Types: `string→str`, `number→float|int`, `boolean→bool`, `date→ISO-8601`, `url→valid URL string`, `array→list`. Normalization (trim, date/number parsing, URL canonicalization) is code, not LLM.

Verdicts:
- required + missing → `invalid` (reject field/record per plan)
- type mismatch (unparseable) → `invalid`
- no evidence quote → `unverified` (value=null, status=unverified; never store guessed value)
- quote not substring of normalized source text → `unverified` (hallucination guard):
  `if value is not None and (not quote or norm(quote) not in norm(source_text)): mark_unverified()`
- conflicting sources → flag `conflicting` (keep both provenances, do not average)

No fake confidence percentages. Report counts: verified / missing-evidence / conflicting. See prompts/verifier-system.md.
