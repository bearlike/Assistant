import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  listProjects,
  createVirtualProject,
  updateVirtualProject,
  deleteVirtualProject,
  type ProjectSummary,
} from "../api/client";
import { VirtualProject } from "../types";
import { getErrorMessage, logApiError } from "../utils/errors";

/**
 * Managed ("virtual") projects — the server-owned workspaces under
 * `/api/projects` + `/api/v_projects`.
 *
 * This hook reads the SAME `['projects']` query as `useProjects()` and narrows
 * it with `select` (re-calling a hook on a shared queryKey is not a second
 * fetch). It used to be a hand-rolled `useState` + `useEffect` cache that ALSO
 * invalidated `['projects']` for the rest of the app — a second cache sitting
 * next to the real one, which the console's CLAUDE.md forbids outright. Writes
 * are `useMutation`s that invalidate the one key, so the composer's project
 * picker, the session list's `ProjectLabel` and this pane can never disagree.
 */
const PROJECTS_KEY = ["projects"] as const;

/** Managed entries carry a `project_id`; config-defined ones never do. */
function isManaged(p: ProjectSummary): p is ProjectSummary & { project_id: string } {
  return p.source === "managed" && !!p.project_id;
}

/**
 * `/api/projects` returns the union of config + managed entries. Carry the
 * worktree flags through — earlier versions stripped them, which broke
 * worktree detection downstream (ProjectCard, the composer's picker).
 *
 * `GET /api/projects` (`backend.py::Projects.get`) builds each managed entry
 * from a fixed, narrower dict — `name`/`project_id`/`path`/`description`/
 * `available`/`source`/`is_worktree`/`parent_project_id`/`branch` — and never
 * includes `path_source`/`folder_created`/`created_at`/`updated_at`; those
 * only exist on the FULL record `POST`/`PATCH /api/v_projects/<id>` return
 * (`backend.py::_vproject_to_dict`). This used to fabricate them (`"auto"` /
 * `true` / `""` / `""`) just to satisfy `VirtualProject`'s shape — a lie
 * waiting for the first consumer that renders "Created {created_at}" and
 * prints "Invalid Date". Omit them instead; `VirtualProject` declares them
 * optional for exactly this reason.
 */
function toVirtualProject(p: ProjectSummary & { project_id: string }): VirtualProject {
  return {
    project_id: p.project_id,
    name: p.name,
    description: p.description ?? "",
    path: p.path,
    is_worktree: p.is_worktree ?? false,
    parent_project_id: p.parent_project_id ?? null,
    branch: p.branch ?? null,
  };
}

export function useVirtualProjects() {
  const qc = useQueryClient();

  const query = useQuery<ProjectSummary[], Error, VirtualProject[]>({
    queryKey: PROJECTS_KEY,
    queryFn: () => listProjects(),
    select: (all) => all.filter(isManaged).map(toVirtualProject),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: PROJECTS_KEY });

  // Mutations previously had no `onError` at all, and the hook's `error`
  // field reads only the QUERY (`query.error` below) — so a rejecting
  // create/update/delete produced an unhandled promise rejection and no
  // error anywhere a user could see (a stuck confirm dialog, a card wedged
  // in edit mode). `usePlugins.ts` established the fix for this same
  // refactor: surface mutation failures as `sonner` toasts fired from the
  // HOOK, so every caller gets it for free without hand-rolling its own
  // try/catch.
  const createM = useMutation({
    mutationFn: (input: { name: string; description: string; path?: string }) =>
      createVirtualProject(input.name, input.description, input.path),
    onSuccess: (_data, vars) => {
      toast.success(`Created project "${vars.name}".`);
      invalidate();
    },
    onError: (err, vars) => {
      toast.error(`Failed to create "${vars.name}" — ${getErrorMessage(err)}`);
    },
  });

  const updateM = useMutation({
    mutationFn: (input: { id: string; data: { name?: string; description?: string } }) =>
      updateVirtualProject(input.id, input.data),
    onSuccess: () => {
      toast.success("Project updated.");
      invalidate();
    },
    onError: (err) => {
      toast.error(`Failed to update project — ${getErrorMessage(err)}`);
    },
  });

  const removeM = useMutation({
    mutationFn: (id: string) => deleteVirtualProject(id),
    onSuccess: () => {
      toast.success("Project deleted.");
      invalidate();
    },
    onError: (err) => {
      toast.error(`Failed to delete project — ${getErrorMessage(err)}`);
    },
  });

  return {
    projects: query.data ?? [],
    loading: query.isPending,
    error: query.error ? logApiError("listVirtualProjects", query.error) : null,
    create: (name: string, description: string, path?: string) =>
      createM.mutateAsync({ name, description, path }),
    update: (id: string, data: { name?: string; description?: string }) =>
      updateM.mutateAsync({ id, data }),
    remove: (id: string) => removeM.mutateAsync(id).then(() => undefined),
    /** `true` while a delete is in flight — the confirm dialog reads it. */
    removing: removeM.isPending,
  };
}
