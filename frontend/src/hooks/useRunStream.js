import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

/**
 * Live run stream over SSE.
 *
 * Fixes the defects the backend audit surfaced:
 *  - `run.partial` is now APPLIED to state (it was received and discarded)
 *  - a terminal state closes the socket. A PARTIAL run is persisted as
 *    FAILED + partial=true and its stream is never closed server-side, so
 *    relying on `run.completed` alone left the UI spinning forever
 *  - a real connection status is exposed instead of pretending to be live
 *  - events are deduped by (type + sequence + payload fingerprint) because
 *    EventSource replays the last events on reconnect
 *  - backoff is bounded and the socket is torn down on unmount / id change
 */

const TERMINAL = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'PARTIAL']);

const fingerprint = (e) => {
  const t = e.type || '';
  if (t === 'stage.started' || t === 'stage.progress') return `${t}:${e.stage}`;
  if (t.startsWith('record.')) {
    return `${t}:${e.run_id || ''}:${e.qualifier || e.value || e.reason || ''}`;
  }
  if (t === 'source.fetched' || t === 'source.discovered') {
    return `${t}:${e.url || e.domain || ''}`;
  }
  return `${t}:${e.stage || ''}:${e.error || e.reason || ''}`;
};

const initial = {
  connection: 'idle', // idle | connecting | open | reconnecting | closed | error
  events: [],
  run: null,
  partial: false,
  error: null,
  attempt: 0,
  lastEventAt: null,
  counts: {
    stages: 0,
    sourcesDiscovered: 0,
    sourcesFetched: 0,
    // Kept apart from sourcesFetched: a reused source is a source the run did
    // not request. Counting it as fetched would make a run that re-fetched
    // nothing look identical to one that did the work.
    sourcesReused: 0,
    extracted: 0,
    verified: 0,
    needsReview: 0,
    rejected: 0,
    merged: 0,
  },
};

function tally(counts, e) {
  switch (e.type) {
    case 'stage.started':
      counts.stages += 1;
      break;
    case 'source.discovered':
      counts.sourcesDiscovered += 1;
      break;
    case 'source.fetched':
      counts.sourcesFetched += 1;
      break;
    case 'source.reused':
      counts.sourcesReused += 1;
      break;
    case 'record.extracted':
      counts.extracted += 1;
      break;
    case 'record.verified':
      counts.verified += 1;
      break;
    case 'record.needs_review':
      counts.needsReview += 1;
      break;
    case 'record.rejected':
      counts.rejected += 1;
      break;
    case 'duplicate.merged':
      counts.merged += 1;
      break;
    default:
      break;
  }
}

/**
 * Fold a terminal event's `data` payload into run state.
 *
 * The terminal event carries the authoritative aggregates in `data` —
 * `fields_verified`, `fields_unverified`, `records_needing_review`,
 * `dataset_id` and so on. `GET /runs/{id}` returns a *flattened legacy* row
 * once the run leaves memory, with none of those keys, so reading counters
 * from that endpoint after the fact reported "0 fields proven" for a run that
 * had verified 160. Taking the numbers straight from the event removes the
 * dependency on a second request entirely.
 */
function mergeTerminalData(next, ev) {
  const d = ev.data && typeof ev.data === 'object' ? ev.data : null;
  if (!d || !Object.keys(d).length) return;
  next.run = {
    ...next.run,
    dataset_id: d.dataset_id ?? next.run.dataset_id,
    counters: { ...(next.run.counters || {}), ...d },
  };
}

