import { useQuery } from "@tanstack/react-query";
import { getConfig } from "../api/client";

/**
 * Returns whether the Web IDE feature is enabled on the server. ``null`` while
 * the config is loading — callers should treat that as "unknown" and hide the
 * UI to avoid flicker.
 */
export function useWebIdeEnabled(): boolean | null {
  const q = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
    select: (cfg) => {
      const agent = (cfg as Record<string, unknown> | undefined)?.agent as
        | Record<string, unknown>
        | undefined;
      const webIde = agent?.web_ide as Record<string, unknown> | undefined;
      return Boolean(webIde?.enabled);
    },
  });
  if (q.isPending) return null;
  if (q.error) return false;
  return q.data ?? false;
}
