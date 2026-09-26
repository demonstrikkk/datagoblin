# Dedupe rules (deterministic, see 13-DEDUPLICATION-SPEC)

L1 exact normalized match on dedupe_keys → merge. L2 RapidFuzz token_set on names → candidate merge. L3 FastEmbed+Qdrant → ambiguous only. Never LLM-decide. Emit duplicate.detected/merged.
