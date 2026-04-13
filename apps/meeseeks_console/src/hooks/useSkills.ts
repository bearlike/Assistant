import { useQuery, useQueryClient } from "@tanstack/react-query";
import { listSkills, SkillSummary } from "../api/client";
import { logApiError } from "../utils/errors";

export function useSkills(project?: string | null) {
  const qc = useQueryClient();
  const q = useQuery<SkillSummary[], Error, SkillSummary[]>({
    queryKey: ["skills", project ?? ""] as const,
    queryFn: () => listSkills(project ?? undefined),
    select: (all) => all.filter((s) => s.user_invocable),
  });
  return {
    skills: q.data ?? [],
    loading: q.isPending,
    error: q.error ? logApiError("listSkills", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  };
}
