# Crawl4AI — DATAGOBLIN Fetcher + Reducer + JS fallback

Role: Stage-2 fetcher in waterfall HTTP → Crawl4AI → Jina fallback. Reducer via fit_markdown. Deterministic CssExtraction first, LLM extraction only for irregular pages.

Verified (agent 2026-09-24):
- Repo https://github.com/unclecode/crawl4ai — Docs https://docs.crawl4ai.com/ (v0.9.x)
- Latest seen v0.9.4 (releases page "23 Sep"; year UNVERIFIED, presumed 2026). Licence Apache-2.0 + attribution to UncleCode. Python >=3.10.

Install (README + docs/core/installation + PyPI):
```bash
pip install -U crawl4ai
crawl4ai-setup
crawl4ai-doctor
# browser fix: python -m playwright install --with-deps chromium
```

Minimal (docs/core/quickstart):
```python
import asyncio
from crawl4ai import AsyncWebCrawler
async def main():
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun("https://example.com")
        print(result.markdown[:300])
asyncio.run(main())
```
Reduce (PruningContentFilterLXML → fit_markdown):
```python
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilterLXML
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
md_gen = DefaultMarkdownGenerator(content_filter=PruningContentFilterLXML(threshold=0.4, threshold_type="fixed"))
cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, markdown_generator=md_gen)
async with AsyncWebCrawler() as crawler:
    r = await crawler.arun("https://news.ycombinator.com", config=cfg)
    clean = r.markdown.fit_markdown
```

Limits: heavy (Chromium/RAM), anti-bot not guaranteed (success=False on 403/429/CAPTCHA), LLM extraction slower/costly + chunking required, cache BYPASS default, PDF caps 100MiB/2000p, wall_clock 300s default.
Sources: repo, LICENSE, releases, docs quickstart/installation/anti-bot-fallback, PyPI — all fetched 2026-09-24.
