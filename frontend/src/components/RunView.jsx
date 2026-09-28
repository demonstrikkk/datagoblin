import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api.js';
import useRunStream from '../hooks/useRunStream.js';
import { useRunContext } from '../lib/run-context.jsx';
import { useInspector } from '../lib/inspector.jsx';
import PipelineRail, { Counter } from './PipelineRail.jsx';
import EventFeed from './EventFeed.jsx';
import {
  Dot,
  ErrorNote,
  Notice,
  Pill,
  Skeleton,
  Stat,
  StackedBar,
} from './ui.jsx';
import {
  isEffectivelyRunning,
  isPartialRun,
  num,
  runCounters,
  runRecordTotal,
  runStatusMeta,
  STAGES,
  truncate,
  verifySummary,
  when,
} from '../lib/format.js';

const TERMINAL = ['COMPLETED', 'FAILED', 'CANCELLED', 'PARTIAL'];

/**
 * The live run view. Used by both the Collect flow and the standalone run page.
 *
 * Three sources, deliberately combined:
 *  - the SSE stream for immediacy and the event log
 *  - `GET /api/runs/{id}` polled while live, for current stage and progress
 *  - the `GET /api/history` row, passed in as `enriched`, because once a run
 *    leaves memory the run endpoint falls back to a flattened row whose
 *    counters lost all field-level detail while the history row kept it
 *
 * When they disagree, counters come from whichever source is richest, the
 * stream owns the log, and the polled view owns stage and progress.
 */
