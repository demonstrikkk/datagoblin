"""Isolate the two things bench_reuse.py conflated: does REUSE leak browsers?

Two runs produced contradictory verdicts, and both are reported honestly:

  run 1 (clean machine, 0 chromium at start): 1 of 3 reuse renders died with
    "Connection closed while reading from the driver". Recycle, invalidate,
    reap and leak all passed. Ended with 0 chromium.

  run 2 (started with 4 chromium already alive): chromium climbed to 20
    mid-run and finished at 17 after aclose_pool(). Recycle, invalidate and
    leak all failed. Close-context errors appear alongside every death.

The competing explanations are (a) reuse breaks the driver, and (b) browsers
leaked by earlier work starve the box until everything dies -- which would
make the later failures downstream of the leak rather than separate faults.
A bench that cannot tell those apart has not answered anything.

So this one variable at a time, from a machine with nothing running:

  1. Reap every ms-playwright process left by earlier runs (they are this
     suite's own residue; nothing else on the box uses that path).
  2. Assert the count is genuinely 0. If it is not, every number below is
     meaningless and the run says so instead of continuing.
  3. Arm A -- REUSE_BROWSER=false, 6 renders. Arm B -- true, 6 renders.
  4. Count chromium after every single render, and after shutdown per arm.

A leak is a count that does not return. A failure is a render that came back
on a rung other than crawl4ai. They are reported as separate numbers because
they are separate claims.

Same posture as the other benches: production waterfall, real robots/throttle/
SSRF, route web:crawl4ai, no Tavily, no LLM, no database, no API.
-> outputs/bench_reuse_isolate.json
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import settings  # noqa: E402
from app.providers.crawl import fetcher  # noqa: E402
from app.services import crawler as crawler_svc  # noqa: E402
from app.services import reducer as reducer_svc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "bench_reuse_isolate.json"

URLS = [
    "https://hackerearth.com/hackathon/explore/city/delhi",
    "https://aws.amazon.com/transform/",
    "https://aws.amazon.com/events/sponsorship",
]
ROUNDS = 2

#: Seconds to let a browser actually die before counting it as leaked. Chromium
#: teardown on Windows is slow: children outlive the browser root for up to ~70s
#: while draining. Counting at 8s reports a slow close as a leak.
SETTLE_S = 8
SETTLE_LONG_S = 70


def chromium_pids() -> list[int]:
    """PIDs of chromium processes running from this playwright install."""
    import psutil
    pids = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            exe = (proc.info.get("exe") or "").lower()
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
        except Exception:  # noqa: BLE001
            continue
        if "ms-playwright" in exe or "ms-playwright" in cmd:
            pids.append(proc.info["pid"])
    return sorted(pids)


def reap_orphans(attempts: int = 6) -> dict:
    """Kill every playwright chromium, until there are none left.

    These are this suite's own residue. One pass is not enough: a browser
    being terminated can briefly outlive the first sweep, and the first version
    of this function reported 2 remaining and aborted rather than pretending.
    It also refuses to touch any process whose command line does not name the
    ms-playwright install path, so an unrelated Chrome is never a candidate.

    Returns a log of every attempt, because "it would not go away" is itself
    the finding when it happens.
    """
    import psutil
    log = []
    total = 0
    for attempt in range(1, attempts + 1):
        pids = chromium_pids()
        if not pids:
            log.append({"attempt": attempt, "found": 0, "terminated": 0})
            break
        total += len(pids)
        for pid in pids:
            try:
                psutil.Process(pid).terminate()
            except Exception:  # noqa: BLE001 (already gone)
                pass
        _, alive = psutil.wait_procs(
            [psutil.Process(p) for p in pids if psutil.pid_exists(p)], timeout=6)
        for p in alive:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
        left = chromium_pids()
        log.append({"attempt": attempt, "found": len(pids), "terminated": len(pids),
                    "survivors_after_kill": left})
        if not left:
            break
    remaining = chromium_pids()
    detail = []
    if remaining:
        import psutil as _ps
        for pid in remaining:
            try:
                cmd = " ".join(_ps.Process(pid).cmdline())[:160]
            except Exception:  # noqa: BLE001
                cmd = "<unreadable>"
            detail.append({"pid": pid, "cmdline": cmd})
    return {"attempts": log, "terminated_total": total,
            "remaining": len(remaining), "remaining_detail": detail}


def _mem() -> dict:
    """Memory headroom, sampled so a driver death can be correlated with it.

    "Connection closed while reading from the driver" is a Playwright string, not
    a crawl4ai one: the browser process went away mid-call. Windows logged no
    application crash and no resource-exhaustion event, which is the signature of
    a process killed under pressure rather than one that faulted. This box has
    7.7GB and was running at ~1.6GB free, so headroom is the thing to record
    next to every render instead of guessing afterwards.
    """
    import psutil
    vm = psutil.virtual_memory()
    return {"avail_mb": int(vm.available / 1e6),
            "used_pct": round(vm.percent, 1),
            "total_mb": int(vm.total / 1e6),
            "chromium": len(chromium_pids())}


def machine_profile() -> dict:
    import psutil
    return {"ram_total_mb": int(psutil.virtual_memory().total / 1e6),
            "cpu_logical": psutil.cpu_count(logical=True),
            "platform": sys.platform}


async def _fetch(url: str, method: str) -> dict:
    if method == "crawl4ai":
        return await fetcher.crawl4ai_fetch(url)
    return await fetcher.http_fetch(url)


def _clean(page: dict) -> str:
    if page.get("markdown"):
        return page["markdown"]
    return reducer_svc.reduce_html(page.get("html", ""))


def _set(on: bool) -> None:
    settings.REUSE_BROWSER = on
    settings.REUSE_BROWSER_MAX_PAGES = 25
    settings.REUSE_BROWSER_IDLE_S = 120.0
    settings.REUSE_BROWSER_CONCURRENCY = 1
    if fetcher._POOL is not None:
        try:
            fetcher._POOL.shutdown()
        except Exception:  # noqa: BLE001
            pass
        fetcher._POOL = None


async def _render(url: str, sem: asyncio.Semaphore) -> dict:
    t0 = time.monotonic()
    page: dict = {}
    error = ""
    before = _mem()
    try:
        page = await asyncio.wait_for(
            crawler_svc._one(url, "web:crawl4ai", _fetch, sem), timeout=300)
    except Exception as exc:  # noqa: BLE001
        error = getattr(exc, "message", str(exc))
    clean = _clean(page) if page else ""
    return {"url": url, "s": round(time.monotonic() - t0, 2),
            "method": page.get("method", ""), "error": error[:200],
            "chars": len(clean), "mem_before": before, "mem_after": _mem()}


async def arm(label: str, reuse: bool, sem: asyncio.Semaphore) -> dict:
    _set(reuse)
    print(f"\n-- {label} (REUSE_BROWSER={reuse}) --", flush=True)
    print(f"   start mem: {_mem()}", flush=True)
    rows = []
    peak = 0
    for rnd in range(1, ROUNDS + 1):
        for url in URLS:
            r = await _render(url, sem)
            r["round"] = rnd
            r["chromium_after"] = r["mem_after"]["chromium"]
            peak = max(peak, r["chromium_after"])
            r["failed"] = r["method"] != "crawl4ai"
            rows.append(r)
            print(f"[{label} r{rnd}] {r['s']:>6.2f}s {r['method'] or '-':8s} "
                  f"{r['chars']:>6d}ch avail={r['mem_after']['avail_mb']:>5d}MB "
                  f"used={r['mem_after']['used_pct']:>5.1f}% "
                  f"chromium={r['chromium_after']:>2d} {url}"
                  + ("  <-- NOT the browser rung" if r["failed"] else ""), flush=True)
    held = len(chromium_pids())
    await fetcher.aclose_pool()          # no pool exists when reuse=False
    # Two samples, because one was wrong. An earlier version counted at 8s and
    # called 12 surviving processes a leak; bench_leak_measure.py then showed
    # they were Chromium's renderer/GPU children mid-teardown, draining to zero
    # unaided by ~70s with zero orphans. Both numbers are kept so the artefact
    # stays visible rather than being quietly corrected.
    time.sleep(SETTLE_S)
    at_8s = len(chromium_pids())
    time.sleep(SETTLE_LONG_S - SETTLE_S)
    after = len(chromium_pids())
    failures = [r for r in rows if r["failed"]]
    min_avail = min(r["mem_after"]["avail_mb"] for r in rows)
    print(f"   held_after_arm={held} peak={peak} at_{SETTLE_S}s={at_8s} "
          f"at_{SETTLE_LONG_S}s={after} failures={len(failures)}/{len(rows)} "
          f"min_avail={min_avail}MB", flush=True)
    return {"reuse": reuse, "renders": len(rows), "rows": rows,
            "peak_chromium": peak, "held_after_arm": held,
            "chromium_at_8s": at_8s, "chromium_after_settle": after,
            "min_avail_mb": min_avail,
            "max_used_pct": max(r["mem_after"]["used_pct"] for r in rows),
            "failed": len(failures),
            "failed_urls": [f"{r['url']} :: {r['method'] or 'error'}"
                            for r in failures],
            "failure_avail_mb": [r["mem_after"]["avail_mb"] for r in failures],
            "leaked": after > 0, "clean": after == 0}


async def main() -> None:
    logging.getLogger("datagoblin").setLevel(logging.ERROR)  # only my prints

    cleanup = reap_orphans()
    print(f"cleanup: terminated={cleanup['terminated_total']} "
          f"remaining={cleanup['remaining']}", flush=True)
    for a_ in cleanup["attempts"]:
        print(f"   attempt {a_['attempt']}: found={a_['found']} "
              f"survivors={a_.get('survivors_after_kill', [])}", flush=True)
    for d in cleanup["remaining_detail"]:
        print(f"   STILL ALIVE pid={d['pid']} {d['cmdline']}", flush=True)
    report: dict = {"cleanup": cleanup,
                    "machine": machine_profile(),
                    "settle_s": SETTLE_S, "settle_long_s": SETTLE_LONG_S,
                    "rounds_per_arm": ROUNDS, "targets": URLS}
    if cleanup["remaining"] != 0:
        report["aborted"] = ("could not reach a clean 0-chromium machine; "
                             "counts below would not be attributable")
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        print("\nABORTED: machine not clean; no attribution possible.", flush=True)
        return

    sem = asyncio.Semaphore(1)
    try:
        report["arm_a_no_reuse"] = await arm("A/fresh", False, sem)
        # Between arms, anything arm A leaked would be inherited by arm B.
        report["between_arms"] = reap_orphans()
        print(f"between arms: terminated="
              f"{report['between_arms']['terminated_total']} "
              f"remaining={report['between_arms']['remaining']}", flush=True)
        report["arm_b_reuse"] = await arm("B/reuse", True, sem)
    finally:
        if fetcher._POOL is not None:
            try:
                await fetcher.aclose_pool()
            except Exception:  # noqa: BLE001
                pass

    a, b = report["arm_a_no_reuse"], report["arm_b_reuse"]
    report["verdict"] = {
        "no_reuse_leaks_after_settle": not a["leaked"],
        "reuse_leaks_after_settle": b["leaked"],
        "no_reuse_failures": a["failed"],
        "reuse_failures": b["failed"],
        "reuse_is_clean": (not b["leaked"]) and b["failed"] == 0,
        "leak_delta_chromium": b["chromium_after_settle"]
        - a["chromium_after_settle"],
        # The question this instrument exists to answer: do driver deaths
        # follow the reuse flag, or memory? A verdict that only counted
        # failures could never tell those apart, which is what made the first
        # two runs of this bench contradict each other.
        "min_avail_no_reuse_mb": a["min_avail_mb"],
        "min_avail_reuse_mb": b["min_avail_mb"],
        "peak_chromium_no_reuse": a["peak_chromium"],
        "peak_chromium_reuse": b["peak_chromium"],
        "failures_with_low_memory": None,  # filled below
        "attributable_to_reuse": None,
    }
    # A failure that happened while memory was plentiful is evidence FOR reuse
    # being the cause. A failure that happened at the memory floor is evidence
    # against. Both are reported either way; neither is assumed.
    all_failures = [r for r in a["rows"] + b["rows"] if r["failed"]]
    if all_failures:
        lowest = min(r["mem_after"]["avail_mb"] for r in all_failures)
        floor = min(r["min_avail_mb"] for r in (a, b))
        report["verdict"]["failures_with_low_memory"] = sum(
            1 for r in all_failures
            if r["mem_after"]["avail_mb"] <= floor * 1.25)
        report["verdict"]["failures_total"] = len(all_failures)
        report["verdict"]["lowest_avail_at_any_failure_mb"] = lowest
        report["verdict"]["machine_floor_avail_mb"] = floor
        report["verdict"]["attributable_to_reuse"] = (
            len(a["rows"]) > 0 and a["failed"] > 0)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 72, flush=True)
    for k, v in report["verdict"].items():
        print(f"  {k:34s} {v}", flush=True)
    print("=" * 72, flush=True)
    print(f"machine: {report['machine']}", flush=True)
    print(f"-> {OUT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
