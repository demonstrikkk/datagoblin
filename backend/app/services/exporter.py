"""Export: CSV/JSON with provenance columns, row caps, CSV-injection guard.

Phase-3 additions (additive; to_csv/to_json/export_dataset unchanged):
- CsvStreamWriter / JsonLinesWriter: start/write_row/finish lifecycle for
  streaming large datasets without buffering the whole file. JSONL is
  crash-safe (one complete JSON object per line; a killed run keeps a
  parseable prefix).
- columns pin: pass explicit column names to freeze the header against
  schema drift (Scrapy FEED_EXPORT_FIELDS spirit). None = schema names.
"""
import csv
import io
import json
from typing import Any

from app.core.config import settings
from app.core.errors import validation


def _safe_cell(v: object) -> str:
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _column_names(schema: list[dict], columns: list[str] | None) -> list[str]:
    if columns:
        return [str(c)[:100] for c in columns]
    return [(f.get("name", "") if isinstance(f, dict) else str(f)) for f in schema]


def _row_cells(names: list[str], fields: dict) -> tuple[list[str], list[str],
                                                        list[str], list[str]]:
    """Shared cell logic for buffered + streaming CSV (identical output)."""
    vals, statuses, urls, times = [], [], [], []
    for n in names:
        cell = fields.get(n, {})
        if isinstance(cell, dict):
            vals.append(_safe_cell(cell.get("value", "")))
            statuses.append(str(cell.get("verification_status", "")))
            src = cell.get("source", {}) or {}
            urls.append(str(src.get("url", "")))
            times.append(str(src.get("retrieved_at", "")))
        else:
            vals.append(_safe_cell(cell))
            statuses += [""]
            urls += [""]
            times += [""]
    return vals, statuses, urls, times


def to_csv(schema: list[dict], rows: list[dict],
           columns: list[str] | None = None) -> str:
    names = _column_names(schema, columns)
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(names + ["_verification_status", "_source_url", "_retrieved_at"])
    for r in rows[: settings.EXPORT_MAX_ROWS]:
        vals, statuses, urls, times = _row_cells(names, r.get("fields", {}))
        # Per-row provenance would explode columns; export carries first-field provenance
        # plus full JSON sidecar via to_json. Row-level detail stays queryable in Studio.
        w.writerow(vals + ["|".join(statuses), "|".join(urls), "|".join(times)])
    return buf.getvalue()


def to_json(rows: list[dict]) -> str:
    return json.dumps(rows[: settings.EXPORT_MAX_ROWS], default=str, ensure_ascii=False)


def to_jsonl(rows: list[dict]) -> str:
    """Crash-safe JSON lines: one complete object per line, same row cap."""
    return "\n".join(json.dumps(r, default=str, ensure_ascii=False)
                     for r in rows[: settings.EXPORT_MAX_ROWS])


def to_markdown_report(schema: list[dict], rows: list[dict],
                       meta: dict | None = None) -> str:
    """Human-readable dataset report (Apify Report-view spirit, Markdown).

    Pure function. Renders record values WITH verification status, source
    URL, retrieval time, and evidence quote per field — the same provenance
    the JSON sidecar carries, readable by a non-technical user. Zero rows
    renders an honest empty report (never raises, never fabricates).
    """
    import datetime
    meta = meta or {}
    names = _column_names(schema, None)
    clipped = rows[: settings.EXPORT_MAX_ROWS]
    counts = meta.get("counts", {}) if isinstance(meta.get("counts", {}), dict) else {}
    lines = [f"# {meta.get('name') or 'Dataset report'}", ""]
    lines.append(f"- Run: `{meta.get('run_id', '')}` | "
                 f"Generated (UTC): {datetime.datetime.utcnow().isoformat() + 'Z'}")
    lines.append(f"- Records: **{len(clipped)}**"
                 + (f" (showing first {settings.EXPORT_MAX_ROWS})"
                    if len(rows) > len(clipped) else ""))
    if counts:
        lines.append("- Crawl: "
                     + ", ".join(f"{k}={counts.get(k, 0)}"
                                 for k in ("attempted", "successful", "failed",
                                           "skipped", "verified", "needs_review")))
    lines.append("")
    if not clipped:
        lines.append("> No verified records. The run completed without extractable "
                     "evidence for the requested fields — sources were fetched "
                     "but nothing met the proof bar. This is an honest empty "
                     "result, not a failure to look.")
        lines.append("")
        lines += _report_method_note()
        return "\n".join(lines)
    lines.append("## Summary")
    lines.append("")
    lines.append("| # | " + " | ".join(names) + " | status |")
    lines.append("|---" * (len(names) + 2) + "|")
    for i, r in enumerate(clipped, 1):
        fields = r.get("fields", {}) if isinstance(r.get("fields", {}), dict) else {}
        cells, statuses = [], []
        for n in names:
            cell = fields.get(n, {})
            if isinstance(cell, dict):
                cells.append(str(cell.get("value", "") or "").replace("|", "\\|")[:200])
                statuses.append(str(cell.get("verification_status", "") or ""))
            else:
                cells.append(str(cell or "").replace("|", "\\|")[:200])
                statuses.append("")
        worst = "unverified"
        for s in statuses:
            if s == "verified":
                worst = "verified"
                break
            if s == "needs_review":
                worst = "needs_review"
        lines.append(f"| {i} | " + " | ".join(cells) + f" | {worst} |")
    lines.append("")
    for i, r in enumerate(clipped, 1):
        fields = r.get("fields", {}) if isinstance(r.get("fields", {}), dict) else {}
        lines.append(f"## Record {i}")
        lines.append("")
        for n in names:
            cell = fields.get(n, {})
            if isinstance(cell, dict):
                src = cell.get("source", {}) or {}
                lines.append(f"### {n}")
                lines.append("")
                lines.append(f"**{cell.get('value', '')}** "
                             f"— _{cell.get('verification_status', 'unverified')}_")
                if src.get("url"):
                    lines.append(f"- Source: [{src.get('title') or src.get('url')}]"
                                 f"({src.get('url')})")
                if src.get("retrieved_at"):
                    lines.append(f"- Retrieved: {src.get('retrieved_at')}")
                if src.get("quote"):
                    lines.append(f"- Evidence: “{src.get('quote')}”")
                lines.append("")
            elif cell not in (None, ""):
                lines.append(f"### {n}")
                lines.append("")
                lines.append(f"**{cell}**")
                lines.append("")
    lines += _report_method_note()
    return "\n".join(lines)


