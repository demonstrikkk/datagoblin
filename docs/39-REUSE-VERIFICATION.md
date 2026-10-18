# 39 — REUSE_BROWSER: the live verification that was left open

> **Status:** run 2026-10-18 against `d859d45` as committed.
> **Purpose:** `d859d45` shipped the reusable browser default-off and said, in its
> own commit message, that one thing about it was unproven. This is that proof.
> **Method:** real Chromium, three real pages, both arms measured, and a control
> for every check that could pass for the wrong reason.
> **Result:** reuse is **safe**. Its **payoff is unproven**. The flag stays off,
> and the reason is now the measured one rather than the assumed one.

## Doc map

| if you care about | read |
|---|---|
| what the system is | `ARCHITECTURE-OF-RECORD.md` |
| this verification and its corrections | **this file** |
| crawl order, escalation, rungs | `09-CRAWLING-POLICY.md` |
| evidence layer and what `verified` means | `29-EVIDENCE-LAYER.md`, `11-VALIDATION-SPEC.md` |

---

## 1. What was pending

`d859d45` added `REUSE_BROWSER` and closed with this:

> *"Not proven here, and deliberately left to a live crawl: that crawl4ai's
> `AsyncWebCrawler` is genuinely safe to reuse across `arun` calls. That needs a
> real browser and a real site, which this environment does not have, and it is
> the main reason the flag defaults to off."*

The reason it was still pending eleven hours later is worth recording, because
it is the same shape as the project's other worst bugs: **a precondition was
fixed and the thing it unblocked was never revisited.**

| time | event |
|---|---|
| 21:34 | `d859d45` written — no browser on the machine, verification deferred |
| 23:37–23:39 | Chromium `1248` + headless shell installed; playwright driver directory repaired |
| 23:47 | `2b8f672` — the wait-selector fix that could not be measured without a browser |

Ten minutes after the precondition was satisfied, nothing was re-run. The
environment was the reason; the environment stopped being the reason, and
nothing noticed.

## 2. What it was run against

| | |
|---|---|
| Machine | 7.7 GB RAM (8269 MB), 8 logical CPUs, Windows |
| crawl4ai | 0.9.4, Chromium 1248 |
| Targets | `hackerearth.com/hackathon/explore/city/delhi`, `aws.amazon.com/transform/`, `aws.amazon.com/events/sponsorship` |
| Why these | the exact pages `2b8f672` measured, so reuse timings are comparable with a published baseline (6.1s / 12.8s / 11.4s) |
| Posture | production waterfall `crawler._one(route="web:crawl4ai")` — real robots, throttle, SSRF. No Tavily, no LLM, no Jev, no database, no API |
| Why `web:crawl4ai` | on the shipped `web:http` route crawl4ai only fires to rescue a thin page, so these URLs would never reach the browser rung and the reuse behaviour would go unmeasured while the bench looked clean |

## 3. Verdict

**Safety: proven. Nothing reuse-specific failed.**

Every lifecycle bound behaved exactly as its docstring claims:

| control | how it was exercised | result |
|---|---|---|
| recycle at `MAX_PAGES` | `MAX_PAGES=2` over 5 pages | exactly **2** `"recycling browser after 2 pages"` lines; the page count never passed the cap |
| invalidate on failure | forced 1s timeout through the real path | browser discarded (`pages: 0`, `has_browser: false`); the next page got a fresh one (`pages: 1`) |
| idle reap | idle window against a real render | `"closing idle browser"` logged, `has_browser: false`, process count returned |
| browser survives shutdown | census by browser root | **drains to 0 unaided** in 60–70s, `orphan_roots: 0` at every sample |
| output fidelity | reuse vs a fresh-vs-fresh band | **inside the band on all 3 pages** |

**Payoff: not proven.** Fresh renders measured **5.5–25.6s**; reuse **4.3–24.7s**.
The distributions overlap heavily and on a loaded machine reuse was *no faster at
all*. The launch saving is real on a run's first page and small thereafter.

So the flag stays off — but the sentence attached to it changes. It was
**"unproven to be safe"**, which was true when written and is false now. It is
**"safe, and unproven to pay."** Those are different claims, and `config.py` now
carries the measured one.

## 4. The part worth reading: three checks that were wrong

The first run of this bench returned two FAILs. Both were the bench's fault, and
both were found by a control rather than by review. This section is the point of
the exercise.

### 4.1 Hash equality was never a valid criterion

The bench rendered each page once per arm and compared the markdown. It reported
every page `DIFF`. Before believing that, a control: **two fresh browsers, reuse
switched off entirely, disagreeing with each other.**

| page | fresh round 1 | fresh round 2 | verdict |
|---|---|---|---|
| `hackerearth` | 2778 ch | 2778 ch | different hash, same length |
| `aws/transform` | 17636 ch | 18764 ch | **1128 chars apart** |
| `aws/sponsorship` | 45818 ch | 45809 ch | different hash |

