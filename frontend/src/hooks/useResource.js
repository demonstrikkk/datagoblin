import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Read-only resource fetch with honest states.
 *
 * Distinguishes `idle | loading | ready | error` and keeps the previous data
 * visible during a refetch (`isStale`) so polling the run view never blanks
 * the screen. Requests are aborted on unmount and on dependency change, so a
 * fast navigation cannot land a stale response.
 */
export default function useResource(fetcher, deps = [], { enabled = true, pollMs = 0 } = {}) {
  const [state, setState] = useState({
    data: null,
    error: null,
    status: enabled ? 'loading' : 'idle',
    isStale: false,
    loadedAt: null,
  });

  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const abortRef = useRef(null);
  const mountedRef = useRef(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setState((s) => ({ ...s, status: 'idle' }));
      return undefined;
    }

    let cancelled = false;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setState((s) => ({
      ...s,
      status: s.data === null ? 'loading' : s.status,
      isStale: s.data !== null,
      error: null,
    }));

    fetcherRef
      .current({ signal: ctrl.signal })
      .then((data) => {
        if (cancelled || ctrl.signal.aborted) return;
        setState({
          data,
          error: null,
          status: 'ready',
          isStale: false,
          loadedAt: Date.now(),
        });
      })
      .catch((err) => {
        if (cancelled || ctrl.signal.aborted || err?.name === 'AbortError') return;
        setState((s) => ({
          ...s,
          error: err,
          status: s.data === null ? 'error' : 'ready',
          isStale: s.data !== null,
        }));
      });

    return () => {
      cancelled = true;
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, ...deps]);

  // polling for live views; pauses when the tab is hidden
  useEffect(() => {
    if (!pollMs || !enabled) return undefined;
    let id = null;
    const tick = () => {
      if (!document.hidden) setNonce((n) => n + 1);
      id = setTimeout(tick, pollMs);
    };
    id = setTimeout(tick, pollMs);
    return () => clearTimeout(id);
  }, [pollMs, enabled]);

  const refetch = useCallback(() => setNonce((n) => n + 1), []);

  return { ...state, refetch };
}
