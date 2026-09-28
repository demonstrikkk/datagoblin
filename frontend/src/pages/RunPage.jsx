import { useParams, Link } from 'react-router-dom';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useRunContext } from '../lib/run-context.jsx';
import RunView from '../components/RunView.jsx';
import { Empty, ErrorNote, Loading, Notice } from '../components/ui.jsx';
import { runId, truncate, when } from '../lib/format.js';

export default function RunPage() {
  const { id } = useParams();
  const { setActiveRun } = useRunContext();

  /*
   * A run started from this very page is not in history when the page mounts,
   * so a one-shot fetch left `enriched` permanently null and the view fell back
   * to the run endpoint's flattened counters. Polling while the run is live
   * closes that window. The stream carries the same numbers for display, so
   * this is a consistency backstop, not the only source.
   */
  const { data: history } = useResource((o) => api.history(o), [], { pollMs: 10_000 });
  const row = Array.isArray(history) ? history.find((r) => runId(r) === id) : null;

  const { status, error } = useResource((o) => api.run(id, o), [id], {
    pollMs: 5000,
  });

  if (status === 'loading' && !row) return <Loading rows={6} label="Loading run" />;

  if (error) {
    return (
      <div className="space-y-4">
        <ErrorNote error={error} />
        <Notice tone="muted">
          A run only exists in memory while this backend process is alive. Once it restarts,
          finished runs remain in the database but their live state is gone — the dataset they
          wrote does not.
        </Notice>
        <Link to="/runs" className="btn-outline btn-xs">
          ← Back to runs
        </Link>
      </div>
    );
  }

  if (!row && status === 'error') return <Empty title="Run not found" />;

  return (
    <div className="space-y-4">
      <nav className="flex items-center gap-1.5 text-[11.5px] text-muted">
        <Link to="/runs" className="link">
          Runs
        </Link>
        <span aria-hidden="true">/</span>
        <span className="font-mono text-ink-2">{String(id).slice(0, 8)}</span>
        {row?.started_at || row?.created_at ? (
          <span className="ml-1">· {when(row.started_at || row.created_at)}</span>
        ) : null}
      </nav>

      {/* The history row is passed in because it keeps the field-level
          counters that `GET /runs/{id}` drops once a run leaves memory. */}
      <RunView runId={id} enriched={row} title={row?.query || row?.goal || truncate(id, 40)} />
    </div>
  );
}
