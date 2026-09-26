"""LangChain seam — exactly one: schema-enforced supervisor strategy decisions.

`decide()` returns {"data": SupervisorDecision-dict, "provider": "langchain:<which>"}.
Groq primary -> Gemini fallback (current order); total failure raises (caller falls
back to the raw injected llm, then to deterministic break). Imports are
function-local so the package is optional at runtime.
"""
from typing import Any


def _model(primary: bool) -> Any:
    from langchain.chat_models import init_chat_model
    from app.core.config import settings
    if primary:
        return init_chat_model(settings.GROQ_FALLBACK_MODEL, model_provider="groq")
    return init_chat_model(settings.GEMINI_PLAN_MODEL, model_provider="google_genai")


async def decide(prompt: str, max_queries: int, used_queries: list[str]) -> dict:
    """Schema-enforced SupervisorDecision via LangChain structured output."""
    from app.agents.decisions import SupervisorDecision
    from app.core.config import settings
    if not settings.GEMINI_API_KEY and not settings.GROQ_API_KEY:
        raise RuntimeError("langchain decide: no LLM keys")
    last_err: Exception | None = None
    for primary in (True, False):
        if primary and not settings.GROQ_API_KEY:
            continue
        if not primary and not settings.GEMINI_API_KEY:
            continue
        try:
            import asyncio

            async def _attempt() -> dict:
                structured = _model(primary).with_structured_output(SupervisorDecision)
                out = await structured.ainvoke(prompt[:4000])
                data = out if isinstance(out, dict) else out.model_dump()
                dec = SupervisorDecision(**data).validated(
                    {"max_queries": max_queries, "used_queries": used_queries})
                return {"data": dec.model_dump(),
                        "provider": f"langchain:{'groq' if primary else 'gemini'}"}
            # Whole attempt (model construction + call) bounded: construction
            # itself can stall on network without this.
            return await asyncio.wait_for(_attempt(), timeout=75)
        except Exception as e:  # noqa: BLE001 (fallback across providers)
            last_err = e
    raise last_err if last_err else RuntimeError("langchain decide failed")
