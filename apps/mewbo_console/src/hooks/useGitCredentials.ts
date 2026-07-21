import { useQuery, useQueryClient } from "@tanstack/react-query";
import { listGitCredentials, GitCredentialSummary } from "../api/git";
import { logApiError } from "../utils/errors";

export const GIT_CREDENTIALS_KEY = ["git-credentials"] as const;

/**
 * The product-wide git credential list (`GET /v1/git/credentials`). Consumed
 * by the settings section (full CRUD) and, read-only, by the onboarding wizard
 * to hint when a saved credential already covers the entered repo. Mutations
 * live at the call site (`useMutation`) and invalidate this key on success.
 */
export function useGitCredentials(enabled = true) {
  const qc = useQueryClient();
  const q = useQuery<GitCredentialSummary[]>({
    queryKey: GIT_CREDENTIALS_KEY,
    queryFn: () => listGitCredentials(),
    staleTime: 30_000,
    enabled,
  });
  return {
    credentials: q.data ?? [],
    loading: q.isPending,
    error: q.error ? logApiError("listGitCredentials", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: GIT_CREDENTIALS_KEY }),
  };
}
