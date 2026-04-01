import { useCallback, useEffect, useState } from "react";
import { listTools, invalidateCache, ToolSummary } from "../api/client";
import { logApiError } from "../utils/errors";

export type McpTool = ToolSummary & {
  kind: "mcp" | string;
};

export function useMcpTools(project?: string | null) {
  const [tools, setTools] = useState<McpTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const allTools = await listTools(project ?? undefined);
        if (!mounted) return;
        setTools(allTools.filter((tool) => tool.kind === "mcp"));
      } catch (err) {
        if (mounted) {
          const message = logApiError("listTools", err);
          setError(message);
          setTools([]);
        }
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void load();
    return () => { mounted = false; };
  }, [project, fetchKey]);

  const refresh = useCallback(() => {
    invalidateCache("tools:");
    setFetchKey((k) => k + 1);
  }, []);

  return { tools, loading, error, refresh };
}
