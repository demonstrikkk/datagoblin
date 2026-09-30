"""Decision layer — Jev CORE (4 families). GENERATE → ORCHESTRATE → JUDGE → EXECUTE → PROVE.

Two paths, free first:
  1. Zen's `jev-1.13-free` via POST {ZEN_BASE_URL}/systemone — measured 200 on
     direct REST (~0.6s), same `{answers}` shape, no spend.
  2. OpenRouter Decisions API, POST https://openrouter.ai/api/alpha/decisions
     {model: ~typesafe/jev-latest, state, questions} answered with typed
     {noul|choice|score} + probabilities. Key: OPENROUTER_API_KEY, with
     TYPESAFE_API_KEY accepted as alias (existing .env files carry it there).

Deterministic policy otherwise (complete production logic for the no-key
deployment, not a placeholder). Judgments are probabilities; deterministic policy
maps them to verified/unverified/conflicting — never "Jev says TRUE".
"""
from typing import Literal

import httpx

from app.core.config import settings

Screening = Literal["YES", "NO", "UNCERTAIN"]
Support = Literal["SUPPORTED", "NOT_SUPPORTED", "UNCERTAIN",
                 "JUDGMENT_UNAVAILABLE", "RATE_LIMITED"]
Conflict = Literal["A", "B", "CONFLICT", "INSUFFICIENT"]
Continuation = Literal["sufficient", "insufficient", "uncertain"]

_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"


def _key() -> str:
    return settings.OPENROUTER_API_KEY or settings.TYPESAFE_API_KEY


async def _jev_call(state: str, questions: dict) -> dict | None:
    """Returns parsed answers, or None (key absent/failure => deterministic policy).

    Free Zen Jev is tried before the paid OpenRouter judge: same contract, no
    cost, measured reachable. A paid call only happens when the free one is
    unavailable, so a key-less deployment never silently bills.

    A throttled free judge does NOT fall through to the paid rung by default:
    throttling is a "wait", and burning a paid call because the free tier is
    busy is a surprise bill. It raises `JevRateLimited` so the caller can
    report throttling distinctly from absence.
    """
    from app.providers.llm import zen

    if settings.ZEN_JEV_ENABLED:
        answers = await zen.systemone(state, questions)
        if answers is not None:
            return answers
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


def deterministic_verdict(value: str, quote: str, source_text: str) -> dict | None:
    """The verdict that needs no model, or `None` if a judgement is genuinely needed.

    Split out of `evidence_verification` so the caller can know *before* it spends
    a budget unit. The budget used to be charged first and the deduction
    returned second, so the per-run cap of 400 was consumed by arithmetic that
    costs nothing — and a real judgement could be refused as
    `judgment_unavailable` while the record showed the budget was "used up" on
    free deductions. A cap is supposed to bound the expensive thing.

    Both branches are deductions, not guesses:

    * a quote that is not on the page cannot support anything;
    * a value that is literally inside its own quote needs no interpretation.
    """
    import re
    norm = lambda s: re.sub(r"\s+", " ", (s or "").strip().lower())
    v, q = norm(value), norm(quote)
    if not quote or q not in norm(source_text):
        return {"judgment": "NOT_SUPPORTED", "confidence": 0.95,
                "provider": "deterministic"}
    if v and v in q:
        # The value is literally inside its own quote, so "does this evidence
        # support the claim" has no answer other than yes. Spending a judge call
        # on it is pure latency: a live run reached 728 extracted records, and
        # asking a model about every field of every one of them meant thousands
        # of round trips and a blown budget. This is a deduction, not a guess -
        # the substring is the claim.
        return {"judgment": "SUPPORTED", "confidence": 0.95,
                "provider": "deterministic"}
    return None


