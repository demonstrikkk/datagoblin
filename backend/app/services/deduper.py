"""Dedup: L1 exact-normalized -> L2 RapidFuzz token_set. Merge unions provenance.

L3 embeddings: Later adapter only (FEATURE_SEMANTIC_DEDUP + explicit provider).
MVP stops at L2 — documented, not omitted.
"""
from rapidfuzz import fuzz

from app.services.validator import normalize_key


def _sig(fields: dict, keys: list[str]) -> str:
    return "|".join(normalize_key(fields.get(k)) for k in keys)


def dedupe_records(rows: list[dict], keys: list[str],
                   threshold: float = 88.0) -> tuple[list[dict], int]:
    """Rows: {fields: {name: provenance-field}}. Returns (canonical, merged_count).

    Identity rule: a row whose dedupe signature is EMPTY (all key values missing)
    carries no identity evidence and never merges — neither exact nor fuzzy.
    Two empty signatures would otherwise false-positive on every run.
    """
    from app.core.config import settings as _settings
    if _settings.FEATURE_SEMANTIC_DEDUP:        raise RuntimeError(
            "FEATURE_SEMANTIC_DEDUP is a Phase-2 gate with no embedding provider wired. "
            "Refusing before any merge work (fail fast, no partial state). "
            "Disable the flag to use L1/L2.")
    canon_sigs: list[str] = []
    canon_rows: list[dict] = []
    merged = 0
    for row in rows:
        plain = {k: (v.get("value") if isinstance(v, dict) else v)
                 for k, v in row.get("fields", {}).items()}
        sig = _sig(plain, keys) if keys else _sig(plain, sorted(plain))
        idx = -1
        if sig:
            for i, c in enumerate(canon_sigs):
                if c and sig == c:
                    idx = i
                    break
        if idx < 0 and sig:
            for i, c in enumerate(canon_sigs):
                if not c:
                    continue
                try:
                    score = fuzz.token_set_ratio(c, sig)
                except Exception:
                    score = 0.0
                if score >= threshold:
                    idx = i
                    break
        if idx < 0:
            canon_sigs.append(sig)
            canon_rows.append(row)
        else:
            merged += 1
            target = canon_rows[idx]["fields"]
            for k, v in row.get("fields", {}).items():
                cur = target.get(k, {})
                if not isinstance(cur, dict) or cur.get("value") in (None, ""):
                    target[k] = v
                elif isinstance(v, dict) and v.get("value") not in (None, "") and cur.get("value") != v.get("value"):
                    # Genuine conflict on merge: keep first value, mark conflicting,
                    # preserve the rival {value, source} for the JUDGE step (Jev-C).
                    cur["verification_status"] = "conflicting"
                    cur.setdefault("rivals", []).append(
                        {"value": v.get("value"), "source": v.get("source", {})})
    return canon_rows, merged


async def adjudicate_conflicts(rows: list[dict]) -> dict:
    """Explicit JUDGE step (Jev-C): confirm/adopt/downgrade conflicting cells.

    A: keep incumbent. B: adopt rival value+source. CONFLICT: keep conflicting.
    INSUFFICIENT: downgrade to unverified (honest, never guessed).
    Returns {judged, confirmed, adopted, downgraded}.

    The cells are judged concurrently. One live run merged 35 duplicates and
    then spent the remainder of its 600s budget adjudicating their conflicts
    one at a time, which is what pushed the run past its limit; the cells are
    independent, so the wall cost is now the slowest call rather than the sum.
    """
    import asyncio
    from app.providers.decision import jev
    from app.core.config import settings

    stats = {"judged": 0, "confirmed": 0, "adopted": 0, "downgraded": 0}
    sem = asyncio.Semaphore(max(1, int(getattr(settings, "ITEM_CONCURRENCY", 8) or 8)))

    async def _judge(name: str, cell: dict, rival: dict) -> str:
        async with sem:
            res = await jev.conflict_triage(
                name, str(cell.get("value", "")), str(rival.get("value", "")),
                (cell.get("source", {}) or {}).get("quote", ""),
                (rival.get("source", {}) or {}).get("quote", ""))
        return str(res.get("decision", "CONFLICT"))

    jobs: list[tuple[str, dict, dict]] = []
    for row in rows:
        for name, cell in row.get("fields", {}).items():
            if not isinstance(cell, dict) or cell.get("verification_status") != "conflicting":
                continue
            rivals = cell.get("rivals", []) or []
            if not rivals:
                continue
            jobs.append((name, cell, rivals[0]))

    stats["judged"] = len(jobs)
    if not jobs:
        return stats
    verdicts = await asyncio.gather(
        *(_judge(n, c, rv) for n, c, rv in jobs), return_exceptions=True)
    for (_name, cell, rival), item in zip(jobs, verdicts):
        if isinstance(item, BaseException):
            # A failed judge leaves the cell conflicting, which is the safe
            # outcome: a conflict is never silently resolved one way.
            stats["confirmed"] += 1
            continue
        if item == "B":
            cell["value"] = rival.get("value")
            cell["source"] = rival.get("source", cell.get("source", {}))
            cell["verification_status"] = "verified"
            stats["adopted"] += 1
        elif item == "INSUFFICIENT":
            cell["value"] = None
            cell["verification_status"] = "unverified"
            stats["downgraded"] += 1
        else:
            stats["confirmed"] += 1  # A or CONFLICT: incumbent stands as conflicting
    return stats
