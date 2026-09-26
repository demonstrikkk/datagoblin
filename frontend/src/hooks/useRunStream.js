import { useEffect, useState } from 'react';
import { API } from '../lib/api.js';
/** All 14 run-event types (docs/16). Server always sends named events, so every
 * type needs its own listener — onmessage never fires for named events. */
const EVENT_TYPES = ['run.created', 'stage.started', 'stage.progress', 'source.discovered',
  'source.fetched', 'record.extracted', 'record.verified', 'record.rejected',
  'duplicate.detected', 'duplicate.merged', 'stage.completed', 'run.completed',
  'run.failed', 'run.cancelled'];
/** Live run events from GET /api/runs/:id/stream (docs/16). EventSource GET-only,
 * cleanup on unmount. Closes on terminal events — otherwise the browser's native
 * reconnect loop hammers /stream forever after a run ends (replay + break, repeat). */
const TERMINAL = new Set(['run.completed', 'run.failed', 'run.cancelled']);
export function useRunStream(runId) {
  const [events, setEvents] = useState([]);
  useEffect(() => {
    if (!runId) return;
    const es = new EventSource(API(`/api/runs/${runId}/stream`));
    const append = (e) => {
      try {
        const parsed = JSON.parse(e.data);
        setEvents((p) => (p.length > 2000 ? [...p.slice(-1999), parsed] : [...p, parsed]));
        if (TERMINAL.has(parsed.type)) es.close();
      } catch {}
    };
    EVENT_TYPES.forEach((t) => es.addEventListener(t, append));
    return () => es.close();
  }, [runId]);
  return events;
}
