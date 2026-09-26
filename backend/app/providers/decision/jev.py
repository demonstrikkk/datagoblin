"""Decision layer — Jev CORE (4 families). GENERATE → ORCHESTRATE → JUDGE → EXECUTE → PROVE.

Real path: OpenRouter Decisions API, POST https://openrouter.ai/api/alpha/decisions
{decisionsRequest: {model: ~typesafe/jev-latest, state, questions}} answered with
typed {noul|choice|score} + probabilities. Key: OPENROUTER_API_KEY, with
TYPESAFE_API_KEY accepted as alias (existing .env files carry it there).
Deterministic policy otherwise (complete production logic for the no-key
deployment, not a placeholder). Judgments are probabilities; deterministic policy
maps them to verified/unverified/conflicting — never "Jev says TRUE".
"""
from typing import Literal

import httpx

from app.core.config import settings

Screening = Literal["YES", "NO", "UNCERTAIN"]
Support = Literal["SUPPORTED", "NOT_SUPPORTED", "UNCERTAIN"]
Conflict = Literal["A", "B", "CONFLICT", "INSUFFICIENT"]
Continuation = Literal["sufficient", "insufficient", "uncertain"]

_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"


def _key() -> str:
    return settings.OPENROUTER_API_KEY or settings.TYPESAFE_API_KEY


async def _jev_call(state: str, questions: dict) -> dict | None:
    """Returns parsed answers or None (key absent/failure => deterministic policy)."""
    key = _key()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            # REST body is flat {model, state, questions} (decisionsRequest is
            # only the TS-SDK parameter wrapper, not the HTTP shape).
            r = await c.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": settings.JEV_MODEL, "state": state[:30000],
                      "questions": questions})
            r.raise_for_status()
            data = r.json()
            return (data.get("answers") or {})
    except Exception:
        return None


