import { useQuery, useQueryClient } from "@tanstack/react-query";
import { listProjects, ProjectSummary } from "../api/client";
import { logApiError } from "../utils/errors";

export function useProjects() {
  const qc = useQueryClient();
  const q = useQuery<ProjectSummary[]>({
    queryKey: ["projects"],
    queryFn: () => listProjects(),
  });
  return {
    projects: q.data ?? [],
    loading: q.isPending,
    error: q.error ? logApiError("listProjects", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  };
}
