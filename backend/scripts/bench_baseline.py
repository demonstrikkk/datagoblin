"""Phase-6 browser-fallback bench: 25 hard URLs through the PRODUCTION waterfall.

Each URL runs crawler._one(route=web:http) = http -> (impersonate if listed)
-> crawl4ai, with the real robots/throttle/SSRF posture. No Tavily, no LLM,
no Jev: this measures FETCH coverage only.

Outcome per URL:
  PASS   ok page with >= 500 chars of clean text (markdown or reduced html)
  THIN   ok page but < 500 chars (JS shell / pruned husk — browser-rescuable?)
  POLICY robots-denied / 401/403/paywall/auth (correct refusal; no agent can
         or should fix these under current policy)
  TECHFAIL timeout / transport error / both methods failed

Verdict rule (locked): build the bounded mini-agent iff, over non-POLICY
pages, PASS < 85% AND >= 3 THIN/TECHFAIL pages look browser-rescuable.
Results -> outputs/bench_baseline.json (stdout keeps a live log).
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.providers.crawl import fetcher  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402
from app.services import impersonation as impersonation_svc  # noqa: E402
from app.services import reducer as reducer_svc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "bench_baseline.json"

URLS = [
    # easy static (10)
    "https://example.com",
    "https://www.example.org",
    "https://httpbin.org/html",
    "http://quotes.toscrape.com/",
    "https://books.toscrape.com/",
    "https://news.ycombinator.com/",
    "https://en.wikipedia.org/wiki/Web_scraping",
    "https://www.w3.org/",
    "https://example.com/?utm_source=x&utm_medium=y&a=1",
    "https://httpbin.org/status/404",
    # medium: listings/ajax/scroll/forms (8)
    "http://quotes.toscrape.com/js/",
    "http://quotes.toscrape.com/scroll",
    "http://quotes.toscrape.com/tableful/",
    "http://quotes.toscrape.com/login",
    "https://books.toscrape.com/catalogue/page-2.html",
    "https://www.scrapethissite.com/pages/simple/",
    "https://www.scrapethissite.com/pages/ajax-javascript/",
    "https://www.scrapethissite.com/pages/forms/",
    # hard: bot walls / paywalls / big-tech (7)
    "https://www.linkedin.com/",
    "https://x.com/",
    "https://www.amazon.com/",
    "https://www.cloudflare.com/",
    "https://www.bbc.com/",
    "https://www.wsj.com/",
    "https://www.nytimes.com/",
]

PASS_MIN_CHARS = 500


async def _fetch(url: str, method: str) -> dict:
    if method == "crawl4ai":
        return await fetcher.crawl4ai_fetch(url)
    if method == "impersonate":
        return await impersonation_svc.impersonate_fetch(url)
    return await fetcher.http_fetch(url)


def _clean(page: dict) -> str:
    if page.get("markdown"):
        return page["markdown"]
    return reducer_svc.reduce_html(page.get("html", ""))


def _categorize(url: str, page: dict | None, error: str) -> tuple[str, str]:
    if page is None:
        low = error.lower()
        if any(k in low for k in ("401", "402", "403", "451", "private target",
                                  "robots", "scheme/host")):
            return "POLICY", error[:150]
        return "TECHFAIL", error[:150]
    if page.get("skipped", "").startswith("robots"):
        return "POLICY", page["skipped"]
    if page.get("skipped"):
        return "TECHFAIL", page["skipped"]
    clean = _clean(page)
    if len(clean) >= PASS_MIN_CHARS:
        return "PASS", f"{page.get('method', '')} {len(clean)} chars"
    return "THIN", f"{page.get('method', '')} {len(clean)} chars"


async def main() -> None:
    sem = asyncio.Semaphore(1)  # sequential: politeness-first bench, no hammering
    results = []
    t0 = time.monotonic()
    for i, url in enumerate(URLS, 1):
        try:
            page = await asyncio.wait_for(
                crawler_svc._one(url, "web:http", _fetch, sem), timeout=300)
            outcome, detail = _categorize(url, page, "")
        except Exception as e:  # noqa: BLE001 (bench records, never crashes)
            err = getattr(e, "message", str(e))
            outcome, detail = _categorize(url, None, err)
            page = {}
        results.append({"url": url, "outcome": outcome, "detail": detail,
                        "method": (page or {}).get("method", "")})
        print(f"[{i:02d}/25] {outcome:8s} {url} :: {detail[:80]}", flush=True)
    non_policy = [r for r in results if r["outcome"] != "POLICY"]
    passes = [r for r in results if r["outcome"] == "PASS"]
    rescuable = [r for r in non_policy if r["outcome"] in ("THIN", "TECHFAIL")]
    rate = (len(passes) / len(non_policy)) if non_policy else 0.0
    verdict = ("BUILD mini-agent" if (rate < 0.85 and len(rescuable) >= 3)
               else "SKIP mini-agent")
    summary = {"elapsed_s": round(time.monotonic() - t0, 1),
               "pass_rate_non_policy": round(rate, 3),
               "counts": {k: sum(1 for r in results if r["outcome"] == k)
                          for k in ("PASS", "THIN", "POLICY", "TECHFAIL")},
               "rescuable": [r["url"] for r in rescuable],
               "verdict": verdict, "results": results}
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"pass_rate(non-policy)={rate:.1%} counts={summary['counts']} "
          f"elapsed={summary['elapsed_s']}s -> {verdict}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
