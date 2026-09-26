# 04 — USER FLOWS

## Flow A — New Collection (primary)

New Collection → Prompt → Generate Plan → Review Plan → Run → Live Progress → Dataset → Inspect Evidence → Export

Screens: Command Center (`/new`) → Plan preview (inline) → Execution (`/runs/:id`: timeline + sources + counters + activity) → Dataset Studio (`/datasets/:id`: table + Proof Drawer) → Export menu.

## Flow B — History

History (`/history`) → Previous Run → Original Plan + Run trace + Sources + Dataset + Evidence. Must retain prompt, plan, status, sources, dataset, timestamps, errors (see 22-HISTORY-SPEC).

## Flow C — Cancellation / partial

Running → Cancel (`POST /api/runs/{id}/cancel`) → Partial dataset preserved + run marked CANCELLED → Resume/re-run via same plan (new run_id). Never delete partial evidence.

No other navigation. Do not invent routes.
