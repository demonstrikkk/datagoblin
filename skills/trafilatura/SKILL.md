# Trafilatura — deterministic reduce (HTML → main Markdown)

Role: HTTP HTML → extract(markdown) → truncate → LLM. Strips nav/ads, keeps title/author/date.

Verified 2026-09-24:
- Repo https://github.com/adbar/trafilatura (Apache-2.0; <v1.8 GPL) — Docs https://trafilatura.readthedocs.io/
- v2.2.0 (PyPI 2026-07-31), Python >=3.10. `pip install trafilatura` / `trafilatura[all]`
```python
from trafilatura import fetch_url, extract
downloaded = fetch_url("https://example.com/article")
text_md = extract(downloaded, output_format="markdown", with_metadata=True, include_comments=False, include_tables=False)
```
Limits: None/short on JS-only/paywall/thin; cascade own→readability→jusText; fast=True skips fallbacks; tune favor_precision/recall, prune_xpath, target_language needs [all].
Sources: README + PyPI + usage-python/corefunctions docs.
