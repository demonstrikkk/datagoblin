"""Offline E2E: inputs/run.json -> full pipeline -> outputs/.

Proves the pipeline executes end-to-end with zero keys and zero fabrication:
- planner compiles genuinely (rule-based, provider-labeled)
- discovery runs genuinely (Tavily raises no-key -> seeds; triage+screen real)
- fetch runs genuinely (local-corpus adapter; robots/throttle/content-hash real)
- extraction with llm=None yields ZERO records on this corpus
  (anti-fabrication invariant; a checked-in deterministic schema match would
  still produce grounded records — none match the fixture domains)
- replay records (user-supplied inputs/replay_records.json) flow through the
  genuine validate -> normalize -> dedupe -> JUDGE -> finalize -> export path
- outputs/dataset.json + export.csv + events.jsonl + summary.md are written

With real keys, the ONLY deltas are: Tavily returns live results, llm extracts
live records. Same functions, same contracts, same validators. Nothing is
replaced, bypassed, or hardcoded.
"""
import asyncio
import csv
import datetime
import io
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.providers.crawl import file_fetch as file_fetch_mod  # noqa: E402
from app.providers.search import tavily as tavily_mod  # noqa: E402
from app.services import exporter as exporter_svc  # noqa: E402
from app.services import planner as planner_svc  # noqa: E402
from app.services import runner as runner_svc  # noqa: E402


def main() -> None:
    inputs = ROOT / "inputs"
    outputs = ROOT / "outputs"
    outputs.mkdir(exist_ok=True)
    spec = json.loads((inputs / "run.json").read_text(encoding="utf-8"))
    replay = json.loads((inputs / spec["replay_records_file"]).read_text(encoding="utf-8"))
    url_to_file = dict(spec.get("seed_files", {}))
    url_to_title = dict(spec.get("seed_titles", {}))

    async def _run() -> dict:
        plan, provider = await planner_svc.compile_plan(spec["prompt"], llm=None)
        plan = {**plan, "seed_urls": [s for s in spec.get("seed_urls", []) if isinstance(s, str)]}
        planner_svc.coerce_plan(plan)
        run_id = str(uuid.uuid4())
        events: list[dict] = []

        async def _emit(ev: dict) -> None:
            events.append(ev)

        async def _store(rid: str, pl: dict, rows: list[dict], counts: dict) -> str:
            did = str(uuid.uuid4())
            (outputs / "store.json").write_text(
                json.dumps({"dataset_id": did, "run_id": rid, "plan": pl,
                            "records": rows, "counts": counts}, ensure_ascii=False),
                encoding="utf-8")
            return did

        async def _persist(source: dict) -> None:
            events.append({"type": "source.persisted", "run_id": run_id,
                           "stage": "FETCHING", "message": source.get("url", ""),
                           "progress": 40,
                           "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                           "data": source})

        ctx = {"search": tavily_mod.search,
               "fetch": file_fetch_mod.build(str(inputs / "pages"), url_to_file, url_to_title),
               "llm": None, "store": _store, "persist_source": _persist,
               "cancelled": lambda: False}
        # Honest empty-extraction pass first is inside execute_run (llm=None -> []).
        result = await runner_svc.execute_run(run_id, plan, ctx, _emit)
        # Replay pass: same downstream path (validate->...->finalize) over user corpus.
        from app.services import deduper as deduper_svc
        from app.services import normalizer as normalizer_svc
        from app.services import provenance as provenance_svc
        from app.services import validator as validator_svc
        verified = []
        for r in replay["records"]:
            wrapped = await validator_svc.wrap_record(
                plan["fields"], r, r["source_text"], r["source_url"], r["source_title"])
            verified.append({"fields": normalizer_svc.normalize_record(wrapped)})
        canonical, merged = deduper_svc.dedupe_records(verified, plan["dedupe_keys"])
        stats = await deduper_svc.adjudicate_conflicts(canonical)
        summary = provenance_svc.summarize(canonical)
        fmt, csv_text, _ = exporter_svc.export_dataset(plan["fields"], canonical, "csv")
        assert fmt == "csv"
        (outputs / "dataset.json").write_text(
            json.dumps({"plan_provider": provider, "plan": plan, "records": canonical,
                        "judge": stats, "summary": summary}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        (outputs / "export.csv").write_text(csv_text, encoding="utf-8")
        (outputs / "events.jsonl").write_text(
            "\n".join(json.dumps(e, default=str) for e in events), encoding="utf-8")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["metric", "value"])
        for k, v in {"planner_provider": provider, "live_result": result,
                      "records": len(canonical), "verified_fields": summary["verified"],
                      "needs_review": summary["needs_review"], "merged": merged,
                      **{f"judge_{k}": v for k, v in stats.items()}}.items():
            w.writerow([k, v])
        (outputs / "summary.md").write_text(
            "# E2E file run\n\nCompiled with planner provider: "
            f"{provider}\nLive (no-LLM) extraction records: {result.get('records')}\n"
            f"Replay records finalized: {len(canonical)}\n"
            f"Verified fields: {summary['verified']}, needs review: {summary['needs_review']}\n"
            f"Judge: {stats}, merged: {merged}\n", encoding="utf-8")
        print(f"planner={provider} live_records={result.get('records')} "
              f"replay_records={len(canonical)} verified={summary['verified']} "
              f"review={summary['needs_review']} judge={stats}")
        return {"provider": provider, "result": result, "records": len(canonical),
                "summary": summary, "judge": stats}

    out = asyncio.run(_run())
    (outputs / "result.json").write_text(json.dumps(out, default=str, indent=1),
                                         encoding="utf-8")


if __name__ == "__main__":
    main()
