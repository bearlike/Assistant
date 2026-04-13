import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  archiveSession,
  createSession,
  listSessions,
  regenerateTitle as apiRegenerateTitle,
  unarchiveSession,
  updateSessionTitle,
} from "../api/client";
import { SessionContext, SessionSummary } from "../types";
import { logApiError } from "../utils/errors";

export function useSessions() {
  const qc = useQueryClient();
  const active = useQuery({
    queryKey: ["sessions", "active"],
    queryFn: () => listSessions(false),
  });
  const archived = useQuery({
    queryKey: ["sessions", "archived"],
    queryFn: () => listSessions(true).then((d) => d.filter((s) => s.archived)),
    enabled: false,
  });

  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: ["sessions", "active"] });
  };
  const refreshArchived = async () => {
    await archived.refetch();
  };

  const createM = useMutation({
    mutationFn: (ctx?: SessionContext) =>
      createSession(ctx ?? { mcp_tools: [] }),
    onSuccess: () => {
      void refresh();
    },
  });
  const archiveM = useMutation({
    mutationFn: (id: string) => archiveSession(id),
    onSuccess: () => {
      void refresh();
      void refreshArchived();
    },
  });
  const unarchiveM = useMutation({
    mutationFn: (id: string) => unarchiveSession(id),
    onSuccess: () => {
      void refresh();
      void refreshArchived();
    },
  });

  const applyTitle = (id: string, title: string) => {
    const patch = (prev?: SessionSummary[]) =>
      prev?.map((s) => (s.session_id === id ? { ...s, title } : s)) ?? [];
    qc.setQueryData<SessionSummary[]>(["sessions", "active"], patch);
    qc.setQueryData<SessionSummary[]>(["sessions", "archived"], patch);
  };

  const updateTitleM = useMutation({
    mutationFn: (vars: { id: string; title: string }) =>
      updateSessionTitle(vars.id, vars.title),
    onSuccess: (res) => applyTitle(res.session_id, res.title),
  });
  const regenerateTitleM = useMutation({
    mutationFn: (id: string) => apiRegenerateTitle(id),
    onSuccess: (res) => applyTitle(res.session_id, res.title),
  });

  return {
    sessions: active.data ?? [],
    archivedSessions: archived.data ?? [],
    loading: active.isPending,
    archivedLoading: archived.isFetching,
    error: active.error ? logApiError("listSessions", active.error) : null,
    archivedError: archived.error
      ? logApiError("listArchivedSessions", archived.error)
      : null,
    refresh,
    refreshArchived,
    create: async (ctx?: SessionContext) => createM.mutateAsync(ctx),
    archive: async (id: string) => {
      await archiveM.mutateAsync(id);
    },
    unarchive: async (id: string) => {
      await unarchiveM.mutateAsync(id);
    },
    updateTitle: async (id: string, title: string) => {
      await updateTitleM.mutateAsync({ id, title });
    },
    regenerateTitle: async (id: string) =>
      (await regenerateTitleM.mutateAsync(id)).title,
    applyTitle,
  };
}
