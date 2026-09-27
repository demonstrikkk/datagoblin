import { NavLink, BrowserRouter, Routes, Route } from 'react-router-dom';
import Workspace from './components/workspace.jsx';
import New from './routes/New.jsx';
import Run from './routes/Run.jsx';
import Dataset from './routes/Dataset.jsx';
import History from './routes/History.jsx';
import Intel from './routes/Intel.jsx';

function Sidebar() {
  const link = ({ isActive }) =>
    'flex items-center gap-3 rounded-md px-3 py-2 text-sm ' +
    (isActive ? 'bg-[#E9DFC8] font-semibold text-ink' : 'text-ink/80 hover:bg-[#EDE7D8]');
  return (
    <aside className="flex w-60 shrink-0 flex-col gap-1 border-r border-rule bg-paper p-4" aria-label="Primary">
      <div className="px-1 pb-4">
        <p className="font-display text-2xl font-bold">Fieldwork</p>
        <p className="text-xs text-muted">Real questions. Verified answers.</p>
      </div>
      <nav className="flex flex-col gap-1">
        <NavLink to="/" end className={link}><span aria-hidden>+</span> New collection</NavLink>
        <NavLink to="/intel" className={link}><span aria-hidden>?</span> Intel</NavLink>
        <NavLink to="/history" className={link}><span aria-hidden>◷</span> Runs</NavLink>
      </nav>
      <div className="mt-auto space-y-4 pt-6">
        <div className="rounded-md border border-rule bg-warm p-3 text-xs">
          <p className="font-semibold">👑 Local dev</p>
          <p className="mt-0.5 text-muted">Proof-first datasets, run locally.</p>
        </div>
        <div className="flex items-center gap-2 border-t border-rule px-1 pt-3 text-xs">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-stone-300 font-bold" aria-hidden>DG</span>
          <div>
            <p className="font-semibold">datagoblin</p>
            <p className="text-muted">local workspace</p>
          </div>
        </div>
      </div>
    </aside>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="flex min-h-screen bg-paper text-ink">
        <div className="hidden lg:block"><Sidebar /></div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between border-b border-rule px-4 py-2 lg:hidden">
            <p className="font-display text-xl font-bold">Fieldwork</p>
            <nav className="flex gap-4 text-sm">
              <NavLink to="/">New</NavLink>
              <NavLink to="/intel">Intel</NavLink>
              <NavLink to="/history">Runs</NavLink>
            </nav>
          </div>
          <Routes>
            <Route path="/" element={<Workspace />} />
            <Route path="/new" element={<New />} />
            <Route path="/runs/:id" element={<Run />} />
            <Route path="/datasets/:id" element={<Dataset />} />
            <Route path="/history" element={<History />} />
            <Route path="/intel" element={<Intel />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
}
