"""Diagnose live extraction on one URL. Prints clean-text head, raw LLM head, result."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.providers.crawl import fetcher  # noqa: E402
from app.providers.llm import generate as llm_mod  # noqa: E402
from app.services import extractor as extractor_mod  # noqa: E402
from app.services import reducer as reducer_mod  # noqa: E402

import os

URL = sys.argv[1] if len(sys.argv) > 1 else "https://topstartups.io?industries=Artificial+Intelligence"
METHOD = os.getenv("DIAG_METHOD", "http")


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if METHOD == "crawl4ai":
        page = await fetcher.crawl4ai_fetch(URL)
        clean = page.get("markdown", "")
    else:
        page = await fetcher.http_fetch(URL)
        clean = page.get("markdown") or reducer_mod.reduce_html(page.get("html", ""))
    print(f"clean={len(clean)} chars head={clean[:200]!r}")
    plan = {"fields": [
        {"name": "company_name", "type": "string", "description": "Name", "required": True},
        {"name": "founder", "type": "string", "description": "Founder", "required": False}]}
    prompt = extractor_mod.build_prompt(plan, page["url"], page.get("title", ""), clean)
    print(f"prompt chars={len(prompt)}")
    out = await llm_mod.groq_structured(prompt, {"type": "object"})
    data = out.get("data", {})
    print(f"provider={out.get('provider')} topkeys={list(data)[:5] if isinstance(data, dict) else type(data)}")
    print("raw head:", str(data)[:500])
    recs = extractor_mod.coerce_output(out, page["url"])
    print(f"coerced records={len(recs)}")


if __name__ == "__main__":
    asyncio.run(main())
