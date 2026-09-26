# Jina Reader — fallback fetcher

Role: Crawl4AI fail/empty/blocked → r.jina.ai → else mark failed. Never primary.

Verified 2026-09-24: https://jina.ai/reader + https://github.com/jina-ai/reader
- `https://r.jina.ai/https://your.url` (URL→markdown) / `https://s.jina.ai/?q=` (search, top 5)
```bash
curl "https://r.jina.ai/https://www.example.com"
curl -H "Authorization: Bearer $JINA_API_KEY" -H "X-No-Cache: true" "https://r.jina.ai/https://example.com/article"
```
Quoted: "10M free tokens per new key" / "free for basic usage" / r.jina.ai 20 RPM anon, 500 free+paid, 5000 premium; s.jina.ai blocked anon, 100/1000; s.jina.ai "starting from 10,000 tokens/req". Limits tracked per IP/key (RPM+TPM).
