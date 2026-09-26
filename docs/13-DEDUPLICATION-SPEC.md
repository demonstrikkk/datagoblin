# 13 — DEDUPLICATION SPEC

Three levels, in order. Do NOT use "AI to determine duplicates" as the method.

- L1 Exact: normalize (lowercase, trim, strip Ltd/Inc/punct, canonicalize URL/domain) then exact match on `dedupe_keys` (e.g. company_name+website). Match → merge, union provenance.
- L2 Fuzzy: RapidFuzz token_set_ratio on name keys (e.g. "Acme AI" vs "Acme Artificial Intelligence"). High-similarity → candidate merge (keep both quotes, mark merged). Thresholds live in code config with fixture tests, not LLM opinion.
- L3 Semantic (optional, app works disabled): FastEmbed (ONNX, no GPU) → embedding → Qdrant similarity → candidate duplicate for ambiguous cases only. Qdrant = similarity index, Postgres = truth.

Emit `duplicate.detected` / `duplicate.merged` SSE. See prompts/dedupe-system.md (rules only, no LLM decisions).
