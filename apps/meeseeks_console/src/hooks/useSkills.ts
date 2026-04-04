import { useCallback, useEffect, useState } from "react";
import { listSkills, invalidateCache, peekCache, SkillSummary } from "../api/client";
import { logApiError } from "../utils/errors";

function readCached(project?: string | null): SkillSummary[] {
  const cached = peekCache<SkillSummary[]>(`skills:${project ?? ''}`);
  return cached ? cached.filter((s) => s.user_invocable) : [];
}

export function useSkills(project?: string | null) {
  const [skills, setSkills] = useState<SkillSummary[]>(() => readCached(project));
  const [loading, setLoading] = useState(() => !readCached(project).length);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  useEffect(() => {
    let mounted = true;
    const stale = readCached(project);
    if (stale.length) setSkills(stale);
    setLoading(!stale.length);
    setError(null);
    listSkills(project ?? undefined)
      .then((allSkills) => {
        if (mounted) setSkills(allSkills.filter((s) => s.user_invocable));
      })
      .catch((err) => {
        if (mounted) {
          const message = logApiError("listSkills", err);
          setError(message);
          if (!stale.length) setSkills([]);
        }
      })
      .finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [project, fetchKey]);

  const refresh = useCallback(() => {
    invalidateCache("skills:");
    setFetchKey((k) => k + 1);
  }, []);

  return { skills, loading, error, refresh };
}
