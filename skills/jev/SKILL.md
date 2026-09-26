# Jev — EXPERIMENT (Phase 2 DecisionProvider; Gemini/Groq stay for plan/extract)

Verified 2026-09-24: TypeSafe System One model — state+typed questions→typed decisions (Choice/Score/Noul), no prose. Docs https://docs.typesafe.ai/ (quickstart/models/system-one) + https://github.com/typesafe-ai (SDKs). `pip install typesafe-sdk` (py≥3.10). `jev-latest=jev-1.13.0`. Hosted-only, 64k/req, text-only.
```bash
curl -X POST https://api.typesafe.ai/v1/systemone -H "Authorization: Bearer $TYPESAFE_API_KEY" \
 -d '{"state":"...","model":"jev-latest","questions":{"approve":{"type":"choice","instructions":"?","criteria":{"yes":"pass","no":"fail"}}}}}'
```
Role: accept/reject/review + relevance behind providers/decision/jev.py; code owns thresholds/branching. DATAGOBLIN use itself UNVERIFIED (proposed) — pilot only.
