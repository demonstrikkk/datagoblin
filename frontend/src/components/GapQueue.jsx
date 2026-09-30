import { useCallback, useState } from 'react';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { Empty, ErrorNote, Loading, Notice, Pill, Stat } from './ui.jsx';
import { num } from '../lib/format.js';

/**
 * The gap queue: what is missing, what has been tried, and what may be tried next.
 *
 * The distinction this view exists to make is between a gap and a queue. A field
 * the run never extracted, and a field we have already re-read and searched for
 * and come back empty-handed, are both "a gap" and only one of them is work. The
 * old backlog list reported both as an open count, so a dataset that had been
 * thoroughly worked over looked exactly like one that had never been touched.
 *
 * So every row states which remedy could close it, how far that remedy has been
 * taken, and what happens next:
 *
 *   schema_gap    no record carries the field — the pages we fetched do not
 *                 answer it. Re-reading them is free; searching the web for it
 *                 is a separate decision and is not offered until they fail.
 *   depth_gap     some records carry it and some do not — the crawl reached some
 *                 entities and not others. This is what a second pass closes.
 *   evidence_gap  a value exists but carries no verdict, or two sources
 *                 disagree. Nothing is missing. There is deliberately no button:
 *                 the remedy is the judge or a person, and offering a search here
 *                 would spend money to produce a second unproven value.
 *
 * The phase ladder is enforced server-side, and the button is hidden until the
 * gap reports a `next_phase`. A gap that has never been through the free phase
 * shows "read stored pages" rather than "search the web", because sending a
 * search at a question the stored pages have not been asked yet spends money on
 * an answer we may already hold.
 */
const CATEGORY = {
  schema_gap: { label: 'Schema gap', tone: 'warn',
    blurb: 'No record carries this field, so the pages this run fetched do not answer it.' },
  depth_gap: { label: 'Depth gap', tone: 'info',
    blurb: 'Some records carry it and some do not — the crawl reached some entities and not others.' },
  evidence_gap: { label: 'Evidence gap', tone: 'judge',
    blurb: 'A value exists but carries no verdict, or two sources disagree. Nothing is missing.' },
};

const PHASE_LABEL = {
  0: 'never attempted',
  1: 'stored pages re-read',
  2: 'web search and fetch',
};

const STATE_TONE = { open: 'warn', resolved: 'ok', exhausted: 'muted', refused: 'muted' };

