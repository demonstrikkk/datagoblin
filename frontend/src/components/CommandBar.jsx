import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import { Dot, Kbd, Spinner } from './ui.jsx';
import { runStatusMeta, num } from '../lib/format.js';

/** Compact system pulse. Truthful by construction: unknown stays unknown. */
function HealthPulse() {
  const { inspect } = useInspector();
  const { data, status, isStale } = useResource((o) => api.health(o), [], { pollMs: 20_000 });
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDoc = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const adapter = data?.persistence?.adapter || data?.adapter || null;
  const jev = data?.jev || null;

  /*
   * `jev` is `{calls, succeeded, unavailable, rate_limited, retried}` — there is
   * no `ok` flag. Derive one, and only from real counters: a judge with zero
   * calls has not been proven healthy, so it reads "idle", not "ok".
   */
  const jevCalls = Number(jev?.calls) || 0;
  const jevBad = (Number(jev?.unavailable) || 0) + (Number(jev?.rate_limited) || 0);
  const jevTone =
    !jev || jevCalls === 0
      ? 'muted'
      : jevBad === 0
      ? 'ok'
      : jevBad / jevCalls > 0.2
      ? 'danger'
      : 'warn';
  const jevLabel =
    !jev || jevCalls === 0
      ? 'idle'
      : jevBad === 0
      ? 'healthy'
      : `${jevBad} degraded`;

  const down = status === 'error';
  const tone = down ? 'danger' : jevTone === 'muted' ? 'muted' : jevTone;

  const detail = (e) => {
    e.stopPropagation();
    inspect({ kind: 'diagnostics', health: data, loading: status === 'loading' });
  };

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        onDoubleClick={detail}
        title="System health — double-click for diagnostics"
        aria-expanded={open}
        className={`focusable flex items-center gap-1.5 rounded-sm border px-2 py-1 text-[11px] transition-all duration-150 ease-swift ${
          open ? 'border-rule-2 bg-warm' : 'border-transparent hover:border-rule hover:bg-warm/60'
        } ${isStale ? 'opacity-70' : ''}`}
      >
        {status === 'loading' && !data ? (
          <Spinner className="text-muted" />
        ) : (
          <Dot tone={tone} />
        )}
        <span className="text-muted">{adapter || (down ? 'offline' : 'api')}</span>
      </button>

      {open ? (
        <div className="absolute right-0 top-[calc(100%+6px)] z-50 w-[290px] origin-top-right rounded-lg border border-rule bg-paper-3 p-3 shadow-deep animate-scale-in">
          <p className="eyebrow mb-2">System</p>
          <dl className="space-y-1.5 text-[12px]">
            <Row k="API" v={down ? 'unreachable' : 'reachable'} tone={down ? 'danger' : 'ok'} />
            <Row
              k="Persistence"
              v={adapter ? adapter : 'unknown'}
              tone={adapter === 'postgres' ? 'ok' : adapter ? 'warn' : 'muted'}
            />
            {data?.persistence?.detail ? (
              <Row k="Store" v={data.persistence.detail} tone="muted" />
            ) : null}
            <Row k="Judge (Jev)" v={jevLabel} tone={jevTone === 'muted' ? 'muted' : jevTone} />
            {jevCalls ? (
              <Row
                k="Judge calls"
                v={`${num(jev.succeeded)}/${num(jevCalls)} ok${
                  jev.rate_limited ? ` · ${num(jev.rate_limited)} limited` : ''
                }${jev.unavailable ? ` · ${num(jev.unavailable)} down` : ''}`}
                tone="muted"
              />
            ) : null}
            {jev?.retried ? <Row k="Judge retries" v={num(jev.retried)} tone="muted" /> : null}
            {data?.version ? <Row k="Version" v={data.version} tone="muted" /> : null}
          </dl>
          <button type="button" onClick={detail} className="btn-outline btn-xs mt-3 w-full">
            Open diagnostics
          </button>
        </div>
      ) : null}
    </div>
  );
}

function Row({ k, v, tone = 'muted' }) {
  const tones = { ok: 'text-ok', warn: 'text-warn', danger: 'text-danger', judge: 'text-judge', muted: 'text-ink' };
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-muted">{k}</dt>
      <dd className={`font-medium ${tones[tone]}`}>{v}</dd>
    </div>
  );
}

/** Active-run chip. Also the fastest route back into a live run. */
function RunChip({ run }) {
  const nav = useNavigate();
  if (!run) return null;
  const meta = runStatusMeta(run);
  return (
    <button
      type="button"
      onClick={() => nav(`/runs/${run.run_id || run.id}`)}
      className="focusable group flex max-w-[240px] items-center gap-1.5 rounded-sm border border-rule bg-warm/50 px-2 py-1 text-[11px] transition-all duration-150 ease-swift hover:border-accent/40 hover:bg-warm"
      title={`${meta.label} — open run`}
    >
      {meta.live ? (
        <span className="live-dot text-accent" />
      ) : (
        <span className="text-muted" aria-hidden="true">
          {meta.glyph}
        </span>
      )}
      <span className="truncate font-mono text-ink">{String(run.run_id || run.id).slice(0, 8)}</span>
      <span className="shrink-0 text-muted transition-colors group-hover:text-accent">{meta.label}</span>
    </button>
  );
}

export default function CommandBar({ title, run, onOpenPalette, onOpenSettings, right }) {
  return (
    <header className="chrome-blur sticky top-0 z-30 flex h-12 shrink-0 items-center gap-3 border-b border-b-rule border-r-0 px-[var(--shell-pad)]">
      <h1 className="h-display shrink-0 text-[16px] leading-none text-ink">{title}</h1>

      <div className="flex min-w-0 flex-1 items-center gap-2">
        <RunChip run={run} />
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        {right}
        <button
          type="button"
          onClick={onOpenPalette}
          className="focusable hidden items-center gap-2 rounded-sm border border-rule bg-warm/50 px-2.5 py-1 text-[11.5px] text-muted transition-all duration-150 ease-swift hover:border-rule-2 hover:text-ink sm:flex"
          title="Command palette"
        >
          <span>Search or jump to…</span>
          <Kbd>⌘K</Kbd>
        </button>
        <HealthPulse />
        <button
          type="button"
          onClick={onOpenSettings}
          aria-label="Settings"
          className="focusable grid h-7 w-7 place-items-center rounded-sm text-muted transition-colors duration-150 hover:bg-warm hover:text-ink sm:hidden"
        >
          <svg
            width="15"
            height="15"
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            aria-hidden="true"
          >
            <circle cx="10" cy="10" r="2.6" />
            <path d="M10 2.2v1.6M10 16.2v1.6M17.8 10h-1.6M3.8 10H2.2M15.5 4.5l-1.1 1.1M5.6 14.4l-1.1 1.1M15.5 15.5l-1.1-1.1M5.6 5.6 4.5 4.5" />
          </svg>
        </button>
      </div>
    </header>
  );
}
