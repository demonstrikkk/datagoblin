"""Report every non-ASCII character in app/ with its codepoint.

Run: python scripts/mojibake_scan.py [--fix]
"""
from __future__ import annotations

import pathlib
import sys

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def main() -> int:
    fix = "--fix" in sys.argv
    total = 0
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        out = []
        changed = False
        for i, line in enumerate(src.splitlines(keepends=True), 1):
            bad = {c for c in line if ord(c) > 127}
            if bad:
                total += 1
                names = ", ".join(f"U+{ord(c):04X}" for c in sorted(bad))
                print(f"{path.relative_to(APP.parent)}:{i}: {names}")
                print(f"    {line.rstrip()[:150]}")
                if fix:
                    new = line
                    # mojibake sequences only; leave legitimate typography alone
                    for seq in ("Ã¢â‚¬â€", "Ã¢â‚¬", "â€”", "â€™",
                                "â€œ", "â€\x9d"):
                        new = new.replace(seq, "")
                    new = new.replace("  ", " ")
                    if new != line:
                        changed = True
                    out.append(new)
                    continue
            out.append(line)
        if fix and changed:
            path.write_text("".join(out), encoding="utf-8")
    print(f"\n{total} line(s) with non-ASCII")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())