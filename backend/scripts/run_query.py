"""Run one full backend pass: compile -> run -> poll -> dump dataset JSON.

Usage: python backend/scripts/run_query.py "<prompt>" [max_pages]
Durable runner (kept): drives the real HTTP API, prints progress, writes
outputs/run_<run_id>.json with plan + records + sources + counts.
"""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

API = "http://127.0.0.1:8000"


def main() -> None:
    prompt = sys.argv[1]
    c = httpx.Client(timeout=60)
    plan = c.post(f"{API}/api/workflows/compile", json={"prompt": prompt}).json()["data"]
    print(f"plan_id={plan['plan_id']} provider={plan['provider']}", flush=True)
    print(f"entity={plan['plan']['entity']} queries={plan['plan']['search_queries']}",
          flush=True)
    run = c.post(f"{API}/api/runs",
                 json={"plan_id": plan["plan_id"]}).json()["data"]
    run_id = run["run_id"]
    print(f"run_id={run_id}", flush=True)
    status = {}
    for _ in range(58):
        time.sleep(10)
        status = c.get(f"{API}/api/runs/{run_id}").json()["data"]
        print(f"  {status['status']} stage={status['current_stage']} "
              f"progress={status['progress']} counters={status['counters']}", flush=True)
        if status["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
    out = {"prompt": prompt, "plan": plan["plan"], "run": status,
           "dataset": None, "records": [], "sources": {}, "job": None}
    did = status.get("dataset_id")
    if did:
        out["dataset"] = c.get(f"{API}/api/datasets/{did}").json()["data"]
        out["records"] = out["dataset"].get("records", [])
        out["sources"] = c.get(f"{API}/api/datasets/{did}/sources").json()["data"]
    try:
        out["job"] = c.get(f"{API}/api/jobs/{run_id}").json()["data"]
    except Exception as e:  # noqa: BLE001
        out["job"] = {"error": str(e)[:150]}
    path = Path("outputs") / f"run_{run_id}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"dumped {path} records={len(out['records'])}", flush=True)


if __name__ == "__main__":
    main()
