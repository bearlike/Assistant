import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchGitDiff } from "../api/client";
import { DiffFile } from "../types";
import { extractUnifiedDiffs } from "../utils/diff";

type GitDiffState = { gitRepo: boolean; files: DiffFile[] };

export function useGitDiff(
  sessionId: string,
  scope: "uncommitted" | "branch",
  enabled = true,
): { gitRepo: boolean; files: DiffFile[]; loading: boolean; refresh(): void } {
  const qc = useQueryClient();
  const key = ["git-diff", sessionId, scope] as const;

  const q = useQuery<GitDiffState>({
    queryKey: key,
    enabled,
    queryFn: async () => {
      const resp = await fetchGitDiff(sessionId, scope);
      if (!resp.git_repo || !resp.diff) {
        return { gitRepo: false, files: [] };
      }
      return { gitRepo: true, files: extractUnifiedDiffs(resp.diff) };
    },
  });

  return {
    gitRepo: q.data?.gitRepo ?? false,
    files: q.data?.files ?? [],
    loading: enabled && q.isFetching,
    refresh: () => {
      void qc.invalidateQueries({ queryKey: key });
    },
  };
}
