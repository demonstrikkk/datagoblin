# 17 — FRONTEND SPEC (Vite React JSX + Tailwind, no Next/TSX)

Routes (exact): `/new` (Command Center), `/runs/:id` (Execution), `/datasets/:id` (Dataset Studio), `/history` (History), `/sources` (part of dataset/run views; standalone list optional).

Components (exact names): PromptComposer, PlanPreview, RunTimeline, SourceList, ActivityFeed, DatasetTable, ColumnHeader, ProofDrawer, FilterBar, DatasetToolbar, HistoryList, ExportMenu.

Screens:
- Command Center: big prompt area + [Build dataset →], suggested examples (3 demo scenarios), source-policy badges (✓ public / ✕ auth-paywall-private).
- Plan preview: Target, Fields ✓, Sources count, Collection count, Workflow Search→Discover→Extract→Verify→Deduplicate, [RUN COLLECTION].
- Execution: stage checklist, Sources list, counters (Records 11/15, Fields 47, Verified 43, Needs review 4), live activity stream.
- Dataset Studio: title + count, search + Filters + Export, dense table with Verification column, click cell → ProofDrawer (value, status, source, URL, quote, collected time, Open source).
- History: prompt + records + status + time → dataset+plan+trace.
