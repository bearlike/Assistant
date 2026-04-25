import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useState } from
'react';
import { Route, Switch, useLocation, useRoute } from 'wouter';
import { AppLayout } from './components/AppLayout';
import { HomeView } from './components/HomeView';
import { SessionDetailView, SessionTokenTotals } from './components/SessionDetailView';
const SettingsView = lazy(() => import('./components/SettingsView').then(m => ({ default: m.SettingsView })));
const PluginsView = lazy(() => import('./components/PluginsView'));
const ProjectsView = lazy(() => import('./components/ProjectsView').then(m => ({ default: m.ProjectsView })));
const IdeLoader = lazy(() => import('./components/IdeLoader'));
import {
  createShare,
  exportSession,
  getConfig,
  postQuery,
  uploadAttachments
} from './api/client';
import { useSessions } from './hooks/useSessions';
import { AttachmentPayload, QueryMode, SessionContext, SessionSummary } from './types';
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
  onTokenTotalsChange: (totals: SessionTokenTotals) => void;
  onBack: () => void;
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
  onTokenTotalsChange,
  onBack,
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
        onTokenTotalsChange={onTokenTotalsChange}
      />
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-6">
      {loading ?
        <div className="text-sm text-zinc-500">Loading session…</div> :
        <div className="space-y-2">
          <div className="text-sm text-zinc-500">
            Session not found.
          </div>
          <button
            onClick={onBack}
            className="text-xs text-zinc-400 hover:text-zinc-200 transition-colors">
            Back to sessions
          </button>
        </div>
      }
    </div>
  );
}

export function App() {
  const [, setLocation] = useLocation();
  const [isSettings] = useRoute('/settings');
  const [isPlugins] = useRoute('/plugins');
  const [isProjects] = useRoute('/projects');
  const [isSessionRoute, sessionParams] = useRoute<{ id: string }>('/s/:id');
  const [isIdeLoader, ideLoaderParams] = useRoute<{ sessionId: string }>('/ide-loader/:sessionId');

  const activeSessionId = isSessionRoute && sessionParams
    ? decodeURIComponent(sessionParams.id)
    : null;

  const [actionError, setActionError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const [sessionTokenTotals, setSessionTokenTotals] = useState<SessionTokenTotals>(null);
  const [langfuseBaseUrl, setLangfuseBaseUrl] = useState<string | null>(null);
  const {
    notifications,
    dismiss: dismissNotification,
    clearAll: clearNotifications
  } = useNotifications();
  // Ensure dark mode is applied on initial mount
  useEffect(() => {
    document.documentElement.classList.remove('light');
  }, []);
  // Fetch Langfuse config once to construct session dashboard URLs
  useEffect(() => {
    getConfig()
      .then((cfg) => {
        const lf = cfg?.langfuse as Record<string, unknown> | undefined;
        if (lf?.enabled && lf?.host && lf?.project_id) {
          const host = String(lf.host).replace(/\/+$/, '');
          setLangfuseBaseUrl(`${host}/project/${lf.project_id}/sessions`);
        }
      })
      .catch(() => { /* Langfuse link is optional */ });
  }, []);
  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark';
      if (next === 'light') {
        document.documentElement.classList.add('light');
      } else {
        document.documentElement.classList.remove('light');
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
  const handleBack = useCallback(() => {
    setActionError(null);
    setSessionTokenTotals(null);
    goHome();
    void refresh();
  }, [goHome, refresh]);
  const handleSettingsClick = useCallback(() => {
    setActionError(null);
    setLocation('/settings');
  }, [setLocation]);
  const handlePluginsClick = useCallback(() => {
    setActionError(null);
    setLocation('/plugins');
  }, [setLocation]);
  const handleProjectsClick = useCallback(() => {
    setActionError(null);
    setLocation('/projects');
  }, [setLocation]);
  const handleShareSession = useCallback(async (sessionId: string) => {
    try {
      const record = await createShare(sessionId);
      const shareUrl = `${window.location.origin}/api/share/${record.token}`;
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(shareUrl);
      } else {
        window.prompt('Copy share link', shareUrl);
      }
    } catch (err) {
      const message = logApiError('shareSession', err);
      window.alert(message);
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
      window.alert(message);
    }
  }, []);
  useEffect(() => {
    if (isSettings) {
      document.title = 'Truss | Settings';
    } else if (isPlugins) {
      document.title = 'Truss | Plugins';
    } else if (isProjects) {
      document.title = 'Truss | Projects';
    } else if (isIdeLoader) {
      document.title = 'Truss | Opening Web IDE';
    } else if (isSessionRoute) {
      const title = activeSession?.title?.trim();
      document.title = title ? `${title} | Truss` : 'Session | Truss';
    } else {
      document.title = 'Truss';
    }
  }, [isSettings, isPlugins, isProjects, isIdeLoader, isSessionRoute, activeSession]);

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

  const layoutMode: 'home' | 'detail' = isSessionRoute ? 'detail' : 'home';

  return (
    <>
    <NotificationBalloon notifications={notifications} />
    <AppLayout
      mode={layoutMode}
      session={activeSession}
      onBack={handleBack}
      theme={theme}
      onToggleTheme={toggleTheme}
      notifications={notifications}
      onDismissNotification={dismissNotification}
      onClearNotifications={clearNotifications}
      onArchiveSession={archive}
      onUnarchiveSession={unarchive}
      onUpdateSessionTitle={updateTitle}
      onRegenerateTitle={regenerateTitle}
      onShareSession={handleShareSession}
      onExportSession={handleExportSession}
      onSettingsClick={handleSettingsClick}
      onPluginsClick={handlePluginsClick}
      onProjectsClick={handleProjectsClick}
      langfuseUrl={langfuseBaseUrl && activeSessionId ? `${langfuseBaseUrl}/${activeSessionId}` : null}
      sessionTokenTotals={sessionTokenTotals}>
      <Switch>
        <Route path="/settings">
          <Suspense fallback={<SuspenseFallback />}>
            <SettingsView />
          </Suspense>
        </Route>
        <Route path="/plugins">
          <Suspense fallback={<SuspenseFallback />}>
            <PluginsView />
          </Suspense>
        </Route>
        <Route path="/projects">
          <Suspense fallback={<SuspenseFallback />}>
            <ProjectsView />
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
              onTokenTotalsChange={setSessionTokenTotals}
              onBack={handleBack}
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
              onSessionSelect={handleSessionSelect}
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
