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

    def find_page_by_url(self, url: str, include_html: bool = True) -> dict | None:
        """The stored page for `url` from any run, newest first. See PostgresRepo."""
        found = None
        for p in self._scan("pages"):
            if p.get("url") != url or p.get("status", "ok") != "ok":
                continue
            if found is None or str(p.get("retrieved_at") or "") >= str(found.get("retrieved_at") or ""):
                found = p
        if found and not include_html:
            found = {k: v for k, v in found.items() if k != "raw_html"}
        return found

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
                                "snapshot_chars": int(page.get("snapshot_chars", 0) or 0),
                                # `retrieved_at` was dropped here while PostgresRepo
                                # kept it, so the two did not actually mirror each
                                # other: `get_pages` documents itself as "newest
                                # first" and had no timestamp to order by, and the
                                # source browser's "last used" column was
                                # permanently null under local persistence.
                                "retrieved_at": page.get("retrieved_at") or ""})
        return pid

    def get_pages(self, run_id: str, limit: int = 200) -> list[dict]:
        rows = [p for p in self._scan("pages") if p.get("run_id") == run_id]
        return rows[-max(1, min(int(limit), 500)):]

    def list_sources(self, limit: int = 500) -> list[dict]:
        """Every stored page grouped by host. See PostgresRepo.list_sources."""
        agg: dict = {}
        for p in self._scan("pages"):
            url = str(p.get("url") or "")
            host = ""
            if "://" in url:
                host = url.split("://", 1)[1].split("/", 1)[0]
            if host.startswith("www."):
                host = host[4:]
            row = agg.setdefault(
                host,
                {"host": host, "pages": 0, "chars": 0, "runs": set(), "last_used": None},
            )
            row["pages"] += 1
            row["chars"] += int(p.get("snapshot_chars") or 0)
            row["runs"].add(str(p.get("run_id") or ""))
            got = p.get("retrieved_at")
            if got and (row["last_used"] is None or str(got) > str(row["last_used"])):
                row["last_used"] = got

        out = []
        for r in agg.values():
            out.append(
                {
                    "host": r["host"],
                    "pages": r["pages"],
                    "chars": r["chars"],
                    "runs": len(r["runs"]),
                    "last_used": r["last_used"],
                }
            )
        out.sort(key=lambda r: (-r["pages"], -r["chars"]))
        return out[: max(1, min(int(limit), 2000))]

    def get_pages_by_ids(self, page_ids: list[str]) -> list[dict]:
        """Full page rows for specific ids, in one pass. See PostgresRepo."""
        wanted = {str(p) for p in (page_ids or []) if p}
        if not wanted:
            return []
        return [p for p in self._scan("pages") if str(p.get("id")) in wanted]

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
        """Newest datasets. Mirrors PostgresRepo: `run_id` and `schema` included.

        `schema` is what lets the dashboard's coverage matrix report a field
        that no record ever carried. Without it, `field_coverage` can only see
        field names that appear in the records themselves, and a never-extracted
        field is simply absent rather than reported as a gap.
        """
        out = []
        for d in self._scan("datasets")[-max(1, limit):]:
           out.append({"id": d["id"], "run_id": d.get("run_id", ""),
                       "name": d.get("name", ""),
                       "schema": d.get("schema") or [],
                       "record_count": len(d.get("records", [])),
                       "created_at": d.get("_ts", "")})
        return out

    def get_dataset(self, dataset_id: str) -> dict | None:
        # Last write wins, matching every other read here: a corrected record is
        # appended as a new line, so returning the first match would serve the
        # stale copy forever.
        found = None
        for d in self._scan("datasets"):
            if d.get("id") == dataset_id:
                found = {"id": d["id"], "run_id": d.get("run_id", ""), "name": d.get("name", ""),
                         "schema": d.get("schema", []), "records": d.get("records", []),
                         "counts": d.get("counts", {}), "created_at": d.get("_ts", "")}
        return found

    def run_readonly_sql(self, sql: str, params: tuple = ()) -> list[dict]:
        """Always refuses.

        The dataset question endpoint asks a model to write SQL, and this
        adapter stores rows as JSONL lines. There is no SQL to run, and
        hand-rolling a query engine over JSON to satisfy a signature would be
        a much worse failure than saying no: the caller would get plausible
        rows computed by something other than the database.

        The method exists so both adapters answer the same question, and it
        answers "not here" out loud. Set PERSISTENCE=postgres to use it.
        """
        from app.core.errors import AppError
        raise AppError("code=E_UNSUPPORTED",
                       message="Natural-language SQL queries need Postgres. "
                               "This instance runs on the local JSONL adapter "
                               "(PERSISTENCE=local), which has no query engine.")

    def get_dataset_row(self, dataset_id: str) -> dict | None:
        """The dataset's own columns, with no records attached. See PostgresRepo."""
        found = None
        for d in self._scan("datasets"):
            if d.get("id") == dataset_id:
                found = {"id": d["id"], "run_id": d.get("run_id", ""),
                         "name": d.get("name", ""), "schema": d.get("schema", []),
                         "record_count": len(d.get("records", [])),
                         "created_at": d.get("_ts", "")}
        return found

    def get_dataset_schema(self, dataset_id: str) -> list | None:
        """Just the declared schema — no record copy. See PostgresRepo."""
        for d in self._scan("datasets"):
            if d.get("id") == dataset_id:
                return d.get("schema", []) or []
        return None

    def coverage_aggregates(self, dataset_ids: list[str]) -> dict:
        """Same contract as `PostgresRepo.coverage_aggregates`, computed in Python.

        This adapter has no database to aggregate in, so it reads the records and
        counts them locally. That is slow for the same reason the Postgres version
        is fast — the records have to be in the process — but the *numbers* are
        identical, which is the property the two adapters have to share: the
        dashboard must not show different figures depending on which one is live.

        Delegated to `coverage.field_coverage` rather than reimplemented, so there
        is exactly one definition of what `unverified` and `not_proven` add up to
        and no chance of the two adapters drifting apart on the same data.
        """
        from app.services import coverage as coverage_svc

        ids = [str(d) for d in (dataset_ids or []) if d]
        out: dict = {}
        for did in ids:
            row = self.get_dataset_row(did)
            schema = (row or {}).get("schema") or []
            recs = list((self.get_records(did, "", 5000, 0) or {}).get("records") or [])
            matrix = coverage_svc.field_coverage(recs, schema)
            out[did] = {"records": int(matrix.get("records") or 0), "fields": {}}
            for f in matrix.get("fields") or []:
                out[did]["fields"][f["field"]] = {
                    "present": int(f.get("present") or 0),
                    "missing": int(f.get("missing") or 0),
                    "records": int(f.get("records") or 0),
                    "verified": int(f.get("verified") or 0),
                    "unverified": int(f.get("unverified") or 0),
                    "conflicting": int(f.get("conflicting") or 0),
                }
        return out

    def get_records(self, dataset_id: str, q: str = "", limit: int = 100, offset: int = 0) -> dict:
        ds = self.get_dataset(dataset_id)
        if ds is None:
            return {}
        rows = ds.get("records", [])
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in json.dumps(r, default=str).lower()]
        window = rows[offset:offset + max(1, min(limit, 500))]
        return {"dataset_id": dataset_id, "total": len(rows),
                "records": [{"record_id": f"local:{offset + i}",
                             "fields": r.get("fields", r)}
                            for i, r in enumerate(window)]}

    def update_record_cell(self, dataset_id: str, record_id: str,
                           field: str, cell: dict) -> bool:
        """Write one field of one record by appending a corrected dataset line.

        Append-only like every other write here, and `get_dataset` returns the
        last line for an id, so the corrected copy supersedes the original
        without mutating a line that other readers may still be scanning.
        """
        ds = self.get_dataset(dataset_id)
        if ds is None:
            return False
        try:
            idx = int(str(record_id).split(":", 1)[1])
        except (ValueError, IndexError):
            return False
        records = ds.get("records", [])
        if not 0 <= idx < len(records):
            return False
        row = dict(records[idx])
        fields = dict(row.get("fields", {}))
        fields[field] = cell
        row["fields"] = fields
        records[idx] = row
        self._append("datasets", {"id": dataset_id, "run_id": ds.get("run_id", ""),
                                  "name": ds.get("name", ""), "schema": ds.get("schema", []),
                                  "records": records, "counts": ds.get("counts", {})})
        return True

    def update_dataset_schema(self, dataset_id: str, schema: list) -> bool:
        """Replace the declared schema. See PostgresRepo for the whole-array write.

        Records are carried across untouched: declaring a column must not disturb
        the rows that are already there, and the new column starts empty on every
        one of them, which is what backfill then fills.
        """
        ds = self.get_dataset(dataset_id)
        if ds is None:
            return False
        self._append("datasets", {"id": dataset_id, "run_id": ds.get("run_id", ""),
                                  "name": ds.get("name", ""), "schema": list(schema or []),
                                  "records": ds.get("records", []),
                                  "counts": ds.get("counts", {})})
        return True

    # -- gaps -----------------------------------------------------------------
    # Append-only JSONL has no upsert, so the (dataset_id, field) unique index
    # that 007_dataset_gaps.sql declares in Postgres is reproduced here by
    # scanning for the last row per key. Same observable behaviour: repeated
    # writes converge on one row whose `attempts` is the real count, rather than
    # accumulating a line per press of the button.

    def upsert_gap(self, dataset_id: str, field: str, gap: dict) -> dict:
        row = {
            "id": str(gap.get("id") or uuid.uuid4()),
            "dataset_id": dataset_id,
            "field": field,
            "category": str(gap.get("category") or "depth_gap"),
            "missing": int(gap.get("missing") or 0),
            "unverified": int(gap.get("unverified") or 0),
            "conflicting": int(gap.get("conflicting") or 0),
            "state": str(gap.get("state") or "open"),
            "phase": int(gap.get("phase") or 0),
            "attempts": int(gap.get("attempts") or 0),
            "reason": str(gap.get("reason") or ""),
            "stats": dict(gap.get("stats") or {}),
            "last_error": str(gap.get("last_error") or ""),
            "resolved_at": gap.get("resolved_at") or "",
        }
        self._append("dataset_gaps", row)
        return row

    def list_gaps(self, dataset_id: str, *, state: str | None = None) -> list[dict]:
        latest: dict[str, dict] = {}
        for r in self._scan("dataset_gaps"):
            if r.get("dataset_id") != dataset_id:
                continue
            latest[str(r.get("field") or "")] = r
        rows = list(latest.values())
        if state:
            rows = [r for r in rows if r.get("state") == state]
        rows.sort(key=lambda r: (-(int(r.get("missing") or 0)
                                  + int(r.get("unverified") or 0)
                                  + int(r.get("conflicting") or 0)),
                                str(r.get("field") or "")))
        return rows

    def get_gap(self, dataset_id: str, field: str) -> dict | None:
        for r in reversed(self._scan("dataset_gaps")):
            if r.get("dataset_id") == dataset_id and r.get("field") == field:
                return r
        return None

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
