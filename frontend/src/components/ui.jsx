import { forwardRef, useCallback, useEffect, useRef, useState } from 'react';
import { toneClass, toneVar, truncate, num } from '../lib/format.js';
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
    // `dither`, not a gradient sweep: the placeholder is a field of dots with a
    // denser band passing through it, so brightness changes through dot density
    // rather than opacity. It reads as a printhead crossing paper, and it is
    // never a flat block of nothing.
    return <div className={`dither ${className}`} style={style} aria-hidden="true" />;
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
    ok: 'rgb(var(--ok))',
    danger: 'rgb(var(--danger))',
    warn: 'rgb(var(--warn))',
    info: 'rgb(var(--info))',
    judge: 'rgb(var(--judge))',
    muted: 'rgb(var(--rule-2))',
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

/* ------------------------------------------------------------------ figures */

/**
 * A histogram, for a field that is actually numeric.
 *
 * Drawn as thin columns on a baseline with the median marked, rather than as a
 * filled area or a bar list. Two reasons it is shaped this way: a distribution
 * is about where the mass sits, and a solid block of colour hides the gap; and
 * the median is the one number a reader actually wants, so it is drawn rather
 * than written under the title.
 *
 * Returns null when there is nothing to draw. One bucket is a sentence with
 * axes, and a caller that passes one is describing a constant, not a shape.
 */
export function Histogram({ buckets, median: med, format = (n) => String(n), height = 64 }) {
  const list = (buckets || []).filter((b) => b && Number.isFinite(b.n));
  if (list.length < 2) return null;
  const max = Math.max(...list.map((b) => b.n)) || 1;
  const total = list.reduce((a, b) => a + b.n, 0) || 1;
  // The median's bucket, found from the cumulative count. Asking the caller for
  // a pixel position would mean two passes over the same data.
  let seen = 0;
  let medBucket = 0;
  for (let i = 0; i < list.length; i += 1) {
    if (seen + list[i].n >= total / 2) {
      medBucket = i;
      break;
    }
    seen += list[i].n;
  }
  return (
    <figure className="min-w-0">
      <svg
        viewBox={`0 0 ${list.length * 10} ${height}`}
        preserveAspectRatio="none"
        className="w-full"
        style={{ height }}
        role="img"
        aria-label={`Distribution across ${list.length} buckets, peak ${max}, median ${format(med)}`}
      >
        {list.map((b, i) => {
          const h = (b.n / max) * (height - 6);
          return (
            <rect
              key={i}
              x={i * 10 + 1}
              y={height - h}
              width={8}
              height={Math.max(b.n > 0 ? 1.5 : 0, h)}
              rx={1}
              fill="rgb(var(--accent))"
              opacity={0.35 + 0.65 * (b.n / max)}
            >
              <title>{`${format(b.from)} – ${format(b.to)}: ${b.n}`}</title>
            </rect>
          );
        })}
        <line
          x1={medBucket * 10 + 5}
          x2={medBucket * 10 + 5}
          y1={0}
          y2={height}
          stroke="rgb(var(--ink))"
          strokeWidth={1}
          strokeDasharray="2 2"
          opacity={0.5}
        />
      </svg>
      <figcaption className="mt-1 flex justify-between text-[10px] tabular-nums text-muted">
        <span>{format(list[0].from)}</span>
        <span className="tnum">median {format(med)}</span>
        <span>{format(list[list.length - 1].to)}</span>
      </figcaption>
    </figure>
  );
}

/**
 * Squarified treemap — an equal-area layout over shares.
 *
 * Used for the "where these records are" panel, and deliberately *not* a world
 * map. A hand-drawn map would be a picture of a world this run never visited,
 * and it would need geographic data the dataset does not have. Tiles sized by
 * share answer the same question with the data that exists.
 *
 * Squarifying (Bruls, Huizing & van Wijk) keeps every tile as close to a square
 * as it can, so no category is visually inflated by being placed in a long thin
 * strip. That matters here: with a naive slice-and-dice the first category gets
 * a full-width band and reads as dominant even when it is not.
 */
