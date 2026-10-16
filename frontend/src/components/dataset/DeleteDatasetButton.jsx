import { useState } from 'react';
import { api } from '../../lib/api.js';
import { num } from '../../lib/format.js';

/**
 * Delete one dataset, asked twice.
 *
 * This is the only destructive control in the product, so it is built to feel
 * that way: the first press only arms it, the second names exactly what goes
 * ("Delete 8 records?"), and the server independently requires an exact
 * `confirm` equal to the id — a stray or replayed call deletes nothing either
 * way. There is no undo, and the confirm says so.
 *
 * What goes: the dataset row, its records, its gap history, its export ledger.
 * What stays: the run (the audit trail of work that happened) and its stored
 * pages (reusable by future runs).
 *
 * `onDeleted` fires after the server confirms, so the caller can navigate away
 * from a page whose subject no longer exists.
 */
export default function DeleteDatasetButton({
  datasetId,
  recordCount = null,
  onDeleted,
  compact = false,
  className = '',
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);

  async function remove(e) {
    e?.preventDefault?.();
    e?.stopPropagation?.();
    if (!armed) {
      setArmed(true);
      setFailure(null);
      return;
    }
    setBusy(true);
    setFailure(null);
    try {
      await api.deleteDataset(datasetId);
      onDeleted?.();
    } catch (err) {
      setFailure(err);
      setArmed(false);
    } finally {
      setBusy(false);
    }
  }

  function standDown(e) {
    e?.preventDefault?.();
    e?.stopPropagation?.();
    setArmed(false);
    setFailure(null);
  }

  if (!armed) {
    return (
      <span className={className}>
        <button
          type="button"
          onClick={remove}
          title="Delete this dataset, its records and its gap history. The run and its stored pages stay."
          className={compact ? 'btn-ghost btn-xs text-muted hover:text-danger' : 'btn-ghost btn-xs text-muted hover:text-danger'}
        >
          Delete
        </button>
        {failure ? (
          <span className="ml-1.5 text-[10.5px] text-danger" role="alert">
            {failure?.message || 'Delete failed'}
          </span>
        ) : null}
      </span>
    );
  }

  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`} onClick={(e) => { e.preventDefault?.(); e.stopPropagation?.(); }}>
      <span className="text-[11px] text-muted">
        Delete{recordCount !== null ? ` ${num(recordCount)} records` : ' this dataset'}? No undo.
      </span>
      <button
        type="button"
        disabled={busy}
        onClick={remove}
        className="btn-danger btn-xs"
      >
        {busy ? 'Deleting…' : 'Yes, delete'}
      </button>
      <button type="button" disabled={busy} onClick={standDown} className="btn-ghost btn-xs">
        Keep
      </button>
    </span>
  );
}
