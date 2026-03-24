import { useEffect, useState } from "react";
import { listSkills, SkillSummary } from "../api/client";
import { logApiError } from "../utils/errors";

export function useSkills() {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const allSkills = await listSkills();
        if (!mounted) {
          return;
        }
        setSkills(allSkills.filter((s) => s.user_invocable));
      } catch (err) {
        if (mounted) {
          const message = logApiError("listSkills", err);
          setError(message);
          setSkills([]);
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }
    void load();
    return () => {
      mounted = false;
    };
  }, []);
  return {
    skills,
    loading,
    error,
  };
}
