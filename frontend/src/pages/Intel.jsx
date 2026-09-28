import { useMemo, useState } from 'react';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import {
  CopyButton,
  Dot,
  Empty,
  ErrorNote,
  Field,
  Loading,
  Notice,
  Pill,
  Segmented,
  Spinner,
  Stat,
  Textarea,
  Toggle,
} from '../components/ui.jsx';
import { host, num, pct, truncate, when } from '../lib/format.js';

/* -------------------------------------------------------------- agreement */

function AgreementBadge({ consensus }) {
  if (!consensus) return null;
  const { agreement, distinct_models: distinct, independent, answered, requested } = consensus;

  // The label the backend computes is the source of truth. It distinguishes
  // genuine multi-model agreement from one model echoed several times, and the
  // UI refuses to call the latter consensus.
  const map = {
    'single-model': { tone: 'warn', glyph: '⚠', label: 'Single model only' },
    unanimous: { tone: 'ok', glyph: '✓', label: 'Unanimous' },
    majority: { tone: 'info', glyph: '◑', label: 'Majority' },
    split: { tone: 'danger', glyph: '≠', label: 'Split' },
    none: { tone: 'muted', glyph: '·', label: 'No answers' },
  };
  const m = map[agreement] || { tone: 'muted', glyph: '·', label: agreement || 'unknown' };

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Pill tone={m.tone} glyph={m.glyph}>
        {m.label}
      </Pill>
      <Pill tone={distinct > 1 ? 'ok' : 'warn'} title="Distinct models that actually produced an answer">
        {num(distinct)} distinct model{distinct === 1 ? '' : 's'}
      </Pill>
      {independent ? null : (
        <Pill tone="warn" title="Agreement here is not independent — verify against sources">
          not independent
        </Pill>
      )}
      <span className="text-[11px] text-muted">
        {answered} of {requested} answered
      </span>
    </div>
  );
}

function ResultCard({ row, rank }) {
  const { inspect } = useInspector();
  const mismatch = row.model_mismatch || (row.served_by && row.served_by !== row.model);

  if (!row.ok) {
    return (
      <article className="surface overflow-hidden opacity-90">
        <header className="flex items-center justify-between gap-2 border-b border-rule bg-danger-soft px-3 py-2">
          <span className="flex min-w-0 items-center gap-1.5">
            <Dot tone="danger" />
            <code className="truncate font-mono text-[11.5px] text-ink">{row.model}</code>
          </span>
          <Pill tone="danger">failed</Pill>
        </header>
        <p className="px-3 py-2.5 text-[12px] leading-relaxed text-ink-2">
          {row.error || 'No answer returned.'}
        </p>
        {row.attempts > 1 ? (
          <p className="border-t border-rule px-3 py-1.5 font-mono text-[10.5px] text-muted">
            {row.attempts} attempts
          </p>
        ) : null}
      </article>
    );
  }

  const text =
    typeof row.answer === 'string'
      ? row.answer
      : row.answer
      ? JSON.stringify(row.answer, null, 2)
      : row.text || '';

  return (
    <article className="surface flex flex-col overflow-hidden">
      <header className="flex items-center justify-between gap-2 border-b border-rule px-3 py-2">
        <span className="flex min-w-0 items-center gap-1.5">
          <Dot tone="ok" />
          <code className="truncate font-mono text-[11.5px] text-ink">{row.model}</code>
          {row.attempts > 1 ? (
            <span className="font-mono text-[10px] text-muted">×{row.attempts}</span>
          ) : null}
        </span>
        <button
          type="button"
          onClick={() => inspect({ kind: 'event', event: row, title: row.model, json: row })}
          className="btn-ghost btn-xs shrink-0"
        >
          Raw
        </button>
      </header>

      {mismatch ? (
        <div className="flex items-center gap-1.5 border-b border-warn/20 bg-warn-soft px-3 py-1.5 text-[11px] text-warn">
          <span aria-hidden="true">⚠</span>
          <span className="min-w-0 truncate">
            Requested <code className="font-mono">{row.model}</code> but{' '}
            <code className="font-mono">{row.served_by || 'another model'}</code> answered
          </span>
        </div>
      ) : null}

      <div className="flex-1 px-3 py-2.5">
        <pre className="whitespace-pre-wrap break-words font-sans text-[12.5px] leading-relaxed text-ink-2">
          {text}
        </pre>
      </div>

      <footer className="flex items-center justify-between gap-2 border-t border-rule px-3 py-1.5">
        <span className="text-[10.5px] text-muted">
          served by <span className="font-mono">{row.served_by || row.model}</span>
        </span>
        {rank ? <Pill tone="muted">cluster {rank}</Pill> : null}
      </footer>
    </article>
  );
}

