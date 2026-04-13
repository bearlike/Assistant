import { useQuery } from "@tanstack/react-query";
import { fetchUsage } from "../api/client";
import { SessionUsage } from "../types";

const POLL_INTERVAL_MS = 4000;

/**
 * Fetch the session's faceted token usage from the backend (root agent vs
 * sub-agents + compaction stats). Polls while the session is running; falls
 * back to the query cache's staleTime otherwise.
 */
export function useSessionUsage(
  sessionId?: string,
  running?: boolean,
): {
  usage: SessionUsage | null;
  isLoading: boolean;
  error: unknown;
} {
  const q = useQuery<SessionUsage>({
    queryKey: ["session-usage", sessionId ?? ""],
    enabled: Boolean(sessionId),
    queryFn: () => {
      if (!sessionId) {
        // Unreachable — `enabled` gates invocation — but TypeScript needs
        // the narrowing and ESLint rejects the non-null assertion.
        return Promise.reject(new Error("no session id"));
      }
      return fetchUsage(sessionId);
    },
    refetchInterval: running ? POLL_INTERVAL_MS : false,
    staleTime: 2000,
  });

  return {
    usage: q.data ?? null,
    isLoading: q.isLoading,
    error: q.error,
  };
}
