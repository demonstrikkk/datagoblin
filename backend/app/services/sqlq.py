"""Ask a dataset a question in English; run the SQL it wrote, safely.

The model proposes SQL. It never gets to run arbitrary SQL. Two independent
constraints, either of which alone would be thin:

1. **A grammar, not a blacklist.** Every bare identifier in the statement must
   be one of this dataset's columns or a function we allow. A regex blocklist
   can be walked around (`pg_catalog`, an unlisted function, a comment trick);
   an allowlist cannot. Anything unrecognised is rejected by name, so the error
   says which word was not permitted.
2. **A read-only, single-statement, row-capped transaction.** The submitted
   text is wrapped as a subquery with a hard `LIMIT`, run with
   `SET TRANSACTION READ ONLY`, and rolled back rather than committed. Even if
   the grammar were bypassed, the database itself refuses to write.

The exposed relation is built here, with column names quoted by us, so the
model can only ever see this dataset's rows — no other table, no joins onto
`users` or `seen_fingerprints`, no cross-dataset reach.

The result is a *reading*, never an edit: this cannot change stored data.
"""
from __future__ import annotations

import re

#: Functions the model may call. Deliberately tiny: aggregates, string helpers,
#: and numeric helpers. No file, network, or system access, and no `pg_sleep`
#: (a trivially available way to pin a worker for as long as it likes).
ALLOWED_FUNCS = frozenset("""
count avg min max sum round abs coalesce nullif length lower upper trim
substring left right concat cast coalesce greatest least floor ceil mod
row_number rank dense_rank
""".split())

#: Structural keywords. Everything else that looks like a word is treated as an
#: identifier and must be a column.
STRUCTURAL = frozenset("""
select from where group by order having limit offset as and or not in like
ilike between is null true false case when then else end distinct join on
left inner outer with union all asc desc cast exists intersect except
""".split())

#: One word that must not appear. These are the only statements that can change
#: anything or escape the relation; the grammar rejects them anyway, and this
#: gives a precise error instead of "unknown identifier" for a `;`-splice.
FORBIDDEN = frozenset("""
insert update delete drop alter create truncate grant revoke copy call do
execute set reset begin commit rollback vacuum analyze comment lock notify
listen discard reindex cluster refresh merge upsert into values returning
""".split())

_TOKEN = re.compile(r"""
      (?P<ws>\s+)
    | (?P<comment>--[^\n]*|/\*.*?\*/)
    | (?P<str>'(?:[^']|'')*')
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<op><=|>=|<>|!=|\|\||::|[-+*/%<>=(),.;\[\]])
    | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<other>.)
""", re.VERBOSE | re.DOTALL)


