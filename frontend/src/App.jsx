import { Component, Suspense, lazy } from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import Shell from './components/Shell.jsx';
import { InspectorProvider } from './lib/inspector.jsx';
import { ErrorNote } from './components/ui.jsx';

const Collect = lazy(() => import('./pages/Collect.jsx'));
const Runs = lazy(() => import('./pages/Runs.jsx'));
const RunPage = lazy(() => import('./pages/RunPage.jsx'));
const Library = lazy(() => import('./pages/Library.jsx'));
const DatasetPage = lazy(() => import('./pages/DatasetPage.jsx'));
const Intel = lazy(() => import('./pages/Intel.jsx'));

class Boundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error('Unhandled UI error', error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="grid h-full place-items-center p-6">
          <div className="w-full max-w-md space-y-3">
            <h1 className="h-display text-[22px] text-ink">Something broke</h1>
            <p className="text-[13px] leading-relaxed text-muted">
              A view failed to render. The run itself is unaffected — it keeps executing on the
              server. Reloading usually clears it.
            </p>
            <ErrorNote error={this.state.error} />
            <div className="flex gap-2">
              <button type="button" className="btn-primary" onClick={() => this.setState({ error: null })}>
                Try again
              </button>
              <button type="button" className="btn-outline" onClick={() => window.location.reload()}>
                Reload
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function RouteFallback() {
  return (
    <div className="space-y-4" role="status" aria-busy="true" aria-label="Loading view">
      <div className="skeleton h-7 w-52" />
      <div className="skeleton h-24 w-full rounded-lg" />
      <div className="skeleton h-56 w-full rounded-lg" />
    </div>
  );
}

function NotFound() {
  return (
    <div className="grid place-items-center py-24 text-center animate-fade-up">
      <p className="font-display text-[64px] leading-none text-rule-2">404</p>
      <h1 className="mt-3 h-display text-[20px] text-ink">No such view</h1>
      <p className="mt-1.5 max-w-sm text-[12.5px] text-muted">
        That address does not match anything in the workspace.
      </p>
      <a href="/" className="btn-primary mt-5">
        Back to Collect
      </a>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <InspectorProvider>
        <Boundary>
          <Suspense fallback={<RouteFallback />}>
            <Routes>
              <Route element={<Shell />}>
                <Route index element={<Collect />} />
                <Route path="runs" element={<Runs />} />
                <Route path="runs/:id" element={<RunPage />} />
                <Route path="library" element={<Library />} />
                <Route path="library/:id" element={<DatasetPage />} />
                <Route path="intel" element={<Intel />} />
                <Route path="*" element={<NotFound />} />
              </Route>
            </Routes>
          </Suspense>
        </Boundary>
      </InspectorProvider>
    </BrowserRouter>
  );
}
