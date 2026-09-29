import { forwardRef, useCallback, useEffect, useRef, useState } from 'react';
import { toneClass, truncate } from '../lib/format.js';
import { useReveal } from '../hooks/useUi.js';

/* ------------------------------------------------------------------ status */

export function Pill({ tone = 'muted', glyph, children, title, size = 'sm', className = '' }) {
  const s =
    size === 'xs'
      ? 'h-[18px] px-1.5 text-[10px] gap-1'
      : 'h-[22px] px-2 text-[11px] gap-1.5';
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-xs border font-medium leading-none whitespace-nowrap ${s} ${toneClass(tone)} ${className}`}
    >
      {glyph ? <span aria-hidden="true">{glyph}</span> : null}
      {children}
    </span>
  );
}

export function Dot({ tone = 'muted', live = false, title }) {
  const colors = {
    ok: 'bg-ok',
    warn: 'bg-warn',
    danger: 'bg-danger',
    info: 'bg-info',
    judge: 'bg-judge',
    accent: 'bg-accent',
    muted: 'bg-muted',
  };
  return (
    <span
      title={title}
      className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${colors[tone] || colors.muted} ${
        live ? 'live-dot' : ''
      }`}
      style={live ? { position: 'relative' } : undefined}
    />
  );
}

/* --------------------------------------------------------------- feedback */