def tokenize(sql: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _TOKEN.finditer(sql or ""):
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        out.append((kind, m.group()))
    return out


def _normalise_columns(fields: list) -> list[str]:
    names: list[str] = []
    for f in fields or []:
        name = f.get("name") or f.get("field") if isinstance(f, dict) else f
        if name:
            names.append(str(name))
    return names


def relation_sql(dataset_id: str, fields: list) -> tuple[str, list[str]]:
    """The single relation the model may read from, as (sql, column_names).

    Two columns per field, because one is not enough to be honest about:

    * ``field``          — the extracted value
    * ``field__status``  — the verdict it carries (verified / unverified /
      conflicting / judgment_unavailable)

    A dataset's whole claim is that values are traceable, so a question that
    can only see values cannot answer "how many of these are actually verified"
    or "which are disputed" — the questions the product exists to answer.

    Cells are stored as ``{"value": ..., "source": {...},
    "verification_status": ...}``, so reading the bare key returned the whole
    object serialised as text: every row looked unique, and a `GROUP BY` on it
    returned a count of 1 for everything. The bare ``->>`` read is kept as a
    COALESCE fallback for the older rows that stored a plain scalar.
    """
    names = _normalise_columns(fields)
    parts = ["id::text AS record_id"]
    cols = ["record_id"]
    for n in names:
        safe = n.replace('"', '""')
        lit = safe.replace("'", "''")
        # The scalar fallback is conditional on the cell actually being a
        # scalar. An unconditional COALESCE fell through to `->> 'field'` when
        # the value was null, and that read returns the whole cell object as
        # text — so the most empty rows produced the longest strings, and
        # `GROUP BY` counted them separately instead of counting them as empty.
        scalar = (f"CASE WHEN jsonb_typeof(row_json -> '{lit}') = 'string' "
                  f"THEN row_json ->> '{lit}' END")
        parts.append(f"COALESCE(row_json -> '{lit}' ->> 'value', {scalar}) AS \"{safe}\"")
        cols.append(n)
        parts.append(
            f"COALESCE(row_json -> '{lit}' ->> 'verification_status', '') "
            f"AS \"{safe}__status\"")
        cols.append(f"{n}__status")
    sql = ("SELECT " + ", ".join(parts)
           + " FROM dataset_records WHERE dataset_id = '" + dataset_id.replace("'", "") + "'")
    return sql, cols


def validate(sql: str, columns: list[str]) -> str:
    """Return the SQL if it is within the grammar, else raise ValueError.

    ValueError is caught by the route and turned into a 422, so a rejected
    query reads as "that is not allowed" rather than a 500.
    """
    text = (sql or "").strip()
    if not text:
        raise ValueError("no SQL was produced")
    if ";" in text.rstrip(";"):
        raise ValueError("only one statement may be run")

    tokens = tokenize(text)
    if not tokens:
        raise ValueError("no SQL was produced")

    # A trailing semicolon is tolerated; it is not a second statement.
    if tokens and tokens[-1] == ("op", ";"):
        tokens = tokens[:-1]

    lowered = {t[1].lower() for t in tokens if t[0] == "word"}
    banned = lowered & FORBIDDEN
    if banned:
        raise ValueError(f"not permitted in a read-only query: {', '.join(sorted(banned))}")

    words = [t[1] for t in tokens if t[0] == "word"]
    if not words or words[0].lower() not in ("select", "with"):
        raise ValueError("a query must start with SELECT or WITH")

    # The one relation the model is told to read from, plus any name it
    # introduces itself. `name AS (` is a CTE or derived table, and since the
    # grammar permits no other table an invented name can only ever refer to a
    # subquery the model wrote itself. The word after `AS` is an output label
    # or a cast type — a name, never a reference to anything.
    allowed = {c.lower() for c in columns} | {"records"}
    for i, tok in enumerate(tokens):
        if tok[0] != "word":
            continue
        before = tokens[i - 1] if i else None
        after = tokens[i + 1] if i + 1 < len(tokens) else None
        if before and before[0] == "word" and before[1].lower() == "as":
            allowed.add(tok[1].lower())
        if after and after[0] == "word" and after[1].lower() == "as":
            allowed.add(tok[1].lower())

    for kind, value in tokens:
        if kind != "word":
            continue
        low = value.lower()
        if low in STRUCTURAL or low in ALLOWED_FUNCS:
            continue
        if low in allowed:
            continue
        # Anything else is an identifier we did not put there: another table,
        # a system catalog, a function that can reach the filesystem.
        raise ValueError(
            f"`{value}` is not available. Only this dataset's columns "
            f"({', '.join(sorted(allowed)[:8])}"
            f"{'…' if len(allowed) > 8 else ''}) and read-only functions may be used."
        )
    return text


def build_query(candidate_sql: str, relation: str, limit: int) -> str:
    """Wrap validated SQL so it can only ever return a bounded result set.

    The candidate becomes a subquery, so even `ORDER BY … LIMIT 999999` is
    re-capped, and the model never has to be trusted to remember a limit.
    """
    return (f"SELECT * FROM ({candidate_sql}) AS q "
            f"ORDER BY 1 LIMIT {max(1, int(limit))}")


def build_prompt(question: str, fields: list, row_count: int, examples: str = "") -> str:
    """Ask for one SELECT, and show the columns it is allowed to use."""
    cols = _normalise_columns(fields)
    listing = "\n".join(
        f"  {c} (text)  |  {c}__status (text: verified/unverified/conflicting/judgment_unavailable)"
        for c in cols)
    return (
        "Write ONE PostgreSQL SELECT that answers the question using a single "
        f"table called `records` with {row_count} rows and exactly these columns:\n"
        f"{listing}\n\n"
        "Rules: SELECT only. No INSERT/UPDATE/DELETE/DDL. No other tables, no "
        "schemas, no system catalogs. Every value is text, so cast when you "
        "compare or aggregate numbers: CAST(\"batch_number\" AS integer). "
        "Treat an empty string as missing, e.g. "
        "\"company_name\" IS NOT NULL AND \"company_name\" <> ''. "
        "Return a `SELECT` with a useful column alias. No prose, no semicolon "
        "beyond the end.\n"
        + (f"\nExamples of accepted shape:\n{examples}\n" if examples else "")
        + f"\nQuestion: {question[:1000]}\n"
        # The provider only ever returns JSON — it raises rather than hand back
        # prose — so the reply shape is stated rather than assumed. A prompt
        # asking for bare SQL fails at the provider, not here, which is why the
        # first version reported "the model did not return a query".
        + 'Reply with a single JSON object only, no prose, no backticks: '
        + '{"sql": "<the SELECT statement>"}.'
    )


_SELECT_RE = re.compile(r"\b(select|with)\b", re.IGNORECASE)


def extract_sql(payload: object) -> str:
    """Pull a SELECT out of whatever shape the model replied with.

    Models are asked for `{"sql": "..."}` and answer with that key, with
    `query`, with `answer`, or with a sentence containing the statement. The
    first version read one key and reported "no SQL was produced" on every
    other shape, which reads like the model refusing rather than a parser that
    looked in one place.

    Whatever is found still has to pass `validate`, so being generous about
    where to look costs nothing in safety.
    """
    if isinstance(payload, str):
        return _first_select(payload)
    if not isinstance(payload, dict):
        return ""
    for key in ("sql", "query", "statement", "answer", "result", "response", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            found = _first_select(value)
            if found:
                return found
        if isinstance(value, dict):
            found = extract_sql(value)
            if found:
                return found
    # Last resort: any string value anywhere in the reply.
    for value in payload.values():
        if isinstance(value, str):
            found = _first_select(value)
            if found:
                return found
    return ""


def _first_select(text: str) -> str:
    """The first SELECT/WITH statement in `text`, up to its end or semicolon."""
    m = _SELECT_RE.search(text or "")
    if not m:
        return ""
    tail = text[m.start():]
    # Cut at the first semicolon, and drop a trailing fence or prose.
    tail = tail.split(";")[0]
    return tail.strip()


DEFAULT_EXAMPLES = (
    "SELECT \"industry\", count(*) AS companies FROM records "
    "WHERE \"industry\" <> '' GROUP BY \"industry\" ORDER BY companies DESC\n"
    "SELECT record_id, \"company_name\", \"company_name__status\" FROM records "
    "WHERE \"company_name__status\" = 'conflicting'"
)