function squarify(items, x, y, w, h) {
  const out = [];
  const total = items.reduce((a, it) => a + it.value, 0) || 1;
  let rest = items.slice();
  let area = { x, y, w, h };

  const worst = (row, side) => {
    if (!row.length) return Infinity;
    const s = row.reduce((a, it) => a + it.value, 0);
    const rMax = Math.max(...row.map((it) => it.value));
    const rMin = Math.min(...row.map((it) => it.value));
    const s2 = s * s;
    const w2 = side * side;
    return Math.max((w2 * rMax) / s2, s2 / (w2 * rMin));
  };

  while (rest.length) {
    const side = Math.min(area.w, area.h);
    const row = [];
    let i = 0;
    while (i < rest.length) {
      const next = [...row, rest[i]];
      if (row.length && worst(next, side) > worst(row, side)) break;
      row.push(rest[i]);
      i += 1;
    }
    rest = rest.slice(row.length);
    const s = row.reduce((a, it) => a + it.value, 0);
    const rowArea = (s / total) * area.w * area.h;
    const thickness = side ? rowArea / side : 0;
    const horizontal = area.w >= area.h;
    let offset = horizontal ? area.y : area.x;
    for (const it of row) {
      const len = s ? (it.value / s) * (horizontal ? area.w : area.h) : 0;
      out.push(
        horizontal
          ? { ...it, x: area.x, y: offset, w: Math.max(0, len), h: Math.max(0, thickness) }
          : { ...it, x: offset, y: area.y, w: Math.max(0, thickness), h: Math.max(0, len) }
      );
      offset += len;
    }
    if (horizontal) {
      area = { x: area.x, y: area.y + thickness, w: area.w, h: area.h - thickness };
    } else {
      area = { x: area.x + thickness, y: area.y, w: area.w - thickness, h: area.h };
    }
    if (area.w <= 0.5 || area.h <= 0.5) break;
  }
  return out;
}

/**
 * A treemap of shares — shape only, no in-tile labels.
 *
 * Labels were drawn here and were removed. The SVG carries
 * `preserveAspectRatio="none"`, so a 100x100 viewBox is stretched to whatever
 * the container is: text in viewBox units cannot be sized against a tile in
 * viewBox units and land correctly, and every attempt produced clipped
 * fragments — "Ki…", "rse…", "any" — laid over their neighbours. An SVG <text>
 * is not clipped by a neighbouring <rect>, so a label too wide for its tile
 * simply runs over the next one.
 *
 * The tiles now show only proportion, which is the part a treemap is actually
 * good at, and the full name and count are on the tile's <title> for hover.
 * Every value is also listed with its count and share in the ranked column
 * beside the chart, so nothing is only reachable by hovering.
 */
export function Treemap({ items, height = 132, onSelect, activeKey }) {
  const list = (items || []).filter((it) => it && it.value > 0);
  if (!list.length) return null;
  const rects = squarify(list, 0, 0, 100, 100);
  return (
    <svg
      viewBox="0 0 100 100"
      className="w-full"
      style={{ height }}
      role="img"
      aria-label={list.map((i) => `${i.label} ${i.n}`).join(', ')}
      preserveAspectRatio="none"
    >
      {rects.map((r) => {
        const dim = activeKey && activeKey !== r.key;
        return (
          <g
            key={r.key}
            onClick={onSelect ? () => onSelect(activeKey === r.key ? null : r.key) : undefined}
            className={onSelect ? 'cursor-pointer' : undefined}
            opacity={dim ? 0.3 : 1}
          >
            <rect
              x={r.x + 0.4}
              y={r.y + 0.4}
              width={Math.max(0, r.w - 0.8)}
              height={Math.max(0, r.h - 0.8)}
              rx={1.5}
              fill={r.tone ? toneVar(r.tone) : 'rgb(var(--accent))'}
              opacity={r.tone ? 0.9 : 0.82}
            >
              <title>{`${r.label} — ${r.n}`}</title>
            </rect>
          </g>
        );
      })}
    </svg>
  );
}

/**
 * A timeline, for a field whose values are points in time.
 *
 * A year bar per year, with the empty years left visible rather than collapsed —
 * a gap in a date column is a finding, and skipping those years would draw the
 * same picture as no gap at all. Which is the difference between saying "records
 * arrived in 2019 and 2022" and saying "records arrived over four years".
 */
