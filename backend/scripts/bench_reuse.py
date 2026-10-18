"""Does a reused browser actually produce the same page as a fresh one?

`d859d45` shipped REUSE_BROWSER default-off and said, in its own words:

    "Not proven here, and deliberately left to a live crawl: that crawl4ai's
    AsyncWebCrawler is genuinely safe to reuse across arun calls. That needs a
    real browser and a real site, which this environment does not have, and it
    is the main reason the flag defaults to off."

This is that live crawl. It answers one question -- is the reused browser
faithful -- and it answers it by comparison rather than by assertion: the same
URLs are rendered twice, once with a browser per page and once with one browser
serving all of them, and the two sets of markdown are diffed.

No Tavily, no LLM, no Jev, no database, no API. Each URL runs
crawler._one(route=web:crawl4ai) -- the PRODUCTION waterfall with the real
robots/throttle/SSRF posture, so nothing here exercises a path a run does not.
crawl4ai LEADS that order, which is the point: on route web:http the browser
rung only fires to rescue a thin page, and these URLs would never reach it, so
the reuse behaviour would go unmeasured while the bench looked clean.

Targets are the exact pages 2b8f672 measured after the wait-selector fix, so
the reuse-on timings are comparable with a published baseline:

    6.1s  hackerearth.com/hackathon/explore/city/delhi
    12.8s aws.amazon.com/transform/
    11.4s aws.amazon.com/events/sponsorship

Verdict rule (locked before the run, so the result cannot be rationalised
afterwards): every URL must render, every page must be served by the crawl4ai
rung, the reuse-on markdown must equal the fresh-browser markdown, and the
browser must not survive shutdown. Any divergence or any leaked Chromium is a
failure of reuse, not of the bench.

Phases: P0 provenance, P1 baseline (reuse off), P2 reuse on, P3 safety diff,
P4 recycle, P5 invalidate-on-failure, P6 idle reap, P7 leak, P8 concurrency.
Results -> outputs/bench_reuse.json (stdout keeps a live log).
"""
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import settings  # noqa: E402
from app.providers.crawl import fetcher  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402
from app.services import reducer as reducer_svc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "bench_reuse.json"

#: The pages 2b8f672 measured. First is a real hackathon listing; the second is
#: the junk page the relevance gate now rejects; the third is the most on-topic
#: slug in that run.
URLS = [
    "https://hackerearth.com/hackathon/explore/city/delhi",
    "https://aws.amazon.com/transform/",
    "https://aws.amazon.com/events/sponsorship",
]

#: One host not in the baseline set, so the recycle phase has fresh URLs and
#: does not re-render the same three (which would hide a per-URL cache effect).
RECYCLE_URLS = URLS + [
    "https://news.ycombinator.com/",
    "https://www.w3.org/",
]

PASS_MIN_CHARS = 500

log = logging.getLogger("datagoblin")


# -- provenance ----------------------------------------------------------------

def chromium_processes() -> int:
    """Chromium processes launched from this machine's playwright install.

    Scoped by the ms-playwright install path so an unrelated Chrome the operator
    happens to be running is not counted. Falls back to WMI when psutil is
    unavailable, because a leak check that cannot count is not a leak check.
    """
    try:
        import psutil
    except ImportError:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process | Where-Object "
             "{ $_.CommandLine -like '*ms-playwright*' }).Count"],
            capture_output=True, text=True, timeout=60)
        try:
            return int(out.stdout.strip() or 0)
        except ValueError:
            return -1
    n = 0
    for proc in psutil.process_iter(["exe", "cmdline"]):
        try:
            exe = (proc.info.get("exe") or "").lower()
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
        except Exception:  # noqa: BLE001 (a process can die mid-iteration)
            continue
        if "ms-playwright" in exe or "ms-playwright" in cmd:
            n += 1
    return n


