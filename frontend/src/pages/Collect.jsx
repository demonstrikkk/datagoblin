import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useLocalStore } from '../hooks/useUi.js';
import RunView from '../components/RunView.jsx';
import {
  Dot,
  Empty,
  ErrorNote,
  Field,
  Kbd,
  Notice,
  Pill,
  Reveal,
  Spinner,
  Textarea,
  Toggle,
} from '../components/ui.jsx';
import { num, runId, runRecordTotal, runStatusMeta, truncate, verifySummary, when } from '../lib/format.js';

const EXAMPLES = [
  'NGO operating in Kenya with a published 2023 annual report',
  'Series B climate software companies in the EU',
  'Clinical trials for a specific drug phase started since 2024',
];

/* ------------------------------------------------------------------- ask */

function AskPanel({
  onCompiled,
  busy,
  error,
  prompt,
  setPrompt,
  seeds,
  setSeeds,
  budget,
  setBudget,
  useSeeds,
  setUseSeeds,
  forceRefresh,
  setForceRefresh,
}) {
  const [advanced, setAdvanced] = useState(false);
  const ref = useRef(null);

  const seedList = seeds
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);

  const submit = (e) => {
    e?.preventDefault();
    if (!prompt.trim() || busy) return;
    onCompiled({ prompt: prompt.trim(), seed_urls: useSeeds ? seedList : [] });
  };

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="max-w-[620px]">
        <h2 className="h-display text-[30px] leading-tight text-ink balance">
          What are you looking for?
        </h2>
        <p className="mt-2 text-[13px] leading-relaxed text-muted">
          Describe the data in plain English. A planner turns it into a field schema and a
          source strategy, and shows you both before anything runs.
        </p>
      </div>

      <div className="surface p-3.5">
        <Field
          label="Request"
          required
          hint="Be specific about the entity and any qualifier that would disqualify a result."
        >
          <Textarea
            ref={ref}
            autoFocus
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit(e);
            }}
            placeholder="e.g. NGO operating in Kenya with a published 2023 annual report"
            rows={3}
            className="min-h-[84px] text-[14px]"
            aria-label="Research request"
          />
        </Field>

        {prompt.trim().length === 0 ? (
          <div className="mt-3 flex flex-wrap items-center gap-1.5">
            <span className="eyebrow mr-0.5">Try</span>
            {EXAMPLES.map((x) => (
              <button
                key={x}
                type="button"
                onClick={() => setPrompt(x)}
                className="rounded-xs border border-rule bg-warm/50 px-2 py-1 text-[11.5px] text-ink-2 transition-all duration-150 ease-swift hover:border-accent/40 hover:bg-warm hover:text-ink"
              >
                {truncate(x, 46)}
              </button>
            ))}
          </div>
        ) : null}

        <div className="mt-3 border-t border-rule pt-3">
          <button
            type="button"
            onClick={() => setAdvanced((v) => !v)}
            aria-expanded={advanced}
            className="focusable flex items-center gap-1.5 rounded-xs text-[11.5px] font-medium text-muted transition-colors hover:text-ink"
          >
            <span
              className="inline-block transition-transform duration-200 ease-swift"
              style={{ transform: advanced ? 'rotate(90deg)' : 'none' }}
              aria-hidden="true"
            >
              ›
            </span>
            Constraints
            <span className="text-rule-2">
              {advanced ? '— optional, all have defaults' : ''}
            </span>
          </button>

          {advanced ? (
            <div className="mt-3 space-y-3.5 animate-fade-in">
              <Toggle
                checked={useSeeds}
                onChange={setUseSeeds}
                label="Start from specific URLs"
                hint="Seeds are added to the plan's source list. Discovery still runs around them."
              />
              {useSeeds ? (
                <Field
                  label="Seed URLs"
                  hint={`${seedList.length} of 12 will be used. One per line, or comma separated.`}
                >
                  <Textarea
                    value={seeds}
                    onChange={(e) => setSeeds(e.target.value)}
                    rows={3}
                    placeholder="https://example.org/report"
                    className="font-mono text-[11.5px]"
                  />
                </Field>
              ) : null}
              <Field
                label="Credit budget"
                hint="Caps spend for this run. A run that hits the budget stops and saves what it already verified."
              >
                <input
                  type="number"
                  min={0}
                  value={budget}
                  onChange={(e) => setBudget(e.target.value)}
                  className="input-sm input font-mono text-[12px]"
                />
              </Field>
            </div>
          ) : null}
        </div>
      </div>

      {error ? <ErrorNote error={error} /> : null}

      <div className="flex flex-wrap items-center gap-2">
        <button type="submit" disabled={!prompt.trim() || busy} className="btn-accent h-9 px-4">
          {busy ? (
            <>
              <Spinner /> Compiling plan…
            </>
          ) : (
            <>
              Compile plan <Kbd>⌘↵</Kbd>
            </>
          )}
        </button>
        {budget ? (
          <span className="text-[11.5px] text-muted">
            Budget {budget} credits
          </span>
        ) : null}
      </div>
    </form>
  );
}

