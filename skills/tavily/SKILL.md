# Tavily — DATAGOBLIN discovery backend

Role: broad web discovery only (3–5 queries/run, max_results 5, then dedupe URLs for crawl). Not extraction.

Verified 2026-09-24:
- Endpoint `POST https://api.tavily.com/search`, auth `Authorization: Bearer tvly-...`
- Client: https://github.com/tavily-ai/tavily-python (MIT) — `pip install tavily-python`
```python
from tavily import TavilyClient
c = TavilyClient(api_key="tvly-YOUR_API_KEY")
res = c.search("Who is Leo Messi?")
```
- Credits (quoted docs/api-credits + /pricing): "1,000 free API Credits every month. No credit card required." / basic|fast|ultra-fast = 1 credit, advanced = 2. PAYG "$0.008 / credit". Reset "first day of each month".
- Params: max_results 0–20 (def 10), chunks_per_source 1–3, include_domains ≤300, topic general|news|finance. Errors 429/432/433/401.
Sources: docs.tavily.com api-reference + endpoint/search + api-credits, tavily.com/pricing, github tavily-python.