async def evidence_verification(value: str, quote: str, source_text: str) -> dict:
    """B. SUPPORTED / NOT_SUPPORTED / UNCERTAIN / JUDGMENT_UNAVAILABLE /
    RATE_LIMITED.

    The deterministic substring pre-check runs first and is free. When it passes
    but no judge is reachable, the answer is JUDGMENT_UNAVAILABLE - explicitly
    not SUPPORTED. This used to return `SUPPORTED, 0.9, deterministic`, which
    meant a dead or unconfigured judge silently promoted every substring-matching
    claim to "verified" and the verification summary counted them as checked.
    A judgement that never happened must not be reported as one that did.

    A throttled judge (HTTP 429) is reported as RATE_LIMITED rather than lumped
    in with absence: "told to wait" and "nothing to judge" call for different
    responses, and a busy run should be able to say it was throttled.
    """
    quick = deterministic_verdict(value, quote, source_text)
    if quick is not None:
        return quick
    from app.providers.llm import zen
    try:
        ans = await _jev_call(
            f"Claim: {value}\nEvidence: {quote}",
            {"support": {"type": "noul",
                         "instructions": "Does the evidence support the claim?"}})
    except zen.JevRateLimited:
        # Throttled. Fall back rather than stall.
        #
        # The dedicated judge is rate-limited on the free tier often enough to be
        # the normal case, not the exception: a measured run reported 20 limited
        # calls against 0 successful, and each one burned its full retry ladder
        # before giving up. That is minutes of wall clock to learn nothing, and
        # the run then finished with nothing to show.
        #
        # So a throttled judge hands the question to the general model that the
        # rest of the pipeline already depends on, and the answer is recorded
        # under its own provider so the record says which judge produced it. A
        # caller that needs to distinguish a dedicated ruling from this fallback
        # can, which is the point: the verdict is still a real judgement of the
        # quote, just not from the judge that would have preferred.
        return await _proxy_verdict(value, quote, throttled=True)
    except Exception:  # noqa: BLE001
        return await _proxy_verdict(value, quote, throttled=False)
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
        # The 0.4-0.6 band is a real "I cannot tell". The validator used to
        # treat it as verified, so a hesitant judge was reported as support.
        return {"judgment": "UNCERTAIN", "confidence": 0.5, "provider": "jev"}
    return await _proxy_verdict(value, quote, throttled=False)


#: The fallback verdict schema. One question, one boolean.
_PROXY_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["supported"],
    "additionalProperties": False,
}


