"""Regenerate `contracts/api.openapi.yaml` from the live FastAPI app.

    python scripts/gen_contract.py            # print the spec to stdout
    python scripts/gen_contract.py --check    # exit 1 if the file is stale
    python scripts/gen_contract.py --write    # rewrite the file

Generated rather than hand-maintained. The previous contract had drifted in both
directions — it documented response shapes the code does not return (the export
endpoint returns `{data:{format,content}}`, not a raw attachment) while omitting
routes the code does serve and roughly thirty response fields the frontend
actually reads. A hand-edited document in that state has no way to stay true.

Run with `--write` and commit the result on its own, separate from any code
change, so a contract diff is reviewable as "the API changed" rather than being
buried in a behavioural change.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "api.openapi.yaml"


def build() -> dict:
    """The app's OpenAPI document, plus a little hand-written framing."""
    spec = app.openapi()

    # FastAPI emits `{}` for a `dict` body because there is no declared model.
    # That is honest — the handler validates by hand — but it makes the contract
    # useless for the one question a client actually has, which is what keys to
    # send. These are the bodies the routes read; see the handlers for how each
    # is interpreted.
    spec["components"] = spec.get("components", {})
    spec["components"]["schemas"] = spec["components"].get("schemas", {})
    spec["components"]["schemas"].update({
        "RunBody": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "dataset_name": {"type": "string"},
                "credit_budget": {"type": "integer", "description":
                                   "0 or absent means unlimited"},
                "reuse_stored_pages": {"type": "boolean"},
            },
            "required": ["plan_id"],
        },
        "ExportBody": {
            "type": "object",
            "properties": {
                "format": {"type": "string",
                           "enum": ["csv", "json", "jsonl", "md"],
                           "default": "json"},
                "fields": {"type": "array", "items": {"type": "string"},
                           "description": "column subset; absent means all"},
            },
        },
        "RefreshBody": {
            "type": "object",
            "properties": {
                "apply": {"type": "boolean"},
                "limit": {"type": "integer", "default": 10},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "host or URL substrings; absent means all"},
            },
        },
        "RejudgeBody": {
            "type": "object",
            "properties": {
                "apply": {"type": "boolean"},
                "field": {"type": "string"},
                "phase": {"type": "integer"},
                "judge_budget": {
                    "type": "integer", "minimum": 0,
                    "description": "cap on paid judge calls. 0 means ZERO, "
                                   "not unlimited — pass a positive number to "
                                   "allow any at all"},
            },
        },
        "GapFillBody": {
            "type": "object",
            "properties": {"apply": {"type": "boolean"},
                           "field": {"type": "string"},
                           "phase": {"type": "integer", "enum": [1, 2]}},
        },
        "DatasetQueryBody": {
            "type": "object",
            "properties": {"question": {"type": "string"},
                           "sql": {"type": "string"},
                           "limit": {"type": "integer"}},
        },
        "SelectorSaveBody": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "page_url": {"type": "string"},
                "provider": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "object"}},
                "overwrite": {"type": "boolean"},
            },
        },
        "SelectorProposeBody": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "page_id": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["url", "fields"],
        },
        "AddColumnBody": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"},
                "type": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
    })

    spec["info"] = {
        **spec.get("info", {}),
        "description": (
            "Generated by `scripts/gen_contract.py` from the live FastAPI app.\n\n"
            "Do not edit by hand: `tests/test_run_reporting.py` fails when a\n"
            "route exists here but not in the app, or the reverse.\n\n"
            "Auth: every route except `/api/health` requires `X-API-Key`, and "
            "fails closed when no key is configured.\n\n"
            "All responses use the envelope `{data, error, meta}`."
        ),
    }
    return spec


def to_yaml(spec: dict) -> str:
    """Minimal, readable YAML.

    `pyyaml` is a dependency already (the contract test parses this file), but it
    cannot emit JSON tags on arbitrary nodes without a custom representer. This
    writer keeps one key per line and quotes anything ambiguous, which is what
    makes the output reviewable in a diff.
    """
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    def _str_presenter(dumper, data):
        # Multi-line descriptions read far better as a literal block.
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    Dumper.add_representer(str, _str_presenter)
    return yaml.dump(spec, Dumper=Dumper, sort_keys=False,
                     default_flow_style=False, width=88, allow_unicode=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args()

    text = to_yaml(build())

    if args.write:
        CONTRACT.write_text(text, encoding="utf-8")
        print(f"wrote {CONTRACT} ({len(text.splitlines())} lines)")
        return 0

    if args.check:
        current = CONTRACT.read_text(encoding="utf-8")
        if current != text:
            print(f"{CONTRACT} is stale — re-run with --write", file=sys.stderr)
            return 1
        print(f"{CONTRACT} is up to date")
        return 0

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())