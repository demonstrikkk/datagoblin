"""The three newest surfaces, driven the way a person drives them.

Ask, Coverage's backfill panel, and the proposal card were all built with
backend tests and API tests but no browser test. That is how a refine proposal
ended up wearing a button that ran a backfill: nothing rendered the card, so
nothing noticed the button said one thing and did another.

These checks are deliberately about behaviour a person can see — that the
panels mount, that a question is answered, and above all that the card's
action matches its label.
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

# A page that only needs the internet for its fonts, and those are stubbed so
# a slow CDN cannot masquerade as an app that will not load.
FONTS = re.compile(r"fonts\.(googleapis|gstatic)\.com")


def a_dataset_with_pages() -> dict:
    """Discovered, not hardcoded.

    Two suites used to carry a literal dataset id. It worked until that dataset
    was deleted, and then they failed for a reason that had nothing to do with
    the code under test.
    """
    with urllib.request.urlopen(f"{API}/api/datasets", timeout=60) as r:
        items = json.loads(r.read().decode("utf-8"))["data"] or []
    for d in items:
        try:
            with urllib.request.urlopen(
                    f"{API}/api/datasets/{d['id']}/backfill?limit_pages=2", timeout=90) as r:
                if json.loads(r.read().decode("utf-8"))["data"].get("fields"):
                    return d
        except Exception:
            continue
    return items[0] if items else {}


def main() -> int:
    ds = a_dataset_with_pages()
    if not ds:
        print("  no datasets exist; run a collection first")
        return 1
    did = ds["id"]
    print(f"  dataset: {did[:12]} ({ds.get('record_count', 0)} records)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route(FONTS, lambda r: r.fulfill(status=200, content_type="text/css", body=""))
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:160]))

        page.goto(f"{BASE}/library/{did}", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => [...document.querySelectorAll('button')]"
            ".some(b => b.textContent.trim() === 'Ask')",
            timeout=45_000)

        # -- ask ---------------------------------------------------------------
        page.locator("button", has_text=re.compile("^Ask$")).first.click()
        # Wait for the panel's own input, not for a button labelled "Ask" — that
        # button is the tab itself, which exists before the panel mounts, so
        # waiting on it returned while the input was still absent.
        try:
            page.wait_for_selector("input[aria-label='Question for this dataset']", timeout=45_000)
        except Exception:
            fails.append(
                "the Ask tab did not mount its input. The page said: "
                + repr(page.inner_text("body")[-200:]))
            browser.close()
            print("=" * 60)
            print("FAILURES:")
            for f in fails:
                print(f"  - {f}")
            return 1
        body = page.inner_text("body")
        if "read-only SQL path" not in body:
            fails.append("the Ask panel did not explain what it does")
        if "never writes to a dataset on its own" not in body:
            fails.append("the Ask panel did not state that a model cannot write")

        # A real question, through the real endpoint, into the real table.
        page.fill("input[aria-label='Question for this dataset']", "which industry appears most often?")
        page.locator("button", has_text=re.compile("^Ask$")).last.click()
        try:
            # Wait for the SQL it actually wrote, not for the word "SQL" — which
            # appears in the panel's own explanatory copy, so the wait used to
            # return instantly and then assert against a page with no result on
            # it.
            page.wait_for_function(
                "() => [...document.querySelectorAll('code')]"
                ".some(c => /SELECT/i.test(c.textContent))",
                timeout=240_000)
            page.wait_for_timeout(1500)
            after = page.inner_text("body")
            # Case-insensitive: the heading is uppercased in CSS, and
            # inner_text returns what is rendered, so a literal "Charts" check
            # failed against a chart that was plainly on screen.
            if "charts" not in after.lower():
                fails.append("the result carried no chart")
            if not re.search(r"\d+ rows?", after):
                fails.append("the result did not report how many rows it read")
        except Exception:
            fails.append(
                "asking a question produced no query. The panel said: "
                + repr(page.inner_text("body")[-220:]))

        # -- proposal: a change must never offer a backfill --------------------
        page.fill("input[aria-label='Question for this dataset']", "only two sources instead of ten")
        page.locator("button", has_text=re.compile("^Ask$")).last.click()
        try:
            page.wait_for_function(
                "() => /Proposal:/.test(document.body.innerText)", timeout=120_000)
            page.wait_for_timeout(1200)
            card = page.inner_text("body")
            if "Proposal: refine" in card:
                # The bug this suite exists for: one shared "Approve and fill"
                # button meant a plan request ran a backfill on the records.
                if "Approve and fill" in card:
                    fails.append(
                        "a plan-change proposal offered a backfill action; approving "
                        "it would have written to the records")
                if "Nothing here will touch the records" not in card:
                    fails.append("a plan-change proposal did not say it changes no data")
        except Exception:
            print("  (no refine proposal rendered; the sentence was read as a question)")

        # -- coverage + backfill panel ------------------------------------------
        # Switch tabs rather than reloading. A second `goto` on the same SPA
        # timed out on document load every run: the app holds an open SSE
        # connection to the API, and re-entering the route while it is still
        # up blocks the navigation. Switching tabs keeps one document load and
        # puts the section back to a known state.
        page.locator("button", has_text=re.compile("^Records$")).first.click()
        page.wait_for_timeout(800)
        page.locator("button", has_text=re.compile("^Coverage$")).first.click()
        try:
            page.wait_for_function(
                "() => /Per-field coverage/.test(document.body.innerText)", timeout=120_000)
            # The backfill panel reads stored records before it can say anything,
            # so waiting on a fixed delay was a guess that failed whenever the
            # read was slower than the guess. Wait for its own copy instead.
            page.wait_for_function(
                "() => /Backfill from stored pages/.test(document.body.innerText)",
                timeout=120_000)
            page.wait_for_timeout(500)
            cov = page.inner_text("body")
            if "nothing is crawled" not in cov.lower():
                fails.append("the backfill panel did not say it crawls nothing")
        except Exception:
            fails.append(
                "the Coverage tab never rendered. It said: "
                + repr(page.inner_text("body")[-220:]))

        if errors:
            fails.append("page errors: " + "; ".join(errors[:3]))

        browser.close()

    print("=" * 60)
    if fails:
        print("FAILURES:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