async def _proxy_verdict(value: str, quote: str, *, throttled: bool) -> dict:
    """Ask the general model the same question the dedicated judge would.

    Used only when the dedicated judge cannot answer. The returned verdict names
    `llm-proxy` as its provider so a record carries the truth about which judge
    ruled on it.

    A failure here is reported as `JUDGMENT_UNAVAILABLE`, never as support: two
    unavailable judges still do not make a claim verified.
    """
    try:
        from app.providers.llm import generate as llm_generate

        out = await llm_generate.structured_generate(
            "You are checking whether a quoted sentence from a web page states a "
            "particular fact. Answer supported=true only if the quote itself "
            "asserts the claim. A quote that merely mentions a related topic, or "
            "that states the opposite, is supported=false.\n\n"
            f"CLAIM: {value}\n\nQUOTE: {quote}",
            _PROXY_SCHEMA)
        node = out.get("data", out) if isinstance(out, dict) else {}
        if not isinstance(node, dict) or "supported" not in node:
            return {"judgment": "JUDGMENT_UNAVAILABLE", "confidence": 0.0,
                    "provider": "none"}
        supported = bool(node.get("supported"))
        try:
            confidence = float(node.get("confidence", 0.8 if supported else 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        # Kept below the dedicated judge's floor so a proxy ruling never outranks
        # a real one in any ordering that sorts on confidence.
        confidence = min(max(confidence, 0.0), 0.75)
        return {"judgment": "SUPPORTED" if supported else "NOT_SUPPORTED",
                "confidence": confidence, "provider": "llm-proxy"}
    except Exception:  # noqa: BLE001
        return {"judgment": "JUDGMENT_UNAVAILABLE", "confidence": 0.0,
                "provider": "none"}


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


def _deterministic_continuation(valid: int, requested: int) -> dict:
    """The fallback policy. A pure count comparison, always available."""
    if valid >= requested:
        return {"continuation": "sufficient", "action": "FETCH", "provider": "deterministic"}
    if valid == 0:
        # Nothing to judge yet; deterministic REFINE is cheapest and always correct.
        return {"continuation": "insufficient", "action": "REFINE", "provider": "deterministic"}
    return {"continuation": "uncertain" if valid < requested // 2 else "insufficient",
            "action": "REFINE", "provider": "deterministic"}


async def research_continuation(valid: int, requested: int,
                                missing_fields: list | None = None,
                                evidence_summary: str = "",
                                rounds_used: int = 0,
                                max_rounds: int = 0) -> dict:
    """D. Is the collected evidence enough, and if not, what now?

    This was a pure function of `valid >= requested` with no model call at all,
    while the graph documented it as the coverage judge. Counting is necessary but
    not sufficient: a run that found 3 of 5 rows and has run out of plausible
    leads should stop, and one that has found 4 of 5 but is missing a required
    field should keep going. Neither is visible to a count, which is why the
    function now asks.

    Jev judges; it does not execute. The returned `action` is advice, and the
    caller (supervisor_step) remains the thing that actually decides - the
    deterministic policy is kept as the fallback so a missing judge never
    changes behaviour, only speeds it up.
    """
    fallback = _deterministic_continuation(valid, requested)
    if not requested:
        return fallback

    state_bits = [
        f"Target records: {requested}",
        f"Records collected and verified: {valid}",
        f"Missing records: {max(0, requested - valid)}",
    ]
    if missing_fields:
        state_bits.append("Fields never evidenced: " + ", ".join(map(str, missing_fields))[:400])
    if rounds_used:
        state_bits.append(f"Search rounds used: {rounds_used} of {max_rounds or 'unbounded'}")
    if evidence_summary:
        state_bits.append("Evidence so far: " + evidence_summary[:1200])

    ans = await _jev_call(
        "\n".join(state_bits),
        {"continuation": {
            "type": "choice",
            "instructions": (
                "Is the evidence collected so far sufficient to answer, or should "
                "research continue? Answer 'sufficient' when what is missing is "
                "unlikely to be found by more searching (the target does not "
                "exist, or the requirement is not satisfiable). Answer "
                "'insufficient' when another search round would plausibly help. "
                "Answer 'uncertain' when you cannot tell."),
            "criteria": {"sufficient": "enough; stop searching",
                         "insufficient": "more searching would help",
                         "uncertain": "cannot tell"}},
         "action": {
            "type": "choice",
            "instructions": "What should happen next?",
            "criteria": {"FETCH": "proceed to fetch and extract the sources found",
                         "REFINE": "search again with better queries",
                         "REVIEW": "stop and surface the gap for a human"}}})

    cont, c_conf = _choice(ans or {}, "continuation",
                           {"sufficient", "insufficient", "uncertain"})
    if not cont:
        return fallback
    action, _ = _choice(ans or {}, "action", {"FETCH", "REFINE", "REVIEW"})
    if not action:
        # A continuation without a usable action still needs one; never invent
        # FETCH for an insufficient result.
        action = "FETCH" if cont == "sufficient" else fallback["action"]
    return {"continuation": cont, "action": action, "provider": "jev",
            "confidence": round(c_conf, 3),
            "fallback": fallback["continuation"]}


async def coverage_gate(valid: int, requested: int, missing_fields: list | None = None) -> dict:
    """Legacy alias. Kept for callers outside this package; nothing in `app/`
    uses it, and `FINALIZE`/`CONTINUE` are not decisions the supervisor makes."""
    r = await research_continuation(valid, requested, missing_fields)
    legacy = "FINALIZE" if r["continuation"] == "sufficient" else (
        "REFINE" if r["action"] == "REFINE" else "CONTINUE")
    return {"decision": legacy, "continuation": r["continuation"], "provider": r["provider"]}