export function Timeline({ byYear, min, max, height = 56 }) {
  const rows = (byYear || []).filter((b) => b && Number.isFinite(b.n));
  if (rows.length < 2) return null;
  const maxN = Math.max(...rows.map((r) => r.n)) || 1;
  const W = 100;
  const step = W / rows.length;
  return (
    <figure className="min-w-0">
      <svg
        viewBox={`0 0 ${W} ${height}`}
        preserveAspectRatio="none"
        className="w-full"
        style={{ height }}
        role="img"
        aria-label={`${rows.length} years from ${min} to ${max}, peak ${maxN}`}
      >
        {rows.map((r, i) => {
          const h = r.n ? Math.max(1.5, (r.n / maxN) * (height - 2)) : 0;
          return (
            <rect
              key={r.year}
              x={i * step + 0.3}
              y={height - h}
              width={Math.max(0.5, step - 0.6)}
              height={h}
              fill={r.n ? 'rgb(var(--accent))' : 'rgb(var(--rule))'}
              opacity={r.n ? 0.4 + 0.6 * (r.n / maxN) : 1}
            >
              <title>{`${r.year}: ${r.n}`}</title>
            </rect>
          );
        })}
      </svg>
      <figcaption className="mt-1 flex justify-between text-[10px] tabular-nums text-muted">
        <span>{min}</span>
        <span className="tnum">{max - min + 1} year{max - min === 0 ? '' : 's'}</span>
        <span>{max}</span>
      </figcaption>
    </figure>
  );
}

/**
 * A ranked bar chart.
 *
 * This replaces a treemap for host yield, and the reason is the shape of the
 * data rather than taste: stored-text totals across sources are extremely
 * long-tailed — on this workspace the largest host holds 2.0 MB and the
 * smallest 70 KB, a 28x spread across 72 rows. Squarified into equal areas that
 * is a stack of hairlines: technically correct, visually a barcode, and it
 * answers nothing.
 *
 * Bars show the same distribution honestly. The taper *is* the finding — a few
 * hosts carry most of the evidence and the rest contribute a long thin tail —
 * and it is legible at a glance where a treemap of the same numbers is not.
 *
 * One row per host, on a single line, with the bar in its own middle column.
 * The bar does not run the full width of the page: at 1100px a 35% share is a
 * 400px rule, which reads as an underline rather than as a quantity. Confining
 * it to a fixed-width column keeps the eye comparing lengths.
 */
export function RankBars({ items, total, limit = 12, format = num, showShare = true, tailFormat }) {
  const rows = (items || []).filter((r) => Number.isFinite(r.value) && r.value >= 0);
  if (!rows.length) return null;
  const shown = rows.slice(0, limit);
  const max = Math.max(...shown.map((r) => r.value), 1);
  const sum = total ?? rows.reduce((a, r) => a + r.value, 0);
  const rest = (sum || 0) - shown.reduce((a, r) => a + r.value, 0);
  const tail = tailFormat || format;

  return (
    <div>
      <ol className="space-y-[5px]">
        {shown.map((r) => {
          const share = sum ? (r.value / sum) * 100 : 0;
          return (
            <li
              key={r.key}
              className="grid grid-cols-[minmax(0,168px)_minmax(80px,260px)_auto] items-center gap-x-3"
            >
              <span
                className="truncate font-mono text-[11px] text-ink-2"
                title={r.label}
              >
                {r.label}
              </span>
              <span className="h-[6px] overflow-hidden rounded-[2px] bg-warm/80">
                <span
                  className="block h-full rounded-[2px] transition-[width] duration-500 ease-swift"
                  style={{
                    width: `${Math.max(r.value > 0 ? 1.5 : 0, (r.value / max) * 100)}%`,
                    background: 'rgb(var(--accent))',
                  }}
                />
              </span>
              <span className="flex items-baseline gap-2 text-[11px] tabular-nums">
                <span className="font-mono text-ink">{format(r.value)}</span>
                {showShare ? (
                  <span className="w-11 text-right text-[10px] text-muted">
                    {share >= 0.1 ? `${share.toFixed(1)}%` : '<0.1%'}
                  </span>
                ) : null}
              </span>
            </li>
          );
        })}
      </ol>
      {rows.length > shown.length ? (
        <p className="mt-2 text-[10.5px] text-muted">
          {num(rows.length - shown.length)} more host{rows.length - shown.length === 1 ? '' : 's'}{' '}
          contributed {tail(rest)} between them.
        </p>
      ) : null}
    </div>
  );
}

export { truncate };
