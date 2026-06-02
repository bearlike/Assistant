/**
 * Wiki-internal route helpers. The wiki section lives under `/wiki/*`; sub-
 * routes are encoded into the same URL space so they're shareable and the
 * browser back button works.
 *
 *   /wiki                          → LandingScreen (project gallery)
 *   /wiki/configure?url=...        → ConfigureWizard
 *   /wiki/repo?slug=owner/name     → WelcomeScreen ("not indexed")
 *   /wiki/indexing?jobId=...       → IndexingScreen
 *   /wiki/p/:pageId                → WikiScreen
 *   /wiki/qa?q=...&page=...&model=...
 *                                  → QAScreen
 *   /wiki/graph?slug=...           → KnowledgeGraphScreen
 */

import { useLocation } from "wouter";

import type { Platform } from "./api/types";

export type PlatformId = Platform["id"];

export type WikiRoute =
  | { kind: "landing" }
  | { kind: "configure"; url?: string }
  | { kind: "welcome"; slug?: string; platform?: PlatformId }
  | { kind: "indexing"; jobId?: string; slug?: string; platform?: PlatformId }
  | { kind: "page"; pageId: string; slug?: string; platform?: PlatformId }
  | {
      kind: "qa";
      question: string;
      pageId: string;
      slug?: string;
      model?: string;
      platform?: PlatformId;
    }
  | { kind: "graph"; slug?: string; platform?: PlatformId };

const PLATFORM_IDS: readonly PlatformId[] = [
  "github",
  "gitlab",
  "bitbucket",
  "gitea",
  "azure",
  "git",
];

function parsePlatform(value: string | null): PlatformId | undefined {
  if (!value) return undefined;
  return (PLATFORM_IDS as readonly string[]).includes(value)
    ? (value as PlatformId)
    : undefined;
}

export function parseWikiRoute(path: string, queryString: string): WikiRoute {
  const params = new URLSearchParams(queryString);
  const tail = path.replace(/^\/wiki/, "") || "/";

  if (tail === "/" || tail === "") return { kind: "landing" };

  if (tail.startsWith("/configure")) {
    return { kind: "configure", url: params.get("url") || undefined };
  }
  if (tail.startsWith("/repo")) {
    return {
      kind: "welcome",
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  if (tail.startsWith("/indexing")) {
    return {
      kind: "indexing",
      jobId: params.get("jobId") || undefined,
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  if (tail.startsWith("/graph")) {
    return {
      kind: "graph",
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  if (tail.startsWith("/qa")) {
    return {
      kind: "qa",
      question: params.get("q") || "",
      pageId: params.get("page") || "core",
      slug: params.get("slug") || undefined,
      model: params.get("model") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  const pageMatch = tail.match(/^\/p\/(.+)$/);
  if (pageMatch) {
    return {
      kind: "page",
      pageId: decodeURIComponent(pageMatch[1]),
      slug: params.get("slug") || undefined,
      platform: parsePlatform(params.get("platform")),
    };
  }
  return { kind: "landing" };
}

function appendPlatform(params: URLSearchParams, platform?: PlatformId): void {
  if (platform) params.set("platform", platform);
}

export function buildHref(route: WikiRoute): string {
  switch (route.kind) {
    case "landing":
      return "/wiki";
    case "configure":
      return route.url
        ? `/wiki/configure?url=${encodeURIComponent(route.url)}`
        : "/wiki/configure";
    case "welcome": {
      const params = new URLSearchParams();
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/repo${qs ? `?${qs}` : ""}`;
    }
    case "indexing": {
      const params = new URLSearchParams();
      if (route.jobId) params.set("jobId", route.jobId);
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/indexing${qs ? `?${qs}` : ""}`;
    }
    case "page": {
      const params = new URLSearchParams();
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/p/${encodeURIComponent(route.pageId)}${qs ? `?${qs}` : ""}`;
    }
    case "qa": {
      const params = new URLSearchParams();
      params.set("q", route.question);
      params.set("page", route.pageId);
      if (route.slug) params.set("slug", route.slug);
      if (route.model) params.set("model", route.model);
      appendPlatform(params, route.platform);
      return `/wiki/qa?${params.toString()}`;
    }
    case "graph": {
      const params = new URLSearchParams();
      if (route.slug) params.set("slug", route.slug);
      appendPlatform(params, route.platform);
      const qs = params.toString();
      return `/wiki/graph${qs ? `?${qs}` : ""}`;
    }
  }
}

/**
 * Read the current wiki route. wouter normalises pathname only, so this
 * pulls the search string from `window.location` (the router proxies the
 * History API so the value is fresh).
 */
export function useWikiRoute(): WikiRoute {
  const [pathname] = useLocation();
  const search = typeof window !== "undefined" ? window.location.search : "";
  return parseWikiRoute(pathname, search);
}