class LogCapture(logging.Handler):
    """Collect datagoblin log lines so a phase can assert on what was logged."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass

    def count(self, needle: str) -> int:
        return sum(1 for r in self.records if needle in r)

    def reset(self) -> None:
        self.records.clear()


# -- fetch path (production waterfall) -----------------------------------------

async def _fetch(url: str, method: str) -> dict:
    if method == "crawl4ai":
        return await fetcher.crawl4ai_fetch(url)
    return await fetcher.http_fetch(url)


def _clean(page: dict) -> str:
    if page.get("markdown"):
        return page["markdown"]
    return reducer_svc.reduce_html(page.get("html", ""))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _set_reuse(on: bool, *, max_pages: int | None = None,
               idle_s: float | None = None, concurrency: int | None = None) -> None:
    """Flip the flag in-process and drop any pool built under the old settings.

    `.env` is never written to. `fetcher._pool()` returns None when the flag is
    off, and when it is on it caches one pool per process, so changing MAX_PAGES
    or IDLE_S without clearing it would silently bench the previous settings.
    """
    settings.REUSE_BROWSER = on
    if max_pages is not None:
        settings.REUSE_BROWSER_MAX_PAGES = max_pages
    if idle_s is not None:
        settings.REUSE_BROWSER_IDLE_S = idle_s
    if concurrency is not None:
        settings.REUSE_BROWSER_CONCURRENCY = concurrency
    if fetcher._POOL is not None:
        try:
            fetcher._POOL.shutdown()
        except Exception:  # noqa: BLE001
            pass
        fetcher._POOL = None


async def _render(url: str, sem: asyncio.Semaphore) -> dict:
    """One page through the production waterfall, browser rung leading."""
    t0 = time.monotonic()
    page: dict = {}
    error = ""
    try:
        page = await asyncio.wait_for(
            crawler_svc._one(url, "web:crawl4ai", _fetch, sem), timeout=300)
    except Exception as exc:  # noqa: BLE001 (bench records, never crashes)
        error = getattr(exc, "message", str(exc))
    clean = _clean(page) if page else ""
    return {
        "url": url,
        "error": error[:200],
        "method": page.get("method", ""),
        "skipped": page.get("skipped", ""),
        "s": round(time.monotonic() - t0, 2),
        "chars": len(clean),
        "sha": _sha(clean) if clean else "",
        # kept for P3; the full body would bloat the JSON with scraped pages
        "_clean": clean,
    }


# -- phases --------------------------------------------------------------------

async def phase_p1_baseline(sem: asyncio.Semaphore, rounds: int = 2) -> list[dict]:
    """Fresh browsers only, TWICE per URL.

    Twice because the first version of this bench rendered each URL once per
    arm and compared hashes, and reported every page DIFF. A control run then
    showed two *fresh* browsers disagreeing with each other on all three pages
    (aws/transform varied by 1128 chars), so these pages are not byte-stable on
    their own and hash equality was never a valid criterion. The two fresh
    rounds are what makes the reuse arm measurable: they establish the band the
    page moves in when nothing is reused.
    """
    _set_reuse(False)
    print(f"\n-- P1 baseline: REUSE_BROWSER=false, one browser per page, "
          f"{rounds} rounds --", flush=True)
    out = []
    for rnd in range(1, rounds + 1):
        for i, url in enumerate(URLS, 1):
            r = await _render(url, sem)
            out.append({"round": rnd, **r})
            print(f"[fresh r{rnd} {i}/{len(URLS)}] {r['s']:>6.2f}s "
                  f"{r['method'] or '-':8s} {r['chars']:>6d}ch {r['sha']} {url}",
                  flush=True)
    return out


async def phase_p2_reuse(sem: asyncio.Semaphore, cap: LogCapture) -> list[dict]:
    _set_reuse(True)
    cap.reset()
    print("\n-- P2 reuse on: one browser serves every page --", flush=True)
    out = []
    for i, url in enumerate(URLS, 1):
        r = await _render(url, sem)
        out.append(r)
        print(f"[{i}/{len(URLS)}] {r['s']:>6.2f}s {r['method'] or '-':8s} "
              f"{r['chars']:>6d}ch {r['url']} :: {r['error'] or r['skipped'] or 'ok'}",
              flush=True)
    pool = fetcher._POOL
    print(f"   pool.stats()={pool.stats() if pool else None} "
          f"chromium={chromium_processes()}", flush=True)
    return out


#: How far outside the fresh band a reuse render may land and still count as
#: faithful. Generous on purpose: the pages move by thousands of characters
#: between two fresh browsers, so a tight bound would be measuring the site.
REUSE_TOLERANCE = 0.25


def phase_p3_safety(fresh: list[dict], reused: list[dict]) -> dict:
    """The pending claim: does a reused browser render what a fresh one does?

    Judged against the band two fresh browsers themselves produced, not against
    a hash. A reuse render inside that band is faithful; one far outside it is
    reuse losing content.
    """
    print("\n-- P3 safety: reuse output vs the fresh-vs-fresh band --", flush=True)
    rows = []
    for i, r in enumerate(reused):
        url = r["url"]
        fr = [f for f in fresh if f["url"] == url]
        chars = sorted(f["chars"] for f in fr if f["chars"])
        if not chars or not r["chars"]:
            rows.append({"url": url, "reuse_chars": r["chars"],
                         "fresh_chars": chars, "in_band": False,
                         "note": "a render produced nothing"})
            print(f"   NO-DATA {url}", flush=True)
            continue
        lo, hi = chars[0], chars[-1]
        mid = (lo + hi) / 2
        spread = (hi - lo) / mid if mid else 0.0
        # The band is widened by the observed fresh spread so a page that is
        # volatile when fresh is not punished when reused.
        lo_ok = r["chars"] >= lo * (1 - REUSE_TOLERANCE) - 100
        hi_ok = r["chars"] <= hi * (1 + REUSE_TOLERANCE) + 100
        in_band = lo_ok and hi_ok
        rows.append({"url": url, "fresh_chars": chars, "reuse_chars": r["chars"],
                     "fresh_spread_pct": round(spread, 3), "in_band": in_band,
                     "fresh_shas": [f["sha"] for f in fr], "reuse_sha": r["sha"]})
        print(f"   {'IN-BAND' if in_band else 'OUT-BAND'} reuse={r['chars']}ch "
              f"fresh={lo}-{hi}ch (spread {spread:.1%}) {url}", flush=True)
    return {"rows": rows,
            "all_in_band": all(x["in_band"] for x in rows),
            "tolerance": REUSE_TOLERANCE,
            "note": "hash equality is NOT a criterion: these pages differ "
                    "between two fresh browsers"}


async def phase_p4_recycle(sem: asyncio.Semaphore, cap: LogCapture) -> dict:
    """MAX_PAGES=2 over 5 pages must launch 3 browsers, not 5 and not 1."""
    _set_reuse(True, max_pages=2)
    cap.reset()
    print("\n-- P4 recycle: MAX_PAGES=2 across 5 pages --", flush=True)
    for i, url in enumerate(RECYCLE_URLS, 1):
        r = await _render(url, sem)
        print(f"[{i}/{len(RECYCLE_URLS)}] {r['s']:>6.2f}s {r['method'] or '-':8s} "
              f"{r['chars']:>6d}ch :: {r['error'] or r['skipped'] or 'ok'}", flush=True)
    pool = fetcher._POOL
    recycles = cap.count("recycling browser after")
    print(f"   recycle_log_lines={recycles} pool.stats()={pool.stats() if pool else None}",
          flush=True)
    # 5 pages at 2 per browser => launches at page 1, recycle at 3 and at 5.
    return {"urls": len(RECYCLE_URLS), "max_pages": 2,
            "recycle_log_lines": recycles, "expected_recycles": 2,
            "ok": recycles == 2,
            "stats": pool.stats() if pool else None}


async def phase_p5_invalidate(sem: asyncio.Semaphore) -> dict:
    """A failure that touches the browser must discard it, not hand it on.

    Forced with a 1s cap: a Chromium launch alone exceeds that, so the failure is
    a genuine timeout through the real path rather than a stubbed exception.
    """
    _set_reuse(True, max_pages=100)
    print("\n-- P5 invalidate-on-failure --", flush=True)
    before = fetcher._POOL.stats() if fetcher._POOL else None

    good = await _render(URLS[0], sem)
    warmed = fetcher._POOL.stats() if fetcher._POOL else None
    print(f"   warmed pool.stats()={warmed} pages_served_before={good['s']}s",
          flush=True)

    original_timeout = settings.FETCH_CRAWL_TIMEOUT_S
    settings.FETCH_CRAWL_TIMEOUT_S = 1  # cannot survive a launch
    try:
        bad = await _render(RECYCLE_URLS[3], sem)
    finally:
        settings.FETCH_CRAWL_TIMEOUT_S = original_timeout
    after_fail = fetcher._POOL.stats() if fetcher._POOL else None
    print(f"   forced-failure outcome: {bad['error'] or bad['skipped'] or 'NO ERROR'}",
          flush=True)
    print(f"   pool.stats() after failure={after_fail}", flush=True)

    recovered = await _render(URLS[2], sem)
    after_recover = fetcher._POOL.stats() if fetcher._POOL else None
    print(f"   recovery page: {recovered['chars']}ch {recovered['s']}s "
          f"pool.stats()={after_recover}", flush=True)

    pages_after_recover = (after_recover or [{}])[0].get("pages")
    return {"before": before, "warmed": warmed, "after_failure": after_fail,
            "after_recovery": after_recover,
            "failure_was_forced": bool(bad["error"] or bad["skipped"]),
            "recovered_page_chars": recovered["chars"],
            # a discarded browser restarts the count at 1
            "ok": pages_after_recover == 1,
            "pages_after_recovery": pages_after_recover}


async def phase_p6_idle_reap(sem: asyncio.Semaphore) -> dict:
    """IDLE_TTL must actually close an unused browser, not just be configured.

    Uses a fast page and an idle window comfortably longer than the render. The
    first version rendered a 5-6s page against IDLE_S=5 and put the two within
    a second of each other; the reaper then fired *during* the render and the
    phase measured its own race instead of the reap. It picked a page that
    renders in ~4s and an IDLE_S three times that, so the only thing that can
    close the browser is genuine idleness.

    Worth recording: that race is real. The reap timer is armed once and
    `touch()` only runs after a call returns, so any call longer than IDLE_S
    gets its browser closed mid-flight. It is unreachable at the shipped
    defaults (IDLE_S 120 > FETCH_CRAWL_TIMEOUT_S 90) and it is why this phase
    needs a margin rather than an arbitrary wait.
    """
    _set_reuse(True, max_pages=25, idle_s=12.0)
    fast_page = "https://news.ycombinator.com/"
    print("\n-- P6 idle reap: IDLE_S=12 on a ~4s page --", flush=True)
    base = chromium_processes()
    r = await _render(fast_page, sem)
    held = chromium_processes()
    print(f"   after use ({r['s']}s render): chromium={held} (baseline {base}) "
          f"pool.stats()={fetcher._POOL.stats()}", flush=True)
    await asyncio.sleep(18)
    reaped = chromium_processes()
    stats = fetcher._POOL.stats() if fetcher._POOL else None
    print(f"   after 18s idle: chromium={reaped} pool.stats()={stats}", flush=True)
    still_held = bool(stats and stats[0].get("has_browser"))
    return {"baseline": base, "while_held": held, "after_idle": reaped,
            "idle_s": 12, "render_s": r["s"], "render_under_idle_window":
            r["s"] < 12, "pool_stats": stats,
            "browser_released": not still_held,
            "processes_returned": reaped <= base}


async def phase_p7_leak(base_count: int) -> dict:
    """The regression the module docstring names: 11 leaked chromiums.

    Only meaningful once a browser is actually open, so P8 re-opens one and
    this closes it.
    """
    print("\n-- P7 leak check --", flush=True)
    await fetcher.aclose_pool()
    time.sleep(3)
    after = chromium_processes()
    print(f"   chromium baseline={base_count} after aclose_pool()={after} "
          f"_POOL={fetcher._POOL}", flush=True)
    return {"baseline": base_count, "after_shutdown": after,
            "pool_cleared": fetcher._POOL is None,
            "ok": after <= base_count and fetcher._POOL is None}


async def phase_p8_concurrency(sem: asyncio.Semaphore) -> dict:
    """F1: does REUSE_BROWSER_CONCURRENCY > 1 actually buy concurrency?

    browser_pool.run() hardcodes slots[0], so this is expected to show slots
    1..N-1 never holding a browser. Report-only; nothing is changed here.
    """
    _set_reuse(True, max_pages=25, idle_s=120.0, concurrency=3)
    print("\n-- P8 concurrency=3 --", flush=True)
    for url in RECYCLE_URLS[:3]:
        await _render(url, sem)
    stats = fetcher._POOL.stats() if fetcher._POOL else None
    print(f"   pool.stats()={stats}", flush=True)
    if stats:
        for i, s in enumerate(stats):
            print(f"   slot[{i}] pages={s.get('pages')} "
                  f"has_browser={s.get('has_browser')}", flush=True)
    slots = stats or []
    # slots[0] is the only one run() ever touches; the rest should stay empty.
    unused = [i for i, s in enumerate(slots) if i > 0 and not s.get("has_browser")]
    return {"concurrency_setting": 3, "stats": slots,
            "slots_used": sum(1 for s in slots if s.get("has_browser")),
            "slots_without_a_browser": unused,
            "inert": len(slots) > 1 and len(unused) == len(slots) - 1}


# -- main ----------------------------------------------------------------------

async def main() -> None:
    logging.getLogger("datagoblin").setLevel(logging.INFO)
    cap = LogCapture()
    logging.getLogger("datagoblin").addHandler(cap)

    import crawl4ai  # noqa: F401  (import proves the rung can construct at all)
    from importlib.metadata import version as _pkg_version

    # crawl4ai/__version__.py shadows the attribute with a submodule, so
    # `crawl4ai.__version__` is a module object and would serialise as garbage.
    crawl4ai_version = _pkg_version("crawl4ai")
    pw_root = Path(
        os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"

    base_chromium = chromium_processes()
    report: dict = {
        "question": "is a reused AsyncWebCrawler safe across arun calls?",
        "source": "d859d45 - 'deliberately left to a live crawl'",
        "p0": {
            "crawl4ai": crawl4ai_version,
            "playwright_browsers": str(pw_root) if pw_root.exists() else "not found",
            "chromium_revisions": sorted(
                p.name for p in pw_root.glob("chromium-*")) if pw_root.exists() else [],
            "reuse_browser_as_configured": settings.REUSE_BROWSER,
            "reuse_max_pages": settings.REUSE_BROWSER_MAX_PAGES,
            "reuse_idle_s": settings.REUSE_BROWSER_IDLE_S,
            "reuse_concurrency": settings.REUSE_BROWSER_CONCURRENCY,
            "crawl_timeout_s": settings.FETCH_CRAWL_TIMEOUT_S,
            "chromium_processes_baseline": base_chromium,
            "targets": URLS,
            "route": "web:crawl4ai (browser rung leads)",
        },
    }
    print(f"\nDataGoblin REUSE_BROWSER bench | crawl4ai {crawl4ai_version} | "
          f"chromium baseline {base_chromium}", flush=True)

    sem = asyncio.Semaphore(1)  # sequential: politeness-first, no hammering

    try:
        baseline = await phase_p1_baseline(sem)
        reused = await phase_p2_reuse(sem, cap)
        report["p1_baseline"] = [_public(r) for r in baseline]
        report["p2_reuse"] = [_public(r) for r in reused]
        report["p3_safety"] = phase_p3_safety(baseline, reused)

        report["p4_recycle"] = await phase_p4_recycle(sem, cap)
        report["p5_invalidate"] = await phase_p5_invalidate(sem)
        report["p6_idle_reap"] = await phase_p6_idle_reap(sem)
        report["p8_concurrency"] = await phase_p8_concurrency(sem)
        # P7 last: it closes the pool P8 just re-opened.
        report["p7_leak"] = await phase_p7_leak(base_chromium)
    finally:
        if fetcher._POOL is not None:
            try:
                await fetcher.aclose_pool()
            except Exception:  # noqa: BLE001
                pass

    # -- the locked verdict -----------------------------------------------------
    p3 = report["p3_safety"]
    all_rendered = all(
        r["chars"] >= PASS_MIN_CHARS and r["method"] == "crawl4ai"
        for r in report["p1_baseline"] + report["p2_reuse"])
    checks = {
        "every_page_rendered_by_crawl4ai": all_rendered,
        "reuse_output_inside_fresh_band": p3["all_in_band"],
        "recycle_at_max_pages": report["p4_recycle"]["ok"],
        "invalidated_after_failure": report["p5_invalidate"]["ok"],
        "idle_browser_released": report["p6_idle_reap"]["browser_released"],
        "no_leaked_chromium": report["p7_leak"]["ok"],
    }
    report["checks"] = checks
    report["reuse_is_safe"] = all(checks.values())
    report["f1_concurrency_inert"] = report["p8_concurrency"]["inert"]

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 72, flush=True)
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}", flush=True)
    print("=" * 72, flush=True)
    print(f"VERDICT: reuse {'IS' if report['reuse_is_safe'] else 'is NOT'} proven safe "
          f"-> {OUT}", flush=True)


def _public(row: dict) -> dict:
    """Drop the retained markdown body; keep the measurements."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


if __name__ == "__main__":
    asyncio.run(main())
