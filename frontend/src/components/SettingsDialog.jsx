import { useState } from 'react';
import { apiKey, setApiKey } from '../lib/api.js';
import { Field, Kbd, Notice, Toggle } from './ui.jsx';
import { useLocalStore, useMediaQuery } from '../hooks/useUi.js';

const SHORTCUTS = [
  ['⌘K', 'Command palette'],
  ['1 – 4', 'Collect · Runs · Library · Intel'],
  ['⌘B', 'Toggle mobile navigation'],
  ['Esc', 'Close inspector, dialog or palette'],
  ['↑ ↓ ↵', 'Move through and open palette results'],
];

function Row({ k, v }) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <span className="text-[12px] text-muted">{v}</span>
      <Kbd>{k}</Kbd>
    </div>
  );
}

export default function SettingsDialog({ onClose }) {
  const [key, setKey] = useState(() => apiKey());
  const [saved, setSaved] = useState(false);
  const [dense, setDense] = useLocalStore('dg_dense', false);
  const reduced = useMediaQuery('(prefers-reduced-motion: reduce)');

  const base = import.meta.env.VITE_API_BASE || '/api (proxied)';

  const save = () => {
    setApiKey(key.trim());
    setSaved(true);
    setTimeout(() => setSaved(false), 1600);
  };

  return (
    <div
      className="fixed inset-0 z-[60] grid place-items-center bg-ink/20 px-4 backdrop-blur-[2px] animate-fade-in"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        className="scroll-y max-h-[86vh] w-full max-w-[520px] rounded-lg border border-rule bg-paper-2 shadow-deep animate-scale-in"
      >
        <header className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-rule bg-paper-2/95 px-4 py-3 backdrop-blur">
          <h2 className="h-display text-[17px] text-ink">Settings</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="focusable grid h-7 w-7 place-items-center rounded-sm text-muted transition-all duration-150 hover:bg-warm hover:text-ink active:scale-90"
          >
            ✕
          </button>
        </header>

        {/* A real form, not a div: it gives the password field a form owner
            (Chrome warns otherwise, and password managers need it) and makes
            Enter save instead of doing nothing. */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
          className="space-y-6 px-4 py-4"
        >
          <section>
            <Field
              label="API key"
              hint="Sent as X-API-Key on every request. Only needed if the backend has DG_API_KEY set. Stored in this browser's localStorage, never in the bundle."
            >
              <div className="flex gap-1.5">
                <input
                  type="password"
                  name="dg_api_key"
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  placeholder="optional"
                  autoComplete="off"
                  spellCheck={false}
                  className="input font-mono text-[12px]"
                />
                <button type="submit" className="btn-primary shrink-0">
                  {saved ? 'Saved' : 'Save'}
                </button>
              </div>
            </Field>
          </section>

          <section>
            <p className="eyebrow mb-2">Interface</p>
            <div className="space-y-3">
              <Toggle
                checked={dense}
                onChange={setDense}
                label="Compact density"
                hint="Tighter rows and reduced padding in tables and lists."
              />
              {/*
                Reported, not offered. This used to be a Toggle whose onChange
                was a no-op: a control that looked actionable and did nothing,
                which is worse than not having it. Reduced motion is an OS
                accessibility setting and deliberately not overridable here.
              */}
              <div className="rounded-sm border border-rule bg-warm/40 px-3 py-2">
                <p className="text-[12.5px] font-medium text-ink">Reduced motion</p>
                <p className="mt-0.5 text-[11px] leading-snug text-muted">
                  {reduced ? (
                    <>
                      <span className="text-ok">Active</span> — detected from your OS.
                      Decorative animation and the background field are stopped.
                    </>
                  ) : (
                    <>
                      Off, following your OS setting. Animation stops here
                      automatically if you enable it there.
                    </>
                  )}
                </p>
              </div>
            </div>
          </section>

          <section>
            <p className="eyebrow mb-2">Connection</p>
            <dl className="divide-y divide-rule rounded-sm border border-rule">
              <div className="flex justify-between px-3 py-2">
                <dt className="text-[12px] text-muted">API base</dt>
                <dd className="font-mono text-[11.5px] text-ink">{base}</dd>
              </div>
              <div className="flex justify-between px-3 py-2">
                <dt className="text-[12px] text-muted">Envelope</dt>
                <dd className="font-mono text-[11.5px] text-ink">{`{ data, error, meta }`}</dd>
              </div>
            </dl>
          </section>

          <section>
            <p className="eyebrow mb-2">Keyboard</p>
            <div className="divide-y divide-rule">
              {SHORTCUTS.map(([k, v]) => (
                <Row key={k} k={k} v={v} />
              ))}
            </div>
          </section>

          <Notice tone="muted">
            Field values come from the live API. Some of the surrounding chrome does not: the
            per-dataset verification counters depend on what each run wrote, and older runs wrote
            none — those say <em>not counted</em> rather than claiming a quality bar they cannot
            support. Where the backend has a known gap — PDFs, JSON feeds, cross-run deduplication
            — the UI says so rather than showing an empty success state.
          </Notice>
        </form>
      </div>
    </div>
  );
}
