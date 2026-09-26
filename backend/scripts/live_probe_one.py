"""Single-shot provider checks. One call per provider max. Prints status only."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import settings  # noqa: E402


async def check_groq_model():
    from groq import Groq

    def _call():
        return Groq(api_key=settings.GROQ_API_KEY, max_retries=0,
                    timeout=20).chat.completions.create(
            model=settings.GROQ_FALLBACK_MODEL,
            messages=[{"role": "user",
                       "content": 'Reply with JSON {"ok": true}. Output ONLY the JSON object.'}],
            response_format={"type": "json_object"})

    try:
        r = await asyncio.wait_for(asyncio.to_thread(_call), timeout=30)
        print("groq OK:", (r.choices[0].message.content or "")[:80])
    except Exception as e:  # noqa: BLE001
        print("groq FAIL:", type(e).__name__, str(e)[:200])


async def check_jev_openrouter():
    import httpx
    from app.providers.decision.jev import _key
    key = _key()
    if not key:
        print("jev SKIP: OPENROUTER_API_KEY unset")
        return
    body = {"model": settings.JEV_MODEL,
            "state": "Acme AI raised funding news article text here.",
            "questions": {"relevant": {
                "type": "choice",
                "instructions": "Is the source likely relevant to Acme AI?",
                "criteria": {"YES": "about the entity", "NO": "unrelated",
                             "UNCERTAIN": "cannot tell"}}}}
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post("https://openrouter.ai/api/alpha/decisions",
                             headers={"Authorization": f"Bearer {key}",
                                      "Content-Type": "application/json"},
                             json=body)
        print(f"jev HTTP {r.status_code}")
        data = r.json() if r.status_code == 200 else {}
        ans = (data.get("answers") or {})
        print("answer keys:", list(ans)[:5], "| payload:", str(ans)[:300])
    except Exception as e:  # noqa: BLE001
        print("jev FAIL:", type(e).__name__, str(e)[:150])


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "groq"):
        asyncio.run(check_groq_model())
    if which in ("all", "jev"):
        asyncio.run(check_jev_openrouter())
