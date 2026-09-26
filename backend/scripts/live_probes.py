"""Minimal live probes: 1 Gemini call (tiny schema), 1 Tavily basic search (1 credit).
Prints statuses only. No retries beyond code defaults."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.providers.llm import generate as llm_mod  # noqa: E402
from app.providers.search import tavily as tavily_mod  # noqa: E402


async def main():
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"]}
    try:
        out = await llm_mod.gemini_structured('Reply with JSON {"ok": true}.', schema)
        print(f"gemini OK provider={out['provider']} model={out['model']} data={out['data']}")
    except Exception as e:  # noqa: BLE001
        print(f"gemini FAIL {getattr(e, 'code', '?')}: {getattr(e, 'message', e)[:150]}")

    try:
        res = await tavily_mod.search("London AI startups funding", limit=2)
        first = res[0]["url"][:80] if res else None
        print(f"tavily OK results={len(res)} first={first}")
    except Exception as e:  # noqa: BLE001
        print(f"tavily FAIL {getattr(e, 'code', '?')}: {getattr(e, 'message', e)[:150]}")


if __name__ == "__main__":
    asyncio.run(main())
