import { useEffect, useMemo, useState } from 'react';
import { getJSON, postJSON, unwrap } from '../lib/api.js';

const TRANSPORT_LABEL = {
  direct: 'direct REST',
  opencode: 'OpenCode server',
  systemone: 'typed decisions',
};

const DATA_LABEL = {
  private: 'zero-retention',
  'may-train': 'may train on prompts',
  trial: 'provider trial terms',
  contributor: 'training consent',
};

function Registry({ catalogue, selected, onToggle }) {
  const rows = catalogue.text_models || [];
  return (
    <section aria-labelledby="reg-h">
      <h3 id="reg-h" className="font-display text-lg">Free tier</h3>
      <p className="mt-1 text-sm text-muted">{catalogue.gated_note}</p>
      <ul className="mt-3 space-y-2">
        {rows.map((m) => {
          // Advisory only: free-tier availability rotates between runs, so the
          // per-request error below is the real status, not this note.
          const flaky = /intermittent/.test(m.observed || '');
          const on = selected.includes(m.id);
          return (
            <li key={m.id}>
              <label
                className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm transition-colors ${
                  on ? 'border-ink bg-warm' : 'border-rule bg-paper hover:bg-warm'
                }`}
              >
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={on}
                  onChange={() => onToggle(m.id)}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-baseline gap-x-2">
                    <span className="font-semibold">{m.id}</span>
                    <span className="text-xs text-muted">
                      {TRANSPORT_LABEL[m.transport] || m.transport}
                    </span>
                    {flaky && (
                      <span className="rounded bg-stone-200 px-1.5 py-0.5 text-xs">
                        intermittent
                      </span>
                    )}
                  </span>
                  <span className="mt-1 block text-xs text-muted">
                    {DATA_LABEL[m.data] || m.data}
                    {m.note ? ` — ${m.note}` : ''}
                  </span>
                </span>
              </label>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Results({ result }) {
  if (!result) return null;
  const { results = [], consensus = {} } = result;
  return (
    <section aria-labelledby="res-h" className="mt-8">
      <h3 id="res-h" className="font-display text-lg">
        {consensus.answered} of {results.length} answered
      </h3>
      {consensus.agreement && (
        <p className="mt-1 text-sm text-muted">
          Agreement: <strong>{consensus.agreement}</strong> (
          {Math.round((consensus.agreement_ratio || 0) * 100)}%) —{' '}
          {consensus.note}
        </p>
      )}
      <ul className="mt-3 space-y-2">
        {results.map((r) => (
          <li
            key={r.model}
            className={`rounded-md border p-3 text-sm ${
              r.ok ? 'border-rule bg-paper' : 'border-rule bg-stone-100'
            }`}
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-semibold">{r.model}</span>
              <span className="text-xs text-muted">
                {r.ok ? `${r.latency_ms}ms` : 'failed'}
                {r.transport ? ` · ${TRANSPORT_LABEL[r.transport] || r.transport}` : ''}
              </span>
            </div>
            {r.ok ? (
              <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs">
                {typeof r.answer === 'string' ? r.answer : JSON.stringify(r.answer, null, 2)}
              </pre>
            ) : (
              <p className="mt-1 text-xs text-muted">{r.error}</p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function Intel() {
  const [catalogue, setCatalogue] = useState(null);
  const [selected, setSelected] = useState([]);
  const [prompt, setPrompt] = useState('');
  const [url, setUrl] = useState('');
  const [question, setQuestion] = useState('');
  const [asJson, setAsJson] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    getJSON('/api/models/free')
      .then((r) => {
        const data = unwrap(r);
        setCatalogue(data);
        setSelected((data.configured || []).filter((id) =>
          (data.text_models || []).some((m) => m.id === id)));
      })
      .catch((e) => setError(e.message));
  }, []);

  const gate = catalogue ? TRANSPORT_GATE(catalogue) : null;
  const canSubmit = useMemo(
    () => (prompt.trim() || (url.trim() && question.trim())) && selected.length > 0 && !busy,
    [prompt, url, question, selected, busy]
  );

  const toggle = (id) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError('');
    setResult(null);
    try {
      const body = { models: selected, json: asJson };
      if (url.trim()) {
        body.url = url.trim();
        body.question = question.trim();
      } else {
        body.prompt = prompt.trim();
      }
      setResult(unwrap(await postJSON('/api/intel/ask', body)));
    } catch (e2) {
      setError(e2.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <h2 className="font-display text-2xl">Free-model intel</h2>
      <p className="mt-1 text-sm text-muted">
        Ask the whole free tier the same question and see where they disagree.
      </p>

      {!catalogue && !error && (
        <p className="mt-6 text-sm text-muted">Loading free models…</p>
      )}
      {error && (
        <p role="alert" className="mt-6 rounded-md border border-rule bg-stone-100 p-3 text-sm">
          {error}
        </p>
      )}

      {catalogue && (
        <div className="mt-6 space-y-8">
          {gate && !gate.reachable && (
            <p className="rounded-md border border-rule bg-warm p-3 text-sm">
              The OpenCode server is not reachable, so 9 of {catalogue.counts.text}{' '}
              free models cannot answer yet. Start it with{' '}
              <code>opencode serve --port 4096</code> and set{' '}
              <code>OPENCODE_PASSWORD</code>.
            </p>
          )}

          <form onSubmit={submit} className="space-y-4">
            <div>
              <label htmlFor="intel-url" className="text-sm font-semibold">
                Page URL (optional)
              </label>
              <input
                id="intel-url"
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://example.com/report"
                className="mt-1 w-full rounded-md border border-rule bg-paper px-3 py-2 text-sm"
              />
              <p className="mt-1 text-xs text-muted">
                Fetched server-side, same robots and rate-limit rules as a run.
              </p>
            </div>

            {url.trim() && (
              <div>
                <label htmlFor="intel-q" className="text-sm font-semibold">
                  Question about that page
                </label>
                <input
                  id="intel-q"
                  type="text"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  className="mt-1 w-full rounded-md border border-rule bg-paper px-3 py-2 text-sm"
                />
              </div>
            )}

            {!url.trim() && (
              <div>
                <label htmlFor="intel-prompt" className="text-sm font-semibold">
                  Prompt
                </label>
                <textarea
                  id="intel-prompt"
                  rows={4}
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  className="mt-1 w-full rounded-md border border-rule bg-paper px-3 py-2 text-sm"
                />
              </div>
            )}

            <div className="flex items-center gap-2 text-sm">
              <input
                id="intel-json"
                type="checkbox"
                checked={asJson}
                onChange={(e) => setAsJson(e.target.checked)}
              />
              <label htmlFor="intel-json">Ask for JSON (enables agreement scoring)</label>
            </div>

            <Registry
              catalogue={catalogue}
              selected={selected}
              onToggle={toggle}
            />

            <button
              type="submit"
              disabled={!canSubmit}
              className="rounded-md bg-ink px-4 py-2 text-sm font-semibold text-paper disabled:opacity-40"
            >
              {busy ? 'Asking…' : `Ask ${selected.length} model${selected.length === 1 ? '' : 's'}`}
            </button>
          </form>

          <Results result={result} />
        </div>
      )}
    </div>
  );
}

function TRANSPORT_GATE(catalogue) {
  return catalogue.opencode || null;
}
