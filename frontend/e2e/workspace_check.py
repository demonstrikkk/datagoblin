"""Confirm the workspace renders and the selectors the suites depend on still resolve."""
import os, re, sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
API = "http://127.0.0.1:8000/api"

fails = []
with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_context(viewport={"width": 1440, "height": 1000}).new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    r = page.request.get(f"{API}/datasets")
    rows = page.request.get(f"{API}/datasets").json().get("data") or []
    ds = max(rows, key=lambda x: x.get("record_count") or 0)
    did = ds["id"]
    print(f"dataset {did} — {ds.get('record_count')} records")

    page.goto(f"{BASE}/library/{did}", wait_until="domcontentloaded")
    page.wait_for_selector("table tbody tr", timeout=45000)
    page.wait_for_timeout(2500)

    # 1. the rail is a vertical tablist, and Records is still second
    tl = page.locator('[role=tablist][aria-label="Dataset section"]')
    print("dataset tablist      :", tl.count() == 1)
    print("aria-orientation     :", tl.get_attribute("aria-orientation"))
    tabs = page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]')
    labels = [tabs.nth(i).inner_text().strip() for i in range(tabs.count())]
    print("tabs                 :", labels)
    if labels[1] != "Records":
        fails.append(f"Records is not the second tab: {labels[:3]}")

    # 2. exact-label regexes the suites match against must still resolve
    for label in ("Gaps", "Records", "Conflicts", "Sources", "Ask", "Export", "Overview"):
        n = page.locator("button", has_text=re.compile(f"^{label}$")).count()
        if n < 1:
            fails.append(f"no button matches ^{label}$")
    print("exact-label matches  : ok")

    # 3. the health strip refuses to invent a measurement
    print("health strip         :", page.locator('[aria-label="Dataset health"]').count() == 1)
    print("uncounted shown as   :", "not counted" in page.inner_text("main").lower())

    # 4. the record status filter tablist still exists on the Records section
    tabs.nth(1).click()
    page.wait_for_timeout(2500)
    f = page.locator('[role=tablist][aria-label="Record status filter"]')
    print("record filter tabs   :", f.count() == 1)

    # 5. inline row expansion works and is a real disclosure
    row = page.query_selector("table tbody tr")
    row.click()
    page.wait_for_timeout(900)
    print("inspector opened     :", page.locator('aside[aria-label="Inspector"]').count() == 1)
    print("field expanders      :", page.locator('aside[aria-label="Inspector"] button[aria-expanded]').count() > 0)
    exp = page.query_selector('tbody button[aria-expanded]')
    print("row disclosure found :", exp is not None)
    if exp:
        before = exp.get_attribute("aria-expanded")
        exp.click()
        page.wait_for_timeout(600)
        after = exp.get_attribute("aria-expanded")
        print("row expands inline   :", before != after)

    # 6. sources keeps the string the suites assert on
    tabs.nth(6).click()
    page.wait_for_timeout(4000)
    print("'pages stored' shown :", "pages stored" in page.inner_text("main").lower())

    # 7. no mobile overflow
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(1200)
    ov = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    print("mobile h-overflow    :", ov, "px")
    if ov > 2:
        fails.append(f"horizontal overflow on mobile: {ov}px")

    real = [e for e in errors if "ERR_ABORTED" not in e]
    print("console errors       :", len(real))
    for e in real[:4]:
        print("   ", e[:160])
    if real:
        fails.append(f"{len(real)} console errors")

    b.close()

print()
print("FAILURES:" if fails else "ALL CHECKS PASSED")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)