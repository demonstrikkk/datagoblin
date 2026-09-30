"""Screenshot the landing surface at desktop and mobile, plus the two states
that only exist after interaction: the proof rail open, and the scrubber moved
off its citation.

Run against the built preview server, like visual.py:

    npm run preview
    python e2e/landing.py
"""

import asyncio
import os
import re
import sys

BASE = os.environ.get("DG_BASE_URL", "http://localhost:4173")
OUT = os.environ.get(
    "DG_SHOT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screenshots", "landing"),
)
os.makedirs(OUT, exist_ok=True)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

# Google Fonts is reached over the public internet; stub it so this pass is
# hermetic and never waits on a slow network.
OFFLINE_FONTS = re.compile(r"fonts\.(googleapis|gstatic)\.com")

DESKTOP = {"width": 1512, "height": 950}
MOBILE = {"width": 390, "height": 844}


async def settle(p, ms=900):
    """Walk the page so every reveal fires, then return to the top.

    Scrolling the whole document rather than jumping to the bottom matters:
    IntersectionObserver only fires for elements that actually crossed the
    viewport, and a jump leaves everything in the middle unobserved.
    """
    await p.wait_for_timeout(ms)
    height = await p.evaluate("() => document.body.scrollHeight")
    y = 0
    while y < height:
        y += 500
        await p.evaluate("(v) => window.scrollTo(0, v)", y)
        await p.wait_for_timeout(90)
    await p.evaluate("() => window.scrollTo(0, 0)")
    await p.wait_for_timeout(800)


async def shot_viewport(p, name):
    path = os.path.join(OUT, f"{name}.png")
    await p.screenshot(path=path)
    print("wrote", path)


async def shot_full(p, name):
    path = os.path.join(OUT, f"{name}.png")
    await p.screenshot(path=path, full_page=True)
    print("wrote", path)


