import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState } from
'react';
import { AppLayout } from './components/AppLayout';
import { HomeView } from './components/HomeView';
import { SessionDetailView } from './components/SessionDetailView';
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
import { AttachmentPayload, QueryMode, SessionContext } from './types';
import { logApiError } from './utils/errors';
import { useNotifications } from './hooks/useNotifications';
const SESSION_PATH_PREFIX = '/s/';
const IDE_LOADER_PATH_PREFIX = '/ide-loader/';
function parseRoute(pathname: string) {
  if (pathname === '/settings') {
    return { view: 'settings' as const, sessionId: null };
  }
  if (pathname === '/plugins') {
    return { view: 'plugins' as const, sessionId: null };
  }
  if (pathname === '/projects') {
    return { view: 'projects' as const, sessionId: null };
  }
  if (pathname.startsWith(IDE_LOADER_PATH_PREFIX)) {
    const rawId = pathname.slice(IDE_LOADER_PATH_PREFIX.length).replace(/\/$/, '');
    if (rawId) {
      return {
        view: 'ide-loader' as const,
        sessionId: decodeURIComponent(rawId)
      };
    }
  }
  if (pathname.startsWith(SESSION_PATH_PREFIX)) {
    const rawId = pathname.slice(SESSION_PATH_PREFIX.length);
    if (rawId) {
      return {
        view: 'detail' as const,
        sessionId: decodeURIComponent(rawId)
      };
    }
  }
  return {
    view: 'home' as const,
    sessionId: null
  };
}
function pushRoute(path: string) {
  if (window.location.pathname !== path) {
    window.history.pushState({}, '', path);
  }
}
export function App() {
  const initialRoute = useMemo(() => parseRoute(window.location.pathname), []);
  const [activeView, setActiveView] = useState<'home' | 'detail' | 'settings' | 'plugins' | 'projects' | 'ide-loader'>(
    initialRoute.view
  );
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    initialRoute.sessionId
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
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
  useEffect(() => {
    const handlePopState = () => {
      const route = parseRoute(window.location.pathname);
      setActiveView(route.view);
      setActiveSessionId(route.sessionId);
      setActionError(null);
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);
  useEffect(() => {
    if (activeSessionId && !activeSession && !archivedLoading) {
      void refreshArchived();
    }
  }, [activeSessionId, activeSession, archivedLoading, refreshArchived]);
  const goToSession = (sessionId: string) => {
    pushRoute(`${SESSION_PATH_PREFIX}${encodeURIComponent(sessionId)}`);
  };
  const goHome = () => {
    pushRoute('/');
  };
  const handleSessionSelect = (sessionId: string) => {
    setActiveSessionId(sessionId);
    setActiveView('detail');
    setActionError(null);
    goToSession(sessionId);
  };
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
      setActiveSessionId(sessionId);
      setActiveView('detail');
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
  const handleBack = () => {
    setActiveView('home');
    setActiveSessionId(null);
    setActionError(null);
    goHome();
    void refresh();
  };
  const handleSettingsClick = () => {
    setActiveView('settings');
    setActiveSessionId(null);
    setActionError(null);
    pushRoute('/settings');
  };
  const handlePluginsClick = () => {
    setActiveView('plugins');
    setActiveSessionId(null);
    setActionError(null);
    pushRoute('/plugins');
  };
  const handleProjectsClick = () => {
    setActiveView('projects');
    setActiveSessionId(null);
    setActionError(null);
    pushRoute('/projects');
  };
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
    if (activeView === 'settings') {
      document.title = 'Meeseeks | Settings';
    } else if (activeView === 'plugins') {
      document.title = 'Meeseeks | Plugins';
    } else if (activeView === 'projects') {
      document.title = 'Meeseeks | Projects';
    } else if (activeView === 'ide-loader') {
      document.title = 'Meeseeks | Opening Web IDE';
    } else if (activeView === 'home') {
      document.title = 'Meeseeks';
    } else {
      const title = activeSession?.title?.trim();
      document.title = title ? `Meeseeks | ${title}` : 'Meeseeks | Session';
    }
  }, [activeView, activeSession]);

  // The IDE loader is a standalone full-screen page with no chrome — render
  // it outside the AppLayout so it can't be mistaken for a session view.
  if (activeView === 'ide-loader' && activeSessionId) {
    return (
      <Suspense fallback={<div className="min-h-screen flex items-center justify-center"><span className="text-sm text-[hsl(var(--muted-foreground))]">Loading…</span></div>}>
        <IdeLoader sessionId={activeSessionId} />
      </Suspense>
    );
  }

  return (
    <AppLayout
      mode={activeView === 'settings' || activeView === 'plugins' || activeView === 'projects' ? 'home' : activeView === 'ide-loader' ? 'home' : activeView}
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
      langfuseUrl={langfuseBaseUrl && activeSessionId ? `${langfuseBaseUrl}/${activeSessionId}` : null}>
      {activeView === 'settings' ?
      <Suspense fallback={<div className="flex-1 flex items-center justify-center"><span className="text-sm text-[hsl(var(--muted-foreground))]">Loading…</span></div>}>
          <SettingsView />
        </Suspense> :
      activeView === 'plugins' ?
      <Suspense fallback={<div className="flex-1 flex items-center justify-center"><span className="text-sm text-[hsl(var(--muted-foreground))]">Loading…</span></div>}>
          <PluginsView />
        </Suspense> :
      activeView === 'projects' ?
      <Suspense fallback={<div className="flex-1 flex items-center justify-center"><span className="text-sm text-[hsl(var(--muted-foreground))]">Loading…</span></div>}>
          <ProjectsView />
        </Suspense> :
      activeView === 'home' ?
      <div className="flex-1 overflow-y-auto">
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

        </div> :

      <>
          {activeSession ?
        <SessionDetailView session={activeSession} onTitleUpdate={applyTitle} onSessionChange={refresh} onSelectSession={handleSessionSelect} /> :

        <div className="flex-1 overflow-y-auto p-6">
              {loading ?
          <div className="text-sm text-zinc-500">Loading session…</div> :

          <div className="space-y-2">
                  <div className="text-sm text-zinc-500">
                    Session not found.
                  </div>
                  <button
              onClick={handleBack}
              className="text-xs text-zinc-400 hover:text-zinc-200 transition-colors">

                    Back to sessions
                  </button>
                </div>
          }
            </div>
        }
        </>
      }
    </AppLayout>);

}
