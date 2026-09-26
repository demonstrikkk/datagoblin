"""LOCAL DEV adapter — file-backed JSON persistence. EXPLICITLY NOT production.

Used only when Supabase keys are absent (local dev / CI without secrets).
Every record carries {"_adapter": "local-dev"} so it can never be mistaken
for production truth. Same method surface as SupabaseRepo.
"""
import datetime
import json
import threading
import uuid
from pathlib import Path


class LocalRepo:
    def __init__(self, root: str = ".datagoblin-local") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        return self._root / f"{name}.jsonl"

    def _append(self, name: str, row: dict) -> None:
        row = {**row, "_adapter": "local-dev",
               "_ts": datetime.datetime.utcnow().isoformat() + "Z"}
        with self._lock:
            with open(self._path(name), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")

    def create_run(self, run_id: str, workflow_id: str, plan: dict) -> None:
        self._append("runs", {"id": run_id, "workflow_id": workflow_id,
                              "status": "DISCOVERING", "plan": plan})

    def upsert_source(self, run_id: str, source: dict) -> None:
        self._append("sources", {"run_id": run_id, **source})

    def append_event(self, run_id: str, event: dict) -> None:
        self._append("events", {"run_id": run_id, **event})

    def finalize_dataset(self, run_id: str, plan: dict, rows: list[dict], counts: dict) -> str:
        did = str(uuid.uuid4())
        self._append("datasets", {"id": did, "run_id": run_id, "name": plan.get("goal", ""),
                                  "schema": plan.get("fields", []), "records": rows,
                                  "counts": counts})
        return did

    # -- reads (scan jsonl; dev-scale only) -----------------------------------
    def _scan(self, name: str) -> list[dict]:
        p = self._path(name)
        if not p.exists():
            return []
        with self._lock:
            return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
                    if line.strip()]

    def get_run(self, run_id: str) -> dict | None:
        rows = [r for r in self._scan("runs") if r.get("id") == run_id]
        return rows[-1] if rows else None

    def run_history(self, limit: int = 50) -> list[dict]:
        return self._scan("runs")[-max(1, limit):]

    def list_datasets(self, limit: int = 50) -> list[dict]:
        out = []
        for d in self._scan("datasets")[-max(1, limit):]:
            out.append({"id": d["id"], "name": d.get("name", ""),
                        "record_count": len(d.get("records", [])), "created_at": d.get("_ts", "")})
        return out

    def get_dataset(self, dataset_id: str) -> dict | None:
        for d in self._scan("datasets"):
            if d.get("id") == dataset_id:
                return {"id": d["id"], "run_id": d.get("run_id", ""), "name": d.get("name", ""),
                        "schema": d.get("schema", []), "records": d.get("records", []),
                        "counts": d.get("counts", {})}
        return None

    def get_records(self, dataset_id: str, q: str = "", limit: int = 100, offset: int = 0) -> dict:
        ds = self.get_dataset(dataset_id)
        if ds is None:
            return {}
        rows = ds.get("records", [])
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in json.dumps(r, default=str).lower()]
        return {"dataset_id": dataset_id, "total": len(rows),
                "records": rows[offset:offset + max(1, min(limit, 500))]}

    def get_sources(self, dataset_id: str) -> dict:
        ds = self.get_dataset(dataset_id)
        if ds is None:
            return {}
        srcs = [s for s in self._scan("sources") if s.get("run_id") == ds.get("run_id")]
        return {"dataset_id": dataset_id, **ds.get("counts", {}), "sources": srcs}

    def save_export(self, dataset_id: str, fmt: str, byte_size: int) -> str:
        eid = str(uuid.uuid4())
        self._append("exports", {"id": eid, "dataset_id": dataset_id, "format": fmt,
                                 "byte_size": byte_size})
        return eid

    # -- Phase-5 metering + fingerprints (append-only JSONL, same pattern) ----
    def record_charge(self, run_id: str, entry: dict) -> None:
        self._append("usage_ledger", {"run_id": run_id, **entry})

    def ledger(self, run_id: str) -> list[dict]:
        return [r for r in self._scan("usage_ledger")
                if r.get("run_id") == run_id or r.get("job_id") == run_id]

    def note_fingerprint(self, run_id: str, fp: str, url: str) -> None:
        if not fp:
            return
        self._append("seen_fingerprints", {"run_id": run_id, "fp": fp, "url": url})

    def seen_fingerprint(self, fp: str) -> bool:
        return any(r.get("fp") == fp for r in self._scan("seen_fingerprints"))
