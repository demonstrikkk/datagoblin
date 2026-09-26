import { useEffect, useState } from 'react';
import { getJSON, unwrap } from '../lib/api.js';
import { HistoryList } from '../components/studio.jsx';
/** Repo rows use `id`; live run views use `run_id` — normalize for HistoryList. */
function normalize(h) {
  return { run_id: h.run_id || h.id, status: h.status || 'UNKNOWN',
           created_at: h.created_at || h.started_at || '' };
}
export default function History() {
  const [items, setItems] = useState([]);
  useEffect(() => { getJSON('/api/history').then((r) => setItems((unwrap(r) || []).map(normalize))).catch(() => {}); }, []);
  return <div className="max-w-3xl mx-auto p-6"><h2 className="font-display text-2xl">Collection history</h2><HistoryList items={items} /></div>;
}
