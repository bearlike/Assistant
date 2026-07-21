import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState } from
'react';
import { toast } from 'sonner';
import { Redirect, Route, Switch, useLocation, useRoute, useSearchParams } from 'wouter';
import type { AppendMessage } from '@assistant-ui/react';
import { AppLayout } from './components/AppLayout';
import type { ActiveProduct } from './components/nav-rail/products';
import { HomeView } from './components/HomeView';
import { SessionDetailView } from './components/SessionDetailView';
const SettingsView = lazy(() => import('./components/SettingsView').then(m => ({ default: m.SettingsView })));
const IdeLoader = lazy(() => import('./components/IdeLoader').then(m => ({ default: m.IdeLoader })));
const AgenticSearchView = lazy(() => import('./components/agentic_search/AgenticSearchView').then(m => ({ default: m.AgenticSearchView })));
const WikiApp = lazy(() => import('./components/wiki/WikiApp').then(m => ({ default: m.WikiApp })));
const AppsView = lazy(() => import('./components/apps/AppsView').then(m => ({ default: m.AppsView })));
import {
  createShare,
  exportSession,
  postQuery,
  sendMessage,
  uploadAttachments
} from './api/client';
import { useConfig } from './hooks/useConfig';
import { useSessions } from './hooks/useSessions';
import { AttachmentPayload, QueryMode, SessionContext, SessionSummary } from './types';
import { copyText } from './utils/clipboard';
import { logApiError } from './utils/errors';
import { useNotifications } from './hooks/useNotifications';
import { NotificationBalloon } from './components/NotificationBalloon';

function SuspenseFallback({ fullScreen = false }: { fullScreen?: boolean }) {
  const wrapper = fullScreen
    ? "min-h-screen flex items-center justify-center"
    : "flex-1 flex items-center justify-center";
  return (
    <div className={wrapper}>
      <span className="text-sm text-[hsl(var(--muted-foreground))]">Loading…</span>
    </div>
  );
}

/**
 * `/triggers` retired into the Settings automation facet. Unlike the other three
 * redirects this one can't be a static `<Redirect to>`: `SessionTriggersSection`
 * deep-links `/triggers?session=<id>` and the automation pane still reads that
 * param, so the query has to survive the hop.
 */
function TriggersRedirect() {
  const [params] = useSearchParams();
  const session = params.get('session');
  const to = session
    ? `/settings?facet=automation&session=${encodeURIComponent(session)}`
    : '/settings?facet=automation';
  return <Redirect to={to} replace />;
}

interface SessionDetailRouteProps {
  id: string;
  sessions: SessionSummary[];
  archivedSessions: SessionSummary[];
  loading: boolean;
  archivedLoading: boolean;
  refreshArchived: () => Promise<void>;
  refresh: () => Promise<void>;
  applyTitle: (sessionId: string, title: string) => void;
  onSelectSession: (sessionId: string) => void;
  onBack: () => void;
  // Session-header obligations, re-homed from the old detail NavBar.
  onRenameTitle: (sessionId: string, title: string) => Promise<void>;
  onRegenerateTitle: (sessionId: string) => Promise<string>;
  onArchive: (sessionId: string) => void;
  onUnarchive: (sessionId: string) => void;
  onShare: (sessionId: string) => void;
  onExport: (sessionId: string) => void;
  langfuseBaseUrl: string | null;
}

