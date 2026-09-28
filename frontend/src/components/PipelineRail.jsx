import { useEffect, useRef, useState } from 'react';
import { STAGES, STAGE_INDEX, TERMINAL_STAGE_VALUES, TERMINAL_STATUS } from '../lib/format.js';

/**
 * The execution rail.
 *
 * Three rules this component exists to enforce:
 *  1. A stage that never emitted is drawn as "not run" — never as "done".
 *     `REDUCING` and `FINALIZING` are declared but the runner never emits them,
 *     so they render dimmed with a tooltip saying exactly that.
 *  2. Nothing moves that is not actually moving. The active stage is the only
 *     thing with animation, so motion always means "work is happening".
 *  3. `progress` arrives from the API as 0–100; it is normalised here so no
 *     caller has to remember that.
 *
 * Only the eight work stages are drawn. A terminal `current_stage`
 * (COMPLETED/FAILED/CANCELLED) is a status, not a step, so it fills the rail
 * rather than appearing as a node labelled "Complete" — which was readable as
 * "this run finished" while the run was still going.
 */
export default function PipelineRail({
  current,
  reached = [],
  progress,
  compact = false,
  onSelect,
}) {
  const terminal = TERMINAL_STAGE_VALUES.includes(current);
  // A terminal stage means every work stage behind it is done.
  const activeIdx =
    !current || terminal ? -1 : (STAGE_INDEX[current] ?? -1);
  const reachedSet = new Set(reached);
  const frac =
    progress === undefined || progress === null ? null : Math.max(0, Math.min(100, progress)) / 100;

  return (
    <div className="w-full">
      <ol className="flex items-stretch gap-[3px]" aria-label="Execution stages">
        {STAGES.map((s, i) => {
          const isActive = i === activeIdx;
          const isDone = terminal || (!isActive && i < activeIdx);
          const emitted = s.emits;
          const ran = terminal || reachedSet.has(s.key) || isDone;
          const pct = isActive && frac !== null ? frac : isDone ? 1 : 0;

          const state = !emitted
            ? 'reserved'
            : isActive
            ? 'running'
            : ran
            ? 'done'
            : 'pending';

          return (
            <li key={s.key} className="min-w-0 flex-1">
              <button
                type="button"
                onClick={onSelect ? () => onSelect(s) : undefined}
                disabled={!onSelect}
                title={
                  !emitted
                    ? `${s.label} — declared but never emitted by the runner`
                    : isActive
                    ? `${s.label} — in progress`
                    : ran
                    ? `${s.label} — complete`
                    : `${s.label} — waiting`
                }
                className="group block w-full focusable"
              >
                <div className="relative h-[3px] w-full overflow-hidden rounded-full bg-warm">
                  <div
                    className={`absolute inset-y-0 left-0 rounded-full transition-all duration-700 ease-swift ${
                      isActive ? 'bg-accent' : isDone ? 'bg-ok/70' : 'bg-transparent'
                    }`}
                    style={{ width: `${pct * 100}%` }}
                  />
                  {isActive ? (
                    <div
                      className="absolute inset-y-0 w-1/3 animate-[shimmer_1.6s_linear_infinite] bg-gradient-to-r from-transparent via-paper-3/90 to-transparent"
                      style={{ backgroundSize: '200% 100%' }}
                    />
                  ) : null}
                </div>

                {!compact ? (
                  <div className="mt-1.5 flex items-baseline gap-1 overflow-hidden">
                    <span
                      className={`truncate text-[10.5px] leading-none transition-colors duration-300 ${
                        state === 'running'
                          ? 'font-semibold text-accent'
                          : state === 'done'
                          ? 'text-ink-2'
                          : state === 'reserved'
                          ? 'text-rule-2 italic'
                          : 'text-muted/60'
                      }`}
                    >
                      {s.label}
                    </span>
                  </div>
                ) : null}
              </button>
            </li>
          );
        })}
      </ol>

      {!compact ? (
        <p className="mt-2 text-[10.5px] leading-relaxed text-muted">
          {terminal ? (
            <>
              Run stopped at{' '}
              <span className="font-medium text-ink-2">{current.toLowerCase()}</span>.{' '}
              <span className="text-rule-2 italic">Reduce</span> and{' '}
              <span className="text-rule-2 italic">Finalize</span> are declared stages the runner
              does not emit.
            </>
          ) : activeIdx >= 0 ? (
            <>
              Now running{' '}
              <span className="font-medium text-ink-2">{STAGES[activeIdx].label}</span>
              {frac !== null ? ` — ${Math.round(frac * 100)}%` : ''}.{' '}
              <span className="text-rule-2 italic">Reduce</span> and{' '}
              <span className="text-rule-2 italic">Finalize</span> are declared stages the runner
              does not emit.
            </>
          ) : (
            <>
              Stages run left to right.{' '}
              <span className="text-rule-2 italic">Reduce</span> and{' '}
              <span className="text-rule-2 italic">Finalize</span> are declared but never emitted.
            </>
          )}
        </p>
      ) : null}
    </div>
  );
}

/**
 * Stage list for the run page's right column — same state machine, vertical,
 * with per-stage detail text.
 */
export function StageList({ current, reached = [], details = {} }) {
  const activeIdx = current ? (STAGE_INDEX[current] ?? -1) : -1;
  const reachedSet = new Set(reached);

  return (
    <ol className="relative space-y-0.5">
      {STAGES.map((s, i) => {
        const isActive = i === activeIdx;
        const isDone = !isActive && i < activeIdx;
        const ran = reachedSet.has(s.key) || isDone;

        const tone = !s.emits
          ? 'bg-rule-2'
          : isActive
          ? 'bg-accent'
          : isDone
          ? 'bg-ok'
          : ran
          ? 'bg-ok/50'
          : 'bg-rule';
        const label = !s.emits
          ? 'text-rule-2 italic'
          : isActive
          ? 'font-medium text-ink'
          : isDone || ran
          ? 'text-ink-2'
          : 'text-muted/60';

        return (
          <li
            key={s.key}
            className="row flex items-center gap-2.5 rounded-xs px-2 py-[5px]"
            data-active={isActive}
          >
            <span className="relative grid h-3 w-3 shrink-0 place-items-center">
              <span className={`h-[7px] w-[7px] rounded-full ${tone} transition-colors duration-300`} />
              {isActive ? <span className="live-dot absolute text-accent" /> : null}
            </span>
            <span className={`flex-1 truncate text-[12px] ${label}`}>{s.label}</span>
            {details[s.key] ? (
              <span className="max-w-[45%] truncate text-[10.5px] text-muted">{details[s.key]}</span>
            ) : isDone ? (
              <span className="text-[10px] text-ok" aria-label="complete">
                ✓
              </span>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

/** Animated count-up. Cheap: rAF on a number, not a per-frame layout pass. */
export function Counter({ value, duration = 500 }) {
  const [display, setDisplay] = useState(value ?? 0);
  const fromRef = useRef(value ?? 0);
  const rafRef = useRef(0);

  useEffect(() => {
    const from = fromRef.current;
    const to = Number(value ?? 0);
    if (from === to) return undefined;
    const t0 = performance.now();
    const step = (now) => {
      const k = Math.min(1, (now - t0) / duration);
      const eased = 1 - (1 - k) ** 3;
      setDisplay(Math.round(from + (to - from) * eased));
      if (k < 1) rafRef.current = requestAnimationFrame(step);
      else fromRef.current = to;
    };
    rafRef.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(rafRef.current);
  }, [value, duration]);

  return <>{display}</>;
}
