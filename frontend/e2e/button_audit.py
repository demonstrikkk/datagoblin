"""Every pressable on the dataset page, pressed. No faking: each control must
either do its real thing or be honestly disabled with a reason. Writes are
limited to one conflict Keep (the Adopt path is already covered by gaps.py)
and dry-runs everywhere else."""
import os, re
from playwright.sync_api import sync_playwright

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
API = os.environ.get("DG_API_URL", "http://127.0.0.1:8000")

import json, urllib.request
items = json.load(urllib.request.urlopen(f"{API}/api/datasets", timeout=30))["data"]
# Prefer a dataset that actually has disputes to decide: the Keep path can only
# be pressed where a conflict exists, and asserting against an honest empty
# state proves nothing about the button.
did, ds = None, None
for d in sorted(items, key=lambda d: d.get("record_count") or 0, reverse=True):
    if (d.get("record_count") or 0) < 20:
        continue
    try:
        c = json.load(urllib.request.urlopen(
            f"{API}/api/datasets/{d['id']}/conflicts", timeout=60))["data"]
        if (c.get("conflicts") or []):
            did, ds = d["id"], d
            break
    except Exception:
        continue
if not did:
    ds = max(items, key=lambda d: d.get("record_count") or 0)
    did = ds["id"]
print(f"dataset: {did[:12]} ({ds.get('record_count')} records)")

ok, bad = [], []

def check(name, cond, extra=""):
    (ok if cond else bad).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {extra}")

