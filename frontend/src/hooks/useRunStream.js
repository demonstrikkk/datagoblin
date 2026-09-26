import { useEffect, useState } from 'react';
import { API } from '../lib/api.js';
/** All 14 run-event types (docs/16). Server always sends named events, so every
 * type needs its own listener — onmessage never fires for named events. */
const EVENT_TYPES = ['run.created', 'stage.started', 'stage.progress', 'source.discovered',
  'source.fetched', 'record.extracted', 'record.verified', 'record.rejected',
  'duplicate.detected', 'duplicate.merged', 'stage.completed', 'run.completed',
  'run.failed', 'run.cancelled'];
/** Live run events from GET /api/runs/:id/stream (docs/16). EventSource GET-only, cleanup on unmount. */
export function useRunStream(runId) {
  const [events, setEvents] = useState([]);
  useEffect(() => {
    if (!runId) return;
    const es = new EventSource(API(`/api/runs/${runId}/stream`));
    const append = (e) => {
      try {
        const parsed = JSON.parse(e.data);
        setEvents((p) => (p.length > 2000 ? [...p.slice(-1999), parsed] : [...p, parsed]));
      } catch {}
    };
    EVENT_TYPES.forEach((t) => es.addEventListener(t, append));
    return () => es.close();
  }, [runId]);
  return events;
}
