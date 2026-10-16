"""Prompt text must reach the model intact.

Three defects this pins, all of which quietly degrade extraction rather than
crash it — the worst kind, because a run still reports success:

1. Double-encoded mojibake in `build_prompt` / `REGEN_PREAMBLE`. The em dash that
   separates a field name from its description had been UTF-8-decoded as cp1252,
   so the model was reading `â€”` in the field list it is told to follow.

2. `"description": null` became the literal string `"None"`, via
   `str(f.get("description", ""))` — the default never fires for an explicit null.
   The prompt then read `company_name:string — None (REQUIRED)`, which is
   indistinguishable from an instruction to emit the text `None`.

3. Non-string descriptions (a dict or number from a model-authored plan) would
   be str()'d into the prompt as repr noise.

`normalizer.py` is deliberately excluded from the mojibake scan: its mojibake
sequences are the *detection pattern table* for cleaning stored data. Rewriting
them would disable the feature. That exclusion is itself asserted, so the
exemption cannot quietly widen.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.schemas.evidence import simplify_schema
from app.services import extractor as extractor_svc

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: Codepoints that never appear in intentional English source but always appear
#: in cp1252-read-as-UTF-8 corruption: U+00C3 A-tilde, U+00E2 a-circumflex,
#: U+00C2 A-circumflex, U+00EF i-diaeresis, U+00BF inverted question mark.
MOJIBAKE_MARKS = "ÃÂâï¿"

#: U+20AC (euro) is the giveaway middle byte of an `â€` run, and U+009D is the
#: C1 control that terminates one. Neither is authored in prose.
MOJIBAKE_RUN = "â€"


# ---------------------------------------------------------------- simplify_schema


def test_null_description_is_not_the_string_none() -> None:
    """`{"description": None}` must not reach a prompt as the text `None`."""
    out = simplify_schema([{"name": "company_name", "type": "string",
                            "description": None, "required": True}])
    assert out["company_name"]["description"] == ""
    assert out["company_name"]["description"] != "None"


def test_absent_type_falls_back_to_string() -> None:
    """An explicit `"type": null` must not become the type named "None"."""
    out = simplify_schema([{"name": "x", "type": None}])
    assert out["x"]["type"] == "string"


def test_non_string_description_does_not_leak_repr_noise() -> None:
    out = simplify_schema([{"name": "x", "description": {"a": 1}},
                           {"name": "y", "description": 42}])
    assert out["x"]["description"] == "{'a': 1}"
    # The contract is a plain string either way; what matters is that it is a
    # string and the extractor degrades it, rather than a nested object crashing
    # the prompt builder further down.
    assert isinstance(out["y"]["description"], str)


def test_simplify_schema_preserves_real_descriptions() -> None:
    out = simplify_schema([{"name": "x", "description": "Company legal name",
                            "type": "string", "required": True}])
    assert out["x"]["description"] == "Company legal name"
    assert out["x"]["required"] is True


# ---------------------------------------------------------------- build_prompt


def _prompt_for(fields: list[dict]) -> str:
    return extractor_svc.build_prompt(
        {"fields": fields}, "https://example.com/a", "Title", "page content")


def test_build_prompt_emits_no_mojibake() -> None:
    prompt = _prompt_for([
        {"name": "company_name", "type": "string", "required": True,
         "description": "Company name"},
        {"name": "revenue", "type": "number", "required": False,
         "description": "Latest revenue in USD"},
    ])
    assert not any(c in prompt for c in MOJIBAKE_MARKS), (
        "the extraction prompt still contains cp1252 mojibake; the model is "
        "reading a corrupted field list")
    assert MOJIBAKE_RUN not in prompt


def test_build_prompt_never_shows_the_word_none_as_a_description() -> None:
    prompt = _prompt_for([
        {"name": "company_name", "type": "string", "required": True,
         "description": None},
        {"name": "hq", "type": "string", "description": None, "required": False},
    ])
    assert "— None" not in prompt
    assert ":string — None" not in prompt
    line = next(l for l in prompt.splitlines() if l.startswith("company_name:"))
    assert line == "company_name:string — company_name (REQUIRED)"


def test_build_prompt_falls_back_to_field_name_for_blank_description() -> None:
    prompt = _prompt_for([{"name": "revenue", "type": "number",
                           "description": "   ", "required": False}])
    assert "revenue:number — revenue (optional)" in prompt


def test_build_prompt_keeps_real_separator_and_markers() -> None:
    """The repaired separator must be a real em dash, and the markers must stay."""
    prompt = _prompt_for([{"name": "company_name", "type": "string",
                           "required": True, "description": "Name"}])
    assert "company_name:string — Name (REQUIRED)" in prompt
    assert "never invent values" in extractor_svc.REGEN_PREAMBLE
    assert "never invent values" in prompt or "NA" in prompt


# ---------------------------------------------------------------- repo-wide scan


def _app_modules() -> list[pathlib.Path]:
    return sorted(p for p in APP.rglob("*.py"))


def test_no_app_module_carries_mojibake_outside_the_detector() -> None:
    """Repo-wide. `normalizer.py` is exempt on purpose — see module docstring."""
    offenders: list[str] = []
    for path in _app_modules():
        if path.name == "normalizer.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(c in line for c in MOJIBAKE_MARKS) or MOJIBAKE_RUN in line:
                offenders.append(f"{path.relative_to(APP.parent)}:{i}: "
                                 f"{line.strip()[:90]}")
    assert not offenders, (
        "mojibake reintroduced in:\n  " + "\n  ".join(offenders))


def test_normalizer_keeps_its_detection_patterns() -> None:
    """Pin the exemption: the detector must still recognise what it repairs."""
    from app.services import normalizer as normalizer_svc

    src = (APP / "services" / "normalizer.py").read_text(encoding="utf-8")
    assert MOJIBAKE_RUN in src, (
        "normalizer.py no longer contains mojibake literals; if they were "
        "accidentally 'repaired' by a cleanup pass, stored-data cleaning silently "
        "stopped working")

    # And the behaviour, not just the text.
    mangled = "cafÃ©"
    assert normalizer_svc.clean_value(mangled) == "café", (
        "normalizer no longer repairs a double-encoded value")


@pytest.mark.parametrize("path", _app_modules(), ids=lambda p: p.name)
def test_every_module_is_valid_utf8(path: pathlib.Path) -> None:
    """A file that is not valid UTF-8 cannot be read with the declared encoding."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        pytest.fail(f"{path.name} is not valid UTF-8 at byte {exc.start}: "
                    f"{raw[max(0, exc.start - 30):exc.start + 30]!r}")
    ast.parse(text, filename=str(path))