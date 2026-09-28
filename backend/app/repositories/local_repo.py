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

    def close(self) -> None:
        """Release resources.

        The JSONL store holds no sockets or handles, so this is a no-op. It
        exists because both adapters must expose the same surface: shutdown
        calls `close()` on whichever one the process is using, and the parity
        test refuses a method one adapter has and the other does not.
        """

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

    def save_plan(self, plan_id: str, plan: dict) -> None:
        self._append("plans", {"id": plan_id, "plan": plan})

    def get_plan(self, plan_id: str) -> dict | None:
        rows = [r for r in self._scan("plans") if r.get("id") == plan_id]
        return (rows[-1].get("plan") if rows else None) or None

    def update_run(self, run_id: str, *, status: str | None = None,
                   stage: str | None = None, progress: int | None = None,
                   partial: bool | None = None, error: str | None = None,
                   stats: dict | None = None, terminal: bool = False) -> None:
        """Advance run state.

        Append-only JSONL has no UPDATE, so this writes a new line and reads
        the last one per id. That is also how status finally becomes truthful
        here: this adapter previously had no status write at all, so a
        completed run reported DISCOVERING forever.
        """
        prev = self.get_run(run_id) or {}
        row = {"id": run_id,
               "workflow_id": prev.get("workflow_id", ""),
               "status": status if status is not None else prev.get("status", "DISCOVERING"),
               "current_stage": stage if stage is not None else prev.get("current_stage", ""),
               "progress": progress if progress is not None else prev.get("progress", 0),
               "partial": partial if partial is not None else prev.get("partial", False),
               "error": error if error is not None else prev.get("error", ""),
               "stats": stats if stats is not None else prev.get("stats", {}),
               "plan": prev.get("plan", {})}
        if terminal:
            row["completed_at"] = (datetime.datetime.utcnow().isoformat() + "Z")
        self._append("runs", row)

    def upsert_source(self, run_id: str, source: dict) -> None:
        self._append("sources", {"run_id": run_id, **source})

    def upsert_page(self, run_id: str, page: dict) -> str:
        """Store the evidence. Returns the page id records cite.

        Mirrors PostgresRepo: markdown is the reduced text the LLM saw, so
        quotes and offsets resolve against it; raw_html is the snapshot.
        """
        prev = [p for p in self._scan("pages")
                if p.get("run_id") == run_id and p.get("url") == page.get("url", "")]
        pid = (prev[-1]["id"] if prev else None) or page.get("page_id") or str(uuid.uuid4())
        self._append("pages", {"id": pid, "run_id": run_id,
                               "url": page.get("url", ""),
                               "final_url": page.get("final_url", ""),
                               "parent_url": page.get("parent_url", ""),
                               "depth": int(page.get("depth", 0) or 0),
                               "method": page.get("method", ""),
                               "status": page.get("status", "ok"),
                               "error": page.get("error", ""),
                               "content_hash": page.get("content_hash", ""),
                               "markdown": page.get("markdown", ""),
                               "raw_html": page.get("raw_html", ""),
                               "snapshot_chars": int(page.get("snapshot_chars", 0) or 0)})
        return pid

    def get_pages(self, run_id: str, limit: int = 200) -> list[dict]:
        rows = [p for p in self._scan("pages") if p.get("run_id") == run_id]
        return rows[-max(1, min(int(limit), 500)):]

    def get_page(self, page_id: str) -> dict | None:
        rows = [p for p in self._scan("pages") if p.get("id") == page_id]
        return rows[-1] if rows else None

    def append_event(self, run_id: str, event: dict) -> None:
        self._append("events", {"run_id": run_id, **event})

    def finalize_dataset(self, run_id: str, plan: dict, rows: list[dict], counts: dict) -> str:
        did = str(uuid.uuid4())
        self._append("datasets", {"id": did, "run_id": run_id, "name": plan.get("goal", ""),
                                  "schema": plan.get("fields", []), "records": rows,
                                  "counts": counts})
        # Without this the run stayed DISCOVERING forever in local mode: reads
        # take the last line per id, and nothing ever wrote a terminal one.
        # A budget-limited run is FAILED + partial, never COMPLETED.
        partial = bool(counts.get("partial"))
        self.update_run(run_id, status="FAILED" if partial else "COMPLETED",
                        stage="FAILED" if partial else "COMPLETED", progress=100,
                        partial=partial,
                        error=str(counts.get("partial_reason", ""))[:500],
                        stats={"counts": counts, "records": len(rows)}, terminal=True)
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