These pages are not byte-stable on their own. "Fresh output differs from reused
output" therefore says nothing about reuse. The criterion was replaced with
**"inside the band two fresh renders established"**, and every page passed:
`hackerearth` 2778 in a 2778–2778 band, `transform` 17657 in 17657–17694,
`sponsorship` 45809 in 45809–45809.

This was not moving the goalposts to manufacture a pass. The control is
evidence about the **fresh** path, independent of the reuse result. Had it shown
the pages were stable, the `DIFF` would have stood and reuse would have been
reported as defective.

### 4.2 "12 leaked chromium processes" was arithmetic, not a leak

The bench counted processes after shutdown and called it a leak. Chromium runs a
browser process plus renderer, GPU and utility children, so three browsers
correctly tearing down leave a dozen processes alive for a while. Counting
processes and calling it a browser count is the same error as counting a row and
calling it a record.

Counting by browser root instead:

| t | processes | browser roots | orphan roots |
|---|---|---|---|
| 0s | 11 | 3 | 0 |
| 20s | 8 | 2 | 0 |
| 40s | 4 | 1 | 0 |
| 70s | **0** | **0** | **0** |

Nothing was orphaned and everything drained without intervention. **There is no
leak**, and the earlier `browser_pool.py` reference to "observed: 11 leaked
chromiums, wedged server" most likely measured this same teardown lag and read it
as a leak. The bench now records both the 8s and the 70s sample so the artefact
stays visible instead of being quietly corrected.

### 4.3 Two phases measured their own races

The reap timer is armed once and the idle clock moved only on **return**. A phase
that left `REUSE_BROWSER_IDLE_S=5` from a previous step had its browser closed
*during* the next render — which is a real defect (§5) reached by accident, and
a bench that was measuring its own setup. Both phases now use an idle window
comfortably longer than the render they run.

## 5. Real defect found and fixed: the reap closed browsers mid-render

`BrowserPool` arms a `threading.Timer` on use and `_BrowserSlot.touch()` moved
`_last_used` only after a call returned. So for any call longer than `IDLE_S`,
`_reap()` saw the slot as idle **while it was rendering**, and closed the browser.
Every subsequent step of that render failed with `"Target page, context or
browser has been closed"`, and the page silently fell back to a non-rendered
rung.

Unreachable at the shipped defaults — `REUSE_BROWSER_IDLE_S` is 120 and
`FETCH_CRAWL_TIMEOUT_S` is 90 — which is exactly why 16 tests missed it: **the
numbers hid it.** Any operator who tunes `IDLE_S` below the crawl timeout hits it
immediately.

The fix is small: the slot marks itself busy before submitting a call, and
`idle_for()` returns `0.0` while busy, so the existing `>= idle_s` test in `_reap`
skips it. No second condition for the reap to forget.

`test_a_browser_is_not_reaped_while_a_call_is_in_flight` inverts the numbers on
purpose — a call six times the idle window — because that is the only order in
which this is visible. **It was confirmed to fail without the fix**
(`AssertionError: reaped a browser that was mid-render`) and pass with it.

## 6. F1: `REUSE_BROWSER_CONCURRENCY` does not do what it says

`BrowserPool.run()` dispatches to `self._slots[0]`. With `concurrency=N`, N slots
are built and **only the first is ever used**.

Both `browser_pool.py` and `config.py:152-154` claimed raising it "buys
concurrency at the cost of one Chromium per slot". It buys nothing. Measured:
3 slots built, **1** holding a browser, 2 permanently empty.

And nothing could fail on the lie, because
`test_concurrent_callers_do_not_launch_a_browser_each` — 8 threads, 1 browser —
**passes because of** the serialisation rather than in spite of it. The suite
pinned the exact behaviour the comment denied.

Corrected in both places. `test_extra_slots_are_built_and_never_used` now pins
the truth by name, and its docstring says what to do if it ever fails: the
feature landed, so fix the comments in the same change. Real concurrency is
**not** implemented — the browser rung is an escalation fallback, and nothing
measured a need for it.

## 7. Open: the Playwright driver dies mid-render, cause unknown

Not reuse's doing, and not memory's either. Both hypotheses were tested and
falsified.

**Symptom.** `Page.goto` / `Page.wait_for_selector`: *"Connection closed while
reading from the driver."* This is a **Playwright** string, not a crawl4ai one:
the browser process went away mid-call. Windows logged no application error
(event 1000) and no resource exhaustion (event 2004) — the signature of a
process killed under pressure, not one that faulted.

**It is not reuse.** Across every run, **3 of 21** renders failed to come back on
the browser rung: **1/12 with reuse off, 2/9 with it on.** At n=21 that cannot
separate 8% from 22%, and the one clear counter-example is a failure *with the
flag off*.

**It is not memory.** Memory pressure was the leading hypothesis — this box runs
at 86–91% used. The instrumented A/B falsified it: **two of the three failures
occurred at 2443 MB and 2005 MB available**, the highest readings of the run.
The third, at 729 MB, is the only one that fits.

