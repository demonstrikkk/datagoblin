"""Postgres repository — the real persistence substrate, over psycopg 3.

Why psycopg and not the REST adapter: `DATABASE_URL` already carried working
credentials and the migrations are already Postgres DDL, but nothing in
`app/` ever read it, so the app silently wrote JSONL while a fully migrated,
empty database sat unused. psycopg is a first-class driver, so this needs no
new secret and no second schema dialect.

Why this matters for correctness, not just tidiness:

  * `update_run` exists. The previous adapters had no way to advance a run's
    status, so status was written exactly twice — at creation and at finalize.
    A run that died on the runtime budget therefore read `DISCOVERING`
    forever, indistinguishable from a live one.
  * `upsert_page` stores the page. Pages used to be fetched, reduced in RAM,
    extracted from, and discarded, leaving evidence offsets pointing into text
    that no longer existed.
  * Filtering and paging happen in SQL. The REST adapter pulled up to 5000
    rows and substring-matched them in Python, so `total` silently
    under-reported and `offset` was applied client-side.

Every failure is mapped to E_DEPENDENCY so callers pause rather than lose
state. Connections are short-lived per call: a run is I/O-light and a leaked
long-lived pool is a worse failure mode than a connect cost.
"""
from __future__ import annotations

import datetime
import json
import threading
import uuid
from decimal import Decimal
from typing import Any, Iterable

from app.core.config import settings
from app.core.errors import AppError, dependency
from app.core.logging import log


def _jsonb(value: Any) -> Any:
    """jsonb columns take objects; psycopg needs the string form."""
    if value is None:
        return None
    return json.dumps(value, default=str)