export default function useRunStream(runId, { enabled = true } = {}) {
  const [state, setState] = useState(initial);
  const seenRef = useRef(new Set());
  const esRef = useRef(null);
  const timerRef = useRef(null);
  const attemptRef = useRef(0);
  const closedRef = useRef(false);

  const reset = useCallback(() => {
    seenRef.current = new Set();
    attemptRef.current = 0;
    closedRef.current = false;
    setState(initial);
  }, []);

  useEffect(() => {
    if (!runId || !enabled) return undefined;

    reset();
    setState((s) => ({ ...s, connection: 'connecting' }));

    let disposed = false;

    const close = () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };

    const scheduleReconnect = () => {
      if (disposed || closedRef.current) return;
      attemptRef.current += 1;
      const attempt = attemptRef.current;
      // 1s, 2s, 4s, 8s, capped at 15s — the run is long-lived, so be patient
      const delay = Math.min(1000 * 2 ** (attempt - 1), 15_000);
      setState((s) => ({
        ...s,
        connection: 'reconnecting',
        attempt,
        error:
          attempt >= 6
            ? 'Lost contact with the run stream. Retrying — the run is still executing server-side.'
            : s.error,
      }));
      timerRef.current = setTimeout(open, delay);
    };

    const apply = (raw) => {
      let e;
      try {
        e = JSON.parse(raw);
      } catch {
        return;
      }
      if (!e || typeof e !== 'object') return;

      // `stage` is the declared event shape; some emitters also send `data`
      const type = e.type || e.event;
      if (!type) return;
      const ev = { ...e, type };

      const key = fingerprint(ev);
      if (seenRef.current.has(key)) return;
      seenRef.current.add(key);

      setState((s) => {
        const next = {
          ...s,
          events: [...s.events, ev].slice(-400),
          lastEventAt: Date.now(),
          counts: { ...s.counts },
        };
        tally(next.counts, ev);

        switch (type) {
          case 'run.partial':
            // previously dropped on the floor
            next.partial = true;
            next.run = {
              ...(s.run || {}),
              ...(ev.run || {}),
              status: 'PARTIAL',
              partial: true,
            };
            mergeTerminalData(next, ev);
            break;
          case 'run.completed':
            next.run = { ...(s.run || {}), ...(ev.run || {}), status: 'COMPLETED', partial: false };
            next.partial = false;
            mergeTerminalData(next, ev);
            break;
          case 'run.failed':
            next.run = {
              ...(s.run || {}),
              ...(ev.run || {}),
              status: ev.partial ? 'PARTIAL' : 'FAILED',
              partial: Boolean(ev.partial),
            };
            next.partial = Boolean(ev.partial);
            mergeTerminalData(next, ev);
            break;
          case 'run.cancelled':
            next.run = { ...(s.run || {}), ...(ev.run || {}), status: 'CANCELLED' };
            break;
          case 'stage.started':
          case 'stage.progress':
            if (ev.stage) {
              next.run = {
                ...(s.run || {}),
                stage: ev.stage,
                stages: mergeStage(s.run?.stages, ev),
              };
            }
            break;
          default:
            if (ev.run) next.run = { ...(s.run || {}), ...ev.run };
            break;
        }

        const status = next.run?.status;
        if (status && TERMINAL.has(status)) {
          closedRef.current = true;
          next.connection = 'closed';
        }
        return next;
      });

      if (closedRef.current) {
        // terminal reached: let the socket go, the REST view is now canonical
        setTimeout(() => {
          if (!disposed) close();
        }, 350);
      }
    };

    const open = () => {
      if (disposed || closedRef.current) return;
      let es;
      try {
        es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
      } catch {
        setState((s) => ({ ...s, connection: 'error', error: 'Could not open the run stream.' }));
        return;
      }
      esRef.current = es;

      es.onopen = () => {
        if (disposed) return;
        attemptRef.current = 0;
        setState((s) => ({ ...s, connection: 'open', attempt: 0, error: null }));
      };

      es.onmessage = (ev) => apply(ev.data);

      // named listeners for every declared event type, so a server that sets
      // `event:` is handled identically to a bare message
        const named = [
          'run.partial',
          'stage.started',
          'stage.progress',
          'source.discovered',
          'source.fetched',
          // Missing here, so the frame never reached this hook at all. The
          // server names every frame `event: <type>`, and EventSource only routes
          // *untyped* frames to onmessage, so an unlisted name is dropped before
          // `apply` runs — the `case 'source.reused'` below was unreachable. A
          // reused source was invisible in the run view while being counted in
          // the sources panel.
          'source.reused',
          'record.extracted',
          'record.verified',
          'record.needs_review',
          'record.rejected',
          'duplicate.merged',
          'run.completed',
          'run.failed',
          'run.cancelled',
        ];
      named.forEach((n) => es.addEventListener(n, (ev) => apply(ev.data)));

      es.onerror = () => {
        if (disposed || closedRef.current) return;
        // EventSource retries on its own, but we drive it so the UI can be honest
        // about state and stop the browser's opaque auto-retry.
        es.close();
        esRef.current = null;
        if (closedRef.current) return;
        scheduleReconnect();
      };
    };

    open();

    return () => {
      disposed = true;
      close();
    };
  }, [runId, enabled, reset]);

  const dismissError = useCallback(() => {
    setState((s) => ({ ...s, error: null }));
  }, []);

  return useMemo(() => ({ ...state, dismissError }), [state, dismissError]);
}

function mergeStage(list, ev) {
  const out = Array.isArray(list) ? list.slice() : [];
  const i = out.findIndex((s) => s?.stage === ev.stage);
  const next = {
    ...(i >= 0 ? out[i] : {}),
    stage: ev.stage,
    progress: ev.progress ?? out[i]?.progress,
    detail: ev.detail ?? out[i]?.detail,
    status: ev.type === 'stage.started' ? 'running' : out[i]?.status || 'running',
  };
  if (i >= 0) out[i] = next;
  else out.push(next);
  return out;
}
