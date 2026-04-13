import { useCallback, useEffect, useState } from "react";
import {
  listProjects,
  createVirtualProject,
  updateVirtualProject,
  deleteVirtualProject,
  invalidateCache,
} from "../api/client";
import { VirtualProject } from "../types";
import { logApiError } from "../utils/errors";

export function useVirtualProjects() {
  const [projects, setProjects] = useState<VirtualProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    setError(null);
    listProjects()
      .then((all) => {
        if (mounted) {
          // Filter to managed projects and map to VirtualProject shape
          setProjects(
            all
              .filter((p): p is typeof p & { project_id: string } => p.source === "managed" && !!p.project_id)
              .map((p) => ({
                project_id: p.project_id,
                name: p.name,
                description: p.description ?? "",
                path: p.path,
                path_source: "auto",
                folder_created: true,
                created_at: "",
                updated_at: "",
              }))
          );
        }
      })
      .catch((err) => {
        if (mounted) {
          const message = logApiError("listVirtualProjects", err);
          setError(message);
          setProjects([]);
        }
      })
      .finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [fetchKey]);

  const refresh = useCallback(() => {
    invalidateCache("projects");
    setFetchKey((k) => k + 1);
  }, []);

  const create = useCallback(async (name: string, description: string, path?: string) => {
    const proj = await createVirtualProject(name, description, path);
    invalidateCache("projects");
    setFetchKey((k) => k + 1);
    return proj;
  }, []);

  const update = useCallback(async (id: string, data: { name?: string; description?: string }) => {
    const proj = await updateVirtualProject(id, data);
    invalidateCache("projects");
    setProjects((prev) => prev.map((p) => (p.project_id === id ? { ...p, name: proj.name, description: proj.description } : p)));
    return proj;
  }, []);

  const remove = useCallback(async (id: string) => {
    await deleteVirtualProject(id);
    invalidateCache("projects");
    setProjects((prev) => prev.filter((p) => p.project_id !== id));
  }, []);

  return { projects, loading, error, refresh, create, update, remove };
}
