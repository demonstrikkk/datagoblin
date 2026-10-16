"""One live run through the real HTTP API, measuring what came out.

Answers the question a route sweep cannot: is extraction producing *better* data
now, or just more of it? Prints per-field verification, which route produced
each value, and a sample record with its quote so the values can be judged
rather than counted.

    python scripts/live_scrape_check.py ["prompt"]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
PROMPT = (sys.argv[1] if len(sys.argv) > 1 else
          "Find 5 AI startups in London with their founders and latest funding round")


def call(method: str, path: str, body: dict | None = None, timeout: int = 900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:300]}


def main() -> int:
    status, compiled = call("POST", "/api/workflows/compile", {"prompt": PROMPT}, 300)
    if status != 200:
        print(f"compile failed: {status} {json.dumps(compiled)[:300]}")
        return 2
    plan = compiled["data"]["plan"]
    print(f"plan compiled ({compiled['data'].get('provider')})")
    print(f"  entity  : {plan.get('entity')}")
    print(f"  fields  : {[f['name'] for f in plan['fields']]}")
    print(f"  queries : {plan.get('search_queries')}")

    name = f"LIVE CHECK — {PROMPT[:60]}"
    status, started = call("POST", "/api/runs", {
        "plan_id": compiled["data"]["plan_id"],
        "dataset_name": name,
        "reuse_stored_pages": False,
    }, 120)
    if status != 200:
        print(f"run rejected: {status} {json.dumps(started)[:300]}")
        return 2
    run_id = started["data"]["run"]["run_id"] if "run" in started.get("data", {}) \
        else started["data"].get("run_id", "")
    print(f"  run     : {run_id}")
    print()
    print("running…", flush=True)

    t0 = time.monotonic()
    st = run_id
    while time.monotonic() - t0 < 900:
        time.sleep(4)
        _, snap = call("GET", f"/api/runs/{run_id}", timeout=60)
        data = snap.get("data") or {}
        st = data.get("status", "?")
        cur = data.get("current_stage", "?")
        if st in ("COMPLETED", "FAILED", "PARTIAL", "CANCELLED"):
            break
    print(f"finished in {time.monotonic() - t0:.0f}s: {st}")

    _, snap = call("GET", f"/api/runs/{run_id}", timeout=60)
    data = snap.get("data") or {}
    counters = data.get("counters") or {}
    print()
    print("counters:")
    for k in sorted(counters):
        print(f"  {k:<28} {counters[k]}")

    dataset_id = data.get("dataset_id")
    if not dataset_id:
        print(f"\nno dataset stored. reason: {data.get('no_yield_reason') or 'none given'}")
        return 1

    _, rec = call("GET", f"/api/datasets/{dataset_id}/records?limit=200", timeout=120)
    rows = (rec.get("data") or {}).get("records") or []
    _, prof = call("GET", f"/api/datasets/{dataset_id}/profile", timeout=180)

    print()
    print(f"records stored: {len(rows)}")
    print()
    print(f"{'field':<26} {'filled':>7} {'verified':>9}  routes")
    for f in (prof.get("data") or {}).get("fields", []):
        name_ = f.get("field")
        cells = [r.get("fields", {}).get(name_) for r in rows]
        filled = [c for c in cells if isinstance(c, dict) and c.get("value") not in (None, "")]
        verified = [c for c in filled if c.get("verification_status") == "verified"]
        routes: dict[str, int] = {}
        for c in filled:
            m = c.get("read_method") or "?"
            routes[m] = routes.get(m, 0) + 1
        route_txt = ", ".join(f"{k}×{v}" for k, v in sorted(routes.items(), key=lambda kv: -kv[1]))
        print(f"{name_:<26} {len(filled):>7} {len(verified):>9}  {route_txt}")

    if rows:
        print()
        print("first record, with proof:")
        for name_, cell in (rows[0].get("fields") or {}).items():
            if not isinstance(cell, dict):
                continue
            val = str(cell.get("value"))[:38]
            quote = str((cell.get("source") or {}).get("quote") or "")[:66]
            print(f"  {name_:<24} {val:<40} [{cell.get('verification_status')}]")
            if quote:
                print(f"  {'':<24} ↳ {quote}")

    out = Path(__file__).resolve().parents[2] / "outputs" / "live_scrape_last.json"
    out.write_text(json.dumps({"plan": plan, "status": st, "counters": counters,
                               "records": rows}, default=str, indent=1), encoding="utf-8")
    print()
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())