**Where it concentrates.** `aws/transform` 2/7, `hackerearth` 1/7,
`aws/sponsorship` 0/7 — concentrated but non-deterministic, which reads more like
a probabilistic block than a deterministic bug. Not established. A larger sample
on a machine with room to spare would settle it.

### 7.1 What is actionable regardless of cause

When the renderer dies, `crawler._one` escalates to `http` and keeps going. The
stored page then holds **7885 characters where the browser produced 17635** — a
**55% content loss**, with a URL, a title, a hash and no indication that a render
was ever attempted.

It is not entirely invisible: `politeness.stats.note_retry()` counts it and
`GET /api/crawl/timing` records `outcome: "raised:..."` against the rung. But
neither is **per-URL**, and `sources.method` records only `http` — which is
indistinguishable from a page where `http` simply succeeded first. On the shipped
`web:http` route that is the common case.

**A silent drop is indistinguishable from a wrong drop, and the wrong one is the
dangerous one.** A per-URL marker that a renderer was attempted and failed is the
suggested fix. Not implemented here — it is outside the scope of this
verification, and it changes the shape of the `sources` row.

## 8. Spotted in passing, not fixed

`discovery.py:295` passes `triage_fn=triage_source`, and `source_router.triage_source`
is **synchronous** (`source_router.py:7`). But `test_robots_gate.py:40` and
`test_discovery_requery.py:86` both define **`async def _triage`**, and
`graph.py:372` calls `deps.triage_fn(url)` without `await`.

In production this is correct. In those two tests, `accepted_sources[].route` is
a **coroutine object**, not a route string, and Python emits
`RuntimeWarning: coroutine ... was never awaited` on every run. The tests pass
because they assert on `screened_urls` and on emptiness, never on `route` — so
**they do not exercise routing at all**, and would not catch a routing regression.

A check that cannot fail is not a check. Fix is to make the two fakes `def`, but
it was left alone here rather than changed inside a verification commit.

## 9. What this does NOT prove

- **The payoff.** 21 renders across 3 pages is enough to find a reuse-specific
  defect if one exists, and not enough to certify the flag for default-on.
- **That the driver deaths are harmless.** They are unexplained (§7). Reuse is
  exonerated; that is not the same as the browser rung being reliable.
- **Anything about the other thirteen commits** from 2026-10-16. Only this one was
  explicitly pending. `EXTRACT_RELEVANCE_GATE`, the fetch-timing dispatch fix and
  the event batching have unit tests and some live evidence, but have not been
  seen together in a real run.
- **Pages other than these three.** One is a JS-driven listing, one is a bot-walled
  marketing page, one is a large event page. A quieter site is untested.

## 10. Reproducing it

```powershell
# the A/B harness: memory sampling, both arms, leak census, correlation
& backend\.venv\Scripts\python.exe backend\scripts\bench_reuse_isolate.py
# -> outputs/bench_reuse_isolate.json

# lifecycle controls: recycle, invalidate, idle reap, F1, output band
& backend\.venv\Scripts\python.exe backend\scripts\bench_reuse.py
# -> outputs/bench_reuse.json

# browser-root census with no browser launched; settles the leak question
& backend\.venv\Scripts\python.exe backend\scripts\bench_leak_measure.py
# -> outputs/bench_leak_measure.json

& backend\.venv\Scripts\python.exe -m pytest backend\tests -q
# -> 1620 passed, 6 skipped   (1617 before this work; +3 tests added here)
```

`bench_reuse_isolate.py` **terminates every ms-playwright Chromium before it
starts**, scoped to the install path, and aborts rather than reporting numbers if
the machine cannot be brought to zero. Its own residue from an earlier run is
enough to invalidate it — that happened once here.

The JSON these write lands in `outputs/`, which is **gitignored** — the numbers
in this file are the record, not the files. Re-running overwrites them.

## 11. Corrections this file makes to earlier text

| Claim | Where | Reality |
|---|---|---|
| "That needs a real browser and a real site, which this environment does not have" | `d859d45` message; `test_browser_pool.py` docstring | Was true when written. Chromium was installed 10 minutes after that commit and the crawl was never revisited. Safety is now measured (§3). |
| "raising `REUSE_BROWSER_CONCURRENCY` buys concurrency at the cost of one Chromium per slot" | `browser_pool.py` docstring; `config.py:152-154` | False. `run()` dispatches to slot 0 only. 3 built, 1 used (§6). |
| "observed: 11 leaked chromiums, wedged server" | `browser_pool.py` docstring | Most likely teardown lag misread as a leak. Chromium's children outlive the browser root for up to ~70s. 0 orphans at every sample (§4.2). |
| "the reason the flag defaults to off" is that reuse is unproven | `test_browser_pool.py` docstring | It defaults off because the **payoff** is unproven. Safety is settled (§3). |
| Reuse could not be measured because the environment lacked a browser | this file's §1 | Correct at the time. The gap was not that the work was hard, it was that nothing revisited it. |

Working directory: `C:\Users\asus\Downloads\datagoblin`. Backend suite at
`2b8f672` + this work: **1620 passed, 6 skipped**.
