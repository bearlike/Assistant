import { useCallback, useEffect, useState } from "react";
import { listProjects, invalidateCache, peekCache, ProjectSummary } from "../api/client";
import { logApiError } from "../utils/errors";

function readCached(): ProjectSummary[] {
  return peekCache<ProjectSummary[]>('projects') ?? [];
}

export function useProjects() {
  const [projects, setProjects] = useState<ProjectSummary[]>(readCached);
  const [loading, setLoading] = useState(() => !readCached().length);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  useEffect(() => {
    let mounted = true;
    const stale = readCached();
    if (stale.length) setProjects(stale);
    setLoading(!stale.length);
    setError(null);
    listProjects()
      .then((all) => { if (mounted) setProjects(all); })
      .catch((err) => {
        if (mounted) {
          const message = logApiError("listProjects", err);
          setError(message);
          if (!stale.length) setProjects([]);
        }
      })
      .finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [fetchKey]);

  const refresh = useCallback(() => {
    invalidateCache("projects");
    setFetchKey((k) => k + 1);
  }, []);

  return { projects, loading, error, refresh };
}
