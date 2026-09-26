import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { getJSON, postJSON, postText, unwrap } from '../lib/api.js';
import { DatasetTable, ProofDrawer, FilterBar, DatasetToolbar } from '../components/studio.jsx';
export default function Dataset() {
  const { id } = useParams();
  const [ds, setDs] = useState(null);
  const [cell, setCell] = useState(null);
  const [field, setField] = useState('');
  const [q, setQ] = useState('');
  useEffect(() => { getJSON(`/api/datasets/${id}`).then((r) => setDs(unwrap(r))).catch(() => {}); }, [id]);
  if (!ds?.id) return <p className="p-6">Loading... (or open via run history once completed)</p>;
  return (
    <div className="p-6">
      <h2 className="font-display text-2xl">{ds.name} · {(ds.records || []).length} records</h2>
      <div className="flex gap-3 my-3"><FilterBar q={q} setQ={setQ} /><DatasetToolbar onExport={async (f) => {
        if (f === 'csv') {
          const r = await postText(`/api/datasets/${id}/export`, { format: 'csv' });
          const blob = new Blob([r.text], { type: 'text/csv' });
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob); a.download = r.filename; a.click();
          setTimeout(() => URL.revokeObjectURL(a.href), 5000);
          return;
        }
        const r = unwrap(await postJSON(`/api/datasets/${id}/export`, { format: 'json' }));
        const blob = new Blob([r.content], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob); a.download = `dataset.json`; a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      }} /></div>
      <DatasetTable dataset={ds} onCell={(c, f) => { setCell(c); setField(f); }} />
      <ProofDrawer cell={cell} field={field} onClose={() => setCell(null)} />
    </div>
  );
}
