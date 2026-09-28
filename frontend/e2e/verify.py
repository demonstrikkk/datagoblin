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

DS = "4d347326-7675-4179-858c-f22d04255e9a"

async def main():
    fails = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        p = await b.new_page(viewport={"width": 1600, "height": 1000})
        p.on("pageerror", lambda e: (print("PAGEERROR:", e), fails.append(str(e))))

        await p.goto(f"{BASE}/library/{DS}", wait_until="networkidle")
        await p.wait_for_selector("table tbody tr", timeout=45000)
        await p.wait_for_timeout(800)

        # --- open a record, then expand a field to reveal the quote ---
        await p.click("table tbody tr")
        await p.wait_for_timeout(600)
        before = await p.inner_text('aside[aria-label="Inspector"]')
        print("=== record inspector before expansion ===")
        print("  has 'page ' ref :", "page " in before)
        print("  has quote marks :", "“" in before)

        # click the first field row (the FOUNDERS row button)
        field_btn = await p.query_selector('aside[aria-label="Inspector"] button[aria-expanded]')
        if field_btn:
            await field_btn.click()
            await p.wait_for_timeout(600)
            after = await p.inner_text('aside[aria-label="Inspector"]')
            has_quote = "“" in after
            has_page = "page " in after
            has_host = "failory.com" in after or ".com" in after
            print("\n=== after expanding a field ===")
            print("  quote revealed  :", has_quote)
            print("  page id shown   :", has_page)
            print("  source host     :", has_host)
            print("  offsets shown   :", "@" in after)
            if not has_quote:
                fails.append("expanding a verified field did not reveal its quote")
            if not has_page:
                fails.append("expanding a field did not reveal its stored page id")
            print("\n  --- expanded excerpt ---")
            for line in [l for l in after.splitlines() if l.strip()][8:26]:
                print(f"    {line[:120]}")
        else:
            fails.append("no expandable field button in inspector")

        # --- sources tab ---
        print("\n=== sources tab ===")
        tabs = await p.evaluate(
            "[...document.querySelectorAll('[role=tab]')].map(t=>t.textContent.trim())"
        )
        print("  tabs:", tabs)
        try:
            await p.locator('[role=tab]', has_text="Sources").first.click()
            try:
                await p.wait_for_function(
                    "() => document.querySelector('main')?.innerText.toLowerCase().includes('pages stored')",
                    timeout=20000)
            except Exception:
                pass
            txt = await p.inner_text("main")
            print("  has 'Pages stored':", "pages stored" in txt.lower())
            print("  has 'Domains'     :", "domains" in txt.lower())
            print("  has 'Failed'      :", "failed" in txt.lower())
            if "pages stored" not in txt.lower():
                fails.append("sources tab did not render its summary")
            print("\n  --- sources excerpt ---")
            for line in [l for l in txt.splitlines() if l.strip()][:18]:
                print(f"    {line[:110]}")
        except Exception as e:
            fails.append(f"could not click Sources tab: {e}")

        # --- export tab ---
        print("\n=== export tab ===")
        try:
            await p.click('[role=tab]:has-text("Export")')
            await p.wait_for_timeout(900)
            etxt = await p.inner_text("main")
            print("  format selector  :", "CSV" in etxt)
            print("  billed warning   :", "billed work" in etxt)
            if "CSV" not in etxt:
                fails.append("export tab did not render")
        except Exception as e:
            fails.append(f"could not click Export tab: {e}")

        # --- runs table ---
        await p.goto(f"{BASE}/runs", wait_until="networkidle")
        await p.wait_for_selector("tbody tr", timeout=15000)
        await p.wait_for_timeout(700)
        rtxt = await p.inner_text("main")
        print("\n=== runs ===")
        print("  rows rendered :", await p.evaluate("document.querySelectorAll('tbody tr').length"))
        print("  has filters   :", "partial" in rtxt.lower() and "failed" in rtxt.lower())
        print("  has totals    :", "total runs" in rtxt.lower())

        await b.close()

    print("\n" + "=" * 55)
    print("FAILURES:" if fails else "ALL PASSED")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
