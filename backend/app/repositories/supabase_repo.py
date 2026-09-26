"""Supabase repository — real persistence. Idempotent upserts; batched finalize.

Batching note (honest): finalize issues one request per table (datasets,
dataset_records bulk, runs update) — not a single DB transaction. True
all-or-nothing across tables would require a Postgres function (rpc); a run
that fails mid-finalize surfaces E_DEPENDENCY and the run stays resumable
rather than pretending atomicity it doesn't have.
"""
import json

from app.core.errors import dependency


class SupabaseRepo:
    """Wraps supabase-py. All failures -> E_DEPENDENCY (callers pause, never lose state)."""

    def __init__(self, url: str, key: str):
        from supabase import create_client
        self._db = create_client(url, key)

    def _wrap(self, op: str, fn: object) -> object:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 (mapped below)
            raise dependency(f"Supabase {op} failed: {str(e)[:200]}")

    def create_run(self, run_id: str, workflow_id: str, plan: dict) -> None:
        import datetime
        now = datetime.datetime.utcnow().isoformat() + "Z"
        self._wrap("create_run", lambda: self._db.table("workflows").upsert(
            {"id": workflow_id, "prompt": plan.get("goal", ""), "plan_json": plan}).execute())
        self._wrap("create_run", lambda: self._db.table("runs").upsert(
            {"id": run_id, "workflow_id": workflow_id, "status": "DISCOVERING",
             "current_stage": "DISCOVERING", "progress": 5,
             "started_at": now}).execute())

    def upsert_source(self, run_id: str, source: dict) -> None:
        self._wrap("upsert_source", lambda: self._db.table("sources").upsert(
            {"run_id": run_id, "url": source.get("url", ""), "title": source.get("title", ""),
             "content_hash": source.get("content_hash", ""),
             "status": source.get("status", "ok"), "error": source.get("error", "")},
            on_conflict="run_id,url").execute())

    def append_event(self, run_id: str, event: dict) -> None:
        self._wrap("append_event", lambda: self._db.table("run_events").insert(
            {"run_id": run_id, "stage": event.get("stage", ""),
             "message": event.get("message", ""), "metadata_json": event.get("data", {})}).execute())

    def finalize_dataset(self, run_id: str, plan: dict, rows: list[dict], counts: dict) -> str:
        import datetime
        import uuid
        did = str(uuid.uuid4())

        def _tx() -> str:
            self._db.table("datasets").insert(
                {"id": did, "run_id": run_id, "name": str(plan.get("goal", ""))[:120],
                 "schema_json": plan.get("fields", []), "record_count": len(rows)}).execute()
            if rows:
                self._db.table("dataset_records").insert(
                    [{"dataset_id": did, "row_json": r.get("fields", {})}
                     for r in rows]).execute()
            self._builders_update(run_id, did, counts)
            return did
        return self._wrap("finalize_dataset", _tx)
        return self._wrap("finalize_dataset", _tx)

    # -- reads (history / studio; paginated, bounded) -------------------------
    def get_run(self, run_id: str) -> dict | None:
        rows = self._wrap("get_run", lambda: self._db.table("runs").select("*").eq(
            "id", run_id).limit(1).execute()).data or []
        return rows[0] if rows else None

    def run_history(self, limit: int = 50) -> list[dict]:
        return (self._wrap("history", lambda: self._db.table("runs").select("*").order(
            "started_at", desc=True).limit(max(1, min(limit, 100))).execute()).data or [])

    def list_datasets(self, limit: int = 50) -> list[dict]:
        return (self._wrap("list_datasets", lambda: self._db.table("datasets").select(
            "id,run_id,name,record_count,created_at").order(
            "created_at", desc=True).limit(max(1, min(limit, 100))).execute()).data or [])

    def get_dataset(self, dataset_id: str) -> dict | None:
        rows = self._wrap("get_dataset", lambda: self._db.table("datasets").select(
            "*").eq("id", dataset_id).limit(1).execute()).data or []
        if not rows:
            return None
        ds = rows[0]
        recs = self._wrap("get_records", lambda: self._db.table("dataset_records").select(
            "row_json").eq("dataset_id", dataset_id).limit(5000).execute()).data or []
        ds["records"] = [{"fields": r.get("row_json", {})} for r in recs]
        return ds

    def get_records(self, dataset_id: str, q: str = "", limit: int = 100, offset: int = 0) -> dict:
        ds = self.get_dataset(dataset_id)
        if ds is None:
            return {}
        rows = ds.get("records", [])
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in json.dumps(r, default=str).lower()]
        total = len(rows)
        return {"dataset_id": dataset_id, "total": total,
                "records": rows[offset:offset + max(1, min(limit, 500))]}

    def get_sources(self, dataset_id: str) -> dict:
        ds = self._wrap("get_sources", lambda: self._db.table("datasets").select(
            "run_id,sources_attempted,sources_successful,sources_failed").eq(
            "id", dataset_id).limit(1).execute()).data or []
        if not ds:
            return {}
        run_id = ds[0].get("run_id", "")
        srcs = self._wrap("get_sources", lambda: self._db.table("sources").select(
            "url,title,status").eq("run_id", run_id).limit(500).execute()).data or []
        return {"dataset_id": dataset_id, **ds[0], "sources": srcs}

    def _builders_update(self, run_id: str, did: str, counts: dict) -> None:
        import datetime
        now = datetime.datetime.utcnow().isoformat() + "Z"
        self._db.table("datasets").update(
            {"sources_attempted": counts.get("attempted", 0),
             "sources_successful": counts.get("successful", 0),
             "sources_failed": counts.get("failed", 0)}).eq("id", did).execute()
        self._db.table("runs").update(
            {"status": "COMPLETED", "current_stage": "COMPLETED", "progress": 100,
             "completed_at": now}).eq("id", run_id).execute()

    def save_export(self, dataset_id: str, fmt: str, byte_size: int) -> str:
        import uuid
        eid = str(uuid.uuid4())
        self._wrap("save_export", lambda: self._db.table("exports").insert(
            {"id": eid, "dataset_id": dataset_id, "format": fmt,
             "byte_size": byte_size}).execute())
        return eid

    # -- Phase-5 metering + fingerprints ---------------------------------------
    # Required tables (migration before Supabase use):
    #   usage_ledger(run_id text, charge_id text UNIQUE, stage text,
    #                units int, credits int, at timestamptz)
    #   seen_fingerprints(fp text, run_id text, url text, PRIMARY KEY (fp))
    def record_charge(self, run_id: str, entry: dict) -> None:
        self._wrap("record_charge", lambda: self._db.table("usage_ledger").upsert(
            {"run_id": run_id, "charge_id": entry.get("charge_id", ""),
             "stage": entry.get("stage", ""), "units": entry.get("units", 0),
             "credits": entry.get("credits", 0),
             "created_at": entry.get("at", "") or None},
            on_conflict="charge_id").execute())

    def ledger(self, run_id: str) -> list[dict]:
        return (self._wrap("ledger", lambda: self._db.table("usage_ledger").select(
            "*").eq("run_id", run_id).order("created_at").execute()).data or [])

    def note_fingerprint(self, run_id: str, fp: str, url: str) -> None:
        if not fp:
            return
        self._wrap("note_fingerprint", lambda: self._db.table(
            "seen_fingerprints").upsert(
            {"fp": fp, "run_id": run_id, "url": url},
            on_conflict="fp").execute())

    def seen_fingerprint(self, fp: str) -> bool:
        res = self._wrap("seen_fingerprint", lambda: self._db.table(
            "seen_fingerprints").select("fp").eq("fp", fp).limit(1).execute())
        return bool(getattr(res, "data", []))
