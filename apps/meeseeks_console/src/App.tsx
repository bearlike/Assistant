import React, {
  useCallback,
  useEffect,
  useMemo,
  useState } from
'react';
import { AppLayout } from './components/AppLayout';
import { HomeView } from './components/HomeView';
import { SessionDetailView } from './components/SessionDetailView';
import {
  createShare,
  exportSession,
  postQuery,
  uploadAttachments
} from './api/client';
import { useSessions } from './hooks/useSessions';
import { AttachmentPayload, QueryMode, SessionContext } from './types';
import { logApiError } from './utils/errors';
import { useNotifications } from './hooks/useNotifications';
const SESSION_PATH_PREFIX = '/s/';
function parseRoute(pathname: string) {
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
  const [activeView, setActiveView] = useState<'home' | 'detail'>(
    initialRoute.view
  );
  const [activeSessionId, setActiveSessionId] = useState<string | null>(
    initialRoute.sessionId
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const {
    notifications,
    dismiss: dismissNotification,
    clearAll: clearNotifications
  } = useNotifications();
  // Ensure dark mode is applied on initial mount
  useEffect(() => {
    document.documentElement.classList.remove('light');
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
    unarchive
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
    if (activeView === 'home') {
      document.title = 'Meeseeks';
    } else {
      const title = activeSession?.title?.trim();
      document.title = title ? `Meeseeks | ${title}` : 'Meeseeks | Session';
    }
  }, [activeView, activeSession]);
  return (
    <AppLayout
      mode={activeView}
      session={activeSession}
      onBack={handleBack}
      theme={theme}
      onToggleTheme={toggleTheme}
      notifications={notifications}
      onDismissNotification={dismissNotification}
      onClearNotifications={clearNotifications}
      onArchiveSession={archive}
      onUnarchiveSession={unarchive}
      onShareSession={handleShareSession}
      onExportSession={handleExportSession}>
      {activeView === 'home' ?
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
          isCreating={creating} />

        </div> :

      <>
          {activeSession ?
        <SessionDetailView session={activeSession} /> :

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
