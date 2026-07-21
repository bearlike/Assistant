/**
 * Apps-internal route helpers. The Apps section lives under `/apps/*`; sub-
 * routes are encoded into the same URL space so they're shareable and the
 * browser back button works. Mirrors the wiki's `router.ts` seam — extend the
 * `AppsRoute` union AND the parser AND `buildHref` in lock-step; `tsc` flags
 * drift.
 *
 *   /apps                          → AppsLanding (gallery + creation)
 *   /apps/<app_id>                 → AppDetail (rendered app / build progress)
 *   /apps/<app_id>?session=<sid>   → AppDetail, tailing a builder session's SSE
 *                                    for live build progress until `app_ready`
 */

import { useLocation } from "wouter";

export type AppsRoute =
  | { kind: "landing" }
  | {
      kind: "detail";
      appId: string;
      /**
       * Builder session whose EXISTING event stream drives live build progress.
       * Present only on the create → open hop; the detail's polled manifest
       * (`owner_session_id`) is the durable fallback for a plain deep-link.
       */
      sessionId?: string;
    };

export function parseAppsRoute(path: string, queryString: string): AppsRoute {
  const params = new URLSearchParams(queryString);
  const tail = path.replace(/^\/apps/, "") || "/";

  // The trailing `(?:\/.*)?` is deliberate: a deep path under an app id still
  // resolves to that app's detail route rather than falling through to the
  // gallery. Streamlit's multipage nav pushes `<basePath>/<PageName>` on every
  // page switch, so a user who hard-reloads or shares such a URL arrives at
  // `/apps/<id>/<Page>`. Rendering the gallery there would silently lose the
  // app they linked to. (The app itself renders inside an iframe now, so that
  // push no longer reaches this router at runtime — this is the hard-load and
  // deep-link path, which is separate and still needs to resolve.)
  const detailMatch = tail.match(/^\/([^/]+)(?:\/.*)?$/);
  if (detailMatch) {
    return {
      kind: "detail",
      appId: decodeURIComponent(detailMatch[1]),
      sessionId: params.get("session") || undefined,
    };
  }
  return { kind: "landing" };
}

export function buildHref(route: AppsRoute): string {
  switch (route.kind) {
    case "landing":
      return "/apps";
    case "detail": {
      const base = `/apps/${encodeURIComponent(route.appId)}`;
      return route.sessionId
        ? `${base}?session=${encodeURIComponent(route.sessionId)}`
        : base;
    }
  }
}

/**
 * Read the current apps route. wouter normalises pathname only, so this pulls
 * the search string from `window.location` (the router proxies the History API
 * so the value is fresh) — same approach as the wiki's `useWikiRoute`.
 */
export function useAppsRoute(): AppsRoute {
  const [pathname] = useLocation();
  const search = typeof window !== "undefined" ? window.location.search : "";
  return parseAppsRoute(pathname, search);
}
