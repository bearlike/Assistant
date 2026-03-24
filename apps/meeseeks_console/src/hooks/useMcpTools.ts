import { useEffect, useState } from "react";
import { listTools, ToolSummary } from "../api/client";
import { logApiError } from "../utils/errors";
export type McpTool = ToolSummary & {
  kind: "mcp" | string;
};
export function useMcpTools() {
  const [tools, setTools] = useState<McpTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let mounted = true;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const allTools = await listTools();
        if (!mounted) {
          return;
        }
        const mcp = allTools.filter((tool) => tool.kind === "mcp");
        setTools(mcp);
      } catch (err) {
        if (mounted) {
          const message = logApiError("listTools", err);
          setError(message);
          setTools([]);
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
    tools,
    loading,
    error
  };
}