def _plain(value: Any) -> Any:
    """Coerce a driver-native value into something JSON can carry.

    psycopg returns real `UUID` and `datetime` objects for `uuid` and
    `timestamptz` columns. The REST adapter this replaced handed back JSON
    strings, so every typed view downstream was written against strings and
    nothing noticed - until a `DatasetView` was handed a `UUID` and raised a
    pydantic `string_type` error, i.e. a 500 on a dataset the user had just
    finished creating.

    Normalising at this choke point means every read path is JSON-safe, not just
    the one that happened to break.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


class PostgresRepo:
    """Same method surface as LocalRepo, backed by Postgres."""

    def __init__(self, dsn: str, *, connect_timeout: int = 10) -> None:
        self._dsn = dsn
        self._connect_timeout = connect_timeout
        self._lock = threading.Lock()
        # Lazy: created on first query, not at import, so a process that never
        # touches the database never opens a socket. See _get_pool.
        self._pool = None

    # -- plumbing -------------------------------------------------------------
    def _get_pool(self):
        """One connection pool per repo.

        Every operation used to open its own connection. A run does ~200+
        separate repository writes, and against a remote server each of those
        pays a full TLS handshake — connection setup, not query execution, was
        the dominant cost of persisting a run. The pool keeps a small number of
        warm connections instead.

        Built lazily under a lock so concurrent first requests build one pool
        rather than several, and so importing the module does not open sockets.
        """
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is None:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
                try:
                    self._pool = ConnectionPool(
                        conninfo=self._dsn,
                        min_size=1,
                        max_size=6,
                        max_lifetime=1800,   # don't hold a remote connection forever
                        open=True,
                        # dict_row belongs to the pool, not to each connect():
                        # the row factory must be set when the pooled
                        # connections are created, or reads come back as tuples.
                        kwargs={"connect_timeout": self._connect_timeout,
                                "row_factory": dict_row,
                                # Required for a transaction-mode pooler.
                                # psycopg auto-prepares a statement once it has
                                # run twice on a connection and then issues
                                # EXECUTE, which needs a session-scoped
                                # prepared statement. PgBouncer in transaction
                                # mode hands the same backend to different
                                # clients, so one client's prepared statement
                                # collides with another's and the query fails
                                # with `DuplicatePreparedStatement: prepared
                                # statement "_pg3_0" already exists`. That
                                # surfaced as intermittent 503s reading pages,
                                # and it is not a race — it recurs on any pool
                                # that serves more than one caller.
                                # prepare_threshold=None turns the cache off.
                                # The cost is a re-parse per execution; the
                                # alternative is an unstable connection.
                                "prepare_threshold": None})
                except Exception as e:  # noqa: BLE001 (mapped to a typed error)
                    raise dependency(f"Postgres pool open failed: {str(e)[:200]}")
        return self._pool

    def _conn(self):
        # Kept as the single connection factory so callers that substitute a
        # fake connection keep working; the pool sits behind it.
        return self._get_pool().connection()

    def _run(self, op: str, fn) -> Any:
        try:
            with self._conn() as conn:
                try:
                    with conn.cursor() as cur:
                        out = fn(cur)
                    conn.commit()
                except BaseException:
                    # A connection returned to the pool still inside a failed
                    # transaction would hand that abort to the next borrower.
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001 (best effort, already failing)
                        pass
                    raise
                return out
        except AppError:
            # Already typed (pool open failure); don't wrap it twice.
            raise
        except Exception as e:  # noqa: BLE001 (mapped to a typed dependency error)
            raise dependency(f"Postgres {op} failed: {str(e)[:200]}")

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    def _rows(self, op: str, sql: str, params: Iterable = ()) -> list[dict]:
        """Every read goes through here, so every read is JSON-safe.

        Normalising once at the choke point is deliberate: doing it per-endpoint
        means the next typed view added anywhere downstream re-breaks on a bare
        `UUID` or `datetime` from the driver.
        """
        def _fn(cur):
            cur.execute(sql, tuple(params))
            if cur.description is None:
                return []
            return [{k: _plain(v) for k, v in dict(r).items()} for r in cur.fetchall()]
        return self._run(op, _fn) or []

    # -- writes ---------------------------------------------------------------
    def create_run(self, run_id: str, workflow_id: str, plan: dict) -> None:
        def _fn(cur):
            # The plan is persisted, not held in a process dict: PLANS was
            # memory-only, so every plan_id became invalid on restart and a
            # recovered run had nothing to resume from.
            cur.execute(
                """INSERT INTO workflows (id, prompt, plan_json) VALUES (%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET prompt=EXCLUDED.prompt,
                                                plan_json=EXCLUDED.plan_json""",
                (workflow_id, str(plan.get("goal", ""))[:4000], _jsonb(plan)))
            cur.execute(
                """INSERT INTO runs (id, workflow_id, status, current_stage, progress, query)
                   VALUES (%s,%s,'DISCOVERING','DISCOVERING',5,%s)
                   ON CONFLICT (id) DO UPDATE SET workflow_id=EXCLUDED.workflow_id,
                                                query=EXCLUDED.query""",
                (run_id, workflow_id, str(plan.get("goal", ""))[:4000]))
        self._run("create_run", _fn)

    def save_plan(self, plan_id: str, plan: dict) -> None:
        """Persist a compiled plan so its id survives a restart.

        Plans used to live only in a process dict, so every plan_id became
        invalid the moment the backend restarted and a recovered run had
        nothing to resume from.
        """
        self._run("save_plan", lambda cur: cur.execute(
            """INSERT INTO workflows (id, prompt, plan_json) VALUES (%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET prompt=EXCLUDED.prompt,
                                                plan_json=EXCLUDED.plan_json""",
            (plan_id, str(plan.get("goal", ""))[:4000], _jsonb(plan))))

    def get_plan(self, plan_id: str) -> dict | None:
        rows = self._rows("get_plan", "SELECT plan_json FROM workflows WHERE id=%s",
                          (plan_id,))
        if not rows:
            return None
        return rows[0].get("plan_json") or None

    def update_run(self, run_id: str, *, status: str | None = None,
                   stage: str | None = None, progress: int | None = None,
                   partial: bool | None = None, error: str | None = None,
                   stats: dict | None = None, terminal: bool = False) -> None:
        """Advance run state. The single reason a run's status is truthful.

        `partial` is a real column, not a phrase inside an error string: "we
        kept some of it" has to be queryable by the API and the UI.
        """
        sets: list[str] = []
        params: list[Any] = []
        for col, val in (("status", status), ("current_stage", stage),
                         ("progress", progress), ("partial", partial),
                         ("error", error)):
            if val is not None:
                sets.append(f"{col}=%s")
                params.append(val)
        if stats is not None:
            sets.append("stats=%s")
            params.append(_jsonb(stats))
        if terminal:
            sets.append("completed_at=now()")
        if not sets:
            return
        params.append(run_id)
        self._run("update_run", lambda cur: cur.execute(
            f"UPDATE runs SET {', '.join(sets)} WHERE id=%s", tuple(params)))

    def upsert_source(self, run_id: str, source: dict) -> None:
        self._run("upsert_source", lambda cur: cur.execute(
            """INSERT INTO sources (run_id,url,title,content_hash,status,error,
                                   reused_from_run_id,reused_page_id,retrieved_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (run_id,url) DO UPDATE SET title=EXCLUDED.title,
                 content_hash=EXCLUDED.content_hash, status=EXCLUDED.status,
                 error=EXCLUDED.error,
                 reused_from_run_id=EXCLUDED.reused_from_run_id,
                 reused_page_id=EXCLUDED.reused_page_id,
                 retrieved_at=EXCLUDED.retrieved_at""",
            (run_id, source.get("url", ""), source.get("title", ""),
             source.get("content_hash", ""), source.get("status", "ok"),
             source.get("error", ""), source.get("reused_from_run_id") or None,
             source.get("reused_page_id") or None, source.get("retrieved_at") or None)))

    def find_page_by_url(self, url: str, include_html: bool = True) -> dict | None:
        """The stored page for `url` from any run, newest first.

        `get_pages` is deliberately run-scoped and omits raw_html, so neither
        could answer "have we already fetched this exact URL?" across runs —
        which is the question reuse asks.

        Newest first: if a page was re-fetched at some point, the freshest copy
        is the one worth reusing, and the unique index is (run_id, url) so the
        same URL can legitimately exist once per run.
        """
        cols = ("id,run_id,url,final_url,parent_url,depth,method,status,error,"
                "content_hash,markdown,snapshot_chars,retrieved_at")
        if include_html:
            cols += ",raw_html"
        rows = self._rows(
            "find_page_by_url",
            f"SELECT {cols} FROM pages WHERE url=%s AND status='ok'"
            f" ORDER BY retrieved_at DESC LIMIT 1", (url,))
        return rows[0] if rows else None

    def upsert_page(self, run_id: str, page: dict) -> str:
        """Store the evidence. Returns the page id that records cite.

        `markdown` is the reduced text the LLM actually saw, so quotes and
        offsets resolve against it. `raw_html` is the snapshot for
        re-checking content_hash. Re-fetching the same URL updates the row
        rather than forking two contradicting snapshots of one page.
        """
        pid = page.get("page_id") or str(uuid.uuid4())
        self._run("upsert_page", lambda cur: cur.execute(
            """INSERT INTO pages (id,run_id,url,final_url,parent_url,depth,method,
                                  status,error,content_hash,markdown,raw_html,
                                  snapshot_chars,retrieved_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
               ON CONFLICT (run_id,url) DO UPDATE SET final_url=EXCLUDED.final_url,
                 parent_url=EXCLUDED.parent_url, depth=EXCLUDED.depth,
                 method=EXCLUDED.method, status=EXCLUDED.status,
                 error=EXCLUDED.error, content_hash=EXCLUDED.content_hash,
                 markdown=EXCLUDED.markdown, raw_html=EXCLUDED.raw_html,
                 snapshot_chars=EXCLUDED.snapshot_chars, retrieved_at=now()""",
            (pid, run_id, page.get("url", ""), page.get("final_url", ""),
             page.get("parent_url", ""), int(page.get("depth", 0) or 0),
             page.get("method", ""), page.get("status", "ok"),
             page.get("error", ""), page.get("content_hash", ""),
             page.get("markdown", ""), page.get("raw_html", ""),
             int(page.get("snapshot_chars", 0) or 0))))
        # A pre-existing row keeps its own id; report the one now in force.
        found = self._rows("upsert_page_id",
                           "SELECT id FROM pages WHERE run_id=%s AND url=%s",
                           (run_id, page.get("url", "")))
        return str(found[0]["id"]) if found else pid

    def append_event(self, run_id: str, event: dict) -> None:
        self._run("append_event", lambda cur: cur.execute(
            """INSERT INTO run_events (run_id,stage,message,metadata_json)
               VALUES (%s,%s,%s,%s)""",
            (run_id, event.get("stage", ""), str(event.get("message", ""))[:2000],
             _jsonb(event.get("data", {}) or {}))))

    def finalize_dataset(self, run_id: str, plan: dict, rows: list[dict],
                         counts: dict) -> str:
        """Dataset + records + terminal run state, in ONE transaction.

        The REST adapter this replaced issued one request per table and could
        leave a dataset written without its run update; here it is atomic, so a
        run is never marked complete without its records.

        A run that hit its budget is stored as FAILED with partial=true, never
        COMPLETED. Marking a truncated run COMPLETED because a dataset happened
        to be written is how a 15-record run came to look finished.
        """
        did = str(uuid.uuid4())
        partial = bool(counts.get("partial"))
        status = "FAILED" if partial else "COMPLETED"
        reason = str(counts.get("partial_reason", ""))[:500]

        def _fn(cur):
            cur.execute(
                """INSERT INTO datasets (id,run_id,name,schema_json,record_count,
                                         sources_attempted,sources_successful,
                                         sources_failed)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (did, run_id, str(plan.get("goal", ""))[:120],
                 _jsonb(plan.get("fields", [])), len(rows),
                 int(counts.get("attempted", 0)), int(counts.get("successful", 0)),
                 int(counts.get("failed", 0))))
            if rows:
                # One COPY for all records, not one request per record.
                with cur.copy(
                    "COPY dataset_records (dataset_id, row_json) FROM STDIN"
                ) as cp:
                    for r in rows:
                        cp.write_row((did, _jsonb(r.get("fields", {}))))
            cur.execute(
                """UPDATE runs SET status=%s, current_stage=%s, progress=100,
                                   partial=%s, error=%s, completed_at=now(), stats=%s
                   WHERE id=%s""",
                (status, status, partial, reason,
                 _jsonb({"counts": counts, "records": len(rows)}), run_id))
        self._run("finalize_dataset", _fn)
        return did

    def save_export(self, dataset_id: str, fmt: str, byte_size: int) -> str:
        eid = str(uuid.uuid4())
        self._run("save_export", lambda cur: cur.execute(
            "INSERT INTO exports (id,dataset_id,format,byte_size) VALUES (%s,%s,%s,%s)",
            (eid, dataset_id, fmt, int(byte_size))))
        return eid

    def record_charge(self, run_id: str, entry: dict) -> None:
        # `created_at`, not `at`: the column in 002_phase5.sql is created_at.
        # The REST adapter's docstring claimed `at`, and trusting a docstring
        # over the schema is how this failed on first contact.
        self._run("record_charge", lambda cur: cur.execute(
            """INSERT INTO usage_ledger (run_id,charge_id,stage,units,credits,created_at)
               VALUES (%s,%s,%s,%s,%s,now())
               ON CONFLICT (charge_id) DO NOTHING""",
            (run_id, entry.get("charge_id", ""), entry.get("stage", ""),
             int(entry.get("units", 0) or 0), int(entry.get("credits", 0) or 0))))

    def ledger(self, run_id: str) -> list[dict]:
        return self._rows("ledger",
                          "SELECT * FROM usage_ledger WHERE run_id=%s ORDER BY created_at",
                          (run_id,))

    def note_fingerprint(self, run_id: str, fp: str, url: str) -> None:
        if not fp:
            return
        self._run("note_fingerprint", lambda cur: cur.execute(
            """INSERT INTO seen_fingerprints (fp,run_id,url) VALUES (%s,%s,%s)
               ON CONFLICT (fp) DO NOTHING""", (fp, run_id, url)))

    def seen_fingerprint(self, fp: str) -> bool:
        return bool(self._rows("seen_fingerprint",
                               "SELECT 1 FROM seen_fingerprints WHERE fp=%s LIMIT 1",
                               (fp,)))

    # -- reads ----------------------------------------------------------------
    def get_run(self, run_id: str) -> dict | None:
        rows = self._rows("get_run", "SELECT * FROM runs WHERE id=%s", (run_id,))
        return rows[0] if rows else None

    def run_history(self, limit: int = 50) -> list[dict]:
        return self._rows("history",
                          "SELECT * FROM runs ORDER BY started_at DESC LIMIT %s",
                          (max(1, min(int(limit), 100)),))

    def list_datasets(self, limit: int = 50) -> list[dict]:
        return self._rows("list_datasets",
                          """SELECT id,run_id,name,record_count,created_at
                             FROM datasets ORDER BY created_at DESC LIMIT %s""",
                          (max(1, min(int(limit), 100)),))

    def get_dataset_row(self, dataset_id: str) -> dict | None:
        """The dataset's own columns, with no records attached.

        `get_dataset` pulls every record, so asking it for a run id or a schema
        cost a full table read. This is the same row without that.
        """
        rows = self._rows("get_dataset_row",
                          "SELECT id,run_id,name,schema_json,record_count,created_at"
                          " FROM datasets WHERE id=%s", (dataset_id,))
        if not rows:
            return None
        rows[0]["schema"] = rows[0].pop("schema_json", None) or []
        return rows[0]

    def get_dataset(self, dataset_id: str) -> dict | None:
        rows = self._rows("get_dataset", "SELECT * FROM datasets WHERE id=%s",
                          (dataset_id,))
        if not rows:
            return None
        ds = rows[0]
        ds["schema"] = ds.pop("schema_json", None) or []
        # Was a literal `100`, so a dataset reporting 112 records exported 100
        # of them with no indication anything was missing. The ceiling is now
        # the configured export ceiling, which is where this list is consumed.
        ds["records"] = self._records(dataset_id, limit=settings.EXPORT_MAX_ROWS,
                                      offset=0)["records"]
        return ds

    def run_readonly_sql(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run an already-validated SELECT and always roll back.

        Two independent guarantees sit under the caller's grammar check:
        the transaction is declared READ ONLY, so the database refuses any
        write even if the statement somehow got through validation, and it is
        rolled back rather than committed. The statement runs with a server-side
        timeout so a pathological query cannot hold a worker.
        """
        def _fn(cur):
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '15s'")
            # No params unless there are some. Passing an empty tuple still
            # switches psycopg into placeholder interpolation, and it treats
            # every `%` in the statement as one — so a perfectly ordinary
            # `ILIKE '%acme%'` failed with "only '%s', '%b', '%t' are allowed
            # as placeholders". The model's SQL carries no parameters, and a
            # literal `%` in a search pattern has to survive as a literal.
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]

        return self._run("run_readonly_sql", _fn) or []

    def get_dataset_schema(self, dataset_id: str) -> list | None:
        """Just the declared schema — no record load.

        The coverage and conflict views need the schema (a field can be declared
        and never extracted, which is the whole point of showing it) but not the
        rows. Reading them via `get_dataset` meant pulling every record twice
        per request, which on a remote database is the entire latency of the
        page: measured at 4-8s, with 74 records, to draw one bar chart.
        """
        rows = self._rows("get_dataset_schema",
                          "SELECT schema_json FROM datasets WHERE id=%s",
                          (dataset_id,))
        return (rows[0].get("schema_json") or []) if rows else None

    def get_records(self, dataset_id: str, q: str = "", limit: int = 100,
                    offset: int = 0) -> dict:
        return self._records(dataset_id, q, limit, offset)

    def _records(self, dataset_id: str, q: str = "", limit: int = 100,
                 offset: int = 0) -> dict:
        """Filter and page in SQL.

        The REST adapter fetched up to 5000 rows and substring-matched them in
        Python, so `total` under-reported past that ceiling and `offset` was
        applied after the fact. Here both are the database's job.
        """
        limit = max(1, min(int(limit), settings.EXPORT_MAX_ROWS))
        offset = max(0, int(offset))
        ql = (q or "").strip().lower()
        where = "dataset_id=%s"
        params: list[Any] = [dataset_id]
        if ql:
            where += " AND row_json::text ILIKE %s"
            params.append(f"%{ql}%")
        total = self._rows("records_total",
                           f"SELECT count(*) AS n FROM dataset_records WHERE {where}",
                           params)
        rows = self._rows(
            "records",
            f"""SELECT id,row_json FROM dataset_records WHERE {where}
                ORDER BY id LIMIT %s OFFSET %s""",
            [*params, limit, offset])
        # `record_id` is the row's own primary key, carried through so a caller
        # can address exactly one cell later. There was no way to name a record
        # before: the only handle was its position in a list that re-sorts
        # whenever the query or the page window changes.
        return {"dataset_id": dataset_id,
                "total": int(total[0]["n"]) if total else 0,
                "records": [{"record_id": str(r["id"]),
                             "fields": r.get("row_json") or {}} for r in rows]}

    def update_record_cell(self, dataset_id: str, record_id: str,
                           field: str, cell: dict) -> bool:
        """Replace one field of one record, addressed by primary key.

        A jsonb_set against the row's own `id`, so this cannot drift onto a
        neighbour the way a positional write would. Returns False when the row
        is not in this dataset, which is the honest answer to "you addressed a
        record that does not exist here" rather than a silent no-op.
        """
        def _fn(cur):
            cur.execute(
                """UPDATE dataset_records
                      SET row_json = jsonb_set(row_json, %s, %s::jsonb)
                    WHERE id=%s AND dataset_id=%s RETURNING id""",
                ([field], _jsonb(cell), record_id, dataset_id))
            return bool(cur.fetchall())

        return bool(self._run("update_record_cell", _fn))

    def get_sources(self, dataset_id: str) -> dict:
        ds = self._rows("get_sources",
                        """SELECT run_id,sources_attempted,sources_successful,
                                  sources_failed FROM datasets WHERE id=%s""",
                        (dataset_id,))
        if not ds:
            return {}
        row = ds[0]
        run_id = row.get("run_id", "")
        srcs = self._rows("sources",
                          """SELECT url,title,content_hash,status,error,
                                    reused_from_run_id,reused_page_id,retrieved_at
                               FROM sources
                             WHERE run_id=%s ORDER BY url LIMIT 500""", (run_id,))
        return {"dataset_id": dataset_id,
                "sources_attempted": row.get("sources_attempted", 0),
                "sources_successful": row.get("sources_successful", 0),
                "sources_failed": row.get("sources_failed", 0),
                "sources": srcs}

    # -- evidence -------------------------------------------------------------
    def get_pages(self, run_id: str, limit: int = 200) -> list[dict]:
        """Stored evidence, newest first. Content is truncated for transport."""
        return self._rows(
            "get_pages",
            """SELECT id,url,final_url,parent_url,depth,method,status,error,
                      content_hash,markdown,snapshot_chars,retrieved_at
                 FROM pages WHERE run_id=%s ORDER BY retrieved_at DESC LIMIT %s""",
            (run_id, max(1, min(int(limit), 500))))

    def get_pages_by_ids(self, page_ids: list[str]) -> list[dict]:
        """Page rows for specific ids, in one round trip, WITHOUT raw_html.

        Backfill picked its candidate pages one `get_page` at a time, which over
        a remote database is one round trip each: six candidates cost 45
        seconds, and the Coverage tab fires that on every load. One query
        returns them.

        `raw_html` is deliberately excluded. Measured on this instance, one
        page with its raw_html took 2.7s and the same page without it took
        0.13s — the HTML is the whole cost and backfill does not need it. The
        `markdown` column *is* the stored evidence text (the crawler writes
        `page_evidence_text(page)` into it), so handing that to the extractor
        reproduces the same string the offsets were computed against. Losing the
        DOM costs the CSS-selector fast path, which a backfill plan never
        matched anyway.
        """
        ids = [str(p) for p in (page_ids or []) if p]
        if not ids:
            return []
        rows = self._rows(
            "get_pages_by_ids",
            "SELECT id,run_id,url,final_url,parent_url,depth,method,status,error,"
            "content_hash,markdown,snapshot_chars,retrieved_at"
            " FROM pages WHERE id = ANY(%s::uuid[]) ORDER BY retrieved_at DESC",
            (ids,))
        order = {p: i for i, p in enumerate(ids)}
        rows.sort(key=lambda r: order.get(str(r.get("id")), 1 << 30))
        return rows

    def get_page(self, page_id: str) -> dict | None:
        rows = self._rows("get_page", "SELECT * FROM pages WHERE id=%s", (page_id,))
        return rows[0] if rows else None

    # -- restart recovery -----------------------------------------------------
    def recover_orphan_runs(self) -> int:
        """Fail runs left mid-flight by a crash or restart.

        Runs used to live only as an asyncio task, so a crash left a row at
        DISCOVERING forever and `GET /api/jobs/{id}` reported a permanent ghost
        that looked exactly like a healthy run. Marking them FAILED makes the
        truth visible instead of leaving a zombie.
        """
        rows = self._rows("recover_orphan_runs",
                          """UPDATE runs SET status='FAILED', partial=true,
                                           error='backend restarted while this run was in flight',
                                           completed_at=now()
                             WHERE completed_at IS NULL
                               AND status NOT IN ('COMPLETED','FAILED','CANCELLED')
                             RETURNING id""")
        if rows:
            log.warning("recovered orphan runs", extra={"data": {"count": len(rows)}})
        return len(rows)
