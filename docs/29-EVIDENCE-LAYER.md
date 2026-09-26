# 29 — EVIDENCE LAYER (differentiator, explicit)

```
Dataset → Evidence → {Source URL, Quote span, Retrieval metadata}
```

Every ProvenanceField keeps original value + normalized value (see normalizer) + verification_status + source{url,title,quote⊂text,retrieved_at}.
UI ProofDrawer is fed ONLY from this layer. Export preserves url+status+timestamp. Conflicts keep both provenances, never averaged. No confidence %.