// Resolves a session by id from active + archived lists, lazily fetching the
// archived list if the id isn't found locally. Mirrors the hydration logic the
// old activeSession lookup performed via a side-effect useEffect.
function SessionDetailRoute({
  id,
  sessions,
  archivedSessions,
  loading,
  archivedLoading,
  refreshArchived,
  refresh,
  applyTitle,
  onSelectSession,
  onBack,
  onRenameTitle,
  onRegenerateTitle,
  onArchive,
  onUnarchive,
  onShare,
  onExport,
  langfuseBaseUrl,
}: SessionDetailRouteProps) {
  const session =
    sessions.find((s) => s.session_id === id) ||
    archivedSessions.find((s) => s.session_id === id);

  useEffect(() => {
    if (!session && !archivedLoading) {
      void refreshArchived();
    }
  }, [session, archivedLoading, refreshArchived]);

  if (session) {
    return (
      <SessionDetailView
        session={session}
        onTitleUpdate={applyTitle}
        onSessionChange={refresh}
        onSelectSession={onSelectSession}
        onBack={onBack}
        onRenameTitle={onRenameTitle}
        onRegenerateTitle={onRegenerateTitle}
        onArchive={onArchive}
        onUnarchive={onUnarchive}
        onShare={onShare}
        onExport={onExport}
        langfuseUrl={langfuseBaseUrl ? `${langfuseBaseUrl}/${session.session_id}` : null}
      />
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-6">
      {loading ?
        <div className="text-sm text-[hsl(var(--muted-foreground))]">Loading session…</div> :
        <div className="space-y-2">
          <div className="text-sm text-[hsl(var(--muted-foreground))]">
            Session not found.
          </div>
          <button
            onClick={onBack}
            className="text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">
            Back to sessions
          </button>
        </div>
      }
    </div>
  );
}

export function App() {
  const [location, setLocation] = useLocation();
  const [isSettings] = useRoute('/settings');
  const [isSearch] = useRoute('/search');
  const [isWikiRoot] = useRoute('/wiki');
  const [isWikiSub] = useRoute('/wiki/*');
  const isWiki = isWikiRoot || isWikiSub;
  const [isAppsRoot] = useRoute('/apps');
  const [isAppsSub] = useRoute('/apps/*');
  const isApps = isAppsRoot || isAppsSub;
  const [isSessionRoute, sessionParams] = useRoute<{ id: string }>('/s/:id');
  const [isIdeLoader, ideLoaderParams] = useRoute<{ sessionId: string }>('/ide-loader/:sessionId');

  const activeSessionId = isSessionRoute && sessionParams
    ? decodeURIComponent(sessionParams.id)
    : null;

  const [actionError, setActionError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    try {
      return window.localStorage.getItem('mewbo:theme') === 'light' ? 'light' : 'dark';
    } catch {
      return 'dark';
    }
  });
  const {
    notifications,
    dismiss: dismissNotification,
    clearAll: clearNotifications
  } = useNotifications();
  // Keep the <html> class in sync with theme (initial value may be persisted).
  // toggleTheme also mutates the class synchronously before dispatching
  // wiki:theme-change; this effect is an idempotent re-assert, not the primary sync.
  useEffect(() => {
    if (theme === 'light') {
      document.documentElement.classList.add('light');
    } else {
      document.documentElement.classList.remove('light');
    }
  }, [theme]);
  // Langfuse config, to construct session dashboard URLs — reads the SAME
  // ['config'] TanStack query every other config consumer shares, instead of
  // a one-off fetch-in-useEffect that bypassed the cache.
  const { config } = useConfig();
  const langfuseBaseUrl = useMemo(() => {
    const lf = config?.langfuse as Record<string, unknown> | undefined;
    if (!lf?.enabled || !lf?.host || !lf?.project_id) return null;
    const host = String(lf.host).replace(/\/+$/, '');
    return `${host}/project/${lf.project_id}/sessions`;
  }, [config]);
  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark';
      if (next === 'light') {
        document.documentElement.classList.add('light');
      } else {
        document.documentElement.classList.remove('light');
      }
      // Notify wiki Mermaid blocks (and any other theme-aware paint) to re-render.
      window.dispatchEvent(new CustomEvent('wiki:theme-change', { detail: next }));
      try {
        window.localStorage.setItem('mewbo:theme', next);
      } catch {
        // No-op: persistence is best-effort (private browsing, quota, etc.)
      }
      return next;
    });
  }, []);
  const {
    sessions,
    archivedSessions,
    loading,
    archivedLoading,
    error,
    archivedError,
    create,
    refresh,
    refreshArchived,
    archive,
    unarchive,
    updateTitle,
    regenerateTitle,
    applyTitle
  } = useSessions();
  const activeSession =
    sessions.find((session) => session.session_id === activeSessionId) ||
    archivedSessions.find((session) => session.session_id === activeSessionId);

  const goToSession = useCallback((sessionId: string) => {
    setLocation(`/s/${encodeURIComponent(sessionId)}`);
  }, [setLocation]);
  const goHome = useCallback(() => {
    setLocation('/');
  }, [setLocation]);
  const handleSessionSelect = useCallback((sessionId: string) => {
    setActionError(null);
    goToSession(sessionId);
  }, [goToSession]);
  const handleCreateAndRun = async (
  query: string,
  context?: SessionContext,
  mode?: QueryMode,
  attachments?: File[]) =>
  {
    setActionError(null);
    setCreating(true);
    try {
      const sessionId = await create(context);
      const attachmentRecords: AttachmentPayload[] | undefined =
      attachments && attachments.length > 0 ?
      await uploadAttachments(sessionId, attachments) :
      undefined;
      await postQuery(sessionId, query, context, mode, attachmentRecords);
      goToSession(sessionId);
      await refresh();
      window.setTimeout(() => {
        void refresh();
      }, 800);
    } catch (err) {
      const message = logApiError('createAndRun', err);
      setActionError(message);
    } finally {
      setCreating(false);
    }
  };
  // assistant-ui composer submit seam (`onNew` on the external-store runtime).
  // The bespoke InputBar still owns the live composer, so this only fires once
  // the assistant-ui composer WP is wired in. It routes create-and-run (landing)
  // vs steer (open session); that WP refines context/mode/attachment handling.
  const handleComposerNew = async (message: AppendMessage) => {
    const text = message.content
      .map((part) => (part.type === 'text' ? part.text : ''))
      .join('')
      .trim();
    if (!text) return;
    if (activeSessionId) {
      // Mirror useSessionQuery.send: a mid-run turn must STEER via /message,
      // not start a second run via /query (double-fire otherwise).
      if (activeSession?.running) {
        await sendMessage(activeSessionId, text);
      } else {
        await postQuery(activeSessionId, text);
      }
      await refresh();
    } else {
      await handleCreateAndRun(text);
    }
  };
  const handleBack = useCallback(() => {
    setActionError(null);
    goHome();
    void refresh();
  }, [goHome, refresh]);
  const handleShareSession = useCallback(async (sessionId: string) => {
    try {
      const record = await createShare(sessionId);
      const shareUrl = `${window.location.origin}/api/share/${record.token}`;
      // `copyText` already owns the Clipboard-API-vs-execCommand fallback —
      // no need to hand-roll that split here too.
      await copyText(shareUrl);
      toast.success('Share link copied to clipboard.', { description: shareUrl });
    } catch (err) {
      const message = logApiError('shareSession', err);
      toast.error(message);
    }
  }, []);
  const handleExportSession = useCallback(async (sessionId: string) => {
    try {
      const payload = await exportSession(sessionId);
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: 'application/json'
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `session-${sessionId}.json`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (err) {
      const message = logApiError('exportSession', err);
      toast.error(message);
    }
  }, []);
  useEffect(() => {
    if (isSettings) {
      document.title = 'Settings | Mewbo';
    } else if (isSearch) {
      document.title = 'Agentic Search | Mewbo';
    } else if (isWiki) {
      // WikiApp manages its own title; leave the default here.
    } else if (isApps) {
      // AppsView manages its own title; leave the default here.
    } else if (isIdeLoader) {
      document.title = 'Opening Web IDE | Mewbo';
    } else if (isSessionRoute) {
      const title = activeSession?.title?.trim();
      document.title = title ? `${title} | Mewbo` : 'Session | Mewbo';
    } else {
      document.title = 'Agentic Tasks | Mewbo';
    }
  }, [isSettings, isSearch, isWiki, isApps, isIdeLoader, isSessionRoute, activeSession]);

  // The IDE loader is a standalone full-screen page with no chrome — render
  // it outside the AppLayout so it can't be mistaken for a session view.
  if (isIdeLoader && ideLoaderParams) {
    const ideSessionId = decodeURIComponent(ideLoaderParams.sessionId);
    return (
      <Suspense fallback={<SuspenseFallback fullScreen />}>
        <IdeLoader sessionId={ideSessionId} />
      </Suspense>
    );
  }

  // What the rail is scoped to — computed at the same altitude the old
  // `landingNav` was. `wiki > search > apps > tasks`; a session page and the
  // task landing are both Tasks. `/settings` scopes the rail's section to the
  // settings facets while marking NO product row current (it has no switcher
  // row), which is how that route gets primary navigation without a fifth
  // product. Any other route is `null`: no row current, section falls back to
  // Tasks, and the rail still renders.
  const activeProduct: ActiveProduct = isWiki
    ? 'wiki'
    : isSearch
      ? 'search'
      : isApps
        ? 'apps'
        : isSettings
          ? 'settings'
          : (isSessionRoute || location === '/')
            ? 'tasks'
            : null;

  return (
    <>
    <NotificationBalloon notifications={notifications} />
    <AppLayout
      activeProduct={activeProduct}
      theme={theme}
      onToggleTheme={toggleTheme}
      notifications={notifications}
      onDismissNotification={dismissNotification}
      onClearNotifications={clearNotifications}
      sessions={sessions}
      archivedSessions={archivedSessions}
      activeSessionId={activeSessionId}
      isRunning={activeSession?.running ?? false}
      onComposerNew={handleComposerNew}
      onSwitchToThread={handleSessionSelect}
      onSwitchToNewThread={goHome}
      onRenameThread={updateTitle}
      onArchiveThread={archive}>
      <Switch>
        <Route path="/settings">
          <Suspense fallback={<SuspenseFallback />}>
            <SettingsView />
          </Suspense>
        </Route>
        {/* Retired pages — Projects / Plugins / API Keys / Triggers are now
            Settings facets. Redirects (not deletions) so bookmarks and existing
            in-app deep links keep resolving. */}
        <Route path="/projects">
          <Redirect to="/settings?facet=workspace" replace />
        </Route>
        <Route path="/plugins">
          <Redirect to="/settings?facet=plugins" replace />
        </Route>
        <Route path="/keys">
          <Redirect to="/settings?facet=security" replace />
        </Route>
        <Route path="/triggers">
          <TriggersRedirect />
        </Route>
        <Route path="/search">
          <Suspense fallback={<SuspenseFallback />}>
            <AgenticSearchView />
          </Suspense>
        </Route>
        <Route path="/wiki/*?">
          <Suspense fallback={<SuspenseFallback />}>
            <WikiApp />
          </Suspense>
        </Route>
        <Route path="/apps/*?">
          <Suspense fallback={<SuspenseFallback />}>
            <AppsView />
          </Suspense>
        </Route>
        <Route path="/s/:id">
          {(params) => (
            <SessionDetailRoute
              id={decodeURIComponent(params.id)}
              sessions={sessions}
              archivedSessions={archivedSessions}
              loading={loading}
              archivedLoading={archivedLoading}
              refreshArchived={refreshArchived}
              refresh={refresh}
              applyTitle={applyTitle}
              onSelectSession={handleSessionSelect}
              onBack={handleBack}
              onRenameTitle={updateTitle}
              onRegenerateTitle={regenerateTitle}
              onArchive={archive}
              onUnarchive={unarchive}
              onShare={handleShareSession}
              onExport={handleExportSession}
              langfuseBaseUrl={langfuseBaseUrl}
            />
          )}
        </Route>
        <Route>
          <div className="flex-1 overflow-hidden">
            <HomeView
              sessions={sessions}
              archivedSessions={archivedSessions}
              loading={loading}
              archivedLoading={archivedLoading}
              error={error}
              archivedError={archivedError}
              actionError={actionError}
              onSelectSession={handleSessionSelect}
              onCreateAndRun={handleCreateAndRun}
              onLoadArchived={refreshArchived}
              onArchive={archive}
              onUnarchive={unarchive}
              isCreating={creating}
              onRetry={refresh} />
          </div>
        </Route>
      </Switch>
    </AppLayout>
    </>);

}
