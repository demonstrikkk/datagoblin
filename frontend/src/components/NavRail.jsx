import { NavLink, useNavigate } from 'react-router-dom';
import { Kbd } from './ui.jsx';

/* Hand-rolled 17px line icons — no icon dependency, consistent 1.5 stroke.
   Shared attributes live here and are spread onto a real <svg>, never a
   <span>: spreading viewBox/stroke onto a span makes the browser try to parse
   <path>/<circle> as unknown HTML elements. */
const S = {
  width: 16,
  height: 16,
  viewBox: '0 0 20 20',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': 'true',
  focusable: 'false',
};

const ICONS = {
  collect: (
    <>
      <path d="M10 2.5v3M10 14.5v3M2.5 10h3M14.5 10h3" />
      <circle cx="10" cy="10" r="3.6" />
    </>
  ),
  runs: (
    <>
      <path d="M2.5 12.5c2.6 0 2.6-5 5.2-5s2.6 5 5.2 5 2.6-5 4.6-5" />
      <circle cx="3" cy="12.5" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="7.7" cy="7.5" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="12.9" cy="12.5" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="17.5" cy="7.5" r="1.1" fill="currentColor" stroke="none" />
    </>
  ),
  library: (
    <>
      <rect x="2.5" y="3.5" width="15" height="13" rx="1.6" />
      <path d="M2.5 7.6h15M7.6 7.6v8.9M12.4 7.6v8.9" />
    </>
  ),
  sources: (
    <>
      <circle cx="10" cy="10" r="6.2" />
      <path d="M3.9 10h12.2M10 3.8c1.7 1.8 2.6 3.9 2.6 6.2s-.9 4.4-2.6 6.2c-1.7-1.8-2.6-3.9-2.6-6.2S8.3 5.6 10 3.8Z" />
    </>
  ),
  intel: (
    <>
      <circle cx="10" cy="10" r="2.4" />
      <circle cx="4" cy="5.5" r="1.5" />
      <circle cx="16" cy="5.5" r="1.5" />
      <circle cx="4" cy="14.5" r="1.5" />
      <circle cx="16" cy="14.5" r="1.5" />
      <path d="M5.3 6.6 8 8.6M14.7 6.6 12 8.6M5.3 13.4 8 11.4M14.7 13.4 12 11.4" />
    </>
  ),
  settings: (
    <>
      <circle cx="10" cy="10" r="2.6" />
      <path d="M10 2.2v1.6M10 16.2v1.6M17.8 10h-1.6M3.8 10H2.2M15.5 4.5l-1.1 1.1M5.6 14.4l-1.1 1.1M15.5 15.5l-1.1-1.1M5.6 5.6 4.5 4.5" />
    </>
  ),
};

/**
 * Primary destinations.
 *
 * The labels are the point. This was a 60px rail of icons whose names only
 * appeared on hover, which is fine for four well-known glyphs and poor for
 * "Sources" — a globe-ish icon next to a library grid tells you nothing about
 * which one holds the stored pages. A wider, labelled rail costs ~140px of
 * width and buys a navigation someone can read at a glance.
 *
 * Intel is not in the main rail. It reports cross-model agreement on free
 * providers, which is an operator diagnostic rather than part of hunting,
 * proving or reading data, and it was sitting between "Datasets" and the
 * evidence pages where it competed with them. The page and its route are
 * untouched, so it is still reachable at /intel.
 *
 * Routes are unchanged; the width is the only other difference.
 */
const ITEMS = [
  { to: '/', label: 'Investigate', icon: 'collect', end: true, hint: 'Start a hunt' },
  { to: '/dashboard', label: 'Provenance', icon: 'runs', hint: 'Provenance across every dataset' },
  { to: '/runs', label: 'Runs', icon: 'runs', hint: 'Live and past hunts' },
  { to: '/library', label: 'Datasets', icon: 'library', hint: 'Collected results' },
  { to: '/sources', label: 'Sources', icon: 'sources', hint: 'Every page in storage' },
];

function RailLink({ to, label, icon, end, hint, onNavigate }) {
  return (
    <NavLink
      to={to}
      end={end}
      onClick={onNavigate}
      title={hint}
      className="focusable group relative flex items-center gap-2.5 rounded-sm px-2 py-[7px] text-[13px] transition-colors duration-150 ease-swift"
    >
      {({ isActive }) => (
        <>
          <span
            aria-hidden="true"
            className={`absolute -left-2 top-1/2 h-4 w-[2.5px] -translate-y-1/2 rounded-r-full bg-accent transition-all duration-200 ease-swift ${
              isActive ? 'scale-y-100 opacity-100' : 'scale-y-0 opacity-0'
            }`}
          />
          <svg
            {...S}
            className={`shrink-0 transition-colors duration-150 ease-swift ${
              isActive ? 'text-accent' : 'text-muted group-hover:text-ink'
            }`}
          >
            {ICONS[icon]}
          </svg>
          <span className={isActive ? 'font-medium text-ink' : 'text-ink-2'}>{label}</span>
        </>
      )}
    </NavLink>
  );
}

export default function NavRail({ onNavigate, onOpenSettings, onOpenPalette }) {
  const nav = useNavigate();
  return (
    <nav
      aria-label="Primary"
      className="chrome-blur z-30 flex h-full w-[var(--rail-w)] shrink-0 flex-col px-3 py-3"
    >
      <button
        type="button"
        onClick={() => nav('/')}
        aria-label="Datagoblin home"
        className="focusable mb-4 flex items-center gap-2 rounded-sm px-1 py-1 text-left"
      >
        <span
          className="grid h-7 w-7 shrink-0 place-items-center rounded-sm bg-accent font-display text-[15px] leading-none text-accent-ink"
          aria-hidden="true"
        >
          ᚠ
        </span>
        <span className="font-display text-[15px] leading-none text-ink">DataGoblin</span>
      </button>

      <div className="flex flex-1 flex-col gap-0.5">
        {ITEMS.map((it) => (
          <RailLink key={it.to} {...it} onNavigate={onNavigate} />
        ))}
      </div>

      {/* Workspace. The backend identity is a fact about the machine, not a
          navigation target, so it sits below the destinations. */}
      <div className="mt-2 space-y-0.5 border-t border-rule pt-2">
        <button
          type="button"
          onClick={onOpenPalette}
          className="focusable group flex w-full items-center gap-2.5 rounded-sm px-2 py-[7px] text-[13px] text-ink-2 transition-colors duration-150 hover:bg-warm/60"
        >
          <svg {...S} className="shrink-0 text-muted group-hover:text-ink">
            <circle cx="9" cy="9" r="5.2" />
            <path d="m13 13 4 4" />
          </svg>
          <span className="flex-1 text-left">Search</span>
          <Kbd>⌘K</Kbd>
        </button>

        <button
          type="button"
          onClick={onOpenSettings}
          className="focusable group flex w-full items-center gap-2.5 rounded-sm px-2 py-[7px] text-[13px] text-ink-2 transition-colors duration-150 hover:bg-warm/60"
        >
          <svg {...S} className="shrink-0 text-muted group-hover:text-ink">
            {ICONS.settings}
          </svg>
          <span>Settings</span>
        </button>
        {/* The backend identity is deliberately not repeated here. CommandBar
            already carries the live health chip for it, and a second indicator
            in a different corner of the same screen can only ever disagree with
            the first one. */}
      </div>
    </nav>
  );
}
