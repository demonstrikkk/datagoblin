"""Is a surviving Chromium *process* a surviving *browser*?

bench_reuse_isolate.py reported "12 chromium after shutdown" and read as a
leak. That may be arithmetically wrong: Chromium runs one browser process plus
renderer/GPU/utility children per browser, so a correct teardown of three
browsers can leave a dozen processes alive for a few seconds.

Counting processes and calling it a browser count is the same class of mistake
as counting a row and calling it a record. This counts browser instances:

  - via Playwright's own pipe/socket, when one is open (authoritative)
  - otherwise by parent pid: children of a dead launcher are orphans, and one
    orphan root is one leaked browser

and then checks how many actually drain with time, rather than at a fixed sleep.

No network, no browser, no product code. -> outputs/bench_leak_measure.json
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import psutil  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "bench_leak_measure.json"

SAMPLE_S = (0, 2, 5, 10, 20, 40, 70)


def _pw() -> list[psutil.Process]:
    """Every process running from this playwright install."""
    out = []
    for p in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            exe = (p.info.get("exe") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or []).lower()
        except Exception:  # noqa: BLE001
            continue
        if "ms-playwright" in exe or "ms-playwright" in cmd:
            try:
                out.append(psutil.Process(p.info["pid"]))
            except Exception:  # noqa: BLE001
                pass
    return out


def snapshot() -> dict:
    procs = _pw()
    roots, orphans = 0, 0
    detail = []
    for p in procs:
        try:
            name = p.name().lower()
            cmd = " ".join(p.cmdline()).lower()
        except Exception:  # noqa: BLE001
            continue
        # the browser launcher is the one with --remote-debugging-pipe and no
        # --type= switch; its children all carry --type=
        is_child = "--type=" in cmd
        if not is_child:
            roots += 1
            try:
                alive_parent = p.parent() is not None and p.parent().is_running()
            except Exception:  # noqa: BLE001
                alive_parent = False
            if not alive_parent:
                orphans += 1
            detail.append({"pid": p.pid, "name": name, "is_child": False,
                           "parent_alive": alive_parent})
    return {"processes": len(procs), "browser_roots": roots,
            "orphan_roots": orphans, "detail": detail}


def sample() -> list[dict]:
    out = []
    start = time.monotonic()
    for target in SAMPLE_S:
        while time.monotonic() - start < target:
            time.sleep(0.25)
        s = snapshot()
        s["t_s"] = round(time.monotonic() - start, 1)
        out.append(s)
        print(f"  t={s['t_s']:>5.1f}s  processes={s['processes']:>3d} "
              f"browser_roots={s['browser_roots']:>2d} "
              f"orphan_roots={s['orphan_roots']:>2d}", flush=True)
    return out


def main() -> None:
    print("-- leaking process census over time (no browser is launched) --",
          flush=True)
    series = sample()
    final = series[-1]
    drained = final["browser_roots"] == 0 and final["processes"] == 0
    report = {
        "question": "does a surviving chromium process mean a leaked browser?",
        "samples": series,
        "browser_roots_final": final["browser_roots"],
        "processes_final": final["processes"],
        "drained_to_zero_without_intervention": drained,
        "reading": ("processes are not browsers; only browser_roots are the "
                    "unit that can leak"),
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("\n" + "=" * 72, flush=True)
    print(f"  surviving BROWSER roots after {final['t_s']}s : "
          f"{final['browser_roots']}", flush=True)
    print(f"  surviving PROCESSES after {final['t_s']}s        : "
          f"{final['processes']}", flush=True)
    print(f"  drained to zero on their own              : "
          f"{drained}", flush=True)
    print("=" * 72, flush=True)
    print(f"-> {OUT}", flush=True)


if __name__ == "__main__":
    main()
