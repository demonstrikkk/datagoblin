import { NavLink, useNavigate } from 'react-router-dom';

/* Hand-rolled 17px line icons — no icon dependency, consistent 1.5 stroke.
   Shared attributes live here and are spread onto a real <svg>, never a
   <span>: spreading viewBox/stroke onto a span makes the browser try to parse
   <path>/<circle> as unknown HTML elements. */
const S = {
  width: 17,
  height: 17,
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

const ITEMS = [
  { to: '/', label: 'Collect', icon: 'collect', end: true },
  { to: '/runs', label: 'Runs', icon: 'runs' },
  { to: '/library', label: 'Library', icon: 'library' },
  { to: '/intel', label: 'Intel', icon: 'intel' },
];

function RailButton({ to, label, icon, end, onNavigate }) {
  return (
    <NavLink
      to={to}
      end={end}
      onClick={onNavigate}
      aria-label={label}
      className="group relative flex h-11 w-11 items-center justify-center rounded-md transition-all duration-200 ease-swift focusable"
    >
      {({ isActive }) => (
        <>
          <span
            aria-hidden="true"
            className={`absolute -left-[7px] top-1/2 h-5 w-[2.5px] -translate-y-1/2 rounded-r-full bg-accent transition-all duration-250 ease-swift ${
              isActive ? 'scale-y-100 opacity-100' : 'scale-y-0 opacity-0'
            }`}
          />
          <svg
            {...S}
            className={`transition-all duration-200 ease-swift ${
              isActive ? 'text-accent' : 'text-muted group-hover:scale-105 group-hover:text-ink'
            }`}
          >
            {ICONS[icon]}
          </svg>
          <span className="pointer-events-none absolute left-[52px] z-50 hidden whitespace-nowrap rounded-sm border border-rule bg-paper-3 px-2 py-1 text-[11.5px] text-ink shadow-lift group-hover:block group-focus-visible:block animate-scale-in">
            {label}
          </span>
        </>
      )}
    </NavLink>
  );
}

export default function NavRail({ onNavigate, onOpenSettings }) {
  const nav = useNavigate();
  return (
    <nav
      aria-label="Primary"
      className="chrome-blur z-30 flex h-full w-[var(--rail-w)] shrink-0 flex-col items-center gap-1 py-3"
    >
      <button
        type="button"
        onClick={() => nav('/')}
        aria-label="Datagoblin home"
        className="focusable mb-2 grid h-9 w-9 place-items-center rounded-md font-display text-[19px] leading-none text-accent transition-transform duration-200 ease-spring hover:scale-105 active:scale-95"
      >
        ᚠ
      </button>

      <div className="flex flex-1 flex-col items-center gap-1.5">
        {ITEMS.map((it) => (
          <RailButton key={it.to} {...it} onNavigate={onNavigate} />
        ))}
      </div>

      <button
        type="button"
        onClick={onOpenSettings}
        aria-label="Settings"
        className="focusable group relative grid h-11 w-11 place-items-center rounded-md text-muted transition-colors duration-200 hover:text-ink"
      >
        <svg {...S}>
          {ICONS.settings}
        </svg>
        <span className="pointer-events-none absolute left-[52px] z-50 hidden whitespace-nowrap rounded-sm border border-rule bg-paper-3 px-2 py-1 text-[11.5px] text-ink shadow-lift group-hover:block animate-scale-in">
          Settings
        </span>
      </button>
    </nav>
  );
}
