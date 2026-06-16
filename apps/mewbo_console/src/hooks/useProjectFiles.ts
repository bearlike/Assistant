import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchProjectFiles, ProjectFiles } from "../api/client";
import { logApiError } from "../utils/errors";

/**
 * Fetch the file/attachment names a chat message can reference via `@`.
 *
 * Mirrors `useSkills` — one scoped query, a thin `refresh()` shim, and graceful
 * error logging. We fetch the FULL referenceable list once per session/project
 * (no `q`) and let the picker filter client-side as the user types: it's
 * snappier than a round-trip per keystroke and the lists are bounded. The query
 * stays disabled until there is something to scope it to AND the caller opts in
 * (`enabled`) so the home composer doesn't fetch before a project is chosen.
 */
export function useProjectFiles({
  session,
  project,
  enabled = true,
}: {
  session?: string | null;
  project?: string | null;
  enabled?: boolean;
}): {
  files: string[];
  attachments: string[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
} {
  const qc = useQueryClient();
  const q = useQuery<ProjectFiles, Error>({
    queryKey: ["project-files", session ?? "", project ?? ""] as const,
    queryFn: () => fetchProjectFiles({ session, project }),
    enabled: enabled && Boolean(session || project),
    staleTime: 60_000,
  });
  return {
    files: q.data?.files ?? [],
    attachments: q.data?.attachments ?? [],
    loading: q.isPending,
    error: q.error ? logApiError("fetchProjectFiles", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: ["project-files"] }),
  };
}
