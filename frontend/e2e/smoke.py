"""Smoke-test the rebuilt frontend in a real browser.

The build passing proves the modules parse. It does not prove the React tree
mounts, that the API envelopes are unwrapped correctly, or that the WebGL field
initialises. This drives the actual app and fails loudly on any console error,
page error, or failed request.
"""
import asyncio
import os

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
OUT = os.environ.get(
    "DG_SHOT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots"),
)
os.makedirs(OUT, exist_ok=True)

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

ROUTES = [
    ("/", "Collect"),
    ("/runs", "Runs"),
    ("/library", "Library"),
    ("/intel", "Intel"),
]

# Errors we deliberately tolerate: favicon noise and the WebGL software
# rasteriser warning that headless Chromium always emits.
IGNORED = ("favicon", "Automatic fallback to software WebGL")


async def main() -> int:
    failures: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        console: list[str] = []
        page.on(
            "console",
            lambda m: console.append(f"[{m.type}] {m.text}")
            if m.type in ("error", "warning")
            else None,
        )
        page.on("pageerror", lambda e: console.append(f"[pageerror] {e}"))
        page.on(
            "requestfailed",
            lambda r: console.append(f"[requestfailed] {r.url} {r.failure}")
            if not any(i in r.url for i in IGNORED)
            else None,
        )

        for path, label in ROUTES:
            console.clear()
            await page.goto(f"{BASE}{path}", wait_until="networkidle")
            await page.wait_for_timeout(900)

            body = await page.inner_text("body")
            mounted = await page.evaluate("document.querySelector('#root')?.children.length || 0")
            has_boot = await page.evaluate("!!document.getElementById('boot')")
            has_canvas = await page.evaluate("!!document.querySelector('canvas.field-canvas')")

            problems = [
                c
                for c in console
                if "[pageerror]" in c
                or "[error]" in c
                or "[requestfailed]" in c
            ]

            print(f"\n=== {label} ({path}) ===")
            print(f"  mounted children : {mounted}")
            print(f"  boot placeholder : {'still present' if has_boot else 'removed'}")
            print(f"  text length      : {len(body)}")
            print(f"  canvas           : {has_canvas}")
            print(f"  console problems : {len(problems)}")
            for p in problems[:8]:
                print(f"     {p[:200]}")

            if mounted == 0:
                failures.append(f"{path}: #root has no children — React did not mount")
            if len(body) < 80:
                failures.append(f"{path}: rendered almost no text ({len(body)} chars)")
            if has_boot:
                failures.append(f"{path}: boot placeholder never removed")
            for p in problems:
                failures.append(f"{path}: {p[:180]}")

        # Interaction checks on the main flow.
        console.clear()
        await page.goto(f"{BASE}/", wait_until="networkidle")
        await page.wait_for_timeout(600)

        # Command palette
        await page.keyboard.press("Control+k")
        await page.wait_for_timeout(450)
        palette = await page.evaluate("!!document.querySelector('[aria-label=\"Command palette\"]')")
        if not palette:
            failures.append("command palette did not open on Ctrl+K")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)

        # Digit shortcut navigates. Blur the autofocused textarea first: a bare
        # digit is *supposed* to be ignored while typing.
        await page.evaluate("document.activeElement?.blur()")
        await page.keyboard.press("3")
        await page.wait_for_timeout(1200)
        url_after = page.url
        print(f"\n=== interaction ===")
        print(f"  key '3' -> url      : {url_after}")
        if "/library" not in url_after:
            failures.append(f"digit shortcut did not navigate (got {url_after})")

        # Command palette must work even with the caret in a textarea.
        await page.goto(f"{BASE}/", wait_until="networkidle")
        await page.wait_for_timeout(600)
        focused = await page.evaluate(
            "document.activeElement?.tagName || 'none'"
        )
        await page.keyboard.press("Control+k")
        await page.wait_for_timeout(500)
        palette = await page.evaluate(
            "!!document.querySelector('[aria-label=\"Command palette\"]')"
        )
        print(f"  focus before cmd+k  : {focused}")
        print(f"  cmd+k opens palette : {palette}")
        if not palette:
            failures.append("command palette did not open on Ctrl+K")
        if palette:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)

        # Navigate to the library through the nav rail, then wait for the
        # dataset cards to actually appear rather than sleeping a fixed amount.
        await page.click("nav[aria-label='Primary'] a[href='/library']")
        await page.wait_for_selector("a[href^='/library/']", timeout=15000)
        print(f"  library via nav     : {page.url}")

        # A dataset row should be clickable and open the inspector.
        await page.wait_for_timeout(900)
        link = await page.query_selector("a[href^='/library/']")
        if link:
            await link.click()
            await page.wait_for_selector("table tbody tr", timeout=15000)
            await page.wait_for_timeout(700)
            has_table = await page.evaluate("!!document.querySelector('table')")
            has_inspector = await page.evaluate("!!document.querySelector('[aria-label=\"Inspector\"]')")
            print(f"  dataset table      : {has_table}")
            print(f"  inspector present  : {has_inspector}")

            # Click a record row -> inspector shows field-level provenance.
            # Evidence is behind a per-field expander (progressive disclosure),
            # so expand one before asserting the quote is reachable.
            row = await page.query_selector("tbody tr")
            if row:
                await row.click()
                await page.wait_for_timeout(700)
                insp_text = await page.inner_text('[aria-label="Inspector"]')
                print(f"  record inspector   : {bool(insp_text)}")

                expander = await page.query_selector(
                    'aside[aria-label="Inspector"] button[aria-expanded]'
                )
                if not expander:
                    failures.append("no expandable field in the record inspector")
                    has_quote = False
                else:
                    await expander.click()
                    await page.wait_for_timeout(500)
                    insp_text = await page.inner_text('[aria-label="Inspector"]')
                    has_quote = "“" in insp_text
                    has_page = "page " in insp_text.lower()

                print(f"  expands to quote   : {has_quote}")
                print(f"  cites stored page  : {has_page}")
                if not has_quote:
                    failures.append("expanding a field did not reveal its quote")
                if not has_page:
                    failures.append("expanding a field did not reveal its stored page id")
            else:
                failures.append("dataset page rendered no record rows")

            # Sources tab. Select by name, not position: the records status
            # filter is also a role=tab, so an index is ambiguous. inner_text()
            # reflects CSS text-transform and .eyebrow is uppercased, so the
            # comparison is case-insensitive.
            try:
                await page.locator('[role=tab]', has_text="Sources").first.click()
                # /sources takes ~2.5s server-side; wait for content, not a sleep
                try:
                    await page.wait_for_function(
                        "() => document.querySelector('main')?.innerText.toLowerCase().includes('pages stored')",
                        timeout=25000)
                except Exception:
                    pass
                src_text = (await page.inner_text("main")).lower()
                ok = "pages stored" in src_text
                print(f"  sources rendered   : {ok}")
                if not ok:
                    failures.append("sources tab did not render its summary")
            except Exception as e:
                failures.append(f"could not open sources tab: {e}")
        else:
            print("  (no dataset links - library empty, skipping dataset checks)")
            failures.append("library rendered no dataset links despite 10 datasets in the API")

        # Mobile viewport
        console.clear()
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.goto(f"{BASE}/", wait_until="networkidle")
        await page.wait_for_timeout(700)
        overflow = await page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        print(f"  mobile h-overflow  : {overflow}px")
        if overflow > 4:
            failures.append(f"horizontal overflow on mobile: {overflow}px")

        await browser.close()

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
