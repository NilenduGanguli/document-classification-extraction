/**
 * App shell: persistent nav, theme, API-key prompt, error boundary, four routes.
 *
 * The nav order is the order a reviewer works in, not alphabetical:
 *   /analyze   run a document through and read the decision trail   (default)
 *   /registry  what the service knows how to recognise
 *   /review    what it declined to decide, waiting for a human
 *   /posture   what this deployment is allowed to do
 *
 * The header carries the egress invariant permanently. It is the one fact that makes every
 * other screen mean what it says, and it should never require navigating to find.
 */
import { Component, useCallback, useEffect, useState, type ReactNode } from 'react';
import { NavLink, Route, Routes, Navigate } from 'react-router-dom';

import { getApiKey, readiness, setApiKey } from './api';
import { Badge, ErrorState, ToastProvider } from './components';
import { declaredTrustBoundary, readsRemotely } from './ocr';
import type { ReadinessResponse } from './types';

import Analyze from './pages/Analyze';
import Posture from './pages/Posture';
import Registry from './pages/Registry';
import Review from './pages/Review';

/* ------------------------------------------------------------ theming */

type Theme = 'light' | 'dark' | 'system';
const THEME_KEY = 'dce.theme';

function readTheme(): Theme {
  try {
    const v = window.localStorage.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    return 'system';
  }
}

function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', theme);
    try {
      if (theme === 'system') window.localStorage.removeItem(THEME_KEY);
      else window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* private browsing */
    }
  }, [theme]);
  return [theme, setTheme];
}

/* ------------------------------------------------------- error boundary */

/**
 * One page throwing must not take the console with it — the pages are written independently,
 * and a reviewer in the middle of a queue should be able to navigate away from a broken panel.
 */
class Boundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="page">
          <ErrorState
            title="this page crashed"
            error={this.state.error}
            body="The rest of the console still works — the nav above is live. Reload to try again."
            action={
              <button className="btn" onClick={() => this.setState({ error: null })}>
                retry
              </button>
            }
          />
        </div>
      );
    }
    return this.props.children;
  }
}

/* ------------------------------------------------------------- header */

/**
 * The invariant, in the header, permanently. Falls back to silence if /readyz is unreachable.
 *
 * Two facts, not one, and the pill must not show the green one while the other is true. The
 * `egress` block is a claim about the **classifier**: nothing between receiving a document and
 * deciding what it is opens a socket. Recognition happens *before* that, so a deployment given a
 * remote OCR provider transmits unclassified documents while `preclassification_allowed` is
 * still false. A green "no pre-classification egress" there would be the most misleading pixel
 * in the console — it is the one badge visible on every screen, and it would be asserting
 * precisely what is not the case.
 */
function PosturePill({ ready }: { ready: ReadinessResponse | null }) {
  if (!ready) return null;
  const blocked = !ready.egress.preclassification_allowed;
  const readRemotely = readsRemotely(ready);
  if (blocked && readRemotely) {
    // The pill states the OPERATION — documents are read remotely — in both boundary
    // readings, because that is what is true either way and it is the fact a reader needs on
    // every screen. Only the tooltip's closing clause moves, and it moves by rendering the
    // service's own attribution rather than a sentence composed here. An `on_premises`
    // deployment that saw this pill call its own appliance a third party would be reading two
    // opposite accounts of one socket, which is precisely what the shared reader exists to stop.
    const declaration = declaredTrustBoundary(ready);
    const base =
      'the classifier opens no socket, but a remote OCR provider is available on this deployment: a document can be sent to be READ before anything has classified it.';
    const closing =
      declaration.attribution ||
      (declaration.boundary === 'on_premises'
        ? 'This deployment declares that host is inside its own trust boundary — the operator’s declaration, not verified here.'
        : 'No trust boundary has been declared for that host.');
    return (
      <Badge tone="warn" title={`${base} ${closing} See /posture.`}>
        documents are read remotely
      </Badge>
    );
  }
  return (
    <Badge
      tone={blocked ? 'accept' : 'danger'}
      title={ready.egress.note || 'pre-classification egress posture'}
    >
      {blocked ? 'no pre-classification egress' : 'EGRESS ALLOWED'}
    </Badge>
  );
}

function ApiKeyControl() {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(getApiKey);

  if (!editing) {
    return (
      <button
        className="btn btn-ghost btn-sm"
        onClick={() => setEditing(true)}
        title="X-API-Key sent with every /api/v1 call. Only needed if this deployment set API_KEY."
      >
        {value ? 'api key set' : 'api key'}
      </button>
    );
  }
  return (
    <form
      className="row"
      style={{ gap: 'var(--s-1)' }}
      onSubmit={(e) => {
        e.preventDefault();
        setApiKey(value.trim());
        setEditing(false);
      }}
    >
      <input
        type="password"
        value={value}
        autoFocus
        placeholder="X-API-Key"
        onChange={(e) => setValue(e.target.value)}
        style={{ width: 160 }}
      />
      <button className="btn btn-sm btn-primary" type="submit">
        save
      </button>
    </form>
  );
}

const TABS: Array<[string, string, string]> = [
  ['/analyze', 'Analyze', 'run a document and read the decision trail'],
  ['/registry', 'Registry', 'what this service can recognise'],
  ['/review', 'Review', 'what it declined to decide'],
  ['/posture', 'Posture', 'what this deployment is allowed to do'],
];

/* ---------------------------------------------------------------- app */

export default function App() {
  const [theme, setTheme] = useTheme();
  const [ready, setReady] = useState<ReadinessResponse | null>(null);

  const refresh = useCallback(() => {
    readiness()
      .then(setReady)
      .catch(() => setReady(null));
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const cycle = () => setTheme(theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system');

  return (
    <ToastProvider>
      <div className="app">
        <nav className="nav">
          <NavLink to="/analyze" className="nav-brand">
            <span>Document AI</span>
            <span className="sub">classification &amp; extraction</span>
          </NavLink>
          <div className="nav-links">
            {TABS.map(([to, label, title]) => (
              <NavLink
                key={to}
                to={to}
                title={title}
                className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}
              >
                {label}
              </NavLink>
            ))}
          </div>
          <div className="nav-right">
            <PosturePill ready={ready} />
            <ApiKeyControl />
            <button
              className="btn btn-ghost btn-sm"
              onClick={cycle}
              title={`theme: ${theme} — click to change`}
              aria-label={`theme: ${theme}`}
            >
              {theme === 'dark' ? '◓' : theme === 'light' ? '◒' : '◑'}
            </button>
          </div>
        </nav>

        <Boundary>
          <Routes>
            <Route path="/" element={<Navigate to="/analyze" replace />} />
            <Route path="/analyze" element={<Analyze readiness={ready} />} />
            <Route path="/registry" element={<Registry readiness={ready} />} />
            <Route path="/review" element={<Review readiness={ready} />} />
            <Route path="/posture" element={<Posture readiness={ready} onRefresh={refresh} />} />
            <Route path="*" element={<Navigate to="/analyze" replace />} />
          </Routes>
        </Boundary>
      </div>
    </ToastProvider>
  );
}
