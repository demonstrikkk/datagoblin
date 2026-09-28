import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api.js';
import useResource from '../hooks/useResource.js';
import { useLocalStore } from '../hooks/useUi.js';

const Ctx = createContext(null);

// A run is live until it reaches a terminal status. `PLANNING` is the status
// the API assigns the instant a run is created, so it counts as live.
const LIVE = new Set(['PLANNING', 'RUNNING', 'QUEUED']);

/**
 * Tracks which run the operator is looking at, so the header chip, the
 * inspector and the run page never disagree.
 *
 * Polls only while the run is actually live, and stops entirely once it
 * reaches a terminal state — a finished run is immutable, so there is nothing
 * to re-poll for.
 */
export function RunContextProvider({ children }) {
  const [runId, setRunId] = useLocalStore('dg_active_run', null);
  const [tick, setTick] = useState(0);

  const { data, status, error } = useResource(
    (o) => api.run(runId, o),
    [runId, tick],
    { enabled: Boolean(runId), pollMs: 5000 }
  );

  const run = runId ? data : null;
  const isLive = Boolean(run && LIVE.has(run.status));

  const setActiveRun = useCallback(
    (id) => {
      setRunId(id || null);
      setTick((n) => n + 1);
    },
    [setRunId]
  );

  const refresh = useCallback(() => setTick((n) => n + 1), []);

  // Exactly one extra fetch on the live -> terminal transition, to pick up the
  // final counts, dataset id and partial flag. Keyed off the status string, not
  // the object identity, or a new payload every poll would re-trigger it.
  const statusKey = data?.status || null;
  const prevStatus = useRef(statusKey);
  useEffect(() => {
    if (prevStatus.current === statusKey) return;
    const wasLive = LIVE.has(prevStatus.current);
    prevStatus.current = statusKey;
    if (wasLive && !LIVE.has(statusKey)) refresh();
  }, [statusKey, refresh]);

  useEffect(() => {
    prevStatus.current = null;
  }, [runId]);

  const value = useMemo(
    () => ({
      runId,
      run,
      status,
      error,
      isLive,
      isStale: status === 'loading' && Boolean(data),
      setActiveRun,
      refresh,
      clearRun: () => setActiveRun(null),
    }),
    [runId, run, status, error, isLive, setActiveRun, refresh]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRunContext() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useRunContext must be used inside <RunContextProvider>');
  return ctx;
}