export function Spinner({ className = '' }) {
  return (
    <svg
      className={`animate-spin ${className}`}
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity=".2" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function Skeleton({ className = '', style }) {
  return <div className={`skeleton ${className}`} style={style} aria-hidden="true" />;
}

export function SkeletonLines({ n = 3, className = '' }) {
  const widths = ['100%', '92%', '76%', '96%', '84%', '68%'];
  return (
    <div className={`space-y-2 ${className}`} aria-hidden="true">
      {Array.from({ length: n }, (_, i) => (
        <Skeleton key={i} style={{ width: widths[i % widths.length], height: 10 }} />
      ))}
    </div>
  );
}

export function Loading({ label = 'Loading', rows = 0, className = '' }) {
  return (
    <div
      className={`flex flex-col gap-3 ${className}`}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <span className="sr-only">{label}</span>
      {rows > 0 ? (
        <div className="space-y-2">
          {Array.from({ length: rows }, (_, i) => (
            <div key={i} className="flex items-center gap-3">
              <Skeleton className="h-6 w-6 rounded-full" />
              <Skeleton className="h-3 flex-1" style={{ maxWidth: `${88 - i * 9}%` }} />
              <Skeleton className="h-3 w-14" />
            </div>
          ))}
        </div>
      ) : (
        <SkeletonLines n={3} />
      )}
    </div>
  );
}

export function Empty({ icon = '◌', title, hint, action, className = '' }) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-2 px-6 py-14 text-center animate-fade-in ${className}`}
    >
      <span className="font-display text-[30px] leading-none text-rule-2" aria-hidden="true">
        {icon}
      </span>
      <p className="h-display text-[17px] text-ink">{title}</p>
      {hint ? <p className="max-w-sm text-[12.5px] leading-relaxed text-muted">{hint}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function ErrorNote({ error, onRetry, className = '', compact = false }) {
  if (!error) return null;
  const msg = error?.message || String(error);
  return (
    <div
      role="alert"
      className={`flex items-start gap-2.5 rounded-sm border border-danger/25 bg-danger-soft ${
        compact ? 'px-2.5 py-2' : 'px-3 py-2.5'
      } animate-scale-in ${className}`}
    >
      <span className="mt-px text-danger" aria-hidden="true">
        ⚠
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[12.5px] font-medium leading-snug text-danger break-words">{msg}</p>
      </div>
      {onRetry ? (
        <button type="button" className="btn-ghost btn-xs text-danger" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function Notice({ tone = 'info', children, className = '' }) {
  return (
    <div className={`rounded-sm border px-3 py-2 text-[12.5px] leading-relaxed ${toneClass(tone)} ${className}`}>
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ layout */

export function Section({ title, sub, right, children, className = '', dense = false }) {
  return (
    <section className={className}>
      {(title || right) && (
        <header className="mb-2 flex items-end justify-between gap-3">
          <div className="min-w-0">
            {title ? <h2 className="eyebrow">{title}</h2> : null}
            {sub ? <p className="mt-0.5 text-[11.5px] text-muted">{sub}</p> : null}
          </div>
          {right ? <div className="flex shrink-0 items-center gap-1.5">{right}</div> : null}
        </header>
      )}
      <div className={dense ? '' : 'surface p-3.5'}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone = 'ink', mono = true, live = false }) {
  const tones = {
    ink: 'text-ink',
    ok: 'text-ok',
    warn: 'text-warn',
    danger: 'text-danger',
    info: 'text-info',
    judge: 'text-judge',
    muted: 'text-muted',
    accent: 'text-accent',
  };
  return (
    <div className="min-w-0">
      <p className="eyebrow truncate">{label}</p>
      <p
        className={`mt-1 flex items-center gap-1.5 truncate text-[19px] leading-none ${
          tones[tone] || tones.ink
        } ${mono ? 'font-mono tnum' : 'h-display'}`}
      >
        {live ? <span className="live-dot text-accent" /> : null}
        {value}
      </p>
      {sub ? <p className="mt-1 truncate text-[11px] text-muted">{sub}</p> : null}
    </div>
  );
}

/**
 * Proportional distribution bar. This is the app's primary "how good is this
 * data" read — one glance, no legend hunting, and every segment is clickable
 * so it doubles as a filter.
 */
export function StackedBar({ rows, total, onSelect, activeKey, height = 8, className = '' }) {
  const list = (rows || []).filter((r) => r.n > 0);
  const sum = total || list.reduce((a, r) => a + r.n, 0);
  if (!sum) {
    return (
      <div className={`h-2 w-full rounded-full bg-warm ${className}`} aria-hidden="true" />
    );
  }
  const colors = {
    ok: 'var(--ok)',
    danger: 'var(--danger)',
    warn: 'var(--warn)',
    info: 'var(--info)',
    judge: 'var(--judge)',
    muted: 'var(--rule-2)',
  };
  return (
    <div
      className={`flex w-full overflow-hidden rounded-full bg-warm ${className}`}
      style={{ height }}
      role="img"
      aria-label={list.map((r) => `${r.label} ${r.n}`).join(', ')}
    >
      {list.map((r, i) => {
        const share = r.n / sum;
        const dim = activeKey && activeKey !== r.key;
        const Tag = onSelect ? 'button' : 'div';
        return (
          <Tag
            key={r.key}
            type={onSelect ? 'button' : undefined}
            onClick={onSelect ? () => onSelect(activeKey === r.key ? null : r.key) : undefined}
            title={`${r.label} — ${r.n} (${(share * 100).toFixed(0)}%)`}
            className={`h-full transition-all duration-300 ease-swift ${
              onSelect ? 'focusable cursor-pointer hover:brightness-110' : ''
            }`}
            style={{
              width: `${share * 100}%`,
              background: colors[r.tone] || colors.muted,
              opacity: dim ? 0.28 : 1,
              marginRight: i < list.length - 1 ? '1.5px' : 0,
            }}
          />
        );
      })}
    </div>
  );
}

export function Segmented({ options, value, onChange, size = 'sm', className = '', label }) {
  const h = size === 'xs' ? 'h-6' : 'h-7';
  return (
    <div
      role="tablist"
      // A tablist with no name is indistinguishable from every other tablist
      // on the page. The dataset view has two: the panel switcher and the
      // record-status filter. Naming this group is what lets a screen-reader
      // user — and any automation — tell them apart.
      aria-label={label}
      className={`inline-flex items-center gap-0.5 rounded-sm border border-rule bg-warm/60 p-0.5 ${className}`}
    >
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            role="tab"
            aria-selected={on}
            type="button"
            title={o.title || o.label}
            onClick={() => onChange(o.value)}
            className={`focusable relative rounded-xs ${h} px-2.5 text-[11.5px] font-medium transition-all duration-150 ease-swift ${
              on ? 'bg-paper-3 text-ink shadow-[0_1px_2px_rgba(28,25,23,.1)]' : 'text-muted hover:text-ink'
            }`}
          >
            {o.label}
            {o.count !== undefined ? (
              <span className={`ml-1.5 tnum ${on ? 'text-muted' : 'text-muted/70'}`}>{o.count}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------- form */

export function Field({ label, hint, error, required, children, className = '' }) {
  return (
    <label className={`block ${className}`}>
      {label ? (
        <span className="label">
          {label}
          {required ? <span className="ml-0.5 text-accent">*</span> : null}
        </span>
      ) : null}
      {children}
      {error ? (
        <span className="mt-1 block text-[11.5px] text-danger animate-fade-in">{error}</span>
      ) : hint ? (
        <span className="mt-1 block text-[11px] text-muted">{hint}</span>
      ) : null}
    </label>
  );
}

/**
 * Forwarded so callers can hold a ref and focus the field. Without
 * forwardRef, `<Textarea ref={r} autoFocus />` silently does nothing and React
 * warns that a function component was given a ref.
 */
export const Textarea = forwardRef(function Textarea({ className = '', invalid, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      {...props}
      className={`input resize-y min-h-[76px] leading-relaxed ${
        invalid ? 'border-danger focus:border-danger focus:ring-[var(--danger-soft)]' : ''
      } ${className}`}
    />
  );
});

export function Select({ options, className = '', ...props }) {
  return (
    <div className="relative">
      <select {...props} className={`input appearance-none pr-7 cursor-pointer ${className}`}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <span
        className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-muted"
        aria-hidden="true"
      >
        ▼
      </span>
    </div>
  );
}

export function Toggle({ checked, onChange, label, hint, id }) {
  const inputId = id || `tg-${label?.replace(/\W+/g, '-').toLowerCase()}`;
  return (
    <div className="flex items-start gap-2.5">
      <button
        id={inputId}
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`focusable relative mt-px h-[18px] w-8 shrink-0 rounded-full border transition-colors duration-200 ease-swift ${
          checked ? 'border-accent bg-accent' : 'border-rule-2 bg-warm'
        }`}
      >
        <span
          className="absolute top-[2px] h-[12px] w-[12px] rounded-full bg-paper-3 shadow-[0_1px_2px_rgba(28,25,23,.25)] transition-transform duration-200 ease-spring"
          style={{ left: checked ? 17 : 2 }}
        />
      </button>
      <label htmlFor={inputId} className="min-w-0 cursor-pointer select-none">
        <span className="block text-[12.5px] font-medium text-ink">{label}</span>
        {hint ? <span className="mt-0.5 block text-[11px] leading-snug text-muted">{hint}</span> : null}
      </label>
    </div>
  );
}

/* ------------------------------------------------------------------- misc */

export function Kbd({ children }) {
  return (
    <kbd className="inline-flex h-[17px] min-w-[17px] items-center justify-center rounded-[3px] border border-rule-2 bg-warm px-1 font-mono text-[10px] font-medium text-muted">
      {children}
    </kbd>
  );
}

export function CopyButton({ value, label = 'Copy', className = '' }) {
  const [done, setDone] = useState(false);
  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(String(value ?? ''));
      setDone(true);
      setTimeout(() => setDone(false), 1400);
    } catch {
      /* clipboard blocked */
    }
  }, [value]);
  return (
    <button
      type="button"
      onClick={copy}
      className={`btn-ghost btn-xs ${className}`}
      aria-live="polite"
    >
      <span className={done ? 'animate-scale-in inline-block' : 'inline-block'}>
        {done ? '✓' : '⧉'}
      </span>
      {label}
    </button>
  );
}

export function KeyVal({ k, v, mono = false, className = '' }) {
  return (
    <div className={`flex items-baseline justify-between gap-3 py-1 ${className}`}>
      <span className="shrink-0 text-[11.5px] text-muted">{k}</span>
      <span
        className={`min-w-0 truncate text-right text-[12px] text-ink ${mono ? 'font-mono tnum' : ''}`}
        title={typeof v === 'string' ? v : undefined}
      >
        {v === null || v === undefined || v === '' ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)}
      </span>
    </div>
  );
}

/** Scroll-reveal wrapper for long, sectioned content. */
export function Reveal({ children, className = '', as: Tag = 'div', ...rest }) {
  const [ref, shown] = useReveal();
  return (
    <Tag
      ref={ref}
      className={`${className}`}
      style={
        shown
          ? { animation: 'rise-in .45s var(--e-swift) both' }
          : { opacity: 0, transform: 'translate3d(0,14px,0)' }
      }
      {...rest}
    >
      {children}
    </Tag>
  );
}

export function Ticker({ children, ms = 1000 }) {
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), ms);
    return () => clearInterval(id);
  }, [ms]);
  return <>{children}</>;
}

export { truncate };
