"""The proof view must actually prove something.

A quote's `start`/`end` offsets are only evidence if they resolve against the
page that was stored at run time. This opens a field, asks for the quote in
context, and checks that the highlighted span is the quote itself rather than
whatever the offsets happen to land near.

It also catches the encoding class of bug that a hand-check can miss: offsets
are computed in Python (code points) and rendered in JavaScript (UTF-16 code
units), so a page containing characters outside the BMP shifts every later
position and the wrong span gets marked.
"""
import asyncio
import os
import re
import sys

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
OUT = os.environ.get(
    "DG_SHOT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots"),
)
os.makedirs(OUT, exist_ok=True)


def _discover_dataset() -> str:
    """The newest dataset, found at run time.

    This was a hardcoded id. It worked until that dataset was deleted,
    and then the suite failed for a reason that had nothing to do with the
    code under test. A test that breaks when unrelated data changes is not
    testing anything.
    """
    import json as _json
    import urllib.request as _u
    api = os.environ.get("DG_API_URL", "http://127.0.0.1:8000")
    with _u.urlopen(f"{api}/api/datasets", timeout=60) as r:
        items = _json.loads(r.read().decode("utf-8"))["data"] or []
    if not items:
        raise SystemExit("no datasets exist; run a collection first")
    return items[0]["id"]

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

# Google Fonts is reached over the public internet, and when it is slow the
# networkidle waits below never resolve: a hermetic failure that looks like the
# app hanging. Stub it so the suite needs no network.
OFFLINE_FONTS = re.compile(r"fonts\.(googleapis|gstatic)\.com")

DS = _discover_dataset()
PROOF_BTN = "Show this quote in the stored page"


async def main() -> int:
    fails: list[str] = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        p = await b.new_page(viewport={"width": 1600, "height": 1000})
        await p.route(OFFLINE_FONTS, lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        p.on("pageerror", lambda e: (print("PAGEERROR:", e), fails.append(str(e))))

        await p.goto(f"{BASE}/library/{DS}", wait_until="networkidle")
        await p.wait_for_selector("table tbody tr", timeout=45000)
        await p.wait_for_timeout(700)
        await p.click("table tbody tr")
        await p.wait_for_timeout(700)

        inspector = 'aside[aria-label="Inspector"]'
        fields = await p.query_selector_all(f"{inspector} button[aria-expanded]")
        print(f"=== expandable fields: {len(fields)} ===")

        checked = 0
        for i in range(len(fields)):
            if checked >= 3:
                break

            # Collapse anything left open from the previous pass. Click only the
            # first open row each round: closing a field removes its proof
            # toggle from the DOM, so a pre-collected list goes stale and a
            # later click in it silently does nothing — which leaves an earlier
            # field's quote in the panel and reads as a mismatch in the product.
            for _ in range(8):
                open_rows = await p.query_selector_all(f'{inspector} button[aria-expanded="true"]')
                if not open_rows:
                    break
                try:
                    await open_rows[0].click()
                except Exception:
                    pass
                await p.wait_for_timeout(250)

            all_rows = await p.query_selector_all(f"{inspector} button[aria-expanded]")
            if i >= len(all_rows):
                break
            try:
                await all_rows[i].click()
            except Exception:
                continue
            await p.wait_for_timeout(400)

            proof_btn = p.locator(f"{inspector} button", has_text=PROOF_BTN).first
            if await proof_btn.count() == 0:
                continue  # this field has no page/offsets; nothing to prove

            await proof_btn.click()

            # Poll for a terminal state. A fixed wait misclassifies a slow page
            # fetch as a missing highlight, and the suite ran late in a long
            # session where that actually happened.
            outcome = None
            for _ in range(32):
                await p.wait_for_timeout(250)
                if await p.locator(f"{inspector} mark").count() > 0:
                    outcome = "ok"
                    break
                panel = await p.inner_text(inspector)
                if "do not fall inside" in panel:
                    outcome = "bad-offsets"
                    break
                if "no longer matches this quote" in panel:
                    outcome = "mismatch"
                    break

            if outcome == "bad-offsets":
                print(f"  field {i}: offsets outside stored page (reported, not faked)")
                continue
            if outcome == "mismatch":
                print(f"  field {i}: page/quote disagreement reported honestly")
                continue
            if outcome is None:
                panel = await p.inner_text(inspector)
                fails.append(
                    f"field {i}: proof requested but no highlight rendered "
                    f"(panel said: {panel[-240:]!r})"
                )
                continue

            # Read the quote from the SAME field as the highlight: walk up from
            # the mark until an ancestor holds the blockquote that quote came
            # from, rather than taking the first quoted line in the panel.
            pair = await p.evaluate(
                """() => {
                  const mark = document.querySelector('aside[aria-label="Inspector"] mark');
                  if (!mark) return null;
                  let node = mark;
                  while (node && !node.querySelector('blockquote')) node = node.parentElement;
                  const bq = node ? node.querySelector('blockquote') : null;
                  return { mark: mark.textContent || '', quote: bq ? bq.textContent : '' };
                }"""
            )
            if not pair:
                fails.append(f"field {i}: highlight not found in the DOM")
                continue

            marked = pair["mark"]
            needle = pair["quote"].strip().strip("“”").strip()
            head = needle[:80]
            ok = bool(head) and (head in marked or marked[:80] in needle)
            print(f"  field {i}: highlight {len(marked)} chars, matches quote: {ok}")
            if not ok:
                fails.append(
                    f"field {i}: highlight does not match the quote "
                    f"(mark={marked[:60]!r} quote={needle[:60]!r})"
                )
            panel = await p.inner_text(inspector)
            if "@" not in panel:
                fails.append(f"field {i}: proof view omitted the character offsets")
            checked += 1

        print(f"  fields proven: {checked}")
        if checked == 0:
            fails.append("no field offered a proof view")

        await p.screenshot(path=os.path.join(OUT, "proof-view.png"), full_page=False)
        await b.close()

    print("\n" + "=" * 55)
    print("FAILURES:" if fails else "ALL PASSED")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
