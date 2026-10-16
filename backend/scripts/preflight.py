"""Preflight: can this deployment actually run? Check every dependency, spend nothing.

    python scripts/preflight.py          # report and exit non-zero on a blocker
    python scripts/preflight.py --quiet  # exit code only

Exit codes: 0 usable, 1 a blocker, 2 a warning worth reading.

Every check is a real call against the real dependency, not a config read. The
whole point is that "the .env says the key is set" is not the same question as
"a request will succeed", and the previous failure mode was discovering the
difference halfway through a run: twelve fetches, three minutes, zero records,
reported as success.

Under `OPENCODE_STRICT=true` the local reader is the only reader, so an unusable
one is a blocker rather than a warning. Otherwise it degrades to a cloud
provider and is only a warning.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.providers.llm import opencode as opencode_llm  # noqa: E402
from app.repositories import factory as factory_repo  # noqa: E402

BLOCKER = "BLOCKER"
WARNING = "WARNING"
OK = "ok"


async def check_reader() -> tuple[str, str]:
    """The model that reads pages. Fatal under strict mode, a warning otherwise."""
    state = await opencode_llm.available()
    if state.get("reachable"):
        return OK, f"opencode reachable at {state.get('base_url')}"

    reason = state.get("error") or "unreachable"
    detail = opencode_llm.explain_unusable(state)

    if settings.OPENCODE_STRICT:
        return BLOCKER, detail
    # Not strict: a cloud provider takes over. Worth knowing, not worth stopping.
    return WARNING, (f"{detail} — OPENCODE_STRICT is off, so a cloud provider "
                     f"will serve the run instead")


async def check_search() -> tuple[str, str]:
    """Tavily. Without it discovery has no seeds and a run yields nothing."""
    if not (settings.TAVILY_API_KEY or "").strip():
        return BLOCKER, ("TAVILY_API_KEY is unset, so nothing can be discovered. "
                         "A run with no sources returns zero records by design.")
    from app.providers.search import tavily
    try:
        # `limit=1` — this is a liveness probe, and the point is not to spend.
        results = await asyncio.wait_for(tavily.search("datagoblin preflight", limit=1),
                                         timeout=25)
    except Exception as exc:  # noqa: BLE001
        return BLOCKER, f"Tavily did not answer: {str(exc)[:140]}"
    if not results:
        return WARNING, ("Tavily answered with no results for a deliberately "
                         "silly query. Usually fine, but the key may be wrong.")
    return OK, f"Tavily answered ({len(results)} result(s))"


async def check_store() -> tuple[str, str]:
    """The database. A run cannot report anything that was never stored."""
    try:
        repo = factory_repo.build_repo()
        datasets = repo.list_datasets()
    except Exception as exc:  # noqa: BLE001
        return BLOCKER, (f"{settings.PERSISTENCE} store is unusable: {str(exc)[:140]}")
    return OK, (f"{settings.PERSISTENCE} store reachable "
                f"({len(datasets)} dataset(s) already stored)")


async def check_pool_deps() -> tuple[str, str]:
    """Declared-vs-installed, for the packages whose absence is a raw 500."""
    missing = []
    try:
        import psycopg_pool  # noqa: F401
    except ImportError:
        missing.append("psycopg-pool")
    if settings.PERSISTENCE == "postgres" and missing:
        return BLOCKER, (f"{', '.join(missing)} not installed but PERSISTENCE="
                         f"postgres. Every database call will raise "
                         f"ModuleNotFoundError. Run: pip install -r requirements.txt")
    if missing:
        return WARNING, f"{', '.join(missing)} not installed (not needed right now)"
    return OK, "database driver pool present"


async def main() -> int:
    quiet = "--quiet" in sys.argv
    checks = [
        ("reader (opencode)", check_reader()),
        ("search (tavily)", check_search()),
        ("store", check_store()),
        ("driver pool", check_pool_deps()),
    ]

    results = []
    for name, coro in checks:
        try:
            results.append((name,) + await coro)
        except Exception as exc:  # noqa: BLE001
            results.append((name, BLOCKER, f"the check itself failed: {str(exc)[:140]}"))

    blockers = [r for r in results if r[1] == BLOCKER]
    warnings = [r for r in results if r[1] == WARNING]

    if not quiet:
        print("DATAGOBLIN preflight")
        print("=" * 62)
        for name, status, detail in results:
            mark = {OK: "  ok  ", WARNING: " warn ", BLOCKER: "BLOCK "}[status]
            print(f"[{mark}] {name:22} {detail}")
        print("-" * 62)
        print(f"strict reader: {'ON (no cloud fallback)' if settings.OPENCODE_STRICT else 'off'}")
        print(f"persistence : {settings.PERSISTENCE}")
        print(f"verdict     : "
              f"{'BLOCKED' if blockers else ('usable with warnings' if warnings else 'usable')}")

    if blockers:
        print("\nNot runnable. Fix the blockers above first.", file=sys.stderr)
        return 1
    if warnings:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))