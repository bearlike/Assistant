import { describe, it, expect } from "vitest";

import { buildHref, parseAppsRoute } from "./router";

describe("parseAppsRoute", () => {
  it("resolves the gallery and a plain detail route", () => {
    expect(parseAppsRoute("/apps", "")).toEqual({ kind: "landing" });
    expect(parseAppsRoute("/apps/", "")).toEqual({ kind: "landing" });
    expect(parseAppsRoute("/apps/app-1", "")).toEqual({
      kind: "detail",
      appId: "app-1",
      sessionId: undefined,
    });
  });

  it("carries the builder session id from the query string", () => {
    expect(parseAppsRoute("/apps/app-1", "?session=sess-9")).toEqual({
      kind: "detail",
      appId: "app-1",
      sessionId: "sess-9",
    });
  });

  it("resolves a DEEP path under an app id to that app, not the gallery", () => {
    // Streamlit's multipage nav pushes `<basePath>/<PageName>`, so a user can
    // hard-reload or share `/apps/<id>/<Page>`. The app renders in an iframe
    // now, so that push no longer reaches this router at runtime — but a
    // pasted or bookmarked deep link is a separate path, and returning the
    // gallery there silently drops the app the link pointed at.
    expect(parseAppsRoute("/apps/app-1/Actions", "")).toEqual({
      kind: "detail",
      appId: "app-1",
      sessionId: undefined,
    });
    expect(parseAppsRoute("/apps/app-1/deep/er/still", "")).toMatchObject({
      kind: "detail",
      appId: "app-1",
    });
  });

  it("keeps the session param across a deep path", () => {
    expect(parseAppsRoute("/apps/app-1/Actions", "?session=sess-9")).toEqual({
      kind: "detail",
      appId: "app-1",
      sessionId: "sess-9",
    });
  });

  it("decodes a percent-encoded app id and ignores the tail", () => {
    expect(parseAppsRoute("/apps/my%20app/Page", "")).toMatchObject({
      kind: "detail",
      appId: "my app",
    });
  });
});

describe("buildHref", () => {
  it("round-trips a detail route through the parser", () => {
    const route = { kind: "detail", appId: "app-1", sessionId: "sess-9" } as const;
    const href = buildHref(route);
    const [path, query] = href.split("?");
    expect(parseAppsRoute(path, query ? `?${query}` : "")).toEqual(route);
  });

  it("emits the bare gallery path", () => {
    expect(buildHref({ kind: "landing" })).toBe("/apps");
  });
});
