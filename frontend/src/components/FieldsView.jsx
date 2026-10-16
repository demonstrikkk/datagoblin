import { useMemo, useState } from 'react';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import DynamicVisual, { kindTone } from './dataset/DynamicVisual.jsx';
import { Empty, ErrorNote, Loading, Notice, Pill, StackedBar, Treemap } from '../components/ui.jsx';
import { num, when } from '../lib/format.js';

/**
 * What is actually in this dataset, per field.
 *
 * The Records tab answers "what does one row look like" and the Coverage tab
 * answers "how much of each field is there". Neither answers "what is this data",
 * which is the question someone opens a dataset to ask — and the only way to
 * answer it is to look at every field rather than at the first screenful of
 * rows. So this reads the server-side profile: the same coverage numbers the
 * Coverage tab shows, plus a shape for each field.
 *
 * Three rules, and each of them is a refusal:
 *
 * * a field with one distinct value gets no chart. One bar is a sentence with
 *   axes, and drawing it implies a distribution that is not there;
 * * a field with no values is described, not drawn as a zero-height bar;
 * * a field that is filled on some records and empty on others says so
 *   separately from one that is empty on all of them, because the two call for
 *   different work — a top-up, and a schema that asks for something these
 *   sources do not carry.
 *
 * The "where" panel is deliberately not a map. Tiles are sized by share over
 * the country field the schema actually has; a drawn map would be a picture of
 * a world this run never visited.
 */
export default function FieldsView({ datasetId, records = 0, profile = null }) {
  /*
   * The profile is usually passed in, because the dataset page shows the domain
   * panel above the tab bar and needs it there too. Fetching it here as well
   * would be two identical requests for the same payload — the profiler reads
   * every field of up to 2,000 records, so it is not a cheap duplicate — and two
   * copies that could disagree if one were stale.
   *
   * Left able to fetch for itself so the component still works on its own.
   */
  const own = useResource((o) => api.profile(datasetId, undefined, o), [datasetId], {
    enabled: !profile,
  });
  const data = profile ?? own.data;
  const status = profile ? 'ready' : own.status;
  const error = profile ? null : own.error;
  const isStale = own.isStale;
  const refetch = own.refetch;
  const { inspect } = useInspector();
  const [kind, setKind] = useState('all');
  const [place, setPlace] = useState(null);

  const fields = useMemo(() => (Array.isArray(data?.fields) ? data.fields : []), [data]);

  const shown = useMemo(() => {
    if (kind === 'all') return fields;
    if (kind === 'gaps') return fields.filter((f) => f.trust.missing > 0);
    if (kind === 'empty') return fields.filter((f) => f.never_extracted);
    if (kind === 'disputed') return fields.filter((f) => f.trust.conflicting > 0);
    if (kind === 'complete') return fields.filter((f) => f.fully_filled);
    return fields;
  }, [fields, kind]);

  if (status === 'loading' && !data) return <Loading rows={4} label="Reading every field" />;
  if (error) return <ErrorNote error={error} onRetry={refetch} />;
  if (!data) return <Empty title="Nothing to profile" />;

  const t = data.totals || {};
  const filters = [
    { value: 'all', label: `All ${t.fields ?? 0}` },
    { value: 'complete', label: `Complete ${t.complete ?? 0}` },
    { value: 'gaps', label: `With gaps ${(t.partial ?? 0) + (t.never_extracted ?? 0)}` },
    { value: 'empty', label: `Never filled ${t.never_extracted ?? 0}` },
    { value: 'disputed', label: `Disputed ${t.conflicting ?? 0}` },
  ].filter((f) => !f.value.includes('0 '));

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="eyebrow">Fields</p>
          <p className="mt-0.5 text-[12.5px] text-muted">
            {num(data.records)} record{data.records === 1 ? '' : 's'}, {t.fields ?? 0} field
            {(t.fields ?? 0) === 1 ? '' : 's'} declared
            {data.sampled ? (
              <>
                {' '}
                · shape drawn from the first {num(data.records_read)}
              </>
            ) : null}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {filters.map((f) => (
            <button
              key={f.value}
              type="button"
              onClick={() => setKind(f.value)}
              className={`rounded-full border px-2.5 py-0.5 text-[11px] transition-colors duration-200 ease-swift focusable ${
                kind === f.value
                  ? 'border-ink bg-ink text-paper'
                  : 'border-rule-2 text-muted hover:border-ink hover:text-ink'
              }`}
            >
              {f.label}
            </button>
          ))}
          <button
            type="button"
            onClick={refetch}
            className="btn-ghost btn-xs"
            title={isStale ? 'Refreshing…' : when(data.records_read ? new Date() : null)}
          >
            Refresh
          </button>
        </div>
      </header>

      {data.place && data.place.values?.length > 1 ? (
        <PlacePanel place={data.place} active={place} onPick={setPlace} />
      ) : null}

      {!shown.length ? (
        <Empty
          title={kind === 'disputed' ? 'No field has sources that disagree' : 'No field matches that filter'}
          hint="Filters are the ones this dataset can actually answer with."
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {shown.map((f) => (
            <FieldCard key={f.field} field={f} onInspect={() =>
              inspect({ kind: 'field', field: f.field, json: f, title: f.field })} />
          ))}
        </div>
      )}
    </div>
  );
}

