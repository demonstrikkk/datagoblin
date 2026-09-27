"""Free-model fan-out: ask N Zen models the same question, then compare.

Why fan out at all: free models are individually weak and mutually
inconsistent, so a single free answer is not trustworthy. Asking the same
question across the free tier and reporting *disagreement* is a better product
than one confident guess — and it costs nothing.

Three properties this service guarantees:

1. **Bounded concurrency** (`ZEN_FANOUT_CONCURRENCY`, default 4). Ten parallel
   calls would hammer the local OpenCode server and trip its rate limiter.
2. **Per-model isolation.** One model failing — including the two that return
   upstream 500 today — never fails the request. The other answers still return,
   each tagged with its own error, so the UI can show a partial result instead
   of an empty one.
3. **Provenance.** Every answer names the model that actually answered, which
   matters because the transport (direct vs opencode) and the privacy posture
   differ per model.

`consensus` is deliberately conservative. It reports agreement *structure*, not
a merged truth: a majority cluster is described, never auto-substituted for the
evidence rule in the extract pipeline. Models that agree on one wrong answer are
still wrong, so nothing here writes to a dataset.
"""
import asyncio
import json
import time
from typing import Any

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import log
from app.providers.llm import zen

#: Above this a fan-out is not worth the wall clock; the API clamps to it.
MAX_MODELS = 12


def _prompt(prompt: str, system: str | None, json_mode: bool) -> str:
    if json_mode:
        return ("Reply with a single JSON object only. No prose, no backticks.\n"
                + (prompt or ""))
    return prompt or ""


def _fold(value: Any) -> Any:
    """Fold a parsed answer into a form whose JSON encoding is comparison-stable.

    Numbers carry a `$n` tag and booleans a `$b` tag so that the number 3, the
    float 3.0, 3.0000001 and the *string* "3" cannot collapse into each other:
    rounding is what we want, type confusion is not.
    """
    if isinstance(value, bool):
        return {"$b": value}
    if isinstance(value, (int, float)):
        return {"$n": f"{round(float(value), 3):g}"}
    if isinstance(value, dict):
        return {str(k): _fold(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fold(v) for v in value]
    return value


def _canonical(value: Any) -> str:
    """Semantic key for a structured answer.

    Key order, indentation and separator spacing must never read as
    disagreement. Measured: three models all answering {"capital": "Paris"} were
    split into a 2-1 'majority' purely because one of them omitted the space
    after the colon — an agreement tool that under-reports agreement is worse
    than no consensus at all, because the noise looks like a real conflict.
    """
    return json.dumps(_fold(value), sort_keys=True, separators=(",", ":"),
                      default=str)[:400]


def _normalise(value: Any) -> str:
    """Collapse a parsed answer to a comparable key.

    Numbers are rounded to 3dp so 3.0 and 3.0000001 do not read as a conflict.
    Text that is really JSON is compared as JSON, not as typography.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{round(float(value), 3):g}"
    if isinstance(value, (dict, list)):
        return _canonical(value)
    if isinstance(value, str):
        parsed = zen.parse_json_lenient(value)
        if isinstance(parsed, (dict, list)):
            return _canonical(parsed)
        return " ".join(value.lower().split())[:400]
    return " ".join(str(value).lower().split())[:400]


def _consensus(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe agreement across successful answers. No invented truth."""
    ok = [r for r in results if r["ok"]]
    if not ok:
        return {"answered": 0, "requested": len(results),
                "agreement": None, "clusters": [],
                "note": "no model returned a usable answer"}
    # Which model ACTUALLY answered, not which one was requested. Measured: a
    # silently mis-pinned transport served every request from one model, so four
    # rows agreed 4/4 while only one model had been consulted. Agreement is only
    # independent when distinct models produced it.
    served = [(r.get("served_by") or r.get("model") or "?") for r in ok]
    answers = [r["answer"] for r in ok]
    if all(isinstance(a, (dict, list)) for a in answers):
        keys = [_normalise(a) for a in answers]
    else:
        keys = [_normalise(a if a is not None else r["text"]) for a, r in zip(answers, ok)]
    clusters: dict[str, list[str]] = {}
    for name, k in zip(served, keys):
        clusters.setdefault(k, []).append(name)
    ranked = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)
    top, top_models = ranked[0]
    answered = len(ok)
    ratio = len(top_models) / answered
    distinct = len(set(served))
    unanimous = len(clusters) == 1
    # Unanimous, but only one model ever spoke: not consensus, an echo.
    single_source = distinct == 1
    if single_source:
        label = "single-model"
    elif unanimous:
        label = "unanimous"
    elif ratio >= 0.6:
        label = "majority"
    elif ratio >= 0.4:
        label = "split"
    else:
        label = "polarised"
    note = "models agreeing is not evidence; verify against sources"
    if single_source:
        note = (f"all {answered} answers came from ONE model "
                f"({top_models[0]}) - this is not independent agreement; "
                f"verify against sources")
    return {"answered": answered, "requested": len(results),
            "agreement": label, "unanimous": unanimous and not single_source,
            "agreement_ratio": round(ratio, 3),
            "distinct_models": distinct,
            "independent": distinct > 1,
            "clusters": [{"answer": k, "models": m, "count": len(m)}
                         for k, m in ranked],
            "top_answer": top,
            "note": note}


