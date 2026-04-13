import { useCallback, useEffect, useState } from "react";
import { fetchGitDiff } from "../api/client";
import { DiffFile } from "../types";
import { extractUnifiedDiffs } from "../utils/diff";

export function useGitDiff(
  sessionId: string,
  scope: "uncommitted" | "branch",
  enabled = true
): { gitRepo: boolean; files: DiffFile[]; loading: boolean; refresh(): void } {
  const [gitRepo, setGitRepo] = useState(false);
  const [files, setFiles] = useState<DiffFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) {
      setGitRepo(false);
      setFiles([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    fetchGitDiff(sessionId, scope)
      .then((resp) => {
        if (cancelled) return;
        if (!resp.git_repo || !resp.diff) {
          setGitRepo(false);
          setFiles([]);
        } else {
          setGitRepo(true);
          setFiles(extractUnifiedDiffs(resp.diff));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setGitRepo(false);
          setFiles([]);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, scope, enabled, tick]);

  return { gitRepo, files, loading, refresh };
}
