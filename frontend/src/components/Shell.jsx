import { useEffect, useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import NavRail from './NavRail.jsx';
import CommandBar from './CommandBar.jsx';
import Inspector from './Inspector.jsx';
import CommandPalette from './CommandPalette.jsx';
import SettingsDialog from './SettingsDialog.jsx';
import AmbientField from './AmbientField.jsx';
import { useHotkey } from '../hooks/useUi.js';
import { useInspector } from '../lib/inspector.jsx';
import { RunContextProvider, useRunContext } from '../lib/run-context.jsx';

const TITLES = [
  [/^\/$/, 'Investigate'],
  [/^\/dashboard$/, 'Provenance'],
  [/^\/runs\/?$/, 'Runs'],
  [/^\/runs\//, 'Run'],
  [/^\/library\/?$/, 'Datasets'],
  [/^\/library\//, 'Dataset'],
  [/^\/sources\/?$/, 'Sources'],
  [/^\/intel\/?$/, 'Intel'],
];

function titleFor(pathname) {
  for (const [re, t] of TITLES) if (re.test(pathname)) return t;
  return 'Datagoblin';
}

function Frame() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [palette, setPalette] = useState(false);
  const [settings, setSettings] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const { run } = useRunContext();
  const { close, open: inspectorOpen } = useInspector();

  // Global shortcuts. Digits are ignored while typing in a field, which
  // useHotkey enforces, so "3" in a prompt never navigates. The order matches
  // the sidebar: Investigate, Provenance, Runs, Datasets, Sources.
  useHotkey({
    'mod+k': () => setPalette(true),
    escape: () => {
      setPalette(false);
      setSettings(false);
      setMobileNav(false);
      close();
    },
    'mod+b': () => setMobileNav((v) => !v),
    1: () => navigate('/'),
    2: () => navigate('/dashboard'),
    3: () => navigate('/runs'),
    4: () => navigate('/library'),
    5: () => navigate('/sources'),
  });

  useEffect(() => {
    setMobileNav(false);
  }, [pathname]);

  return (
    <div className="flex h-full w-full overflow-hidden">
      {/* Decorative only: fixed, non-interactive, and every content surface
          above it is near-opaque so contrast never depends on it. */}
      <AmbientField />

      <div className="shell-layer flex h-full w-full overflow-hidden">
        <div className="hidden sm:block">
          <NavRail
            onOpenSettings={() => setSettings(true)}
            onOpenPalette={() => setPalette(true)}
          />
        </div>

        <div className="flex min-w-0 flex-1 flex-col">
          <CommandBar
            title={titleFor(pathname)}
            run={run}
            onOpenPalette={() => setPalette(true)}
            onOpenSettings={() => setSettings(true)}
          />

          <div className="flex min-h-0 flex-1">
            <main className="scroll-y min-w-0 flex-1">
              <div
                key={pathname}
                className="mx-auto w-full max-w-[1180px] px-[var(--shell-pad)] py-5 animate-fade-up"
              >
                <Outlet />
              </div>
            </main>

            {inspectorOpen ? <Inspector onClose={close} /> : null}
          </div>
        </div>

        {/* mobile: bottom tab bar */}
        {mobileNav ? (
          <div className="fixed inset-x-0 bottom-0 z-40 flex justify-around border-t border-rule bg-paper-2/95 py-1.5 backdrop-blur sm:hidden animate-slide-left">
            {[
              ['/', 'Investigate'],
              ['/dashboard', 'Provenance'],
              ['/runs', 'Runs'],
              ['/library', 'Datasets'],
              ['/sources', 'Sources'],
            ].map(([to, label]) => (
              <button
                key={to}
                type="button"
                onClick={() => {
                  navigate(to);
                  setMobileNav(false);
                }}
                aria-current={pathname === to ? 'page' : undefined}
                className={`focusable flex-1 rounded-sm py-1.5 text-[11px] transition-colors ${
                  pathname === to ? 'text-accent' : 'text-muted'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        ) : null}

        {palette ? <CommandPalette onClose={() => setPalette(false)} /> : null}
        {settings ? <SettingsDialog onClose={() => setSettings(false)} /> : null}
      </div>
    </div>
  );
}

export default function Shell() {
  return (
    <RunContextProvider>
      <Frame />
    </RunContextProvider>
  );
}
