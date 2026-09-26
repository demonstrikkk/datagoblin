# Gemini + Groq — planner/extractor LLMs (Gemini primary → Groq fallback)

Role: structured plan + structured extract only. Always Model.model_validate() after parse.

Verified 2026-09-24:
- Gemini structured https://ai.google.dev/gemini-api/docs/structured-output (+generate-content legacy) — `pip install google-genai pydantic`, `genai.Client()`, Interactions API `response_format={mime_type:application/json, schema}`.
- Groq structured https://console.groq.com/docs/structured-outputs — `pip install groq`, strict `json_schema{strict:true, additionalProperties:false}` on select models (gpt-oss-20b/120b...). No streaming+tools with structured.
- Quoted (no hardcode): Gemini "Free Tier... up to free tier rate limits" / "limits tied to usage tier"; Groq "RPM/RPD/TPM/TPD... 429 on exceed — check limits page".
- Fallback: Gemini → 429/400/validation err → Groq strict → best-effort JSON + pydantic retry ≤3 → dead-letter.
Sources: rate-limits/pricing/billing + quickstart/text-chat pages above (Gemini page updated 2026-09-02).