export default function RunView({ runId, title, enriched }) {
  const stream = useRunStream(runId);
  const { setActiveRun, refresh } = useRunContext();
  const nav = useNavigate();
  const { inspect } = useInspector();
  const [polled, setPolled] = useState(null);

  useEffect(() => {
    setActiveRun(runId);
  }, [runId, setActiveRun]);

  useEffect(() => {
    let alive = true;
    let id = null;
    const pull = async () => {
      try {
        const data = await api.run(runId);
        if (!alive) return;
        setPolled(data);
        if (TERMINAL.includes(data?.status)) {
          refresh();
          return; // a finished run is immutable; stop asking
        }
      } catch {
        /* transient — or the backend is busy with this very run. The stream
           carries the truth meanwhile, and the health pulse says so. */
      }
      if (alive) id = setTimeout(pull, 4000);
    };
    pull();
    return () => {
      alive = false;
      if (id) clearTimeout(id);
    };
  }, [runId, refresh]);

  // The polled view owns stage and progress; the stream owns the event log.
  // Neither is authoritative for counters on its own: the polled row goes
  // legacy-flattened once the run leaves memory, and history may not contain a
  // run that was started from this very page. All three are offered to
  // runCounters, which picks whichever actually carries field-level detail.
  const live = polled
    ? { ...stream.run, ...polled, dataset_id: polled.dataset_id ?? stream.run?.dataset_id }
    : stream.run;
  const status = live?.status || 'PLANNING';
  const isTerminal = TERMINAL.includes(status);

  const c = runCounters(live, enriched, polled, stream.run);
  const partial = isPartialRun(live, enriched, polled, stream.run);
  const records = runRecordTotal(live, enriched, polled, stream.run);

  /*
   * The API assigns `status: "PLANNING"` when a run is created and only
   * rewrites it on a terminal event, so a run that is deep into discovery
   * still reports "Planning". The stage is the truthful signal, so the pill
   * says Running and names the stage rather than repeating a stale enum.
   */
  const stageLabel = live?.current_stage
    ? STAGES.find((s) => s.key === live.current_stage)?.label || live.current_stage
    : null;
  const effectivelyRunning = isEffectivelyRunning(live) || isEffectivelyRunning(enriched);
  const meta = effectivelyRunning
    ? { label: 'Running', tone: 'info', glyph: '◍', live: true }
    : runStatusMeta({ ...(live || {}), status, partial });

  const reached = stream.events.filter((e) => e.type === 'stage.started').map((e) => e.stage);
  const vsum = verifySummary(live, enriched, polled, stream.run);

  const cancel = async () => {
    try {
      await api.cancelRun(runId);
      refresh();
    } catch {
      /* the stream will report it */
    }
  };

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <Pill tone={meta.tone} glyph={meta.glyph} live={meta.live}>
              {meta.label}
            </Pill>
            {/* The stage is only worth naming while the run is going. On a
                finished run it is FAILED or COMPLETED, which would just repeat
                the status pill beside it. */}
            {effectivelyRunning && stageLabel ? (
              <Pill tone="muted" title="The API keeps status at PLANNING until the run ends">
                {stageLabel}
              </Pill>
            ) : null}
            {/* Only flag "partial" when the status alone does not already say
                so — otherwise Partial + "partial saved" is the same fact twice. */}
            {partial && meta.label !== 'Partial' && meta.label !== 'Partial (error)' ? (
              <Pill tone="warn" glyph="◐" title="Records were stored before the run stopped early">
                partial saved
              </Pill>
            ) : null}
            {partial ? (
              <span
                className="text-[11px] text-warn"
                title="This run stored real records but did not finish every stage"
              >
                stored what it verified
              </span>
            ) : null}
            <span className="font-mono text-[11px] text-muted">{String(runId).slice(0, 8)}</span>
          </div>
          {title ? (
            <h2 className="mt-1.5 h-display text-[22px] leading-tight text-ink balance">
              {truncate(title, 110)}
            </h2>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <button
            type="button"
            onClick={() => inspect({ kind: 'run', run: live, json: live })}
            className="btn-ghost btn-xs"
          >
            Details
          </button>
          {!isTerminal ? (
            <button type="button" onClick={cancel} className="btn-danger btn-xs h-7 px-2.5">
              Cancel run
            </button>
          ) : live?.dataset_id ? (
            <button
              type="button"
              onClick={() => nav(`/library/${live.dataset_id}`)}
              className="btn-primary h-7 px-3"
            >
              Open dataset →
            </button>
          ) : null}
        </div>
      </header>

      <div className="surface p-3.5">
        <PipelineRail current={live?.current_stage} reached={reached} progress={live?.progress} />
      </div>

      {live?.no_yield_reason ? (
        <Notice tone="warn">
          <span className="font-medium">No records were produced.</span> {live.no_yield_reason}
        </Notice>
      ) : null}

      {live?.error && status !== 'COMPLETED' ? <ErrorNote error={{ message: live.error }} /> : null}

      {stream.error ? (
        <Notice tone="warn">
          {stream.error}
          {stream.attempt ? ` (attempt ${stream.attempt})` : ''}
        </Notice>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label="Sources fetched"
          value={<Counter value={c.successful ?? stream.counts.sourcesFetched} />}
          sub={`${num(c.attempted ?? stream.counts.sourcesDiscovered)} attempted`}
        />
        <Stat
          label="Records"
          value={<Counter value={records || stream.counts.extracted} />}
          sub={
            c.failed
              ? `${num(c.failed)} fetch${c.failed === 1 ? '' : 'es'} failed`
              : 'no fetches failed'
          }
        />
        <Stat
          label="Fields proven"
          value={<Counter value={c.fields_verified ?? 0} />}
          tone={c.fields_verified ? 'ok' : 'muted'}
          sub={
            c.fields_judgment_unavailable
              ? `${num(c.fields_judgment_unavailable)} unjudged`
              : c.fields_unverified
              ? `${num(c.fields_unverified)} unverified`
              : 'all judged'
          }
        />
        <Stat
          label="Fully proven"
          value={<Counter value={c.records_fully_verified ?? 0} />}
          tone={c.records_fully_verified ? 'ok' : 'muted'}
          sub={
            c.records_needing_review
              ? `${num(c.records_needing_review)} of ${num(records)} need review`
              : records
              ? 'no record needs review'
              : 'no review queue'
          }
        />
      </div>

      {vsum?.total ? (
        <div className="surface p-3.5">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <p className="eyebrow">Evidence quality across {num(vsum.total)} fields</p>
            <span className="text-[11px] text-muted">
              {num(vsum.fullyVerified)} of {num(records)} records fully proven
            </span>
          </div>
          <StackedBar rows={vsum.rows} total={vsum.total} height={10} />
          <ul className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1">
            {vsum.rows.map((r) => (
              <li key={r.key} className="flex items-center gap-1.5 text-[11px] text-muted">
                <Dot tone={r.tone} />
                <span className="text-ink-2">{r.label}</span>
                <span className="font-mono tnum text-ink">{num(r.n)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="grid gap-3 lg:grid-cols-[1.6fr_1fr]">
        <EventFeed
          events={stream.events}
          connection={stream.connection}
          height={430}
          emptyHint={isTerminal ? 'This run emitted no events.' : 'Waiting for the first event…'}
        />

        <div className="surface p-3.5">
          <p className="eyebrow mb-2">Latest activity</p>
          {stream.events.length ? (
            <ul className="space-y-2">
              {stream.events
                .slice(-7)
                .reverse()
                .map((e, i) => (
                  <li key={i} className="flex items-start gap-2 text-[11.5px] leading-snug">
                    <Dot
                      tone={
                        e.type === 'record.verified'
                          ? 'ok'
                          : e.type === 'record.rejected' || e.type === 'run.failed'
                          ? 'danger'
                          : e.type === 'record.needs_review' || e.type === 'run.partial'
                          ? 'warn'
                          : e.type === 'source.fetched'
                          ? 'ok'
                          : 'muted'
                      }
                    />
                    <span className="min-w-0 flex-1 text-ink-2">
                      <span className="text-muted">{e.stage || e.type}</span>{' '}
                      {truncate(e.message || e.detail || '', 88)}
                    </span>
                    <span className="shrink-0 font-mono text-[9.5px] text-muted/70">
                      {when(e.timestamp)}
                    </span>
                  </li>
                ))}
            </ul>
          ) : (
            <div className="space-y-2" aria-hidden="true">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="flex items-center gap-2">
                  <Skeleton className="h-1.5 w-1.5 rounded-full" />
                  <Skeleton className="h-2.5 flex-1" style={{ maxWidth: `${90 - i * 12}%` }} />
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