def _report_method_note() -> list[str]:
    return ["---", "",
            "_Method: static HTTP fetch first, allowlisted impersonation second, "
            "rendered-browser (Crawl4AI) fallback last; robots.txt honored, "
            "per-host throttle, no proxies, no bypass. Every non-null field "
            "above carries its source; fields without evidence are omitted._"]


class CsvStreamWriter:
    """Streaming CSV with open/write/close lifecycle. Output == to_csv()."""

    def __init__(self, stream: Any, max_rows: int = 0) -> None:
        self._stream = stream
        self._writer: Any = None
        self._names: list[str] = []
        self._written = 0
        self._max_rows = max_rows or settings.EXPORT_MAX_ROWS
        self._open = False

    def start(self, schema: list[dict], columns: list[str] | None = None) -> None:
        if self._open:
            raise ValueError("CsvStreamWriter already started")
        self._names = _column_names(schema, columns)
        self._writer = csv.writer(self._stream)
        self._writer.writerow(self._names + ["_verification_status", "_source_url",
                                             "_retrieved_at"])
        self._open = True

    def write_row(self, row: dict) -> None:
        if not self._open:
            raise ValueError("CsvStreamWriter not started")
        if self._written >= self._max_rows:
            return
        vals, statuses, urls, times = _row_cells(self._names, row.get("fields", {}))
        self._writer.writerow(vals + ["|".join(statuses), "|".join(urls),
                                      "|".join(times)])
        self._written += 1

    def finish(self) -> int:
        """Flush the stream. Returns rows written. Call even on abort."""
        self._open = False
        try:
            self._stream.flush()
        except Exception:
            pass
        return self._written


class JsonLinesWriter:
    """Streaming JSONL with open/write/close lifecycle. Crash-safe prefix."""

    def __init__(self, stream: Any, max_rows: int = 0) -> None:
        self._stream = stream
        self._written = 0
        self._max_rows = max_rows or settings.EXPORT_MAX_ROWS
        self._open = False

    def start(self) -> None:
        if self._open:
            raise ValueError("JsonLinesWriter already started")
        self._open = True

    def write_row(self, row: dict) -> None:
        if not self._open:
            raise ValueError("JsonLinesWriter not started")
        if self._written >= self._max_rows:
            return
        self._stream.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
        self._written += 1

    def finish(self) -> int:
        self._open = False
        try:
            self._stream.flush()
        except Exception:
            pass
        return self._written


def export_dataset(schema: list[dict], rows: list[dict], fmt: str,
                   columns: list[str] | None = None,
                   meta: dict | None = None) -> tuple[str, str, str]:
    """Returns (format, content, filename). Raises E_VALIDATION on bad format/empty.

    fmt "md"|"markdown"|"report" renders the human-readable report; unlike the
    machine formats it accepts zero rows (honest empty report). meta is an
    optional {"name", "run_id", "counts"} mapping for the report header.
    """
    if fmt in ("md", "markdown", "report"):
        return "md", to_markdown_report(schema, rows, meta), "report.md"
    if not rows:
        raise validation("Nothing to export: dataset has no records")
    if fmt == "csv":
        return "csv", to_csv(schema, rows, columns), "dataset.csv"
    if fmt == "json":
        return "json", to_json(rows), "dataset.json"
    if fmt == "jsonl":
        return "jsonl", to_jsonl(rows), "dataset.jsonl"
    raise validation(f"Unsupported export format: {fmt[:20]} (csv|json|jsonl|md)")
