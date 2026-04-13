import { useEffect, useState } from "react";
import { getConfig } from "../api/client";

/**
 * Returns whether the Web IDE feature is enabled on the server. ``null`` while
 * the config is loading — callers should treat that as "unknown" and hide the
 * UI to avoid flicker. ``getConfig`` is promise-cached in ``api/client.ts`` so
 * all callers share a single in-flight request.
 */
export function useWebIdeEnabled(): boolean | null {
  const [enabled, setEnabled] = useState<boolean | null>(null);

  useEffect(() => {
    let active = true;
    getConfig()
      .then((cfg) => {
        if (!active) return;
        const agent = cfg?.agent as Record<string, unknown> | undefined;
        const webIde = agent?.web_ide as Record<string, unknown> | undefined;
        setEnabled(Boolean(webIde?.enabled));
      })
      .catch(() => {
        if (active) setEnabled(false);
      });
    return () => {
      active = false;
    };
  }, []);

  return enabled;
}
