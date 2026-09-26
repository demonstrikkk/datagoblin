"""Live scraping proof: every method against real public pages. No keys needed.

Targets are scrape-friendly by design (example.com, quotes.toscrape.com,
wikipedia, w3.org test PDF). Prints a per-method verdict; exits nonzero on any
unexpected failure. Jina anon path attempted (FEATURE_JINA forced on here only).
"""
import asyncio
import os
import sys
import time
from pathlib import Path

os.environ["FEATURE_JINA"] = "true"
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.providers.crawl import fetcher  # noqa: E402
from app.services import reducer as reducer_mod  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def main():
    # 1. Static HTTP
    t0 = time.monotonic()
    page = await fetcher.http_fetch("https://example.com")
    dt = time.monotonic() - t0
    check("http static", page["method"] == "http" and "Example Domain" in page.get("text", ""),
          f"{len(page.get('html',''))}B html in {dt:.1f}s title={page.get('title','')!r}")

    # 2. Robots verdict on a real host (either verdict is a working check)
    allowed, delay = await fetcher.robots_allowed("https://en.wikipedia.org/wiki/Python_(programming_language)")
    check("robots verdict", isinstance(allowed, bool) and delay >= 0,
          f"wikipedia article allowed={allowed} delay={delay}")

    # 3. Trafilatura reduce: smaller than HTML, keeps article text
    md = reducer_mod.reduce_html(page["html"])
    check("trafilatura reduce", 0 < len(md) < len(page["html"]) and "documentation examples" in md,
          f"{len(page['html'])}B -> {len(md)}B (boilerplate stripped, body kept)")

    # 4. Link extraction on a link-rich page
    hn = await fetcher.http_fetch("https://news.ycombinator.com")
    links = fetcher.extract_links(hn.get("html", ""), "https://news.ycombinator.com")
    check("extract_links", len(links) >= 10, f"{len(links)} same-host links")

    # 5. Dynamic JS rendering: HTTP sees ~nothing, Crawl4AI must see quotes
    js = await fetcher.http_fetch("http://quotes.toscrape.com/js/")
    http_has_quotes = "Albert Einstein" in js.get("text", "")
    t0 = time.monotonic()
    rendered = await fetcher.crawl4ai_fetch("http://quotes.toscrape.com/js/")
    dt = time.monotonic() - t0
    has_quotes = "Albert Einstein" in rendered.get("markdown", "")
    check("crawl4ai dynamic", (not http_has_quotes) and has_quotes,
          f"http_empty={not http_has_quotes} rendered={has_quotes} {len(rendered.get('markdown',''))}ch in {dt:.0f}s")

    # 6. Content-type guard on a real PDF
    pdf = await fetcher.http_fetch("https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf")
    check("pdf skipped honestly", "skipped" in pdf and "non-html" in pdf["skipped"],
          pdf.get("skipped", ""))

    # 7. Throttle: back-to-back same-host fetches serialize >= ~1s
    await fetcher.http_fetch("https://example.com")
    t0 = time.monotonic()
    await fetcher.http_fetch("https://example.com")
    gap = time.monotonic() - t0
    check("throttle floor", gap >= 0.75, f"gap={gap:.2f}s (floor=1.0s, timer granularity)")

    # 8. Jina anon fallback (free tier, may rate-limit: 429 is an honest answer too)
    try:
        j = await fetcher.jina_fetch("https://example.com")
        check("jina anon", "Example Domain" in j.get("markdown", ""),
              f"{len(j.get('markdown',''))}ch")
    except Exception as e:  # noqa: BLE001
        msg = getattr(e, "message", str(e))
        check("jina anon", "429" in msg or "rate" in msg.lower(), f"rate-limited honestly: {msg[:80]}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} live checks passed")
    if failed:
        raise SystemExit(f"FAILED: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
