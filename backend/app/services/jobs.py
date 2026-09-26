"""Phase-5 job model: Firecrawl-shaped status over runs + datasets + ledger.

GET /api/jobs/{id} returns {id, status, completed, total, creditsUsed,
expiresAt, next, data[]} where data[] is the records slice (skip/limit).
Derivation is adapter-agnostic and honest about partial state:
- dataset finalized -> COMPLETED (counts + records from the dataset).
- live memory entry (RUNS) -> its status, counters, dataset when present.
- stored run row only -> RUNNING (local-dev rows never update status;
  Supabase rows do), completed 0.
- nothing anywhere -> None (caller 404s).
creditsUsed always sums the persisted ledger (0 when the adapter has none).
expiresAt is "" (no TTL eviction in either adapter — documented, not hidden).
"""
from typing import Any


def _credits(repo: Any, run_id: str) -> int:
    try:
        entries = repo.ledger(run_id) or []
    except Exception:
        return 0
    total = 0
    for e in entries:
        try:
            total += int(e.get("credits", 0))
        except (TypeError, ValueError):
            continue
    return total


def get_job_status(repo: Any, run_id: str, skip: int = 0, limit: int = 100,
                   memory: dict | None = None) -> dict | None:
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or 100), 500))
    mem = memory or {}
    dataset_id = mem.get("dataset_id")
    ds = None
    if dataset_id:
        try:
            ds = repo.get_dataset(dataset_id)
        except Exception:
            ds = None
    if ds:
        records = ds.get("records", []) or []
        total = ds.get("record_count", len(records))
        try:
            page = repo.get_records(dataset_id, "", limit, skip)
            data = (page or {}).get("records", records[skip:skip + limit])
        except Exception:
            data = records[skip:skip + limit]
        has_more = skip + limit < len(records)
        return {"id": run_id, "status": "COMPLETED", "completed": len(records),
                "total": total, "creditsUsed": _credits(repo, run_id),
                "expiresAt": "",
                "next": (f"/api/jobs/{run_id}?skip={skip + limit}&limit={limit}"
                         if has_more else ""),
                "data": data}
    if mem:
        counters = mem.get("counters", {}) or {}
        return {"id": run_id, "status": mem.get("status", "RUNNING"),
                "completed": 0, "total": int(counters.get("attempted", 0)),
                "creditsUsed": _credits(repo, run_id), "expiresAt": "",
                "next": "", "data": []}
    try:
        stored = repo.get_run(run_id)
    except Exception:
        stored = None
    if stored:
        return {"id": run_id, "status": stored.get("status", "RUNNING"),
                "completed": 0, "total": 0, "creditsUsed": _credits(repo, run_id),
                "expiresAt": "", "next": "", "data": []}
    return None
