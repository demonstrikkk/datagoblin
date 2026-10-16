"""Live endpoint sweep against a running API.

One pass, every route, so a 500 anywhere is visible at once instead of surfacing
one per session. Records the status of each and prints anything that is not 2xx.

    python scripts/sweep_endpoints.py [base] [--deep]

`--deep` also exercises the mutating routes (refresh, rejudge, gaps/fill,
export, backfill, query, agent) against a real dataset. Those are read-mostly
by default: rejudge and gaps/fill run as dry runs (apply=false) unless `--apply`
is passed, so a sweep cannot spend a budget or rewrite a stored value.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "http://127.0.0.1:8000"
DEEP = "--deep" in sys.argv
APPLY = "--apply" in sys.argv

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw[:200]
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:200]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200]


def describe(payload: object) -> str:
    """One line about what came back, so a 200 that returned nothing is visible."""
    if isinstance(payload, dict):
        data = payload.get("data")
        err = payload.get("error")
        if err:
            code = err.get("code") if isinstance(err, dict) else err
            msg = err.get("message") if isinstance(err, dict) else ""
            return f"error={code} {msg}"[:110]
        if isinstance(data, list):
            return f"list[{len(data)}]"
        if isinstance(data, dict):
            keys = list(data)[:6]
            return "{" + ", ".join(keys) + "}"
        if data is None:
            return "data=null"
        return str(data)[:80]
    return str(payload)[:110]


def main() -> int:
    rows: list[tuple[str, str, int, str]] = []

    # dataset id, needed by every per-dataset route
    _, ds = call("GET", "/api/datasets")
    dataset_id = ""
    if isinstance(ds, dict) and isinstance(ds.get("data"), list) and ds["data"]:
        dataset_id = ds["data"][0].get("id", "")
    did = dataset_id or "MISSING"

    gets = [
        ("health", "/api/health"),
        ("datasets", "/api/datasets"),
        ("dashboard", "/api/dashboard?limit=5"),
        ("history", "/api/history"),
        ("runs", "/api/history?limit=5"),
        ("sources", "/api/sources?limit=5"),
        ("models/free", "/api/models/free"),
        ("selectors", "/api/selectors"),
        ("reputation/sources", "/api/reputation/sources"),
        ("map: probe", "/api/map"),
    ]
    dataset_gets = [
        ("dataset", "/api/datasets/{did}"),
        ("records", "/api/datasets/{did}/records?limit=5"),
        ("profile", "/api/datasets/{did}/profile"),
        ("conflicts", "/api/datasets/{did}/conflicts"),
        ("gaps", "/api/datasets/{did}/gaps"),
        ("backfill (GET)", "/api/datasets/{did}/backfill"),
        ("refresh proposal", "/api/datasets/{did}/refresh?limit=3"),
        ("staleness", "/api/datasets/{did}/staleness"),
    ]

    for label, path in gets:
        status, payload = call("GET", path)
        rows.append(("GET", label, status, describe(payload)))
    for label, path in dataset_gets:
        status, payload = call("GET", path.format(did=did))
        rows.append(("GET", label, status, describe(payload)))

    if DEEP and dataset_id:
        posts = [
            # The two that were raising NameError on every call.
            ("columns (was NameError)", "/api/datasets/{did}/columns",
             {"name": "dg_probe_col", "type": "string", "description": "sweep"}),
            ("query (was NameError)", "/api/datasets/{did}/query",
             {"question": "how many records?"}),
            ("agent (was NameError)", "/api/datasets/{did}/agent",
             {"prompt": "how many records and what fields are weakest?"}),
            # Mutation routes, dry by default.
            ("refresh (dry)", "/api/datasets/{did}/refresh", {"apply": False}),
            ("rejudge (dry)", "/api/datasets/{did}/rejudge",
             {"apply": APPLY, "judge_budget": 0}),
            ("gaps/fill (dry)", "/api/datasets/{did}/gaps/fill",
             {"apply": APPLY, "phase": 1}),
            ("backfill (dry)", "/api/datasets/{did}/backfill", {"apply": APPLY}),
            ("export csv", "/api/datasets/{did}/export", {"format": "csv"}),
            ("export json", "/api/datasets/{did}/export", {"format": "json"}),
            ("selectors/propose", "/api/selectors/propose",
             {"url": "https://example.com", "fields": [{"name": "x", "type": "string"}]}),
            ("intel/ask", "/api/intel/ask", {"prompt": "what is 2+2?"}),
        ]
        for label, path, body in posts:
            status, payload = call("POST", path.format(did=did), body)
            rows.append(("POST", label, status, describe(payload)))

    width = max(len(r[1]) for r in rows)
    bad = 0
    for method, label, status, note in rows:
        if 200 <= status < 300:
            colour = GREEN
        elif status in (400, 401, 403, 404, 405, 409, 422):
            colour = YELLOW   # a refusal with a reason, not a crash
        else:
            colour = RED
            bad += 1
        print(f"{colour}{status:>3}{RESET} {method:<4} {label:<{width}}  {DIM}{note}{RESET}")

    print()
    print(f"{len(rows)} route(s) exercised against {BASE}")
    if bad:
        print(f"{RED}{bad} returned 5xx or failed to connect{RESET}")
    else:
        print(f"{GREEN}no 5xx anywhere{RESET}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())