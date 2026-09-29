"""Cover the progressive-disclosure and dialog surfaces.

A crash like `setUseSeeds is not defined` only happens when the *Constraints*
panel is opened, so a suite that never expands a panel cannot find it. This
clicks every disclosure control, toggles every switch, and drives both dialogs.
"""
import asyncio
import json
import os
import urllib.request
BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
API = os.environ.get("DG_API_URL", "http://127.0.0.1:8000")


async def _dataset_with_records():
    """The dataset with the most stored records, or None.

    Same reason as in smoke.py: a literal id stops working when that dataset is
    deleted, and "the first card" stops working the day a run produces an empty
    dataset.
    """
    def _fetch():
        with urllib.request.urlopen(f"{API}/api/datasets", timeout=60) as fh:
            return json.loads(fh.read().decode("utf-8"))["data"] or []

    try:
        rows = await asyncio.to_thread(_fetch)
    except Exception:
        return None
    rows = [r for r in rows if (r.get("record_count") or 0) > 0]
    if not rows:
        return None
    return "/library/" + max(rows, key=lambda r: r.get("record_count") or 0)["id"]
OUT = os.environ.get(
    "DG_SHOT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots"),
)
os.makedirs(OUT, exist_ok=True)

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright



async def main() -> int:
    fails: list[str] = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        p = await b.new_page(viewport={"width": 1600, "height": 1000})
        errs: list[str] = []
        p.on("pageerror", lambda e: errs.append(f"pageerror: {str(e)[:200]}"))
        p.on("console", lambda m: errs.append(f"console: {m.text[:200]}") if m.type == "error" else None)

        # ---- 1. Collect: expand every disclosure --------------------------
        print("=== Collect / constraints panel ===")
        await p.goto(f"{BASE}/", wait_until="domcontentloaded")
        await p.wait_for_selector("textarea", timeout=20000)
        await p.wait_for_timeout(600)

        disc = p.locator("button", has_text="Constraints")
        print("  constraints control found:", await disc.count() == 1)
        if await disc.count() != 1:
            fails.append("Constraints disclosure not found")
        await disc.first.click()
        await p.wait_for_timeout(700)
        txt = (await p.inner_text("main")).lower()

        # Only controls that actually reach the API may appear. "Restrict to
        # domain" and "Crawl depth" were collected into state and then never
        # sent, while their tooltips claimed otherwise; they were removed
        # rather than left as decoration that lies.
        for label in ("start from specific urls", "credit budget"):
            ok = label in txt
            print(f"  reveals {label:26}: {ok}")
            if not ok:
                fails.append(f"constraints panel missing {label!r}")

        for gone in ("restrict to domain", "crawl depth"):
            present = gone in txt
            print(f"  absent  {gone:26}: {not present}")
            if present:
                fails.append(f"{gone!r} is still offered but never sent to the API")

        # the seed-URL sub-panel is itself a disclosure
        seed_toggle = p.locator('[role=switch]').first
        before = await seed_toggle.get_attribute("aria-checked")
        await seed_toggle.click()
        await p.wait_for_timeout(500)
        after = await seed_toggle.get_attribute("aria-checked")
        print(f"  seed toggle flips: {before} -> {after}")
        if before == after:
            fails.append("seed URLs switch did not toggle")
        st = (await p.inner_text("main")).lower()
        if "seed urls" not in st:
            fails.append("toggling the seed switch did not reveal the Seed URLs field")
        else:
            print("  reveals Seed URLs field   : True")

        # fill the sub-panel so a bad handler surfaces on change, not just render
        ta = p.locator("textarea")
        n = await ta.count()
        print(f"  textareas now: {n} (prompt + seeds)")
        if n < 2:
            fails.append("seed URL textarea did not appear")
        else:
            await ta.nth(1).fill("https://example.org/report")
        await p.fill("textarea >> nth=0", "Find 3 AI startups in Berlin that raised a Series A")
        await p.screenshot(path=f"{OUT}/17-constraints.png")

        real = [e for e in errs if "ERR_ABORTED" not in e]
        if real:
            fails.extend(real[:3])

        # ---- 2. Settings dialog ------------------------------------------
        print("\n=== settings dialog ===")
        errs.clear()
        await p.goto(f"{BASE}/", wait_until="domcontentloaded")
        await p.wait_for_timeout(700)
        await p.click("nav[aria-label='Primary'] button[aria-label='Settings']")
        await p.wait_for_timeout(700)
        dlg = p.locator('[role=dialog][aria-label="Settings"]')
        print("  dialog opens:", await dlg.count() == 1)
        if await dlg.count() != 1:
            fails.append("settings dialog did not open")
        else:
            # password field must have a form owner
            has_form = await p.evaluate(
                "() => { const i=document.querySelector('input[type=password]');"
                " return i ? !!i.closest('form') : null; }"
            )
            print("  password in a form:", has_form)
            if has_form is False:
                fails.append("password field still has no form owner (Chrome DOM warning)")
            # toggles
            t = p.locator('[role=switch]')
            n = await t.count()
            print("  switches:", n)
            if n:
                b0 = await t.first.get_attribute("aria-checked")
                await t.first.click()
                await p.wait_for_timeout(300)
                b1 = await t.first.get_attribute("aria-checked")
                print(f"  density toggle flips: {b0} -> {b1}")
                if b0 == b1:
                    fails.append("density toggle did not flip")
            # API key save
            await dlg.locator("input[type=password]").fill("test-key-123")
            await dlg.locator("button[type=submit]").click()
            await p.wait_for_timeout(400)
            stored = await p.evaluate("() => localStorage.getItem('dg_api_key')")
            print("  api key persisted:", bool(stored))
            if not stored:
                fails.append("API key did not persist on save")
            else:
                await p.evaluate("() => localStorage.removeItem('dg_api_key')")
            await p.screenshot(path=f"{OUT}/18-settings-open.png")
            await p.keyboard.press("Escape")
            await p.wait_for_timeout(400)
            print("  escape closes:", await p.locator('[role=dialog][aria-label="Settings"]').count() == 0)

        real = [e for e in errs if "ERR_ABORTED" not in e]
        if real:
            fails.extend(real[:3])

        # ---- 3. Record status filters (another disclosure path) -----------
        print("\n=== dataset record filters ===")
        clicked: set[str] = set()
        errs.clear()
        await p.goto(f"{BASE}/library", wait_until="domcontentloaded")
        # Opened by record count from the API, not by clicking the first card.
        # The library sorts newest first, and a run that ends with zero records
        # — a provider auth failure produces exactly that — leaves an empty
        # dataset at the top, so this waited 45s for table rows that cannot
        # exist and reported the app as broken. An empty dataset is a
        # legitimate state; a test that assumes otherwise tests sort order.
        target = await _dataset_with_records()
        if not target:
            fails.append("no dataset with stored records exists to open")
            await b.close()
            return 1
        await p.goto(f"{BASE}{target}", wait_until="domcontentloaded")
        await p.wait_for_selector("table tbody tr", timeout=45000)
        # Wait for the filter counts to hold real numbers before clicking any of
        # them. The row renders as soon as the panel mounts and is replaced when
        # the records arrive (`All0` -> `All50`), so a click issued during that
        # swap lands on an element that no longer exists — which is a test
        # timing bug dressed up as a UI failure.
        await p.wait_for_function(
            """() => {
                 const g = document.querySelector(
                   '[role=tablist][aria-label="Record status filter"]');
                 return !!g && /[1-9]/.test(g.innerText);
               }""",
            timeout=45000)
        await p.wait_for_timeout(600)
        # Click every record-status filter. The tab list shrinks when the
        # Records panel unmounts, so re-count and stop at the first gap.
        while True:
            filters = p.locator('[role=tablist][aria-label="Record status filter"] [role=tab]')
            n = await filters.count()
            target = None
            for i in range(n):
                try:
                    lbl = (await filters.nth(i).inner_text(timeout=2000)).strip().splitlines()[0]
                except Exception:
                    continue
                if lbl not in ("Records", "Sources", "Export") and lbl not in clicked:
                    target, label = i, lbl
                    break
            if target is None:
                break
            clicked.add(label)
            try:
                await filters.nth(target).click(timeout=5000)
                await p.wait_for_timeout(1000)
                rows = await p.evaluate("document.querySelectorAll('table tbody tr').length")
                print(f"  filter '{label}' -> {rows} rows")
            except Exception as e:
                fails.append(f"record filter {label!r} failed: {e}")
                break

        real = [e for e in errs if "ERR_ABORTED" not in e]
        if real:
            fails.extend(real[:3])

        await b.close()

    print("\n" + "=" * 55)
    print("FAILURES:" if fails else "ALL PASSED")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
