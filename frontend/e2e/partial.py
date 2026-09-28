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

RUN = "e95fa5a8"


async def main():
    fails = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        p = await b.new_page(viewport={"width": 1600, "height": 1050})
        errs = []
        p.on("pageerror", lambda e: errs.append(str(e)))
        p.on("console", lambda m: errs.append(m.text[:160]) if m.type == "error" else None)

        # find the full id from history via the UI's own API
        import urllib.request, json as _json
        with urllib.request.urlopen("http://127.0.0.1:8000/api/history", timeout=30) as fh:
            _rows = _json.load(fh)["data"]
        rid = next((x["id"] for x in _rows if x.get("id", "").startswith("e95fa5a8")), "")
        print("run id:", rid)
        if not rid:
            fails.append("could not resolve the partial run id")
            await b.close()
            return 1

        await p.goto(f"{BASE}/runs/{rid}", wait_until="domcontentloaded")
        await p.wait_for_selector("main", timeout=20000)
        await p.wait_for_timeout(4000)
        txt = await p.inner_text("main")

        print("\n=== partial run rendering ===")
        checks = {
            "status pill (Partial)": "partial" in txt.lower(),
            "partial explained": "stored what it verified" in txt.lower(),
            "budget/error surfaced": "budget" in txt.lower() or "overran" in txt.lower(),
            "records count": "88" in txt,
            "quality breakdown": "evidence quality" in txt.lower(),
            "conflicting called out": "conflict" in txt.lower(),
            "stages rail": "deduplicate" in txt.lower(),
            "reduce/finalize honesty": "does not emit" in txt.lower(),
        }
        for k, v in checks.items():
            print(f"  {k:28}: {v}")
            if not v:
                fails.append(f"partial run view missing: {k}")

        # rail must not contain a node labelled Complete
        rail = await p.evaluate(
            "[...document.querySelectorAll('ol li')].map(li=>li.innerText.trim().split('\\n')[0])"
        )
        print("  rail nodes:", rail)
        if any(n.strip().lower() == "complete" for n in rail):
            fails.append("rail still shows a 'Complete' node, readable as a finished run")

        await p.screenshot(path=f"{OUT}/15-partial-run.png")

        # the quality distribution should be clickable
        bars = await p.evaluate("document.querySelectorAll('[role=img]').length")
        print("  quality bar present:", bars > 0)

        # a stuck run should read as running, not as Planning
        await p.goto(f"{BASE}/runs", wait_until="domcontentloaded")
        await p.wait_for_selector("tbody tr", timeout=20000)
        await p.wait_for_timeout(2500)
        await p.screenshot(path=f"{OUT}/16-runs-live.png")

        real = [e for e in errs if "ERR_ABORTED" not in e]
        if real:
            print("  js errors:", real[:4])
            fails.extend(real[:2])

        await b.close()

    print("\n" + "=" * 52)
    print("FAILURES:" if fails else "ALL PASSED")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