/* ------------------------------------------------------------------ plan */

function PlanPanel({ plan, provider, planId, onRun, running, error, budget, onBudget }) {
  const fields = plan?.fields || [];
  const required = fields.filter((f) => f.required);
  const sources = plan?.search_queries || [];

  return (
    <div className="space-y-4 animate-fade-up">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">Compiled plan</p>
          <h2 className="mt-1 h-display text-[24px] leading-tight text-ink balance">
            {plan?.goal || 'Untitled plan'}
          </h2>
          <p className="mt-1 text-[12.5px] text-muted">
            Entity <span className="text-ink-2">{plan?.entity || 'unspecified'}</span> · target{' '}
            <span className="font-mono text-ink-2">{num(plan?.requested_count)}</span> records · up to{' '}
            <span className="font-mono text-ink-2">{num(plan?.max_pages)}</span> pages
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          {provider ? (
            <Pill tone="judge" title="Model that produced this plan">
              {provider}
            </Pill>
          ) : null}
          <Pill tone="muted">{String(planId).slice(0, 8)}</Pill>
        </div>
      </header>

      <div className="grid gap-3 lg:grid-cols-[1.4fr_1fr]">
        <section className="surface overflow-hidden">
          <header className="flex items-center justify-between gap-2 border-b border-rule px-3.5 py-2.5">
            <h3 className="eyebrow">Field schema</h3>
            <span className="text-[11px] text-muted">
              <span className="text-accent">{required.length}</span> required of {fields.length}
            </span>
          </header>
          {fields.length ? (
            <ul className="divide-y divide-rule">
              {fields.map((f) => (
                <li key={f.name} className="row flex items-start gap-2.5 px-3.5 py-2">
                  <span className="mt-[3px] w-3 shrink-0 text-center">
                    {f.required ? (
                      <span className="text-accent" title="Required — records missing it are dropped">
                        ●
                      </span>
                    ) : (
                      <span className="text-rule-2" title="Optional">
                        ○
                      </span>
                    )}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline gap-2">
                      <code className="font-mono text-[12px] text-ink">{f.name}</code>
                      <span className="font-mono text-[10px] uppercase tracking-wide text-muted">
                        {f.type}
                      </span>
                    </span>
                    {f.description ? (
                      <span className="mt-0.5 block text-[11.5px] leading-snug text-muted">
                        {f.description}
                      </span>
              ) : null}
              <Toggle
                checked={forceRefresh}
                onChange={setForceRefresh}
                label="Force re-fetch"
                hint="By default a URL this machine has already fetched is reused from stored evidence instead of being requested again — cheaper, and reported as reused with the date it was actually retrieved. Turn this on when the page has changed and you need the current version."
              />
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty title="No fields in this plan" hint="The planner returned an empty schema." />
          )}
        </section>

        <div className="space-y-3">
          <section className="surface overflow-hidden">
            <header className="border-b border-rule px-3.5 py-2.5">
              <h3 className="eyebrow">Search strategy</h3>
            </header>
            <ul className="space-y-1.5 p-3">
              {sources.map((q, i) => (
                <li key={i} className="flex items-start gap-2 text-[12px] leading-snug">
                  <span className="mt-px font-mono text-[10px] text-muted">
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <span className="text-ink-2">{q}</span>
                </li>
              ))}
            </ul>
            {plan?.seed_urls?.length || plan?.seed_domains?.length ? (
              <div className="border-t border-rule px-3.5 py-2.5">
                <p className="eyebrow mb-1.5">Seeded sources</p>
                <div className="flex flex-wrap gap-1">
                  {[...(plan?.seed_urls || []), ...(plan?.seed_domains || [])].map((s, i) => (
                    <span
                      key={i}
                      className="rounded-xs border border-rule bg-warm/60 px-1.5 py-0.5 font-mono text-[10px] text-ink-2"
                    >
                      {truncate(String(s).replace(/^https?:\/\//, ''), 30)}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}
          </section>

          {(plan?.dedupe_keys?.length || plan?.validation_rules?.length) ? (
            <section className="surface overflow-hidden">
              <header className="border-b border-rule px-3.5 py-2.5">
                <h3 className="eyebrow">Applied after extraction</h3>
              </header>
              <div className="space-y-2 p-3">
                {plan?.dedupe_keys?.length ? (
                  <div>
                    <p className="eyebrow mb-1">Deduplicate on</p>
                    <div className="flex flex-wrap gap-1">
                      {plan.dedupe_keys.map((k) => (
                        <code
                          key={k}
                          className="rounded-xs bg-judge-soft px-1.5 py-0.5 font-mono text-[10.5px] text-judge"
                        >
                          {k}
                        </code>
                      ))}
                    </div>
                  </div>
                ) : null}
                {plan?.validation_rules?.length ? (
                  <div>
                    <p className="eyebrow mb-1">Validation rules</p>
                    <ul className="space-y-0.5">
                      {plan.validation_rules.map((r, i) => (
                        <li key={i} className="text-[11.5px] leading-snug text-ink-2">
                          {r}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            </section>
          ) : null}
        </div>
      </div>

      <Notice tone="muted">
        A record is only counted as verified when the judge confirms it <em>and</em> every
        required field has a verbatim quote that re-locates in the stored page. Records that fail
        are dropped, not softened.
      </Notice>

      {error ? <ErrorNote error={error} /> : null}

      <div className="surface flex flex-wrap items-end gap-3 p-3.5">
        <Field label="Credit budget" className="w-36" hint="0 means no cap.">
          <input
            type="number"
            min={0}
            value={budget}
            onChange={(e) => onBudget(e.target.value)}
            className="input-sm input font-mono text-[12px]"
          />
        </Field>
        <div className="flex-1" />
        <button type="button" onClick={onRun} disabled={running} className="btn-accent h-9 px-4">
          {running ? (
            <>
              <Spinner /> Starting…
            </>
          ) : (
            `Run this plan`
          )}
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ recent runs */

/**
 * Recent runs, shown under the ask form. Two jobs: it resumes interrupted work,
 * and it makes the pipeline legible before you commit to a new one. Rendered
 * from `GET /api/history`, which is the only place a finished run survives.
 */
function RecentRuns() {
  const nav = useNavigate();
  const { data, status } = useResource((o) => api.history(o), [], { pollMs: 15_000 });
  const runs = useMemo(() => (Array.isArray(data) ? data.slice(0, 4) : []), [data]);

  if (status === 'loading' && !runs.length) {
    return (
      <div className="mt-8 space-y-2" aria-hidden="true">
        {[0, 1, 2].map((i) => (
          <div key={i} className="skeleton h-11 w-full rounded-sm" />
        ))}
      </div>
    );
  }
  if (!runs.length) return null;

  return (
    <section className="mt-8">
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="eyebrow">Recent runs</h3>
        <button type="button" onClick={() => nav('/runs')} className="btn-ghost btn-xs">
          See all →
        </button>
      </div>
      <ul className="surface divide-y divide-rule overflow-hidden">
        {runs.map((r, i) => {
          const meta = runStatusMeta(r);
          const total = runRecordTotal(r);
          const v = verifySummary(r);
          const id = runId(r);
          return (
            <li key={id || i}>
              <button
                type="button"
                onClick={() => nav(`/runs/${id}`)}
                className="row flex w-full items-center gap-3 px-3.5 py-2.5 text-left"
              >
                <Dot tone={meta.tone} live={meta.live} title={meta.label} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12.5px] text-ink">
                    {truncate(r.query || r.goal || id, 78)}
                  </span>
                  <span className="mt-0.5 block font-mono text-[10px] text-muted">
                    {String(id).slice(0, 8)} · {when(r.started_at || r.created_at)} · {meta.label}
                    {v?.total ? ` · ${Math.round(v.provenShare * 100)}% fields proven` : ''}
                  </span>
                </span>
                <span className="shrink-0 text-right">
                  <span className="block font-mono text-[13px] leading-none text-ink tnum">
                    {num(total)}
                  </span>
                  <span className="mt-0.5 block text-[9.5px] text-muted">records</span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/* ------------------------------------------------------------------ page */

export default function Collect() {
  const nav = useNavigate();
  const [prompt, setPrompt] = useLocalStore('dg_last_prompt', '');
  const [seeds, setSeeds] = useState('');
  const [useSeeds, setUseSeeds] = useState(false);
  const [budget, setBudget] = useState('');
  const [forceRefresh, setForceRefresh] = useState(false);
  const [plan, setPlan] = useState(null);
  const [planId, setPlanId] = useState(null);
  const [provider, setProvider] = useState(null);
  const [runId, setRunId] = useState(null);
  const [compiling, setCompiling] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState(null);
  const [lastRun, setLastRun] = useLocalStore('dg_last_run', null);

  const compile = useCallback(
    async ({ prompt: p, seed_urls }) => {
      setCompiling(true);
      setError(null);
      setPlan(null);
      setRunId(null);
      try {
        const res = await api.compile({ prompt: p, seed_urls }, { timeout: 120_000 });
        setPlan(res?.plan || null);
        setPlanId(res?.plan_id || null);
        setProvider(res?.provider || null);
      } catch (e) {
        setError(e);
      } finally {
        setCompiling(false);
      }
    },
    []
  );

  const start = useCallback(async () => {
    if (!planId) return;
    setStarting(true);
    setError(null);
    try {
      const res = await api.createRun({
        plan_id: planId,
        ...(budget ? { credit_budget: Number(budget) } : {}),
        // Sent only when set, so an older backend that ignores the flag keeps
        // its own default rather than being handed `false` by an absent toggle.
        ...(forceRefresh ? { reuse_stored_pages: false } : {}),
      });
      const id = res?.run_id;
      if (id) {
        setRunId(id);
        setLastRun(id);
      }
    } catch (e) {
      setError(e);
    } finally {
      setStarting(false);
    }
  }, [planId, budget, forceRefresh, setLastRun]);

  // Resume an in-flight run after a reload instead of stranding the operator.
  useEffect(() => {
    if (!runId && lastRun) {
      api
        .run(lastRun)
        .then((r) => {
          if (['PLANNING', 'RUNNING'].includes(r?.status)) setRunId(lastRun);
        })
        .catch(() => setLastRun(null));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stage = runId ? 'run' : plan ? 'plan' : 'ask';

  return (
    <div className="space-y-6">
      {/*
        The ask state is a single focused task, so it is centred in the
        viewport rather than pinned to the top of a tall empty page. A wider
        measure than the prose uses keeps the field list beside the examples
        readable instead of stretching a 30-character line across 1180px.
      */}
      <div className={stage === 'ask' ? 'mx-auto w-full max-w-[720px] pt-[7vh]' : ''}>
        {stage === 'ask' ? (
          <Reveal key="ask">
            <AskPanel
              prompt={prompt}
              setPrompt={setPrompt}
              seeds={seeds}
              setSeeds={setSeeds}
              useSeeds={useSeeds}
              setUseSeeds={setUseSeeds}
              budget={budget}
              setBudget={setBudget}
    forceRefresh={forceRefresh}
    setForceRefresh={setForceRefresh}
              onCompiled={compile}
              busy={compiling}
              error={error}
            />
            <RecentRuns />
          </Reveal>
        ) : null}

        {stage === 'plan' ? (
          <Reveal key="plan">
            <PlanPanel
              plan={plan}
              planId={planId}
              provider={provider}
              onRun={start}
              running={starting}
              error={error}
              budget={budget}
              onBudget={setBudget}
            />
            <div className="mt-4 flex justify-center">
              <button
                type="button"
                onClick={() => {
                  setPlan(null);
                  setPlanId(null);
                }}
                className="btn-ghost btn-xs"
              >
                ← Change the request
              </button>
            </div>
          </Reveal>
        ) : null}

        {stage === 'run' ? (
          <Reveal key="run">
            <RunView runId={runId} title={prompt} />          </Reveal>
        ) : null}
      </div>

      {stage === 'ask' && lastRun ? (
        <Reveal>
          <div className="surface flex flex-wrap items-center justify-between gap-3 p-3.5">
            <div className="min-w-0">
              <p className="eyebrow">Previous run</p>
              <p className="mt-0.5 truncate text-[12.5px] text-ink-2">
                <span className="font-mono">{String(lastRun).slice(0, 8)}</span> — still open in
                this browser
              </p>
            </div>
            <button type="button" onClick={() => nav(`/runs/${lastRun}`)} className="btn-outline btn-xs">
              Open run
            </button>
          </div>
        </Reveal>
      ) : null}
    </div>
  );
}