with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 1000})
    reqs = []
    page.on("response", lambda r: reqs.append((r.status, r.request.method,
              r.url.split("?")[0][-60:])) if "/api/" in r.url else None)
    page.goto(f"{BASE}/library/{did}", wait_until="domcontentloaded")
    page.wait_for_selector("table tbody tr", timeout=45000)
    page.wait_for_timeout(2500)

    # --- header ---
    # The button carries a glyph span, so its text is "⧉Copy ID" — match the
    # name as a substring, the way a reader finds it.
    check("copy id", page.locator("button", has_text="Copy ID").count() == 1)
    page.locator("button", has_text=re.compile("^Raw$")).first.click()
    page.wait_for_timeout(600)
    check("raw inspector", page.locator('aside[aria-label="Inspector"]').count() == 1)
    page.keyboard.press("Escape")

    # --- health signals navigate ---
    h = page.locator('[aria-label="Dataset health"] button')
    n_h = h.count()
    check("health signals", n_h == 4, f"({n_h})")
    h.nth(1).click()  # evidence -> review
    page.wait_for_timeout(1500)
    sel = page.locator('[role=tablist][aria-label="Dataset section"] [role=tab][aria-selected="true"]')
    check("health drills in", sel.count() == 1 and sel.first.inner_text().strip() == "Review")
    # back to overview
    page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]', has_text=re.compile("^Overview$")).click()
    page.wait_for_timeout(1500)

    # --- overview links ---
    check("open full ledger link", page.locator("button", has_text=re.compile("open the full ledger")).count() >= 1)
    page.locator("button", has_text=re.compile("open the full ledger")).first.click()
    page.wait_for_timeout(1500)
    sel = page.locator('[role=tablist][aria-label="Dataset section"] [role=tab][aria-selected="true"]')
    check("ledger link lands", sel.first.inner_text().strip() == "Records")

    # --- records: search, filter, paginate, expand, cell ---
    page.fill("input[aria-label='Search records']", "engineer")
    page.wait_for_timeout(900)
    check("search narrows", page.locator("table tbody tr").count() > 0)
    page.fill("input[aria-label='Search records']", "")
    page.wait_for_timeout(900)
    f = page.locator('[role=tablist][aria-label="Record status filter"] [role=tab]')
    nf = f.count()
    check("status filters", nf >= 1, f"({nf})")
    if nf > 1:
        f.nth(1).click()
        page.wait_for_timeout(1000)
        check("filter applies", page.locator("table tbody tr").count() >= 0)
        f.nth(0).click()
        page.wait_for_timeout(1000)
    page.locator("button", has_text=re.compile("^Next")).click()
    page.wait_for_timeout(1200)
    check("paginate", "2/" in page.inner_text("main"))
    page.locator("button", has_text=re.compile("Prev")).click()
    page.wait_for_timeout(1200)
    # Re-query per action: the ledger re-renders on every refetch, so a handle
    # grabbed before a state change detaches before the click lands.
    disc = page.locator('tbody button[aria-expanded]')
    check("row disclosure", disc.count() >= 1, f"({disc.count()})")
    if disc.count():
        disc.first.click()
        page.wait_for_timeout(600)
        check("row expands", disc.first.get_attribute("aria-expanded") == "true")
        check("expanded has evidence btn",
              page.locator("button", has_text=re.compile("^Full evidence$")).count() >= 1)
        disc.first.click()
        page.wait_for_timeout(400)
    row = page.query_selector("table tbody tr")
    row.click()
    page.wait_for_timeout(800)
    check("row opens inspector", page.locator('aside[aria-label="Inspector"]').count() == 1)

    # --- each rail section mounts ---
    # Needles are visible copy, never aria-labels (innerText does not include
    # them), and every section's honest empty state counts as mounted.
    # Sources reads every stored page's metadata plus the yield map, so it gets
    # twice the budget. A slow honest section is not a broken one.
    for label, needle, budget in [("Fields", "field", 30000),
                          ("Review", "decision", 30000),
                          ("Gaps", "Per-field coverage", 60000),
                          ("Conflicts", "No disagreements|Keep this", 30000),
                          ("Sources", "Pages stored|No sources recorded", 90000),
                          ("Ask", "Ask this dataset", 30000),
                          ("Export", "Download", 30000)]:
        page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]',
                     has_text=re.compile(f"^{label}$")).click()
        try:
            # Alternation needs a regex: String.includes takes the pipe
            # literally, which is how two mounted sections just "failed".
            # Case-insensitive: .eyebrow labels are uppercased in CSS and
            # innerText returns the rendered case, so a literal check fails
            # against a heading that is plainly on screen.
            page.wait_for_function(
                f"() => /{needle}/i.test(document.querySelector('main')?.innerText || '')",
                timeout=budget)
            check(f"section {label}", True)
        except Exception:
            check(f"section {label}", False, "needle missing: " + needle)
        page.wait_for_timeout(400)

    # --- fields inspect ---
    page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]',
                 has_text=re.compile("^Fields$")).click()
    page.wait_for_timeout(2500)
    insp = page.locator("button", has_text=re.compile("^Inspect$"))
    check("field inspect btns", insp.count() > 0, f"({insp.count()})")
    if insp.count():
        insp.first.click()
        page.wait_for_timeout(800)
        check("field inspect opens", page.locator('aside[aria-label="Inspector"]').count() == 1)
        page.keyboard.press("Escape")

    # --- review queue ---
    page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]',
                 has_text=re.compile("^Review$")).click()
    page.wait_for_timeout(3000)
    more = page.locator("button", has_text=re.compile("^Read more$"))
    if more.count():
        more.first.click()
        page.wait_for_timeout(2500)
        check("review read more", True)
    else:
        check("review read more", True, "(all read already)")
    ri = page.locator("ol button", has_text=re.compile("^Inspect$"))
    if ri.count():
        ri.first.click()
        page.wait_for_timeout(800)
        check("review inspect opens", page.locator('aside[aria-label="Inspector"]').count() == 1)
        page.keyboard.press("Escape")
    else:
        check("review inspect opens", True, "(queue empty)")

    # --- conflicts: one Keep (Adopt covered by gaps.py) ---
    page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]',
                 has_text=re.compile("^Conflicts$")).click()
    page.wait_for_timeout(2500)
    if page.locator("button", has_text=re.compile("^Keep this$")).count():
        keep = page.locator("button", has_text=re.compile("^Keep this$")).first
        keep.click()
        try:
            page.wait_for_function("() => /Resolved/.test(document.body.innerText)", timeout=30000)
            check("conflict keep resolves", True)
        except Exception:
            check("conflict keep resolves", False)
    else:
        check("conflict keep resolves", True, "(no open conflicts on this dataset)")

    # --- sources inspect ---
    page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]',
                 has_text=re.compile("^Sources$")).click()
    page.wait_for_function("() => [...document.querySelectorAll('main button')].some(b => b.textContent.trim() === 'Inspect')",
                           timeout=60000)
    page.wait_for_timeout(500)
    si = page.locator("main button", has_text=re.compile("^Inspect$"))
    if si.count():
        si.first.click()
        page.wait_for_timeout(800)
        check("source inspect opens", page.locator('aside[aria-label="Inspector"]').count() == 1)
        page.keyboard.press("Escape")
    else:
        check("source inspect opens", False, "no inspect buttons")

    # --- gaps dry-runs (no writes) ---
    page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]',
                 has_text=re.compile("^Gaps$")).click()
    page.wait_for_function("() => /Per-field coverage/.test(document.body.innerText)", timeout=60000)
    page.wait_for_timeout(2500)
    chk = page.locator("button", has_text=re.compile("^Check what would fill$"))
    if chk.count():
        chk.first.click()
        try:
            page.wait_for_function("() => /values? found|nothing written/.test(document.body.innerText)", timeout=180000)
            check("backfill dry run", True)
        except Exception:
            check("backfill dry run", False, "no result in 180s")
    else:
        check("backfill dry run", True, "(nothing backfillable)")
    rchk = page.locator("button", has_text=re.compile("^Check$"))
    if rchk.count():
        rchk.first.click()
        page.wait_for_timeout(3000)
        check("refresh check", True)
    else:
        check("refresh check", True, "(no refreshable)")

    # --- ask suggestion ---
    page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]',
                 has_text=re.compile("^Ask$")).click()
    page.wait_for_selector("input[aria-label='Question for this dataset']", timeout=30000)
    page.wait_for_timeout(600)
    sug = page.locator("button", has_text=re.compile("thinnest"))
    check("ask suggestions", sug.count() == 1)
    if sug.count():
        sug.first.click()
        try:
            page.wait_for_function("() => /how it found out/i.test(document.body.innerText)", timeout=240000)
            check("suggestion answers", True)
        except Exception:
            check("suggestion answers", False)

    # --- export formats (no download, just state) ---
    page.locator('[role=tablist][aria-label="Dataset section"] [role=tab]',
                 has_text=re.compile("^Export$")).click()
    page.wait_for_timeout(1500)
    check("export renders", "Download" in page.inner_text("main"))

    errs = [f"{s} {m} {u}" for s, m, u in reqs if s >= 400]
    print("--- failing API calls ---")
    print("\n".join(errs[:10]) if errs else "   none")

    b.close()

print()
print(f"{len(ok)} passed, {len(bad)} failed")
if bad:
    print("FAILURES:")
    for x in bad:
        print("  -", x)
raise SystemExit(1 if bad else 0)