/* ------------------------------------------------------------------- page */

export default function Intel() {
  const [mode, setMode] = useState('prompt');
  const [prompt, setPrompt] = useState('');
  const [url, setUrl] = useState('');
  const [question, setQuestion] = useState('');
  const [useJson, setUseJson] = useState(false);
  const [selected, setSelected] = useState([]);
  const [answer, setAnswer] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const { data: registry, status: regStatus, error: regError, refetch } = useResource(
    (o) => api.freeModels(o),
    [],
    { pollMs: 60_000 }
  );

  const textModels = registry?.text_models || [];
  const decisionModels = registry?.decision_models || [];
  const zen = registry?.zen || {};
  const opencode = registry?.opencode || {};
  const gatedNote = registry?.gated_note || '';
  const transports = registry?.transports || [];

  /*
   * Reachability, stated honestly.
   *
   * `text_models[]` carries no `reachable` boolean. It carries `transport`,
   * `observed` (e.g. "ok 2026-09-27") and sometimes a `note`. Treating a
   * missing field as `reachable !== false` would paint every model green,
   * including the ones the registry itself says answer 403 on direct Zen —
   * which is exactly the false confidence this tool exists to eliminate.
   *
   * So: green only when `observed` starts with "ok". A model routed through
   * OpenCode is marked amber — reachable, but only while the local server is.
   */
  const health = (m) => {
    const observed = String(m.observed || '');
    if (/^ok\b/i.test(observed)) return { tone: 'ok', label: 'ok' };
    if (m.transport === 'opencode') return { tone: 'warn', label: 'via opencode' };
    if (m.transport === 'systemone') return { tone: 'judge', label: 'systemone' };
    if (observed) return { tone: 'danger', label: observed };
    return { tone: 'muted', label: 'unobserved' };
  };

  const tally = useMemo(() => {
    const acc = { ok: 0, warn: 0, other: 0 };
    textModels.forEach((m) => {
      const h = health(m);
      if (h.tone === 'ok') acc.ok += 1;
      else if (h.tone === 'warn') acc.warn += 1;
      else acc.other += 1;
    });
    return acc;
  }, [textModels]);

  const toggle = (id) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  const ask = async () => {
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      const body =
        mode === 'url'
          ? { url: url.trim(), ...(question ? { question } : {}), json: useJson }
          : { prompt: prompt.trim(), json: useJson };
      if (selected.length) body.models = selected;
      const res = await api.ask(body);
      setAnswer(res);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const consensus = answer?.consensus;
  const results = answer?.results || [];
  const ordered = useMemo(() => {
    if (!consensus?.clusters) return results;
    const rank = new Map();
    consensus.clusters.forEach((c, i) => c.models.forEach((m) => rank.set(m, i + 1)));
    return [...results].sort(
      (a, b) => (rank.get(a.served_by || a.model) || 99) - (rank.get(b.served_by || b.model) || 99)
    );
  }, [results, consensus]);

  return (
    <div className="space-y-5">
      <header>
        <h2 className="h-display text-[26px] leading-tight text-ink">Intel</h2>
        <p className="mt-1 max-w-2xl text-[12.5px] leading-relaxed text-muted">
          Ask one question across the free model tier and see where the answers actually agree.
          Agreement is only counted across distinct models — one model answering several times is
          reported as an echo, not as consensus.
        </p>
      </header>

      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <div className="space-y-3">
          <Segmented
            value={mode}
            onChange={setMode}
            options={[
              { value: 'prompt', label: 'Ask a question' },
              { value: 'url', label: 'Analyse a page' },
            ]}
          />

          <div className="surface space-y-3 p-3.5">
            {mode === 'prompt' ? (
              <Field
                label="Prompt"
                required
                hint="Sent to every selected model. One credit for the whole fan-out, not per model."
              >
                <Textarea
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  rows={5}
                  placeholder="Which EU climate software companies raised a Series B after 2023?"
                  className="min-h-[104px] text-[13.5px]"
                />
              </Field>
            ) : (
              <>
                <Field
                  label="URL"
                  required
                  hint="Fetched server-side through the same crawler waterfall runs use. The browser never contacts the target."
                >
                  <input
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="https://example.org/report"
                    className="input font-mono text-[12px]"
                  />
                </Field>
                <Field
                  label="Question"
                  hint="Leave blank to summarise the page."
                >
                  <input
                    value={question}
                    onChange={(e) => setQuestion(e.target.value)}
                    placeholder="What does this page claim, and who published it?"
                    className="input-sm input"
                  />
                </Field>
              </>
            )}

            <Toggle
              checked={useJson}
              onChange={setUseJson}
              label="Request JSON"
              hint="Models are asked to return structured data. Cluster comparison is stricter, so agreement is more meaningful."
            />

            {error ? <ErrorNote error={error} /> : null}

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={ask}
                disabled={busy || (mode === 'prompt' ? !prompt.trim() : !url.trim())}
                className="btn-accent"
              >
                {busy ? (
                  <>
                    <Spinner /> Asking {num(selected.length || textModels.length)} models…
                  </>
                ) : (
                  'Fan out'
                )}
              </button>
              {selected.length ? (
                <button type="button" onClick={() => setSelected([])} className="btn-ghost btn-xs">
                  Clear {selected.length} selected
                </button>
              ) : (
                <span className="text-[11.5px] text-muted">
                  All {num(textModels.length)} text models will be asked
                </span>
              )}
            </div>
          </div>

          {answer ? (
            <div className="space-y-3 animate-fade-up">
              <div className="surface space-y-2.5 p-3.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <AgreementBadge consensus={consensus} />
                  <span className="text-[11px] text-muted">
                    {answer.credits_used ?? 1} credit{answer.credits_used === 1 ? '' : 's'} ·{' '}
                    {answer.job_id}
                  </span>
                </div>
                {consensus?.agreement_ratio !== null && consensus?.agreement_ratio !== undefined ? (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-warm">
                      <div
                        className="h-full rounded-full bg-accent transition-all duration-700 ease-swift"
                        style={{ width: pct(consensus.agreement_ratio) }}
                      />
                    </div>
                    <span className="shrink-0 font-mono text-[10.5px] text-muted">
                      {pct(consensus.agreement_ratio)} agreement
                    </span>
                  </div>
                ) : null}
                {consensus?.note ? (
                  <p className="text-[11.5px] leading-relaxed text-ink-2">{consensus.note}</p>
                ) : null}
                {answer.source?.url ? (
                  <p className="flex flex-wrap items-center gap-1.5 border-t border-rule pt-2 text-[11px] text-muted">
                    <span>Source</span>
                    <a
                      href={answer.source.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="link"
                    >
                      {host(answer.source.url)}
                    </a>
                    {answer.source.chars ? (
                      <span className="font-mono">{num(answer.source.chars)} chars</span>
                    ) : null}
                    {answer.source.rendered ? <Pill tone="info">rendered</Pill> : null}
                    {answer.source.thin ? (
                      <Pill tone="warn" title="Very little text was extracted">
                        thin
                      </Pill>
                    ) : null}
                  </p>
                ) : null}
              </div>

              {ordered.length ? (
                <div className="grid gap-3 md:grid-cols-2">
                  {ordered.map((row) => (
                    <ResultCard key={row.model} row={row} />
                  ))}
                </div>
              ) : (
                <div className="surface">
                  <Empty title="No answers" hint="Every model failed. The raw error for each is above." />
                </div>
              )}
            </div>
          ) : null}
        </div>

        <aside className="space-y-3">
          <section className="surface overflow-hidden">
            <header className="flex items-center justify-between gap-2 border-b border-rule px-3 py-2">
              <h3 className="eyebrow">Free tier</h3>
              <button type="button" onClick={refetch} className="btn-ghost btn-xs">
                Refresh
              </button>
            </header>

            {regStatus === 'loading' && !registry ? (
              <div className="p-3">
                <Loading rows={5} label="Loading registry" />
              </div>
            ) : regError ? (
              <div className="p-3">
                <ErrorNote error={regError} onRetry={refetch} compact />
              </div>
            ) : (
              <>
                <div className="grid grid-cols-3 divide-x divide-rule border-b border-rule">
                  <div className="px-3 py-2">
                    <p className="eyebrow">Direct</p>
                    <p className="mt-0.5 font-mono text-[16px] leading-none text-ok">{num(tally.ok)}</p>
                    <p className="mt-1 text-[10px] leading-tight text-muted">observed ok</p>
                  </div>
                  <div className="px-3 py-2">
                    <p className="eyebrow">Routed</p>
                    <p className="mt-0.5 font-mono text-[16px] leading-none text-warn">{num(tally.warn)}</p>
                    <p className="mt-1 text-[10px] leading-tight text-muted">via OpenCode</p>
                  </div>
                  <div className="px-3 py-2">
                    <p className="eyebrow">Judges</p>
                    <p className="mt-0.5 font-mono text-[16px] leading-none text-ink">
                      {num(decisionModels.length)}
                    </p>
                    <p className="mt-1 text-[10px] leading-tight text-muted">not asked here</p>
                  </div>
                </div>

                {gatedNote ? (
                  <p className="border-b border-rule bg-warn-soft px-3 py-2 text-[11px] leading-relaxed text-warn">
                    {gatedNote}
                  </p>
                ) : null}

                <ul className="scroll-y max-h-[380px] divide-y divide-rule overflow-y-auto">
                  {textModels.map((m) => {
                    const id = m.id || m.model || m.name;
                    const on = selected.includes(id);
                    const h = health(m);
                    return (
                      <li key={id}>
                        <button
                          type="button"
                          onClick={() => toggle(id)}
                          className="row flex w-full items-center gap-2 px-3 py-1.5 text-left"
                          data-active={on}
                          title={m.note || h.label}
                        >
                          <span
                            className={`grid h-3 w-3 shrink-0 place-items-center rounded-[3px] border text-[8px] transition-colors ${
                              on ? 'border-accent bg-accent text-paper-2' : 'border-rule-2'
                            }`}
                            aria-hidden="true"
                          >
                            {on ? '✓' : ''}
                          </span>
                          <Dot tone={h.tone} title={h.label} />
                          <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-2">
                            {id}
                          </code>
                          <span className="shrink-0 font-mono text-[9.5px] uppercase text-muted">
                            {m.transport}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </section>

          <section className="surface space-y-2 p-3">
            <p className="eyebrow">Transports</p>
            {transports.length ? (
              <div className="flex flex-wrap gap-1">
                {transports.map((t) => (
                  <code
                    key={t}
                    className="rounded-xs border border-rule bg-warm/60 px-1.5 py-0.5 font-mono text-[10px] text-ink-2"
                  >
                    {t}
                  </code>
                ))}
              </div>
            ) : null}
            <div className="flex items-center justify-between gap-2 text-[11.5px]">
              <span className="text-muted">OpenCode server</span>
              <span className="flex items-center gap-1.5">
                <Dot tone={opencode.reachable ? 'ok' : 'danger'} />
                <span className="text-ink-2">
                  {opencode.reachable
                    ? `reachable${opencode.http_status ? ` (${opencode.http_status})` : ''}`
                    : opencode.error
                    ? 'unreachable'
                    : 'unknown'}
                </span>
              </span>
            </div>
            {opencode.base_url ? (
              <p className="font-mono text-[10px] text-muted">{opencode.base_url}</p>
            ) : null}
            <div className="flex items-center justify-between gap-2 text-[11.5px]">
              <span className="text-muted">Zen direct</span>
              <span className="flex items-center gap-1.5">
                <Dot tone={zen.enabled && zen.configured ? 'ok' : 'muted'} />
                <span className="text-ink-2">
                  {!zen.configured ? 'no key' : zen.enabled ? 'enabled' : 'disabled'}
                </span>
              </span>
            </div>
            {registry?.concurrency ? (
              <p className="text-[10.5px] text-muted">
                Fan-out concurrency {registry.concurrency}
                {registry.configured
                  ? (() => {
                      const n = String(registry.configured).split(/\s+/).filter(Boolean).length;
                      return ` · ${n} model${n === 1 ? '' : 's'} configured`;
                    })()
                  : ''}
              </p>
            ) : null}
            {!zen.configured ? (
              <p className="text-[10.5px] leading-relaxed text-muted">
                Without a Zen key every model is served through the local OpenCode runtime, which
                means one model can answer for several ids. The UI flags that as a mismatch rather
                than reporting false agreement.
              </p>
            ) : null}
          </section>
        </aside>
      </div>
    </div>
  );
}
