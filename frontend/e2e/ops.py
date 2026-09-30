"""Dashboard, learned yield, top-up and refresh — the four surfaces added last.

All four shipped with backend tests and were verified against the real database
by hand, and none of them had a browser test. That is the same gap that let a
refine proposal wear a button that ran a backfill: a panel can mount, fetch, and
be wired to the wrong endpoint, and nothing notices until a person clicks it.

The checks here are the ones a person can see, plus the one that matters most —
that a control's label matches the request it sends:

* the dashboard is reachable from the rail and reports honest totals;
* a source list shows what each host actually earned, not just that it was
  fetched;
* the top-up checkbox exists, is off by default, and changes the proposal when
  turned on;
* the refresh panel states the three rules that make it safe to run, and its
  "what this would do" text is present before anything is clicked.

Nothing here applies a write. The refresh and backfill buttons are checked for
existence and labelling, not pressed, because both re-fetch or re-extract over
the network and a test that spends real fetches is a test that fails for
weather.
"""
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
API = os.environ.get("DG_API_URL", "http://127.0.0.1:8000")

from playwright.sync_api import sync_playwright  # noqa: E402

fails = []

FONTS = re.compile(r"fonts\.(googleapis|gstatic)\.com")


def _api(path: str, timeout: int = 120):
    with urllib.request.urlopen(f"{API}/api/{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))["data"]


def biggest_dataset() -> dict:
    """Discovered, not hardcoded.

    A literal id worked until that dataset was deleted, and then the suite
    failed for a reason that had nothing to do with the code under test.
    """
    items = _api("datasets")
    return max(items, key=lambda d: d.get("record_count") or 0) if items else {}


def main() -> int:
    ds = biggest_dataset()
    if not ds:
        print("  no datasets exist; run a collection first")
        return 1
    did = ds["id"]
    print(f"  dataset: {did[:12]} ({ds.get('record_count', 0)} records)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        # This is the ninth browser in the e2e chain. Playwright's 30s default
        # is a per-page budget, and after eight suites the Chromium renderer
        # occasionally takes longer than that to come up — which showed as a
        # `Page.goto: Timeout 30000ms exceeded` on a server that answers in
        # 2ms, and twice in a row reported as if the product were broken.
        page.set_default_timeout(90_000)
        page.route(FONTS, lambda r: r.fulfill(status=200, content_type="text/css", body=""))
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))

        def go(path: str) -> None:
            """Navigate, once more if the renderer was merely slow to start."""
            last = None
            for attempt in (1, 2):
                try:
                    page.goto(f"{BASE}{path}", wait_until="domcontentloaded",
                              timeout=90_000)
                    return
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    print(f"  (navigation attempt {attempt} slow: {str(exc)[:60]})")
            raise last

        # -- 1. the dashboard is reachable from the rail ----------------------
        print("\n=== dashboard ===")
        go("/")
        page.wait_for_selector("nav[aria-label='Primary']", timeout=30_000)
        rail = page.inner_text("nav[aria-label='Primary']")
        page.locator("nav[aria-label='Primary'] a[aria-label='Dashboard']").click()
        page.wait_for_function(
            "() => location.pathname === '/dashboard'", timeout=20_000)
        # The aggregate reads every dataset's records, so wait for its own
        # content rather than a fixed sleep.
        try:
            page.wait_for_function(
                """() => {
                     const t = document.querySelector('main')?.innerText || '';
                     return /Records/i.test(t) && !/^Loading/i.test(t.trim());
                   }""",
                timeout=90_000)
        except Exception as exc:
            # Read the page defensively. The obvious `page.inner_text("main")`
            # can itself block for the full timeout when the page is the thing
            # that failed to load, and a 90s wait inside an error handler
            # reports a timeout instead of the real cause.
            try:
                said = page.inner_text("body", timeout=5_000)[-200:]
            except Exception:
                said = "(the page could not be read at all)"
            fails.append(
                f"the dashboard never rendered its totals ({exc}). It said: "
                + repr(said))
            browser.close()
            print("=" * 60)
            print("FAILURES:")
            for f in fails:
                print(f"  - {f}")
            return 1
        body = page.inner_text("main")
        # The section titles and stat labels are uppercased by CSS, and
        # `innerText` reflects rendered text-transform, so these comparisons are
        # case-insensitive. Comparing exactly matched nothing and reported five
        # missing sections on a page that showed all five.
        low = body.lower()

        checks = {
            # The ribbon is the page's argument: sources become pages become
            # records become values become *proven* values, and the last stage
            # being much narrower than the one before is the honest answer to
            # "is this trustworthy".
            "provenance ribbon": all(s in body for s in (
                "Sources fetched", "Records", "Values", "Proven")),
            "proven stated as a share": "of all values" in low,
            "unproven is not called proven": "nothing judged it" in low,
            "disputes surfaced": "sources disagree" in low,
            "host ranking present": "which sites paid" in low,
            "never-filled counted separately": "fields on no record" in low,
            "sortable by what you choose": "most never filled" in low,
        }
        for k, v in checks.items():
            print(f"  {k:28}: {v}")
            if not v:
                fails.append(f"dashboard missing: {k}")

        # The numbers must agree with the API, or the page is decoration.
        truth = _api("dashboard")
        if f"{truth['totals']['proven_pct']}%" not in body:
            fails.append(
                f"dashboard shows a different proven_pct than the API "
                f"({truth['totals']['proven_pct']}%)")
        else:
            print(f"  {'agrees with the API':28}: True")
        if f"{truth['totals']['records']:,}" not in body and str(
                truth["totals"]["records"]) not in body:
            fails.append("dashboard does not show the API's record count")
        else:
            print(f"  {'agrees on record count':28}: True")

        # A zero-state must be described, not rendered as an empty chart.
        if truth["totals"]["cells"] == 0 and "nothing to summarise" not in body.lower():
            fails.append("dashboard has no cells but did not say so")

        # -- 2. sources show what each host earned ---------------------------
        print("\n=== sources: learned yield ===")
        go(f"/library/{did}")
        page.wait_for_selector("[role=tab]", timeout=45_000)
        page.locator("[role=tab]", has_text=re.compile("Sources")).first.click()
        # Wait for the source list itself. The "Inspect" button exists only on a
        # source row, so it is the honest signal that the tab mounted.
        try:
            page.wait_for_function(
                """() => [...document.querySelectorAll('main button')]
                          .some(b => b.textContent.trim() === 'Inspect')""",
                timeout=60_000)
        except Exception:
            fails.append("the Sources tab never showed its stats. It said: "
                         + repr(page.inner_text("main")[-200:]))
            body = ""
        else:
            body = page.inner_text("main")
            ym = _api(f"datasets/{did}/yield")
            hosts = ym.get("hosts") or {}
            earned = [h for h, r in hosts.items() if r.get("pages")]
            if earned:
                # The host with pages must show a figure. A source list that
                # only says "fetched" cannot answer "which site was worth it".
                if "proven" not in body.lower():
                    fails.append("Sources did not show any host's proven-cell count")
                else:
                    print("  per-host yield shown: True")
                top = ym["ranking"][0]
                if top and top in body:
                    print(f"  top host present   : True ({top})")
                else:
                    fails.append(f"Sources did not show the top host {top}")
            else:
                print("  no host has pages; nothing to assert")

        # -- 3. the top-up switch -------------------------------------------
        print("\n=== backfill: top up partly filled columns ===")
        go(f"/library/{did}")
        page.wait_for_selector("[role=tab]", timeout=45_000)
        page.locator("[role=tab]", has_text=re.compile("Gaps")).first.click()
        try:
            page.wait_for_selector(
                "input[type=checkbox]", timeout=60_000)
        except Exception:
            fails.append("the backfill panel offered no top-up switch. It said: "
                         + repr(page.inner_text("main")[-200:]))
        else:
            box = page.locator("input[type=checkbox]").first
            if box.is_checked():
                fails.append("top-up is ON by default, which silently changes "
                             "what Backfill means for everyone who used it before")
            else:
                print("  off by default        : True")
            body = page.inner_text("main")
            if "never overwritten" not in body:
                fails.append("the backfill panel did not promise that a filled "
                             "cell is never overwritten")
            else:
                print("  overwrite refused     : True")
            if "empty cell" not in body.lower():
                fails.append("the backfill panel did not talk about empty cells")
            else:
                print("  talks in empty cells  : True")

            # Turning it on must re-propose. This is the label/behaviour match
            # that no API test can see.
            before = page.inner_text("main")
            box.check()
            try:
                page.wait_for_function(
                    """(prev) => {
                         const t = document.querySelector('main')?.innerText || '';
                         return t !== prev && /empty cell|partly|partially/i.test(t);
                       }""",
                    arg=before, timeout=60_000)
                after = page.inner_text("main")
                if "empty cell" in after.lower() or "partially" in after.lower():
                    print("  re-proposes on change : True")
                else:
                    fails.append("toggling top-up did not restate the proposal")
            except Exception:
                fails.append("toggling top-up changed nothing on screen")
            box.uncheck()

        # -- 4. the refresh panel -------------------------------------------
        print("\n=== refresh ===")
        go(f"/library/{did}")
        page.wait_for_selector("[role=tab]", timeout=45_000)
        page.locator("[role=tab]", has_text=re.compile("Gaps")).first.click()
        try:
            page.wait_for_function(
                """() => /Refresh sources/.test(
                          document.querySelector('main')?.innerText || '')""",
                timeout=60_000)
        except Exception:
            fails.append("the refresh panel never mounted. It said: "
                         + repr(page.inner_text("main")[-200:]))
        else:
            body = page.inner_text("main")
            checks = {
                "mounted": "Refresh sources" in body,
                "dry run offered": "Dry run" in body,
                "apply offered": "Apply" in body,
                # A placeholder is an attribute, not rendered text, so it is read
                # off the element. Asserting on innerText here tested nothing.
                "hosts are nameable": page.locator(
                    "input[aria-label='Hosts to refresh']").count() == 1,
                "states the quote rule": "carries its own quote" in body,
                "states the rival rule": "kept for review" in body,
                "says it re-fetches": "Re-fetches" in body,
            }
            for k, v in checks.items():
                print(f"  {k:22}: {v}")
                if not v:
                    fails.append(f"refresh panel missing: {k}")

            # A refresh over the network is never pressed here, but the hosts it
            # would choose must at least be what the proposal says they are.
            prop = _api(f"datasets/{did}/refresh")
            if prop.get("refreshable") and prop.get("hosts"):
                shown = [h for h in prop["hosts"] if h in body]
                if shown:
                    print(f"  names its hosts     : True ({shown[0]})")
                else:
                    fails.append("refresh panel did not name any host it would read")

        browser.close()

    real = [e for e in errors
            if "font" not in e.lower() and "ERR_FAILED" not in e
            and "Failed to load resource" not in e]
    print(f"\n  page errors: {len(real)}")
    for e in real[:5]:
        print(f"    {e}")
    if real:
        fails.append(f"{len(real)} page error(s) during the run")

    print("\n" + "=" * 60)
    if fails:
        print("FAILURES:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
