import { useCallback, useEffect, useState } from "react";
import { listSkills, invalidateCache, SkillSummary } from "../api/client";
import { logApiError } from "../utils/errors";

export function useSkills(project?: string | null) {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const allSkills = await listSkills(project ?? undefined);
        if (!mounted) return;
        setSkills(allSkills.filter((s) => s.user_invocable));
      } catch (err) {
        if (mounted) {
          const message = logApiError("listSkills", err);
          setError(message);
          setSkills([]);
        }
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void load();
    return () => { mounted = false; };
  }, [project, fetchKey]);

  const refresh = useCallback(() => {
    invalidateCache("skills:");
    setFetchKey((k) => k + 1);
  }, []);

  return { skills, loading, error, refresh };
}