async def main():
    fails = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(
            args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"]
        )

        for label, viewport in (("desktop", DESKTOP), ("mobile", MOBILE)):
            ctx = await b.new_context(viewport=viewport, device_scale_factor=1)
            await ctx.route(OFFLINE_FONTS, lambda route: route.fulfill(
                status=200, content_type="text/css", body=""
            ))
            p = await ctx.new_page()
            errors = []
            # Every request is recorded so "the video must not load until asked"
            # can be asserted as a fact rather than assumed.
            requests = []
            p.on("request", lambda r: requests.append(r.url))
            p.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            p.on("pageerror", lambda e: errors.append(str(e)))

            await p.goto(f"{BASE}/welcome", wait_until="networkidle")
            await settle(p)

            # 1. the first viewport — the thesis
            await shot_viewport(p, f"{label}-01-hero")

            # 2. the proof rail. The page opens the rail by itself after a
            # beat to demonstrate the mechanism, so pick a cell that is not
            # already selected rather than toggling that one closed.
            await p.wait_for_timeout(1900)
            vals = p.locator(".lg-cell.num:not(:has(.lg-cell-null))")
            if await vals.count() == 0:
                fails.append(f"{label}: no verified valuation cell to open")
            else:
                target = vals.nth(1 if await vals.nth(1).count() else 0)
                if await target.get_attribute("aria-pressed") == "true":
                    target = vals.nth(0)
                await target.click()
                await p.wait_for_timeout(700)
                if await p.locator(".lg-rail").count() == 0:
                    fails.append(f"{label}: clicking a value did not open the evidence rail")
                await shot_viewport(p, f"{label}-02-rail-open")

                # 3. the scrubber dragged off its citation: the claim must fail
                slider = p.locator(".lg-scrub input[type=range]")
                if await slider.count():
                    await slider.first.focus()
                    for _ in range(40):
                        await p.keyboard.press("ArrowLeft")
                    await p.wait_for_timeout(450)
                    await shot_viewport(p, f"{label}-03-scrub-missed")
                    matched = await p.locator(".lg-scrub-state").first.get_attribute("data-matched")
                    if matched == "true":
                        fails.append(f"{label}: scrubber still reports a match after 40 ArrowLeft")
                else:
                    fails.append(f"{label}: scrubber not rendered for a cited field")

            # 4. an honest null — the row that kept its emptiness
            nulls = p.locator(".lg-cell.num:has(.lg-cell-null)")
            if await nulls.count():
                await nulls.first.click()
                await p.wait_for_timeout(700)
                await shot_viewport(p, f"{label}-04-null-field")
            else:
                fails.append(f"{label}: the honest-null row is not in the table")

            await shot_full(p, f"{label}-05-full")

            # --- the reel: nothing is fetched until a reader asks for it ---
            media = [
                r
                for r in requests
                if ".mp4" in r or "explainer-poster" in r
            ]
            if any(".mp4" in r for r in media):
                fails.append(f"{label}: the video was fetched before anyone pressed play")

            poster_shown = await p.locator(".lg-reel-poster").count()
            if poster_shown != 1:
                fails.append(f"{label}: no poster is shown before playback")

            play = p.locator(".lg-reel-play")
            if await play.count() == 0:
                fails.append(f"{label}: the reel has no play affordance")
            else:
                await play.first.click()
                await p.wait_for_timeout(2600)

                # Did the clock actually move? A player that renders a frame but
                # never advances is the failure this catches.
                t1 = await p.evaluate(
                    "() => document.querySelector('.lg-reel-video').currentTime"
                )
                await p.wait_for_timeout(1400)
                t2 = await p.evaluate(
                    "() => document.querySelector('.lg-reel-video').currentTime"
                )
                if not (isinstance(t2, (int, float)) and t2 > t1 + 0.3):
                    fails.append(f"{label}: playback did not advance ({t1} -> {t2})")
                await shot_viewport(p, f"{label}-06-reel-playing")

                # Chapter navigation must move the playhead.
                chips = p.locator(".lg-reel-chapters button")
                n_chips = await chips.count()
                if n_chips != 20:
                    fails.append(f"{label}: {n_chips} chapters, expected 20 detected scenes")
                else:
                    await chips.nth(8).click()
                    await p.wait_for_timeout(900)
                    t3 = await p.evaluate(
                        "() => document.querySelector('.lg-reel-video').currentTime"
                    )
                    if abs(t3 - 270.292) > 6:
                        fails.append(
                            f"{label}: chapter 9 seeked to {t3:.1f}s, expected ~270.3s"
                        )
                    now = await p.locator(".lg-reel-chapter").first.inner_text()
                    if "offset" not in now.lower():
                        fails.append(f"{label}: chapter label did not follow the seek ({now!r})")

                # The full-screen inspector.
                await p.locator('button[aria-label="Inspect this frame full screen"]').click()
                await p.wait_for_timeout(1400)
                if await p.locator(".lg-inspect").count() == 0:
                    fails.append(f"{label}: the inspector did not open")
                else:
                    await p.locator('button[aria-label="Zoom in"]').click()
                    await p.locator('button[aria-label="Zoom in"]').click()
                    await p.wait_for_timeout(400)
                    scale = await p.locator(".lg-inspect-scale").inner_text()
                    if scale.strip() != "2.0×":
                        fails.append(f"{label}: inspector zoom reads {scale!r}, expected 2.0×")
                    await shot_viewport(p, f"{label}-07-inspect")
                    await p.keyboard.press("Escape")
                    await p.wait_for_timeout(400)
                    if await p.locator(".lg-inspect").count() != 0:
                        fails.append(f"{label}: Escape did not close the inspector")

            # The limitations section must actually be on the page, not merely
            # present in the bundle: it was invisible once because a reveal
            # never fired for it.
            for sel, label in (("#pipeline .lg-phase", "pipeline phases"),
                               ("#sufficiency .lg-gap-row", "gap diagram rows"),
                               ("#vocabulary .lg-vocab li", "status vocabulary rows")):
                n = await p.locator(sel).count()
                if n < 3:
                    fails.append(f"{label}: only {n} rendered")
                for i in range(n):
                    if not await p.locator(sel).nth(i).is_visible():
                        fails.append(f"{label}: item {i} is in the DOM but not visible")

            # Overflow check: a horizontal scrollbar at any width is a failure.
            overflow = await p.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            if overflow > 1:
                fails.append(f"{label}: horizontal overflow of {overflow}px")

            real = [e for e in errors if "fonts.g" not in e]
            if real:
                fails.append(f"{label}: console errors {real[:3]}")

            await ctx.close()

        await b.close()

    print()
    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    asyncio.run(main())