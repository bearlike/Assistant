import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getIde, IdeInstance } from "../api/ide";

const POLL_INTERVAL_MS = 30_000;
const ACTIVE_POLL_INTERVAL_MS = 2_000;

/**
 * Subscribe to `GET /api/sessions/<sid>/ide`. TanStack Query handles
 * window-focus refetches and tab-visibility pausing globally; this hook
 * just polls faster while the instance is in a transitional state.
 */
export function useIdeStatus(sessionId: string | null | undefined) {
  const qc = useQueryClient();
  const key = ["ide", sessionId ?? null] as const;

  const q = useQuery({
    queryKey: key,
    queryFn: () => (sessionId ? getIde(sessionId) : Promise.resolve(null)),
    enabled: Boolean(sessionId),
    refetchInterval: (query) => {
      const data = query.state.data as IdeInstance | null | undefined;
      if (!data) return false;
      return data.status !== "ready"
        ? ACTIVE_POLL_INTERVAL_MS
        : POLL_INTERVAL_MS;
    },
  });

  return {
    instance: q.data ?? null,
    refresh: async () => {
      await qc.invalidateQueries({ queryKey: key });
    },
    setInstance: (next: IdeInstance | null) => {
      qc.setQueryData(key, next);
    },
  };
}
