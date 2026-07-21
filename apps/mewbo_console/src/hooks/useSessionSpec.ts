import { useQuery } from "@tanstack/react-query";
import { getSessionSpec } from "../api/client";
import { SessionSpecBinding, SessionSpecEditable, SessionSpecResponse } from "../types";

/**
 * Fetch a session's durable purpose-binding (`GET /api/sessions/<id>/spec`) —
 * the projection a read-only binding panel renders and a composer hydrates
 * from. The binding is durable state, not live telemetry, so this never polls;
 * it re-fetches on the query cache's staleTime like any other session GET.
 *
 * Returns the raw response too so a caller can read `source` (durable vs
 * reconstructed). `editable` is the server's fail-closed modifiability map —
 * treat a field editable ONLY when its value is `=== true`.
 */
export function useSessionSpec(sessionId?: string): {
  spec: SessionSpecBinding | null;
  editable: SessionSpecEditable;
  source: SessionSpecResponse["source"] | null;
  isLoading: boolean;
  error: unknown;
} {
  const q = useQuery<SessionSpecResponse>({
    queryKey: ["session-spec", sessionId ?? ""],
    enabled: Boolean(sessionId),
    queryFn: () => {
      if (!sessionId) {
        // Unreachable — `enabled` gates invocation — but TypeScript needs the
        // narrowing and ESLint rejects the non-null assertion.
        return Promise.reject(new Error("no session id"));
      }
      return getSessionSpec(sessionId);
    },
    staleTime: 30_000,
  });

  return {
    spec: q.data?.spec ?? null,
    editable: q.data?.editable ?? {},
    source: q.data?.source ?? null,
    isLoading: q.isLoading,
    error: q.error,
  };
}
