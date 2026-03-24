import { useCallback, useEffect, useState } from "react";
import { archiveSession, createSession, listSessions, unarchiveSession } from "../api/client";
import { SessionContext, SessionSummary } from "../types";
import { logApiError } from "../utils/errors";
export function useSessions() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [archivedSessions, setArchivedSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [archivedLoading, setArchivedLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [archivedError, setArchivedError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await listSessions(false);
      setSessions(data);
    } catch (err) {
      const message = logApiError("listSessions", err);
      setError(message);
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, []);
  const refreshArchived = useCallback(async () => {
    setArchivedLoading(true);
    setArchivedError(null);
    try {
      const data = await listSessions(true);
      setArchivedSessions(data.filter((session) => session.archived));
    } catch (err) {
      const message = logApiError("listArchivedSessions", err);
      setArchivedError(message);
      setArchivedSessions([]);
    } finally {
      setArchivedLoading(false);
    }
  }, []);
  const create = useCallback(async (context?: SessionContext) => {
    const safeContext = context ?? { mcp_tools: [] };
    const sessionId = await createSession(safeContext);
    await refresh();
    return sessionId;
  }, [refresh]);
  const archive = useCallback(async (sessionId: string) => {
    await archiveSession(sessionId);
    await refresh();
    await refreshArchived();
  }, [refresh, refreshArchived]);
  const unarchive = useCallback(async (sessionId: string) => {
    await unarchiveSession(sessionId);
    await refresh();
    await refreshArchived();
  }, [refresh, refreshArchived]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    const handleFocus = () => {
      void refresh();
    };
    const handleVisibility = () => {
      if (!document.hidden) {
        void refresh();
      }
    };
    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [refresh]);
  return {
    sessions,
    archivedSessions,
    loading,
    archivedLoading,
    error,
    archivedError,
    refresh,
    refreshArchived,
    create,
    archive,
    unarchive
  };
}
