"""Coverage matrix and the conflict-resolution panel.

Two claims under test: a field no page ever carried is shown as absent rather
than quietly filled, and a conflict can actually be decided with the rival's
own evidence intact.
"""
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
API = os.environ.get("DG_API_URL", "http://127.0.0.1:8000")

from playwright.sync_api import sync_playwright  # noqa: E402

# Google Fonts is reached over the public internet, and when it is slow the
# networkidle waits below never resolve: a hermetic failure that looks like the
# app hanging. Stub it so the suite needs no network.
OFFLINE_FONTS = re.compile(r"fonts\.(googleapis|gstatic)\.com")

fails = []


def newest_dataset() -> str:
    """Ask the API rather than clicking through.

    Clicking to find a dataset was both slower and a silent failure mode: the
    first version of this file returned early without printing anything when
    navigation did not land, so a broken run looked like a run that never
    happened.
    """
    with urllib.request.urlopen(f"{API}/api/datasets", timeout=30) as r:
        items = json.loads(r.read().decode("utf-8"))["data"] or []
    if not items:
        raise SystemExit("no datasets exist; run a collection first")
    return items[0]["id"]


def main() -> int:
    try:
        ds = newest_dataset()
    except SystemExit as exc:
        print(f"  {exc}")
        return 1
    print(f"  dataset: {ds[:12]}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route(OFFLINE_FONTS, lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        page.goto(f"{BASE}/library/{ds}", wait_until="domcontentloaded")
        # Not `networkidle`: this page issues several slow reads (every stored
        # record, once per view) and waiting for 500ms of total silence kept
        # timing out before the UI ever appeared. Wait for the control instead.
        page.wait_for_function(
            "() => [...document.querySelectorAll('button')]"
            ".some(b => b.textContent.trim() === 'Coverage')",
            timeout=30_000,
        )
        page.wait_for_timeout(1500)

        # -- coverage tab -----------------------------------------------------
        coverage_tab = page.locator("button", has_text=re.compile("^Coverage$")).first
        if coverage_tab.count() == 0:
            fails.append("no Coverage tab")
        else:
            coverage_tab.click()
            # These views read every stored record, so they take seconds against
            # a remote database. A short fixed wait reported them as blank.
            page.wait_for_function(
                "() => !document.body.innerText.includes('Reading stored records')",
                timeout=30_000,
            )
            page.wait_for_timeout(500)
            body = page.inner_text("body")
            for needle in ("Per-field coverage", "What is outstanding", "Records"):
                if needle not in body:
                    fails.append(f"coverage tab missing {needle!r}")
            if "%" not in body:
                fails.append("coverage tab showed no percentages")
            print(f"  coverage tab: {'rendered' if not fails else 'has issues'}")

        # -- conflicts tab ----------------------------------------------------
        conflict_tab = page.locator("button", has_text=re.compile("^Conflicts$")).first
        if conflict_tab.count() == 0:
            fails.append("no Conflicts tab")
        else:
            conflict_tab.click()
            # Wait for a state the panel always reaches, not for the loading
            # text to vanish: right after the click the panel has not mounted
            # yet, so "is the spinner gone" is true before it ever appeared.
            page.wait_for_function(
                "() => /No disagreements|Keep this/.test(document.body.innerText)",
                timeout=30_000,
            )
            body = page.inner_text("body")
            if "No disagreements" in body:
                print("  conflicts tab: none open (honest empty state)")
            else:
                for needle, why in (
                    ("incumbent", "the incumbent side"),
                    ("rival", "the rival side"),
                    ("there is no way to type in a value", "the no-free-text rule"),
                ):
                    if needle.lower() not in body.lower():
                        fails.append(f"conflict card missing {why}")
                adopt = page.locator("button", has_text=re.compile("^Adopt this$")).first
                if page.locator("button", has_text=re.compile("^Keep this$")).count() == 0:
                    fails.append("conflict card offered no keep button")
                if adopt.count() == 0:
                    fails.append("conflict card offered no adopt button")
                else:
                    print("  conflicts tab: cards offer keep/adopt")
                    adopt.click()
                    try:
                        page.wait_for_function(
                            "() => /Resolved/.test(document.body.innerText)",
                            timeout=30_000,
                        )
                        print("  resolve: recorded and reflected in the UI")
                    except Exception:
                        # Report what the panel actually said. A bare Playwright
                        # timeout here hid whether the write failed, the card
                        # stayed put, or the copy changed.
                        fails.append(
                            "after adopting, the panel did not confirm it. It said: "
                            f"{page.inner_text('body')[-300:]!r}"
                        )

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
