"""Pay-once schema generator: URL + field spec -> proposed domain schema JSON.

Prints the proposal for human review by default; --write saves it to
backend/selectors/<domain>.json (human reviews the diff before it counts as
checked in). One polite HTTP fetch + one LLM call per run.

Usage:
  python backend/scripts/gen_selectors.py <url> '<fields-json>' [--write]
  fields-json: [{"name": "...", "type": "string", "description": "...",
                 "required": true}]
"""
import asyncio
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.providers.crawl import fetcher  # noqa: E402
from app.providers.llm import generate as llm_mod  # noqa: E402
from app.services import selectors as selectors_svc  # noqa: E402


def _validate_proposal(data: object) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
        raise ValueError("proposal lacks fields{}")
    fields = {}
    for name, spec in data["fields"].items():
        if not isinstance(spec, dict):
            continue
        sels = [s for s in (spec.get("selectors") or []) if isinstance(s, str)]
        if not sels:
            continue
        snap = spec.get("snapshot") if isinstance(spec.get("snapshot"), dict) else {}
        fields[str(name)] = {"selectors": sels[:5], "snapshot": snap}
    if not fields:
        raise ValueError("proposal has no usable field selectors")
    return {"fields": fields}


async def main() -> None:
    args = sys.argv[1:]
    write = "--write" in args
    args = [a for a in args if a != "--write"]
    if len(args) != 2:
        print(__doc__)
        raise SystemExit(2)
    url, fields_json = args
    fields = json.loads(fields_json)
    page = await fetcher.http_fetch(url)
    html = page.get("html", "")
    if not html:
        print(f"no HTML fetched ({page.get('skipped', 'empty')}); aborting")
        raise SystemExit(1)
    prompt = selectors_svc.build_schema_prompt(fields, html, url)
    out = await llm_mod.structured_generate(prompt, {"type": "object"})
    proposal = _validate_proposal(out.get("data", out))
    domain = selectors_svc.domain_of(url)
    doc = {"domain": domain, "generated_by": out.get("provider", "llm"),
           "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
           **proposal}
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    if write:
        path = selectors_svc.selectors_dir() / f"{domain}.json"
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path} ({len(proposal['fields'])} fields) — review before use")
    else:
        print(text)


if __name__ == "__main__":
    asyncio.run(main())
