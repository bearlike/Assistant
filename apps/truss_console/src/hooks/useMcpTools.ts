import { useQuery, useQueryClient } from "@tanstack/react-query";
import { listTools, ToolSummary } from "../api/client";
import { logApiError } from "../utils/errors";

export type McpTool = ToolSummary & {
  kind: "mcp" | string;
};

// Stable empty array ref so consumers' useEffect deps don't churn on every
// render before the query resolves.
const EMPTY: McpTool[] = [];

export function useMcpTools(project?: string | null) {
  const qc = useQueryClient();
  const q = useQuery<ToolSummary[], Error, McpTool[]>({
    queryKey: ["mcp-tools", project ?? ""] as const,
    queryFn: () => listTools(project ?? undefined),
    select: (all) => all.filter((t): t is McpTool => t.kind === "mcp"),
  });
  return {
    tools: q.data ?? EMPTY,
    loading: q.isPending,
    error: q.error ? logApiError("listTools", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: ["mcp-tools"] }),
  };
}