export default function GapQueue({ datasetId, onChanged }) {
  const { data, status, error, refetch } = useResource((o) => api.gaps(datasetId, o), [datasetId]);
  const [busy, setBusy] = useState(null);
  const [proposal, setProposal] = useState(null);
  const [verdict, setVerdict] = useState(null);
  const [rejudging, setRejudging] = useState(null);
  const [failure, setFailure] = useState('');

  const attempt = useCallback(
    async (gap, apply) => {
      setBusy(`${gap.field}:${apply ? 'apply' : 'dry'}`);
      setFailure('');
      try {
        const out = await api.fillGap(datasetId, {
          field: gap.field,
          phase: gap.next_phase,
          apply,
        });
        if (apply) {
          setProposal(null);
          refetch();
          onChanged?.();
        } else {
          setProposal({ field: gap.field, ...out });
        }
      } catch (err) {
        setFailure(String(err?.message || err));
      } finally {
        setBusy(null);
      }
    },
    [datasetId, refetch, onChanged]
  );

  const rejudge = useCallback(
    async (gap, apply) => {
      setRejudging(apply ? null : gap.field);
      setFailure('');
      try {
        const out = await api.rejudge(datasetId, {
          field: gap.field,
          apply,
        });
        setVerdict({ field: gap.field, apply, ...out });
        if (apply) {
          refetch();
          onChanged?.();
        }
      } catch (err) {
        setFailure(String(err?.message || err));
      } finally {
        setRejudging(null);
      }
    },
    [datasetId, refetch, onChanged]
  );

  if (status === 'loading' && !data) return <Loading rows={3} label="Reading recorded gaps" />;
  if (error) return <ErrorNote error={error} onRetry={refetch} />;
  if (!data) return null;

  const gaps = data.gaps || [];
  const s = data.summary || {};
  const queue = gaps.filter((g) => g.attemptable);
  const settled = gaps.filter((g) => !g.attemptable);

  if (!gaps.length) {
    return (
      <div className="space-y-3">
        <h3 className="text-sm font-medium">Gap queue</h3>
        <Empty
          title="Nothing is missing"
          hint="Every declared field carries a value on every record. Fields with values but no verdict are counted here too, so this means there is neither missing data nor unproven data."
        />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">Gap queue</h3>
        <span className="text-xs text-muted">
          {num(s.attemptable ?? 0)} of {num(s.gaps ?? 0)} can still be worked on
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="Workable"
          value={num(s.attemptable ?? 0)}
          tone={s.attemptable ? 'warn' : 'muted'}
          sub={`${num(s.outstanding_cells ?? 0)} cells`}
        />
        <Stat
          label="Sources checked"
          value={num((s.by_state?.exhausted ?? 0) + (s.by_state?.resolved ?? 0))}
          tone="muted"
          sub="exhausted or filled"
        />
        <Stat
          label="Unproven"
          value={num(s.by_category?.evidence_gap ?? 0)}
          tone="judge"
          sub="need the judge or you"
        />
      </div>

      {!data.tracked ? (
        <Notice tone="info">
          <span className="font-medium">Attempt history is not being recorded.</span>{' '}
          The counts below are real and computed from the stored records, but this workspace
          cannot remember which phase a gap has been through, so every gap looks untried and
          a repeated attempt would repeat the work. Run the{' '}
          <code className="font-mono">007_dataset_gaps</code> migration to enable this.
        </Notice>
      ) : null}

      {failure ? <Notice tone="warn">{failure}</Notice> : null}

      {queue.length ? (
        <div className="space-y-2">
          {queue.map((g) => (
            <GapRow
              key={g.field}
              gap={g}
              busy={busy}
              rejudging={rejudging}
              onDryRun={() => attempt(g, false)}
              onApply={() => attempt(g, true)}
              onRejudge={(apply) => rejudge(g, apply)}
            />
          ))}
        </div>
      ) : null}

      {proposal ? (
        <Notice tone={proposal.report?.written ? 'ok' : 'info'}>
          <div className="space-y-1.5">
            <p className="font-medium">
              {proposal.field}: {proposal.report?.searches_run ?? 0} search(es),{' '}
              {num(proposal.report?.pages_fetched ?? 0)} page(s) fetched,{' '}
              {num(proposal.report?.fillable ?? 0)} cell(s) would fill.
            </p>
            <p className="text-[11.5px] leading-relaxed text-muted">
              {proposal.report?.note}
            </p>
            {proposal.report?.written ? (
              <p className="text-[11.5px]">Written. Each value carries its own quote and verdict.</p>
            ) : proposal.report?.fillable ? (
              <button
                type="button"
                className="btn-outline btn-xs"
                onClick={() => attempt({ field: proposal.field, next_phase: proposal.phase }, true)}
              >
                Write {num(proposal.report.fillable)} value(s)
              </button>
            ) : null}
            <button type="button" className="btn-ghost btn-xs" onClick={() => setProposal(null)}>
              Dismiss
            </button>
          </div>
        </Notice>
      ) : null}

      {verdict ? (
        <Notice tone={verdict.apply ? 'ok' : 'info'}>
          <div className="space-y-1.5">
            <p className="font-medium">
              {verdict.field}: {num(verdict.settled_free ?? 0)} settled by the page
              text alone, {num(verdict.judge_calls ?? 0)} judged by the model.
            </p>
            <p className="text-[11.5px] leading-relaxed text-muted">
              {num(verdict.verified_now ?? 0)} cell(s) would count as verified.{' '}
              {verdict.disagreements
                ? `${num(verdict.disagreements)} disagree with their own quote — the value is kept and the disagreement recorded, because nothing was ever wrong with storing it.`
                : 'None disagreed.'}{' '}
              {verdict.still_unjudged
                ? `${num(verdict.still_unjudged)} stayed unjudged at this budget.`
                : ''}
            </p>
            {!verdict.apply && verdict.verified_now ? (
              <button
                type="button"
                className="btn-outline btn-xs"
                onClick={() => rejudge({ field: verdict.field }, true)}
              >
                Write {num(verdict.verified_now)} verdict(s)
              </button>
            ) : null}
            <button type="button" className="btn-ghost btn-xs" onClick={() => setVerdict(null)}>
              Dismiss
            </button>
          </div>
        </Notice>
      ) : null}

      {settled.length ? (
        <div className="space-y-2">
          <p className="text-xs text-muted">
            Settled. These are findings, not work — re-running them would spend the same
            budget to reach the same answer.
          </p>
          {settled.map((g) => (
            <div
              key={g.field}
              className="flex flex-wrap items-baseline justify-between gap-2 border-b border-rule-2/40 py-1.5 last:border-0"
            >
              <span className="text-sm">{g.field}</span>
              <span className="flex-1 text-xs text-muted">
                {g.blocked_reason || g.reason}
              </span>
              <Pill tone={STATE_TONE[g.state] || 'muted'}>{g.state}</Pill>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function GapRow({ gap, busy, rejudging, onDryRun, onApply, onRejudge }) {
  const cat = CATEGORY[gap.category] || { label: gap.category, tone: 'muted', blurb: '' };
  const drying = busy === `${gap.field}:dry`;
  const applying = busy === `${gap.field}:apply`;
  const searching = gap.next_phase === 2;

  return (
    <div className="surface p-3.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-sm">{gap.field}</span>
          <Pill tone={cat.tone}>{cat.label}</Pill>
        </div>
        <span className="text-xs tabular-nums text-muted">
          {num(gap.outstanding)} of {num(gap.records)} cells
        </span>
      </div>

      <p className="mt-1.5 text-[12px] leading-relaxed text-ink-2">{gap.reason}</p>
      <p className="mt-1 text-[11.5px] text-muted">
        Attempted through phase {gap.phase} — {PHASE_LABEL[gap.phase] || 'unknown'}
        {gap.attempts ? ` · ${gap.attempts} attempt(s)` : ''}
        {gap.stats?.searches_run ? ` · ${gap.stats.searches_run} search(es) spent` : ''}
      </p>

      {gap.category === 'evidence_gap' ? (
        <div className="mt-2.5 space-y-2">
          <p className="text-[11.5px] text-muted">
            Nothing is missing, so searching would add nothing. These cells already
            carry a value and a quote — they were never judged, usually because the
            run hit its judging budget mid-dataset. Judging them uses the pages this
            run already stored, so it costs no new fetching.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-outline btn-xs"
              disabled={Boolean(busy) || !gap.attemptable}
              onClick={() => rejudge(gap, false)}
            >
              {rejudging === gap.field ? 'Judging…' : 'Judge these cells'}
            </button>
            {!gap.attemptable ? (
              <span className="text-[11px] text-muted">{gap.blocked_reason}</span>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn-outline btn-xs"
            disabled={Boolean(busy)}
            onClick={onDryRun}
          >
            {drying || applying ? (
              <span className="flex items-center gap-1.5">
                {searching ? 'Searching' : 'Reading'}…
              </span>
            ) : searching ? (
              'Try a web search'
            ) : (
              'Read stored pages'
            )}
          </button>
          <span className="text-[11px] text-muted">
            {searching
              ? 'Searches the web for each missing record. Runs for real and shows the cost before writing.'
              : 'Re-reads pages this run already stored. No external requests, no new cost.'}
          </span>
        </div>
      )}
    </div>
  );
}
