import { useCallback, useEffect, useState } from "react";
import { listTools, invalidateCache, peekCache, ToolSummary } from "../api/client";
import { logApiError } from "../utils/errors";

export type McpTool = ToolSummary & {
  kind: "mcp" | string;
};

function readCached(project?: string | null): McpTool[] {
  const cached = peekCache<ToolSummary[]>(`tools:${project ?? ''}`);
  return cached ? cached.filter((t): t is McpTool => t.kind === "mcp") : [];
}

export function useMcpTools(project?: string | null) {
  const [tools, setTools] = useState<McpTool[]>(() => readCached(project));
  const [loading, setLoading] = useState(() => !readCached(project).length);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  useEffect(() => {
    let mounted = true;
    // Show stale cache immediately while we refresh.
    const stale = readCached(project);
    if (stale.length) setTools(stale);
    setLoading(!stale.length);
    setError(null);
    listTools(project ?? undefined)
      .then((allTools) => {
        if (mounted) setTools(allTools.filter((t): t is McpTool => t.kind === "mcp"));
      })
      .catch((err) => {
        if (mounted) {
          const message = logApiError("listTools", err);
          setError(message);
          if (!stale.length) setTools([]);
        }
      })
      .finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [project, fetchKey]);

  const refresh = useCallback(() => {
    invalidateCache("tools:");
    setFetchKey((k) => k + 1);
  }, []);

  return { tools, loading, error, refresh };
}
