# Vite + React + Tailwind — Data Studio shell (CORRECTED to spec)

CORRECTION: research agent invented routes (/datasets, /pipelines...) and components — DO NOT USE. Spec routes (docs/17): /new, /runs/:id, /datasets/:id, /history, /sources. Spec components: PromptComposer, PlanPreview, RunTimeline, SourceList, ActivityFeed, DatasetTable, ColumnHeader, ProofDrawer, FilterBar, DatasetToolbar, HistoryList, ExportMenu.

Verified 2026-09-24: https://vite.dev/guide/ (v8.3.1) + https://tailwindcss.com/docs/installation/using-vite (v4.3) + https://react.dev/reference/react/useEffect (v19.2)
```bash
npm create vite@latest datagoblin -- --template react
npm install tailwindcss @tailwindcss/vite
```
```css
@import "tailwindcss";
```
```jsx
useEffect(() => { const es = new EventSource(url); es.onmessage = e => setRows(r => [...r, JSON.parse(e.data)]); return () => es.close(); }, [url]);
```
Limits: Node 20.19+/22.12+, modern browsers (Safari16.4+/Chrome111+), Tailwind v4 no @tailwind base/Sass, EventSource GET-only no headers, StrictMode double-effect needs cleanup.
Note: use JSX template (constitution bans TSX), not agent's react-ts.
