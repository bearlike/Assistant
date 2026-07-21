/**
 * TanStack Query hooks for the reverse-invocation trigger subsystem.
 *
 * Reads: `useTriggers(filters)` (the management page) and
 * `useSessionTriggers(sessionId)` (the armed-triggers chips on a session view).
 * Mutations: pause/resume + cancel apply an OPTIMISTIC patch across every
 * cached trigger list (so a row updates instantly), roll back on error, and
 * invalidate the `['triggers']` prefix on settle. `useTerminateSession`
 * invalidates both the session lists and every trigger list (the cascade
 * cancels this session's armed triggers server-side).
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  cancelTrigger,
  listSessionTriggers,
  listTriggers,
  terminateSession,
  updateTriggerStatus,
  type TriggerDTO,
  type TriggerListFilters,
  type TriggerStatus,
} from "../api/triggers";
import { logApiError } from "../utils/errors";

export const TRIGGERS_ROOT = ["triggers"] as const;

// Stable empty ref so consumers' effects/memos don't churn before first data.
const EMPTY: TriggerDTO[] = [];

/** Normalise filters into a compact, stable object for the query key. */
function normalizeFilters(filters: TriggerListFilters): TriggerListFilters {
  const out: TriggerListFilters = {};
  if (filters.session_id) out.session_id = filters.session_id;
  if (filters.kind) out.kind = filters.kind;
  if (filters.status) out.status = filters.status;
  if (filters.limit != null) out.limit = filters.limit;
  return out;
}

/** GET /api/triggers with optional filters — the management-page list. */
export function useTriggers(filters: TriggerListFilters = {}) {
  const qc = useQueryClient();
  const norm = normalizeFilters(filters);
  const q = useQuery({
    queryKey: [...TRIGGERS_ROOT, "list", norm] as const,
    queryFn: ({ signal }) => listTriggers(norm, signal),
    staleTime: 10_000,
  });
  return {
    triggers: q.data ?? EMPTY,
    loading: q.isPending,
    fetching: q.isFetching,
    error: q.error ? logApiError("listTriggers", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: TRIGGERS_ROOT }),
  };
}

/** GET /api/sessions/{id}/triggers — one session's triggers. */
export function useSessionTriggers(sessionId?: string | null) {
  const q = useQuery({
    queryKey: [...TRIGGERS_ROOT, "session", sessionId ?? ""] as const,
    queryFn: ({ signal }) => listSessionTriggers(sessionId as string, signal),
    enabled: Boolean(sessionId),
    staleTime: 10_000,
  });
  return {
    triggers: q.data ?? EMPTY,
    loading: q.isPending && Boolean(sessionId),
    error: q.error ? logApiError("listSessionTriggers", q.error) : null,
  };
}

/**
 * Patch one trigger (by id) to a new status across EVERY cached trigger list
 * (both the filtered `list` queries and the per-session queries).
 */
function patchTriggerStatus(
  qc: ReturnType<typeof useQueryClient>,
  id: string,
  status: TriggerStatus,
) {
  qc.setQueriesData<TriggerDTO[]>({ queryKey: TRIGGERS_ROOT }, (data) =>
    data?.map((t) => (t.id === id ? { ...t, status } : t)),
  );
}

/**
 * Snapshot of one row's PRIOR state across every cached trigger list, keyed by
 * query key (`undefined` when that list didn't contain the row). Scoping the
 * snapshot to a single id — rather than cloning each whole list — means one
 * mutation's error rollback only ever touches its own row, so a concurrent
 * mutation on a different row (still optimistic, not yet settled) survives.
 */
function snapshotRow(qc: ReturnType<typeof useQueryClient>, id: string) {
  return qc
    .getQueriesData<TriggerDTO[]>({ queryKey: TRIGGERS_ROOT })
    .map(([key, data]) => [key, data?.find((t) => t.id === id)] as const);
}

/** Roll back just row *id* in every cached list, leaving every other row untouched. */
function rollbackRow(
  qc: ReturnType<typeof useQueryClient>,
  id: string,
  snapshot: ReturnType<typeof snapshotRow>,
) {
  for (const [key, prior] of snapshot) {
    qc.setQueryData<TriggerDTO[]>(key, (data) => {
      if (!data) return data;
      return prior ? data.map((t) => (t.id === id ? prior : t)) : data.filter((t) => t.id !== id);
    });
  }
}

/** PATCH /api/triggers/{id} — pause (`paused`) or resume (`armed`). */
export function usePauseResumeTrigger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; status: "paused" | "armed" }) =>
      updateTriggerStatus(vars.id, vars.status),
    onMutate: async (vars) => {
      await qc.cancelQueries({ queryKey: TRIGGERS_ROOT });
      const snapshot = snapshotRow(qc, vars.id);
      patchTriggerStatus(qc, vars.id, vars.status);
      return { id: vars.id, snapshot };
    },
    onError: (err, _vars, ctx) => {
      if (ctx) rollbackRow(qc, ctx.id, ctx.snapshot);
      logApiError("updateTriggerStatus", err);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: TRIGGERS_ROOT }),
  });
}

/** DELETE /api/triggers/{id} — cancel. Optimistically flips to `cancelled`. */
export function useCancelTrigger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => cancelTrigger(id),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: TRIGGERS_ROOT });
      const snapshot = snapshotRow(qc, id);
      patchTriggerStatus(qc, id, "cancelled");
      return { id, snapshot };
    },
    onError: (err, _id, ctx) => {
      if (ctx) rollbackRow(qc, ctx.id, ctx.snapshot);
      logApiError("cancelTrigger", err);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: TRIGGERS_ROOT }),
  });
}

/**
 * POST /api/sessions/{id}/terminate — permanent. Invalidates the session lists
 * (so the row flips to `terminated`) and every trigger list (the cascade
 * cancels this session's armed triggers server-side).
 */
export function useTerminateSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => terminateSession(sessionId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      void qc.invalidateQueries({ queryKey: TRIGGERS_ROOT });
    },
  });
}