def _confidence(node: dict) -> float:
    probs = node.get("probabilities", {}) or {}
    try:
        if probs:
            return max(float(v) for v in probs.values())
        return float(node.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _choice(ans: dict, name: str, allowed: set[str]) -> tuple[str | None, float]:
    node = (ans.get(name) or {})
    if not isinstance(node, dict) or node.get("type") not in (None, "choice"):
        return None, 0.0
    c = node.get("choice", node.get("decision", node.get("label")))
    return (c if c in allowed else None, _confidence(node))


def _stem_variants(word: str) -> set[str]:
    """Plural-tolerant match set: company<->companies, startup<->startups."""
    variants = {word}
    if word.endswith("ies") and len(word) > 4:
        variants.add(word[:-3] + "y")
    if word.endswith("y") and len(word) > 2:
        variants.add(word[:-1] + "ies")
    if word.endswith("s") and len(word) > 3:
        variants.add(word[:-1])
    variants.add(word + "s")
    variants.add(word + "es")
    return variants


async def source_screening(url: str, title: str, snippet: str, entity: str) -> dict:
    """A. YES/NO/UNCERTAIN — NO skips fetch+extract (biggest saver)."""
    ans = await _jev_call(
        f"Entity: {entity}\nURL: {url}\nTitle: {title}\nSnippet: {snippet[:1500]}",
        {"relevant": {"type": "choice", "instructions": "Is the source likely relevant?",
                      "criteria": {"YES": "about the entity", "NO": "unrelated",
                                   "UNCERTAIN": "cannot tell"}}})
    if ans is not None:
        c, conf = _choice(ans, "relevant", {"YES", "NO", "UNCERTAIN"})
        if c:
            return {"judgment": c, "confidence": conf, "provider": "jev"}
    text = f"{title} {snippet}".lower()
    keys = [k for k in entity.lower().split() if len(k) > 2]
    hit = any(v in text for k in keys for v in _stem_variants(k)) if keys else True
    return {"judgment": "YES" if hit else "NO", "confidence": 0.85 if hit else 0.7,
            "provider": "deterministic"}


async def source_relevance(url: str, title: str, snippet: str, entity: str) -> dict:
    r = await source_screening(url, title, snippet, entity)
    return {"score": 0.85 if r["judgment"] == "YES" else 0.2,
            "keep": r["judgment"] == "YES", "judgment": r["judgment"],
            "provider": r["provider"]}


async def evidence_verification(value: str, quote: str, source_text: str) -> dict:
    """B. SUPPORTED/NOT_SUPPORTED/UNCERTAIN. Deterministic substring pre-check first."""
    import re
    norm = lambda s: re.sub(r"\s+", " ", (s or "").strip().lower())
    if not quote or norm(quote) not in norm(source_text):
        return {"judgment": "NOT_SUPPORTED", "confidence": 0.95, "provider": "deterministic"}
    ans = await _jev_call(
        f"Claim: {value}\nEvidence: {quote}",
        {"support": {"type": "noul", "instructions": "Does the evidence support the claim?"}})
    if ans is not None:
        node = ans.get("support", {})
        if not isinstance(node, dict) or node.get("type", "noul") != "noul":
            return {"judgment": "UNCERTAIN", "confidence": 0.5, "provider": "jev"}
        try:
            score = float(node.get("noul", node.get("score", 0.5)))
        except (TypeError, ValueError):
            score = 0.5
        if score >= 0.6:
            return {"judgment": "SUPPORTED", "confidence": score, "provider": "jev"}
        if score <= 0.4:
            return {"judgment": "NOT_SUPPORTED", "confidence": 1 - score, "provider": "jev"}
        return {"judgment": "UNCERTAIN", "confidence": 0.5, "provider": "jev"}
    return {"judgment": "SUPPORTED", "confidence": 0.9, "provider": "deterministic"}


async def conflict_triage(field: str, value_a: str, value_b: str,
                          quote_a: str = "", quote_b: str = "") -> dict:
    """C. A/B/CONFLICT/INSUFFICIENT. CONFLICT => mark conflicting, never merge."""
    na, nb = (value_a or "").strip().lower(), (value_b or "").strip().lower()
    if na == nb:
        return {"decision": "A", "provider": "deterministic"}
    if not na or not nb:
        return {"decision": "INSUFFICIENT", "provider": "deterministic"}
    ans = await _jev_call(
        f"Field: {field}\nA: {value_a} <= {quote_a[:800]}\nB: {value_b} <= {quote_b[:800]}",
        {"which": {"type": "choice", "instructions": "Which value is better supported?",
                   "criteria": {"A": "A better", "B": "B better",
                                "CONFLICT": "genuine conflict",
                                "INSUFFICIENT": "neither supported"}}})
    if ans is not None:
        c, _ = _choice(ans, "which", {"A", "B", "CONFLICT", "INSUFFICIENT"})
        if c:
            return {"decision": c, "provider": "jev"}
    return {"decision": "CONFLICT", "provider": "deterministic"}


async def research_continuation(valid: int, requested: int,
                                missing_fields: list | None = None) -> dict:
    """D. sufficient/insufficient/uncertain => FETCH/REFINE/REVIEW."""
    if valid >= requested:
        return {"continuation": "sufficient", "action": "FETCH", "provider": "deterministic"}
    if valid == 0:
        # Nothing to judge yet; deterministic REFINE is cheapest and always correct.
        return {"continuation": "insufficient", "action": "REFINE", "provider": "deterministic"}
    return {"continuation": "uncertain" if valid < requested // 2 else "insufficient",
            "action": "REFINE", "provider": "deterministic"}


async def coverage_gate(valid: int, requested: int, missing_fields: list | None = None) -> dict:
    r = await research_continuation(valid, requested, missing_fields)
    legacy = "FINALIZE" if r["continuation"] == "sufficient" else (
        "REFINE" if r["action"] == "REFINE" else "CONTINUE")
    return {"decision": legacy, "continuation": r["continuation"], "provider": r["provider"]}


async def decide_record(record: dict, question: str = "Should this record pass?") -> dict:
    _ = question
    fields = record.get("fields", {})
    if any(isinstance(v, dict) and v.get("verification_status") == "unverified"
           for v in fields.values()):
        return {"decision": "review", "score": 0.5, "provider": "deterministic"}
    return {"decision": "accept", "score": 0.9, "provider": "deterministic"}
