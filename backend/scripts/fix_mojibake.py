"""Repair double-encoded (mojibake) text in app/ source, without a full rewrite.

Run: python scripts/fix_mojibake.py [--check]

Scope is deliberately narrow. The corruption pattern here is UTF-8 bytes decoded
as cp1252 and written back out, so `—` (U+2014, bytes E2 80 94) became
`â€”` (U+00E2 U+20AC U+201D). That is one round of mojibake, and reversing it is
`text.encode('cp1252').decode('utf-8')`.

Two things this script must NOT do:

1. Touch `normalizer.py`. Its mojibake literals ARE the detector's pattern
   table — `("Ã", "Â", "â€", "â€™", ...)` is what the module matches against.
   "Fixing" those would disable the very feature that cleans stored data.

2. Rewrite legitimate typography. Em dashes, curly quotes and NBSP in prose
   comments and docstrings are correct UTF-8, not corruption. A blanket
   `ftfy`-style pass would flatten 400 lines of good comments to satisfy a check
   that was measuring the wrong thing.

So: only lines containing a character that cannot occur in intentional English
source (U+00C3 A-tilde, U+00E2 a-circumflex, U+00C2, U+00EF, U+00BF, U+009D, and
U+20AC which is the giveaway middle of an `â€` run) get touched, and only the
mojibake runs inside them are rewritten.
"""
from __future__ import annotations

import pathlib
import sys

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: Never rewrite these: their mojibake sequences are the detection patterns.
EXEMPT = {"normalizer.py"}

#: Characters that mark a cp1252 decode of UTF-8 bytes. A-tilde and a-circumflex
#: and friends are never authored deliberately in Python source.
MOJIBAKE_MARKS = "ÃÂâðÐÑï¿"


def _repair(text: str) -> tuple[str, bool]:
    """Undo one round of cp1252-mojibake, longest sequences first."""
    changed = False
    # Try repeated passes: double-encoded text needs two repairs to become clean.
    for _ in range(2):
        before = text
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch in MOJIBAKE_MARKS:
                # Greedily take the maximal run that round-trips through cp1252.
                j = i
                while j < n:
                    try:
                        text[j].encode("cp1252")
                    except UnicodeEncodeError:
                        break
                    j += 1
                run = text[i:j]
                try:
                    fixed = run.encode("cp1252").decode("utf-8")
                    out.append(fixed)
                    i = j
                    continue
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass
            out.append(ch)
            i += 1
        text = "".join(out)
        if text != before:
            changed = True
        elif not any(c in text for c in MOJIBAKE_MARKS):
            break
    return text, changed


def main() -> int:
    check = "--check" in sys.argv
    dirty: list[tuple[pathlib.Path, int, str]] = []

    for path in sorted(APP.rglob("*.py")):
        if path.name in EXEMPT:
            continue
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines(keepends=True)
        out: list[str] = []
        for i, line in enumerate(lines, 1):
            if any(c in line for c in MOJIBAKE_MARKS):
                fixed, changed = _repair(line)
                if changed:
                    if check:
                        dirty.append((path, i, line.rstrip()))
                    else:
                        out.append(fixed)
                        continue
            out.append(line)
        if not check and out != lines:
            path.write_text("".join(out), encoding="utf-8")
            print(f"rewrote {path.relative_to(APP.parent)}")

    if check:
        for path, i, line in dirty:
            print(f"{path.relative_to(APP.parent)}:{i}: {line.strip()[:110]}")
        print(f"\n{len(dirty)} mojibake line(s) remaining")
        return 1 if dirty else 0

    # Verify.
    remaining = 0
    for path in sorted(APP.rglob("*.py")):
        if path.name in EXEMPT:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(c in line for c in MOJIBAKE_MARKS):
                print(f"STILL BAD {path.relative_to(APP.parent)}:{i}")
                remaining += 1
    print(f"\n{remaining} line(s) still mojibake")
    return 1 if remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())