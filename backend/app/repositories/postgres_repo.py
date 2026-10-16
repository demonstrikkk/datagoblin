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
from app.core.errors import AppError, dependency, validation


def _is_bad_uuid_error(e: Exception) -> bool:
    """Did Postgres reject a parameter as malformed UUID syntax?

    Pure string match on the driver's message, so it is unit-testable without
    a database. Narrow on purpose: only the invalid-syntax diagnostic matches,
    never a connection or constraint failure.
    """
    return "invalid input syntax for type uuid" in str(e)
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
                try:
                    from psycopg.rows import dict_row
                    from psycopg_pool import ConnectionPool
                    self._pool = ConnectionPool(
                        conninfo=self._dsn,
                        min_size=settings.DB_POOL_MIN,
                        max_size=settings.DB_POOL_MAX,
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
            if _is_bad_uuid_error(e):
                # A malformed id (hand-typed, pasted placeholder, truncated) is
                # a client mistake, not a database outage. Mapping it to 503
                # sent readers restarting a healthy server; 422 names the real
                # problem. An unparseable UUID can never match a row, so
                # nothing valid is refused.
                raise validation(
                    "not a valid id (expected a UUID): "
                    f"{str(e)[:120]}")
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

    # -- source reputation ----------------------------------------------------

    def note_source_outcome(self, host: str, *, ok: bool, blocked: bool = False,
                             verified_cells: int = 0, cells: int = 0) -> None:
        """Record one fetch outcome against the host, across every run.

        The counters are additive rather than assigned, so concurrent runs
        reporting the same host cannot lose each other's contribution the way an
        absolute write would. `visited` counts every attempt including the first,
        which is why the column is incremented unconditionally.
        """
        self._run("note_source_outcome", lambda cur: cur.execute(
            """INSERT INTO source_reputation
                 (host, visits, successes, cells, verified_cells, block_count,
                  last_seen, last_success_at, last_blocked_at)
               VALUES (%s, 1, %s, %s, %s, %s, now(), %s, %s)
               ON CONFLICT (host) DO UPDATE SET
                 visits          = source_reputation.visits + 1,
                 successes       = source_reputation.successes + EXCLUDED.successes,
                 cells           = source_reputation.cells + EXCLUDED.cells,
                 verified_cells  = source_reputation.verified_cells
                                    + EXCLUDED.verified_cells,
                 block_count     = source_reputation.block_count
                                    + EXCLUDED.block_count,
                 last_seen       = now(),
                 last_success_at = COALESCE(EXCLUDED.last_success_at,
                                            source_reputation.last_success_at),
                 last_blocked_at = COALESCE(EXCLUDED.last_blocked_at,
                                            source_reputation.last_blocked_at)""",
            (host, 1 if ok else 0, max(0, int(cells)),
             max(0, int(verified_cells)), 1 if blocked else 0,
             "now()" if ok else None, "now()" if blocked else None)))

    def note_source_cells(self, host: str, *, cells: int = 0,
                          verified_cells: int = 0) -> None:
        """Attribute cells to a host without counting a fetch.

        Separate from `note_source_outcome` because that method increments
        `visits` unconditionally, and a visit means "a request was sent to this
        host". A cell arrives from a page fetched minutes or runs ago; charging
        the host another visit would inflate the denominator of
        `proof = verified/cells` in `reputation.trust` and quietly shrink every
        score, so the two are deliberately different statements about a host.

        This is what makes `proof_ratio` non-zero at all. `note_source_outcome`
        is called from the fetch path with no cell counts, so before this the
        column stayed at 0 and `trust()` returned `0.3 * reachability` for every
        host — the docstring's claim that proof is "the only thing that can make
        a host good" was true of the formula and false of the data.
        """
        cells = max(0, int(cells))
        verified_cells = max(0, int(verified_cells))
        if not cells:
            return
        self._run("note_source_cells", lambda cur: cur.execute(
            """INSERT INTO source_reputation
                 (host, visits, successes, cells, verified_cells, block_count,
                  last_seen)
               VALUES (%s, 0, 0, %s, %s, 0, now())
               ON CONFLICT (host) DO UPDATE SET
                 cells          = source_reputation.cells + EXCLUDED.cells,
                 verified_cells = source_reputation.verified_cells
                                    + EXCLUDED.verified_cells,
                 last_seen      = now()""",
            (host, cells, verified_cells)))

    def get_source_reputation(self, hosts: list[str] | None = None) -> list[dict]:
        """Raw reputation rows, optionally restricted to `hosts`.

        Raw counters, not scores: `services.reputation.trust` derives the score,
        so the decay policy can change without rewriting stored data.
        """
        if hosts:
            rows = self._rows("get_source_reputation", (
                "SELECT * FROM source_reputation WHERE host = ANY(%s) ORDER BY host",
                (list(hosts),)))
            return rows
        return self._rows("get_source_reputation",
                          "SELECT * FROM source_reputation ORDER BY host")

    # -- field freshness ------------------------------------------------------

    def note_field_verified(self, dataset_id: str, field: str,
                            verified_cells: int = 1,
                            total_cells: int = 1) -> None:
        """Note that `field` was verified for `dataset_id` right now."""
        self._run("note_field_verified", lambda cur: cur.execute(
            """INSERT INTO field_freshness
                 (dataset_id, field, last_verified_at, last_seen_at,
                  verified_cells, total_cells)
               VALUES (%s,%s, now(), now(), %s, %s)
               ON CONFLICT (dataset_id, field) DO UPDATE SET
                 last_verified_at = now(),
                 last_seen_at     = now(),
                 verified_cells   = field_freshness.verified_cells
                                     + EXCLUDED.verified_cells,
                 total_cells      = field_freshness.total_cells
                                     + EXCLUDED.total_cells""",
            (dataset_id, field, max(0, int(verified_cells)),
             max(0, int(total_cells)))))

    def get_field_freshness(self, dataset_id: str) -> list[dict]:
        return self._rows("get_field_freshness",
                          "SELECT * FROM field_freshness WHERE dataset_id=%s "
                          "ORDER BY field", (dataset_id,))

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
        """Store one run event, keeping its kind.

        `type` is written separately from the message because the message is
        prose and gets reworded, while the kind is a fixed string the code
        already chooses. Storing only the message made every question about a
        run ("which pages failed", "how many records were rejected") a LIKE over
        a sentence, and an answer that changed when someone edited a message.
        Events written before the column existed keep an empty type, which reads
        as "kind unknown" rather than as a wrong kind.
        """
        payload = (run_id, event.get("stage", ""), str(event.get("type", ""))[:120],
                   str(event.get("message", ""))[:2000],
                   _jsonb(event.get("data", {}) or {}))

        def _typed(cur):
            cur.execute(
                """INSERT INTO run_events (run_id,stage,type,message,metadata_json)
                   VALUES (%s,%s,%s,%s,%s)""", payload)

        def _legacy(cur):
            cur.execute(
                """INSERT INTO run_events (run_id,stage,message,metadata_json)
                   VALUES (%s,%s,%s,%s)""",
                (payload[0], payload[1], payload[3], payload[4]))

        try:
            self._run("append_event", _typed)
        except AppError as e:
            # A database that predates 006 has no `type` column. Dropping the
            # event would erase the audit trail to protect it, so write the
            # legacy shape once and say which migration is missing.
            if "run_events" in str(e) and "type" in str(e) and (
                    "does not exist" in str(e) or "UndefinedColumn" in str(e)):
                log.warning("run_events.type column missing; wrote legacy event",
                            extra={"data": {"run_id": run_id,
                                            "migration": "006_run_event_type.sql"}})
                self._run("append_event_legacy", _legacy)
                return
            raise

    def append_events(self, run_id: str, events: list[dict]) -> None:
        """Store many run events in ONE statement.

        Exists because a run emits per record and per page, and each event is
        one round trip at a measured 177ms floor. `executemany` would still
        send one round trip per row on most drivers; a single multi-row INSERT
        is what actually amortises it.

        Order is preserved by the VALUES list, so the audit trail reads in the
        order the run emitted. Falls back to the single-event path when the
        table predates migration 006, rather than duplicating that fallback.
        """
        if not events:
            return
        if len(events) == 1:
            self.append_event(run_id, events[0])
            return

        run_ids = [run_id] * len(events)
        stages = [e.get("stage", "") for e in events]
        types = [str(e.get("type", ""))[:120] for e in events]
        messages = [str(e.get("message", ""))[:2000] for e in events]
        metas = [_jsonb(e.get("data", {}) or {}) for e in events]

        def _typed(cur):
            cur.executemany(
                """INSERT INTO run_events (run_id,stage,type,message,metadata_json)
                   VALUES (%s,%s,%s,%s,%s)""",
                list(zip(run_ids, stages, types, messages, metas)))

        try:
            self._run("append_events", _typed)
        except AppError as e:
            if "run_events" in str(e) and "type" in str(e) and (
                    "does not exist" in str(e) or "UndefinedColumn" in str(e)):
                log.warning("run_events.type column missing; wrote legacy events",
                            extra={"data": {"run_id": run_id,
                                            "migration": "006_run_event_type.sql"}})
                for ev in events:
                    self.append_event(run_id, ev)
                return
            raise

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

    def delete_dataset(self, dataset_id: str) -> bool:
        """Delete one dataset and everything that belongs to it alone.

        `dataset_records`, `exports` and `dataset_gaps` all reference
        `datasets(id) ON DELETE CASCADE`, so one statement removes the row,
        its records, its gap history and its export ledger. Sources, pages
        and runs belong to the *run*, not the dataset, and survive: the run
        stays as the audit trail of work that happened, and its stored pages
        stay reusable by future runs.
        """
        def _fn(cur):
            cur.execute("DELETE FROM datasets WHERE id=%s", (dataset_id,))
            return cur.rowcount
        return bool((self._run("delete_dataset", _fn) or 0) > 0)

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
        """Newest datasets.

        `schema_json` is selected and mapped to `schema` because
        `field_coverage(recs, row.get("schema") or [])` needs it. Without the
        schema the coverage matrix can only see fields that happen to appear in
        at least one record, so a field the plan asked for and no page carried
        is invisible here while `/coverage` — which does get the schema — lists
        it as never extracted. The dashboard then under-reports its own "never
        filled" column and disagrees with the dataset page, which is the one
        thing its own docstring says cannot happen.

        `partial` comes along for the same reason: the dataset page reads it
        from the run, and the dashboard renders a pill for it that could never
        light up because nothing supplied the field.
        """
        rows = self._rows("list_datasets",
                          """SELECT id,run_id,name,schema_json,record_count,created_at
                             FROM datasets ORDER BY created_at DESC LIMIT %s""",
                          (max(1, min(int(limit), 100)),))
        for r in rows:
           r["schema"] = r.pop("schema_json", None) or []
        return rows

    def _run_counts(self, run_id: str) -> dict:
        """Field-level counters for a run, from where `finalize_dataset` wrote
        them. The datasets table has no counts column, so without this join
        every dataset reads as "not counted" — including ones written minutes
        ago by current code — while the local adapter reports them from its
        row. One indexed read; additive, so a failure degrades to {} rather
        than blocking the dataset read it annotates."""
        if not run_id:
            return {}
        try:
            run_rows = self._rows("get_dataset_counts",
                                  "SELECT stats FROM runs WHERE id=%s",
                                  (str(run_id),))
            stats = (run_rows[0].get("stats") if run_rows else {}) or {}
            return stats.get("counts") or {}
        except Exception:  # noqa: BLE001
            return {}

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
        rows[0]["counts"] = self._run_counts(str(rows[0].get("run_id") or ""))
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
        ds["counts"] = self._run_counts(str(ds.get("run_id") or ""))
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
            # `SET LOCAL` does not take a bound parameter in Postgres, so the
            # value is interpolated. It is an int() of a settings field with a
            # numeric validator, so there is nothing to inject.
            cur.execute("SET LOCAL statement_timeout = %d"
                        % int(settings.DB_STATEMENT_TIMEOUT_MS))
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

    def coverage_aggregates(self, dataset_ids: list[str]) -> dict:
        """Per dataset and per field, the verdict counts — computed in the database.

        The dashboard used to answer this by shipping up to `RECORD_SAMPLE`
        `row_json` blobs per dataset and counting them in Python. That is 25
        round trips carrying megabytes of JSON to produce a dozen integers each,
        measured at 23s cold. Nothing about the answer needs the documents in the
        process; only the shape of the evidence does, and that lives at known
        paths inside each document.

So the counting happens where the documents already are. `jsonb_each`
        over `row_json` yields one row per (record, field), and two grouped passes
        turn that into per-field counts and then per-dataset totals — including
        `empty_fields` and `partial_fields`, which need the per-field numbers and
        are therefore not expressible as a single sum.

        **`row_json` is the field map itself, not a record wrapping one.** Its top
        level is `{"company_name": {...}, "country": {...}}`. `get_records`
        presents it as `{"record_id": ..., "fields": row_json}`, so the `fields`
        key appears in every Python fixture and in every record the API returns,
        and reaching for `row_json -> 'fields'` looks correct. It is not: that
        path is NULL for all 723 stored rows, `jsonb_each` returns nothing, and
        every field silently falls through to the declared-at-zero branch — which
        reported every dataset as empty while looking entirely healthy. Unit tests
        could not catch it, because the fake store delegated to
        `coverage.field_coverage` and never executed this SQL at all. Verified
        against the live database before being trusted.

        Two cell shapes have to be handled or the counts under-report:

        * A value stored as JSON `null` — `value ->> 'value'` is NULL, so the cell
          is absent and must not be counted as an unverified value.
        * A field whose value is a bare scalar, stored before provenance existed.
          It is a value with no verdict, so it counts as `unverified`.

        Schema fields that appear in no record are added from `datasets.schema_json`
        with `present = 0`, because "declared and never extracted" is the gap the
        coverage view exists to show and would otherwise disappear from an
        aggregate that only looks at records.

        Returns `{dataset_id: {"records": n, "fields": {name: {...}}}}`. A dataset
        with no records is present with `records: 0` and its declared fields at
        zero, so the caller does not have to distinguish "no records" from
        "unreadable".
        """
        ids = [str(d) for d in (dataset_ids or []) if d]
        if not ids:
            return {}

        rows = self._rows(
            "coverage_aggregates",
            """
            WITH declared AS (
              SELECT d.id AS dataset_id,
                     COALESCE(f->>'name', f->>'field') AS field
                FROM datasets d,
                     LATERAL jsonb_array_elements(
                       CASE WHEN jsonb_typeof(d.schema_json) = 'array'
                            THEN d.schema_json ELSE '[]'::jsonb END) AS f
            ),
            cells AS (
              SELECT r.dataset_id,
                     e.key AS field,
                     e.value AS cell,
                     -- A scalar predates provenance: a value with no verdict.
                     jsonb_typeof(e.value) <> 'object' AS scalar,
                     -- A stored JSON null is an absent value, not an
                     -- unverified one. `->>'value'` yields NULL for both a null
                     -- and a missing key, which is what we want here.
                     NULLIF(e.value->>'value', '') IS NULL
                       AND jsonb_typeof(e.value) = 'object' AS empty
                FROM dataset_records r,
                     LATERAL jsonb_each(
                       CASE WHEN jsonb_typeof(r.row_json) = 'object'
                            THEN r.row_json ELSE '{}'::jsonb END) AS e
            ),
            per_field AS (
              SELECT dataset_id, field,
                     count(*) AS present,
                     count(*) FILTER (WHERE NOT scalar
                                        AND lower(COALESCE(cell->>'verification_status',''))
                                            = 'verified') AS verified,
                     /* Present, and not proven and not disputed: unverified.
                      Stated as a negation rather than a list of statuses, because
                      `coverage._cell_value` falls back to "unverified" for any
                      status it does not recognise and the live data relies on
                      that. `judgment_unavailable` alone is 992 of the 1,811
                      verified-plus-other cells here — "we had no judge to ask" —
                      and a literal IN ('unverified','not_proven') reported zero.
                      A new status stays in the right bucket without a code
                      change, which is the whole point of matching the fallback
                      rather than restating it. */
                     count(*) FILTER (WHERE NOT scalar
                                        AND lower(COALESCE(cell->>'verification_status',''))
                                            NOT IN ('verified','conflicting')) AS unverified,
                     count(*) FILTER (WHERE lower(
                                        COALESCE(cell->>'verification_status',''))
                                        = 'conflicting') AS conflicting
                FROM cells
               WHERE NOT empty
               GROUP BY dataset_id, field
            ),
            record_counts AS (
              SELECT dataset_id, count(*) AS n FROM dataset_records GROUP BY dataset_id
            ),
            /* A declared field seen in no record still has to appear, at zero. */
            filled AS (
              SELECT dataset_id, field, present, verified, unverified, conflicting
                FROM per_field
              UNION ALL
              SELECT d.dataset_id, d.field, 0, 0, 0, 0
                FROM declared d
               WHERE NOT EXISTS (SELECT 1 FROM per_field p
                                  WHERE p.dataset_id = d.dataset_id
                                    AND p.field = d.field)
                 AND d.field IS NOT NULL AND d.field <> ''
            )
            SELECT f.dataset_id, f.field, f.present, f.verified, f.unverified,
                   f.conflicting, COALESCE(r.n, 0) AS records
              FROM filled f
              LEFT JOIN record_counts r ON r.dataset_id = f.dataset_id
             WHERE f.dataset_id = ANY(%s::uuid[])
            """,
            (ids,))

        out: dict = {i: {"records": 0, "fields": {}} for i in ids}
        for row in rows:
            did = str(row.get("dataset_id") or "")
            field = str(row.get("field") or "")
            if not did or not field:
                continue
            entry = out.setdefault(did, {"records": 0, "fields": {}})
            n = int(row.get("records") or 0)
            entry["records"] = max(entry["records"], n)
            present = int(row.get("present") or 0)
            entry["fields"][field] = {
                "present": present,
                "missing": max(0, n - present),
                "records": n,
                "verified": int(row.get("verified") or 0),
                "unverified": int(row.get("unverified") or 0),
                "conflicting": int(row.get("conflicting") or 0),
            }
        return out

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

    def update_dataset_schema(self, dataset_id: str, schema: list) -> bool:
        """Replace the declared schema of a dataset.

        Writing the whole array rather than appending in SQL, because a
        concurrent add of the same column must not produce two copies of it: the
        read-modify-write here is atomic under the row lock, and the caller
        re-reads the schema anyway to build the diff. Returns False when the
        dataset does not exist, which is a real answer rather than a silent
        success on nothing.
        """
        def _fn(cur):
            cur.execute("""UPDATE datasets SET schema_json=%s::jsonb
                            WHERE id=%s RETURNING id""",
                        (_jsonb(schema or []), dataset_id))
            return bool(cur.fetchall())

        return bool(self._run("update_dataset_schema", _fn))

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

    # -- gaps -----------------------------------------------------------------
    _GAP_COLS = ("id, dataset_id, field, category, missing, unverified, "
                 "conflicting, state, phase, attempts, reason, stats, "
                 "last_error, created_at, updated_at, resolved_at")

    def upsert_gap(self, dataset_id: str, field: str, gap: dict) -> dict:
        """Write one gap, keyed on (dataset_id, field).

        An upsert rather than an insert, because the row is a running record of
        attempts and an insert would leave the earlier attempts behind as a second
        row. The unique index makes the key authoritative, so two concurrent gap
        passes converge on one row instead of racing to create two.

        `reason` is written even while the gap is open. The absence of a value is
        a finding in its own right and is the thing a person needs to decide
        whether to spend a search on it; storing it only on a terminal state
        would make the queue unexplainable while it is still a queue.
        """
        gap_id = str(gap.get("id") or uuid.uuid4())

        def _fn(cur):
            cur.execute(
                f"""INSERT INTO dataset_gaps
                        (id,dataset_id,field,category,missing,unverified,conflicting,
                         state,phase,attempts,reason,stats,last_error,updated_at,
                         resolved_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,now(),%s)
                    ON CONFLICT (dataset_id, field) DO UPDATE SET
                        category=EXCLUDED.category,
                        missing=EXCLUDED.missing,
                        unverified=EXCLUDED.unverified,
                        conflicting=EXCLUDED.conflicting,
                        state=EXCLUDED.state,
                        phase=EXCLUDED.phase,
                        attempts=EXCLUDED.attempts,
                        reason=EXCLUDED.reason,
                        stats=EXCLUDED.stats,
                        last_error=EXCLUDED.last_error,
                        updated_at=now(),
                        resolved_at=EXCLUDED.resolved_at
                    RETURNING {self._GAP_COLS}""",
                (gap_id, dataset_id, field,
                 str(gap.get("category") or "depth_gap"),
                 int(gap.get("missing") or 0), int(gap.get("unverified") or 0),
                 int(gap.get("conflicting") or 0), str(gap.get("state") or "open"),
                 int(gap.get("phase") or 0), int(gap.get("attempts") or 0),
                 str(gap.get("reason") or ""), _jsonb(gap.get("stats") or {}),
                 str(gap.get("last_error") or ""),
                 gap.get("resolved_at") or None))
            row = cur.fetchone()
            return {k: _plain(v) for k, v in dict(row).items()} if row else {}

        return self._run("upsert_gap", _fn) or {}

    def list_gaps(self, dataset_id: str, *, state: str | None = None) -> list[dict]:
        """The persisted gaps for a dataset, biggest outstanding first.

        Ordered by the same measure the derived view uses, so the persisted queue
        and the computed one agree on what matters most — two lists ordered
        differently would make it impossible to tell whether they describe the
        same work.
        """
        sql = f"SELECT {self._GAP_COLS} FROM dataset_gaps WHERE dataset_id=%s"
        params: list = [dataset_id]
        if state:
            sql += " AND state=%s"
            params.append(state)
        sql += " ORDER BY (missing + unverified + conflicting) DESC, field ASC"
        return self._rows("list_gaps", sql, tuple(params))

    def get_gap(self, dataset_id: str, field: str) -> dict | None:
        """One gap, or None. A miss is a real answer, not an error."""
        rows = self._rows(
            "get_gap",
            f"SELECT {self._GAP_COLS} FROM dataset_gaps "
            "WHERE dataset_id=%s AND field=%s",
            (dataset_id, field))
        return rows[0] if rows else None

    # -- evidence -------------------------------------------------------------
    def get_pages(self, run_id: str, limit: int = 200) -> list[dict]:
        """Stored evidence, newest first. Content is truncated for transport."""
        return self._rows(
            "get_pages",
            """SELECT id,url,final_url,parent_url,depth,method,status,error,
                      content_hash,markdown,snapshot_chars,retrieved_at
                 FROM pages WHERE run_id=%s ORDER BY retrieved_at DESC LIMIT %s""",
            (run_id, max(1, min(int(limit), 500))))

    def list_sources(self, limit: int = 500) -> list[dict]:
        """Every stored page in the workspace, grouped by host.

        The source browser needs "what has this workspace ever fetched, and how
        much of it paid" across all runs, and there was no read that did that:
        `get_pages` is scoped to one run, so the only way to build the view was
        one request per run from the browser.

        Aggregated in SQL rather than by fetching every page and folding in
        Python, because `markdown` is the bulk of the table and none of it is
        needed to answer this. One row per host, with the page count, the
        characters stored, the number of runs that touched it, and the newest
        retrieval time, ordered by what the host actually supplied.
        """
        return self._rows(
            "list_sources",
            """SELECT
                 host,
                 count(*)::int               AS pages,
                 sum(snapshot_chars)::bigint AS chars,
                 max(retrieved_at)           AS last_used,
                 count(DISTINCT run_id)::int AS runs
               FROM (
                 SELECT run_id, snapshot_chars, retrieved_at,
                        coalesce(nullif(split_part(split_part(url, '://', 2), '/', 1), 'www.'), '')
                          AS host
                 FROM pages
               ) p
               GROUP BY host
               ORDER BY pages DESC, chars DESC
               LIMIT %s""",
            (max(1, min(int(limit), 2000)),))

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