async def _one(model: str, prompt: str, system: str | None,
               json_mode: bool) -> dict[str, Any]:
    """Ask one model. Never raises — a failure is data, not an exception."""
    started = time.perf_counter()
    row: dict[str, Any] = {"model": model, "ok": False, "text": "",
                           "answer": None, "error": "", "latency_ms": 0,
                           "provider": "", "transport": "", "served_by": "",
                           "model_mismatch": False, "attempts": 1}
    try:
        out = await zen.ask(model, _prompt(prompt, system, json_mode),
                            system=system)
        # `served_by` is the model the transport says replied. If it differs
        # from what we asked for, the row is still useful - it is labelled with
        # the truth and consensus refuses to count it as independent agreement.
        served = out.get("model") or model
        row.update(ok=True, text=out["text"][:8000], provider=out["provider"],
                   transport=out.get("transport", ""), cost=out.get("cost"),
                   usage=out.get("usage", {}), served_by=served,
                   attempts=out.get("attempts", 1),
                   model_mismatch=bool(out.get("model_mismatch") or served != model))
        row["answer"] = (zen.parse_json_lenient(out["text"]) if json_mode
                         else out["text"])
    except AppError as e:
        row["error"] = e.message[:220]
        if isinstance(e.details, dict) and e.details.get("attempts"):
            row["attempts"] = int(e.details["attempts"])
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 (one model must not sink the request)
        row["error"] = f"{type(e).__name__}: {str(e)[:180]}"
    row["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return row


async def ask_many(prompt: str, models: list[str] | None = None, *,
                   system: str | None = None, json_mode: bool = False,
                   concurrency: int | None = None) -> dict[str, Any]:
    """Fan one question out across free models.

    `models=None` means "the configured set"; an explicit `[]` means "none" and
    returns empty, so a caller that filtered everything down to zero models does
    not silently get the whole free tier.

    Returns {"results": [...], "consensus": {...}, "models": [...]} where each
    result carries its own ok/error — the caller decides what a partial answer
    is worth.
    """
    picked = [m.strip() for m in (zen.configured_models() if models is None
                                  else models) if m.strip()]
    # Registry order, de-duplicated, so output is stable regardless of input order.
    seen: set[str] = set()
    ordered = [m for m in zen.text_models() if m in picked and not (m in seen or seen.add(m))]
    extra = [m for m in picked if m not in ordered and not (m in seen or seen.add(m))]
    ordered += extra
    ordered = ordered[:MAX_MODELS]
    if not ordered:
        return {"results": [], "consensus": _consensus([]),
                "models": [],
                "note": "no models selected"}

    limit = max(1, min(concurrency or settings.ZEN_FANOUT_CONCURRENCY, MAX_MODELS))
    sem = asyncio.Semaphore(limit)

    async def _guarded(model: str) -> dict[str, Any]:
        async with sem:
            return await _one(model, prompt, system, json_mode)

    log.info("fanout start", extra={"data": {"models": len(ordered), "limit": limit}})
    results = await asyncio.gather(*(_guarded(m) for m in ordered))
    return {"results": list(results), "consensus": _consensus(list(results)),
            "models": ordered}


def catalogue() -> dict[str, Any]:
    """Free-model registry plus what the UI needs to explain availability."""
    rows = zen.free_models()
    text = [r for r in rows if r.get("kind") != "decision"]
    decision = [r for r in rows if r.get("kind") == "decision"]
    transports = sorted({r["transport"] for r in rows})
    return {
        "text_models": text,
        "decision_models": decision,
        "counts": {"text": len(text), "decision": len(decision)},
        "transports": transports,
        "configured": zen.configured_models(),
        "concurrency": settings.ZEN_FANOUT_CONCURRENCY,
        "gated_note": ("Most free models answer 403 FreeTierError on direct Zen "
                       "REST and are reachable only through the local OpenCode "
                       "server."),
    }
