# Frontend end-to-end suites

Playwright suites that drive the **real app against a real backend**. They
exist because a passing `vite build` only proves the modules parse — it does
not prove the React tree mounts, the API envelopes unwrap, or the WebGL field
initialises. Five of the bugs these caught were invisible to the build.

## Running them

They need a backend on `:8000` and a served frontend. Run them against the
production build, **not** the dev server:

```powershell
cd frontend
npm run build
npm run preview -- --port 4173
```

```powershell
# in another shell
cd frontend/e2e
$env:DG_BASE_URL = "http://localhost:4173"
python smoke.py
```

### Why preview and not `npm run dev`

React StrictMode double-invokes effects in development, so every aborted fetch
logs `net::ERR_ABORTED`. That is dev noise, and it drowns out real failures. The
production build has no double-invoke, so a red line here is a real red line.

## The suites

| file | what it proves |
|---|---|
| `smoke.py` | All four routes mount with **zero console errors**; the boot placeholder is removed; the WebGL canvas exists; ⌘K opens *with the caret in a textarea*; digit shortcuts navigate; a record row expands to a quote and a stored page id; the sources tab renders; no horizontal overflow at 390px. |
| `interactions.py` | Every progressive-disclosure control: the Constraints panel, the nested seed-URL sub-panel, the Settings dialog, both switches, the API-key save, and every record status filter. |
| `verify.py` | Per-field evidence expansion, the sources summary, the export panel, the run table totals. |
| `partial.py` | A real partial run: status pill, quality breakdown, and — specifically — that the stage rail contains **no node labelled "Complete"**, which was indistinguishable from the status of the same name. |
| `visual.py` | `readPixels` over the WebGL canvas to prove the field is actually *drawing* rather than being a flat canvas, that consecutive frames differ, plus screenshots at 1600px and 390px. |

## Conventions worth keeping

- **Assert on rendered text, lowercased.** `.eyebrow` is CSS
  `text-transform: uppercase`, and `inner_text()` returns what is rendered, so
  `"Pages stored"` never matches `"PAGES STORED"`.
- **Select by name, not position.** The records status filter is also
  `role="tab"`, so `[role=tab]` by index is ambiguous — use
  `locator('[role=tab]', has_text="Sources")`.
- **Wait for content, not for a clock.** `GET /sources` takes ~2.5s
  server-side; a fixed `wait_for_timeout` is a coin flip.
- **Scope selectors to the dialog.** `button[type=submit]` matches the page
  behind a modal.

These four cost real debugging time. They are recorded here so the next person
does not pay for them again.
