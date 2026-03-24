import { useCallback, useEffect, useState } from "react";
import { listProjects, invalidateCache, ProjectSummary } from "../api/client";
import { logApiError } from "../utils/errors";

export function useProjects() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const all = await listProjects();
        if (!mounted) return;
        setProjects(all);
      } catch (err) {
        if (mounted) {
          const message = logApiError("listProjects", err);
          setError(message);
          setProjects([]);
        }
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void load();
    return () => { mounted = false; };
  }, [fetchKey]);

  const refresh = useCallback(() => {
    invalidateCache("projects");
    setFetchKey((k) => k + 1);
  }, []);

  return { projects, loading, error, refresh };
}
