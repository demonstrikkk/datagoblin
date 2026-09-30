import { useMemo, useState } from 'react';
import { api } from '../lib/api.js';
import useResource from '../hooks/useResource.js';
import { useInspector } from '../lib/inspector.jsx';
import SectorPanel from './SectorPanel.jsx';
import {
  Empty,
  ErrorNote,
  Histogram,
  Loading,
  Notice,
  Pill,
  StackedBar,
  Timeline,
  Treemap,
} from '../components/ui.jsx';
import { num, truncate, when } from '../lib/format.js';

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
export default function FieldsView({ datasetId, records = 0 }) {
  const { data, status, error, refetch, isStale } = useResource(
    (o) => api.profile(datasetId, o),
    [datasetId]
  );
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

      {/*
        The domain reading, above the generic field list and below the header.

        It is a header for the field grid rather than a replacement: the grid
        still lists every declared field, because a domain reading is a
        convenience and must never hide a field the schema asked for. When the
        classifier declines it says why instead of disappearing, so the absence is
        legible rather than looking like a panel that failed to load.
      */}
      <SectorPanel profile={data} />

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

  return (
    <article className="surface flex min-w-0 flex-col gap-2 p-3">
      <header className="flex items-baseline justify-between gap-2">
        <h4 className="truncate font-mono text-[12px] text-ink" title={field.field}>
          {field.field}
        </h4>
        <Pill tone={field.kind === 'numeric' ? 'judge' : field.kind === 'categorical' ? 'info' : 'muted'}>
          {field.kind === 'empty' ? 'never filled' : field.kind}
        </Pill>
      </header>

      {field.kind === 'empty' ? (
        /* Not a zero-height chart. A field the schema asks for and no record has
           is a different problem from one that is partly filled, and it is worth
           saying in words. */
        <p className="text-[11.5px] leading-snug text-muted">
          Declared by the schema, filled on no record. Re-reading these pages may
          not help — the sources may simply not carry it.
        </p>
      ) : field.kind === 'scalar' ? (
        <p className="text-[11.5px] leading-snug text-muted">
          One distinct value across {num(field.values)} record
          {field.values === 1 ? '' : 's'}, so there is no distribution to draw.
        </p>
      ) : field.kind === 'date' && field.shape?.by_year?.length > 1 ? (
        /* Empty years stay visible. A gap in a date column is a finding, and
           collapsing the gaps draws the same picture as having none. */
        <Timeline
          byYear={field.shape.by_year}
          min={field.shape.min}
          max={field.shape.max}
        />
      ) : field.kind === 'numeric' && field.shape?.buckets?.length > 1 ? (
        <Histogram buckets={field.shape.buckets} median={field.shape.median} format={num} />
      ) : field.shape?.values?.length > 1 ? (
        <ul className="space-y-1">
          {field.shape.values.slice(0, 6).map((v) => {
            const max = field.shape.values[0].n || 1;
            return (
              <li key={v.value} className="grid grid-cols-[minmax(0,7rem)_1fr_auto] items-center gap-2">
                <span className="truncate text-[11px] text-muted" title={v.value}>
                  {truncate(v.value, 24)}
                </span>
                <span className="h-2 w-full overflow-hidden rounded-full bg-warm">
                  <span
                    className="block h-full rounded-full"
                    style={{ width: `${Math.max(2, (v.n / max) * 100)}%`, background: 'rgb(var(--accent))' }}
                  />
                </span>
                <span className="tnum shrink-0 text-[11px] text-ink-2">{v.n}</span>
              </li>
            );
          })}
          {field.shape.truncated ? (
            <li className="text-[10.5px] text-muted">+ more distinct values</li>
          ) : null}
        </ul>
      ) : null}

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
