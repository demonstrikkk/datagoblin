import asyncio
import os
import re

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
OUT = os.environ.get(
    "DG_SHOT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots"),
)
os.makedirs(OUT, exist_ok=True)

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

# Google Fonts is reached over the public internet, and when it is slow the
# networkidle waits below never resolve: a hermetic failure that looks like the
# app hanging. Stub it so the suite needs no network.
OFFLINE_FONTS = re.compile(r"fonts\.(googleapis|gstatic)\.com")



async def main():
    import os
    os.makedirs(OUT, exist_ok=True)
    fails = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
        p = await b.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        await p.route(OFFLINE_FONTS, lambda route: route.fulfill(status=200, content_type="text/css", body=""))

        await p.goto(f"{BASE}/", wait_until="networkidle")

        # Is the WebGL field actually drawing, or is the canvas blank?
        #
        # Polled rather than sampled once after a sleep. "Is the shader drawing?"
        # is a question about whether it *ever* draws, and a single read at an
        # arbitrary 2.5s answers a different question — whether it had drawn by
        # 2.5s. Under a full e2e chain that reported a flat canvas twice on a
        # machine where the shader was working fine, which is a test reporting a
        # product bug that did not exist.
        #
        # Reading the pixels back needs a live GL context, so it is polled until
        # something is drawn or the budget runs out. `range` is the channel
        # spread: a uniform buffer is a flat range, and 3 is the floor below
        # which "drawing" and "a solid fill" are indistinguishable.
        READ = """() => {
              const c = document.querySelector('canvas.field-canvas');
              if (!c) return {ok:false, why:'no canvas'};
              const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
              if (!gl) return {ok:false, why:'no gl context (css fallback active)'};
              const w = c.width, h = c.height;
              if (!w || !h) return {ok:false, why:'canvas has no size yet'};
              const px = new Uint8Array(w*h*4);
              gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,px);
              let min=[255,255,255], max=[0,0,0], sum=[0,0,0], n=0;
              for (let i=0;i<px.length;i+=4){
                for (let k=0;k<3;k++){
                  if(px[i+k]<min[k])min[k]=px[i+k];
                  if(px[i+k]>max[k])max[k]=px[i+k];
                  sum[k]+=px[i+k];
                }
                n++;
              }
              return {ok:true, w, h, min, max,
                      mean: sum.map(s=>Math.round(s/n)),
                      range: max.map((m,k)=>m-min[k])};
            }"""

        stats = {"ok": False, "why": "never sampled"}
        drew = False
        for _ in range(20):
            await p.wait_for_timeout(500)
            stats = await p.evaluate(READ)
            if not stats.get("ok"):
                continue
            if max(stats["range"]) >= 3:
                drew = True
                break
        print("=== WebGL field ===")
        print(" ", stats)
        if stats.get("ok"):
            r = stats["range"]
            if not drew:
                fails.append(
                    f"shader canvas never drew (range {r} after 10s) - nothing is "
                    f"being rendered")
            else:
                print(f"  -> drawing, channel spread {r} (paper base with tinted field)")
        else:
            print("  ->", stats.get("why"))

        # Animating? sample twice and compare.
        a = await p.evaluate("document.querySelector('canvas.field-canvas').toDataURL().length")
        await p.wait_for_timeout(900)
        bb = await p.evaluate("document.querySelector('canvas.field-canvas').toDataURL().length")
        print(f"  frame changes over 900ms: {a != bb} ({a} vs {bb} chars)")

        shots = [
            ("01-collect", "/"),
            ("02-runs", "/runs"),
            ("03-library", "/library"),
            ("04-intel", "/intel"),
            ("05-dataset", "/library/4d347326-7675-4179-858c-f22d04255e9a"),
        ]
        for name, path in shots:
            await p.goto(f"{BASE}{path}", wait_until="networkidle")
            await p.wait_for_timeout(1800)
            await p.screenshot(path=f"{OUT}/{name}.png")
            print(f"  shot {name}")

        # record inspector open
        await p.goto(f"{BASE}/library/4d347326-7675-4179-858c-f22d04255e9a", wait_until="networkidle")
        await p.wait_for_selector("table tbody tr", timeout=15000)
        await p.wait_for_timeout(900)
        await p.click("table tbody tr")
        await p.wait_for_timeout(500)
        exp = await p.query_selector('aside[aria-label="Inspector"] button[aria-expanded]')
        if exp:
            await exp.click()
            await p.wait_for_timeout(500)
        await p.screenshot(path=f"{OUT}/06-evidence.png")
        print("  shot 06-evidence")

        # sources
        await p.locator('[role=tab]').nth(1).click()
        await p.wait_for_timeout(1800)
        await p.screenshot(path=f"{OUT}/07-sources.png")
        print("  shot 07-sources")

        # command palette
        await p.goto(f"{BASE}/", wait_until="networkidle")
        await p.wait_for_timeout(900)
        await p.keyboard.press("Control+k")
        await p.wait_for_timeout(700)
        await p.screenshot(path=f"{OUT}/08-palette.png")
        print("  shot 08-palette")

        # settings
        await p.keyboard.press("Escape")
        await p.wait_for_timeout(300)
        await p.click("nav[aria-label='Primary'] button[aria-label='Settings']")
        await p.wait_for_timeout(700)
        await p.screenshot(path=f"{OUT}/09-settings.png")
        print("  shot 09-settings")

        # mobile
        await p.keyboard.press("Escape")
        m = await b.new_page(viewport={"width": 390, "height": 844})
        await m.route(OFFLINE_FONTS, lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        await m.goto(f"{BASE}/", wait_until="networkidle")
        await m.wait_for_timeout(2000)
        await m.screenshot(path=f"{OUT}/10-mobile.png")
        print("  shot 10-mobile")
        ov = await m.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        print("  mobile overflow:", ov)
        if ov > 4:
            fails.append(f"mobile horizontal overflow {ov}px")

        await b.close()

    print("\n" + "=" * 50)
    print("FAILURES:" if fails else "ALL PASSED")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
