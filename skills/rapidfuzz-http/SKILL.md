# RapidFuzz + httpx + BeautifulSoup — L1/L2 + HTTP fetch

Role: L1 exact-normalized + L2 token_set fuzzy + async HTTP + HTML→text/URLs.

Verified 2026-09-24:
- https://github.com/maxbachmann/RapidFuzz/ (rel 3.14.3 Nov 2025, MIT, C++) + https://www.python-httpx.org/async/ + https://www.crummy.com/software/BeautifulSoup/bs4/doc/ (4.15.0)
```bash
pip install rapidfuzz httpx beautifulsoup4 lxml
```
```python
from rapidfuzz import fuzz
fuzz.token_set_ratio("fuzzy was a bear", "fuzzy fuzzy was a bear")  # 100.0 subset
async with httpx.AsyncClient() as client:
    r = await client.get('https://www.example.com/')
from bs4 import BeautifulSoup
soup = BeautifulSoup(html_doc, 'html.parser'); soup.get_text()
```
Limits: RapidFuzz no preprocess since 3.0, Py3.11+, token_set hides extras (use score_cutoff + processor); httpx reuse single client + timeout/Limits; bs4 parsers html.parser/lxml/html5lib tradeoff, no fetching.
