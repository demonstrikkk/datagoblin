import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
fails = []

with sync_playwright() as pw:
    b = pw.chromium.launch()
    p = b.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    p.on("pageerror", lambda e: errs.append(str(e)[:150]))

    p.goto(f"{BASE}/", wait_until="domcontentloaded")
    p.wait_for_timeout(1500)
    enter = p.locator('a[href="/investigate"]')
    print("landing at /            :", "DataGoblin" in p.title() or "datagoblin" in p.inner_text("body")[:500].lower())
    print("enter buttons           :", enter.count())
    if enter.count() == 0:
        fails.append("no Enter button to /investigate on /")
    else:
        enter.first.click()
        p.wait_for_timeout(2000)
        print("enter lands on          :", p.url)
        if "/investigate" not in p.url:
            fails.append(f"Enter did not land on /investigate (got {p.url})")
        ta = p.locator("textarea")
        print("collect textarea        :", ta.count() > 0)
        if ta.count() == 0:
            fails.append("Collect did not mount at /investigate")

    # runs page: bad runs hidden by default, toggle present
    p.goto(f"{BASE}/runs", wait_until="domcontentloaded")
    p.wait_for_timeout(2500)
    body = p.inner_text("main").lower()
    print("hide toggle present     :", "hidden" in body)
    real = [e for e in errs if "ERR_ABORTED" not in e]
    print("page errors             :", len(real))
    for e in real[:3]:
        print("   ", e[:140])
    if real:
        fails.append(f"{len(real)} page errors")
    b.close()

print()
print("FAILURES:" if fails else "ALL CHECKS PASSED")
for f in fails:
    print("  -", f)
raise SystemExit(1 if fails else 0)