function FieldCard({ field, onInspect }) {
  const t = field.trust || {};
  const bars = [
    { key: 'verified', n: t.verified || 0, tone: 'ok', label: 'Proven' },
    { key: 'unverified', n: t.unverified || 0, tone: 'info', label: 'Unproven' },
    { key: 'conflicting', n: t.conflicting || 0, tone: 'judge', label: 'Disputed' },
    { key: 'missing', n: t.missing || 0, tone: 'muted', label: 'Empty' },
  ].filter((b) => b.n > 0);
  const kind = kindTone(field);

  return (
    <article className="surface flex min-w-0 flex-col gap-2 p-3">
      <header className="flex items-baseline justify-between gap-2">
        <h4 className="truncate font-mono text-[12px] text-ink" title={field.field}>
          {field.field}
        </h4>
        {kind ? (
          <Pill tone={kind.tone} title="Detected by the profiler from this field's own values">
            {kind.label}
          </Pill>
        ) : null}
      </header>

      {/* One implementation of "what can this field be drawn as", shared with the
          dataset overview, so the two can never disagree about a field. The
          refusals — a constant gets a sentence, an unfilled field gets a
          sentence — live in `shapeFor` and are covered by a unit test. */}
      <DynamicVisual field={field} />

      <footer className="mt-auto space-y-1.5 pt-1">
        <StackedBar rows={bars} total={field.records} height={5} />
        <div className="flex items-center justify-between gap-2 text-[10.5px] text-muted">
          <span className="tnum">
            {t.present ?? 0}/{field.records} filled
            {t.missing ? `, ${t.missing} empty` : ''}
          </span>
          <button type="button" onClick={onInspect} className="btn-ghost btn-xs">
            Inspect
          </button>
        </div>
      </footer>
    </article>
  );
}

function PlacePanel({ place, active, onPick }) {
  const total = place.values.reduce((a, v) => a + v.n, 0) || 1;
  return (
    <section className="surface overflow-hidden">
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-rule px-3.5 py-2.5">
        <div>
          <p className="eyebrow">Where these records are</p>
          <p className="mt-0.5 text-[11.5px] text-muted">
            Tiles are sized by share of <span className="font-mono">{place.field}</span>
            {' '}· {num(total)} record{total === 1 ? '' : 's'} with a value. Not a map
            projection: this is a distribution, drawn from the field the schema has.
          </p>
        </div>
      </header>
      {place.variants > 0 ? (
        <p className="border-b border-rule bg-warm/40 px-3.5 py-1.5 text-[11px] text-muted">
          {place.as_written_count} distinct spellings collapsed into{' '}
          {place.as_written_count - place.variants} by first segment — "
          {place.raw.find((r) => r.value.includes(','))?.value || 'London, England'}"
          {' '}counts under "{place.values[0]?.value}". Merging is a rule, not a lookup:
          different cities sharing a first segment are still one tile.
        </p>
      ) : null}
      <div className="grid gap-4 p-3.5 sm:grid-cols-[1.6fr_1fr]">
        <Treemap
          height={168}
          activeKey={active}
          onSelect={onPick}
          items={place.values.map((v) => ({ key: v.value, label: v.value, value: v.n, n: v.n }))}
        />
        <ul className="space-y-1 self-center">
          {place.values.slice(0, 8).map((v) => (
            <li
              key={v.value}
              className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-2 text-[11.5px]"
            >
              <span className={`truncate ${active && active !== v.value ? 'text-muted' : 'text-ink-2'}`}>
                {v.value}
              </span>
              <span className="tnum text-muted">
                {v.n} · {Math.round((v.n / total) * 100)}%
              </span>
            </li>
          ))}
          {place.truncated ? (
            <li className="text-[10.5px] text-muted">+ more places</li>
          ) : null}
        </ul>
      </div>
    </section>
  );
}
