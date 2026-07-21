import { describe, it, expect, vi, beforeAll } from "vitest";

// `host.ts` runs `main()` unconditionally at module scope, which imports the
// real `@stlite/browser` (a heavy WASM/Pyodide chain whose transitive
// `parquet-wasm` init does a top-level `fetch()` that jsdom can't service and
// throws an unhandled rejection). Nothing under test ever calls `mount`, so a
// stub is enough — this keeps the suite hermetic and avoids that noise.
vi.mock("@stlite/browser", () => ({ mount: vi.fn(() => ({ unmount: vi.fn() })) }));

let parseWidgetMessage: typeof import("../host").parseWidgetMessage;
let parseAppMessage: typeof import("../host").parseAppMessage;
let parseHostMessage: typeof import("../host").parseHostMessage;
let isTrustedOrigin: typeof import("../host").isTrustedOrigin;

beforeAll(async () => {
  // main() requires `#root` to exist, or the import throws synchronously.
  document.body.innerHTML = '<div id="root"></div>';
  ({ parseWidgetMessage, parseAppMessage, parseHostMessage, isTrustedOrigin } = await import(
    "../host"
  ));
});

const validPayload = {
  widget_id: "w1",
  session_id: "s1",
  files: { "app.py": "print('hi')", "data.json": "{}" },
  requirements: ["pandas"],
};

const validMessage = { type: "mewbo-widget-payload", payload: validPayload };

const validAppPayload = {
  entrypoint: "app.py",
  files: { "app.py": "import mewbo_app", "pages/one.py": "x = 1" },
  requirements: ["pandas"],
  app_context: { token: "tok", api_base: "https://mewbo.local", app_id: "a1" },
};

const validAppMessage = { type: "mewbo-app-payload", payload: validAppPayload };

describe("parseWidgetMessage", () => {
  it("accepts the valid shape, with theme undefined when omitted", () => {
    expect(parseWidgetMessage(validMessage)).toEqual({
      type: "mewbo-widget-payload",
      payload: validPayload,
      theme: undefined,
    });
  });

  it("accepts a valid light/dark theme alongside the payload", () => {
    expect(parseWidgetMessage({ ...validMessage, theme: "light" })?.theme).toBe("light");
    expect(parseWidgetMessage({ ...validMessage, theme: "dark" })?.theme).toBe("dark");
  });

  it("ignores an unrecognized theme value rather than passing it through", () => {
    expect(parseWidgetMessage({ ...validMessage, theme: "solarized" })?.theme).toBeUndefined();
  });

  it("rejects non-object and null payloads", () => {
    expect(parseWidgetMessage(null)).toBeNull();
    expect(parseWidgetMessage(undefined)).toBeNull();
    expect(parseWidgetMessage("mewbo-widget-payload")).toBeNull();
    expect(parseWidgetMessage(42)).toBeNull();
  });

  it("rejects the wrong message type", () => {
    expect(parseWidgetMessage({ type: "mewbo-widget-host-ready" })).toBeNull();
    expect(parseWidgetMessage({ type: "something-else", payload: validPayload })).toBeNull();
  });

  it("rejects a payload missing app.py", () => {
    const bad = {
      type: "mewbo-widget-payload",
      payload: { ...validPayload, files: { "data.json": "{}" } },
    };
    expect(parseWidgetMessage(bad)).toBeNull();
  });

  it("rejects a payload missing data.json", () => {
    const bad = {
      type: "mewbo-widget-payload",
      payload: { ...validPayload, files: { "app.py": "print(1)" } },
    };
    expect(parseWidgetMessage(bad)).toBeNull();
  });

  it("rejects non-string requirements entries", () => {
    const bad = {
      type: "mewbo-widget-payload",
      payload: { ...validPayload, requirements: ["pandas", 42] },
    };
    expect(parseWidgetMessage(bad)).toBeNull();
  });

  it("rejects requirements that aren't an array at all", () => {
    const bad = {
      type: "mewbo-widget-payload",
      payload: { ...validPayload, requirements: "pandas" },
    };
    expect(parseWidgetMessage(bad)).toBeNull();
  });

  it("rejects extra junk that doesn't match the message contract at all", () => {
    expect(parseWidgetMessage({ foo: "bar", nested: { a: 1, b: [1, 2, 3] } })).toBeNull();
    expect(parseWidgetMessage([1, 2, 3])).toBeNull();
  });

  it("does NOT accept a multi-file app payload (that's parseAppMessage's job)", () => {
    expect(parseWidgetMessage(validAppMessage)).toBeNull();
  });
});

describe("parseAppMessage", () => {
  it("accepts a valid multi-file app payload, theme undefined when omitted", () => {
    expect(parseAppMessage(validAppMessage)).toEqual({
      type: "mewbo-app-payload",
      payload: validAppPayload,
      theme: undefined,
    });
  });

  it("carries a valid theme and drops an unrecognized one", () => {
    expect(parseAppMessage({ ...validAppMessage, theme: "dark" })?.theme).toBe("dark");
    expect(parseAppMessage({ ...validAppMessage, theme: "neon" })?.theme).toBeUndefined();
  });

  it("rejects the wrong message type (incl. a widget payload)", () => {
    expect(parseAppMessage(validMessage)).toBeNull();
    expect(parseAppMessage({ type: "mewbo-app-payload" })).toBeNull();
  });

  it("rejects an entrypoint that isn't present in the files map", () => {
    const bad = {
      type: "mewbo-app-payload",
      payload: { ...validAppPayload, entrypoint: "missing.py" },
    };
    expect(parseAppMessage(bad)).toBeNull();
  });

  it("rejects a non-string file in the files map", () => {
    const bad = {
      type: "mewbo-app-payload",
      payload: { ...validAppPayload, files: { "app.py": "ok", "bad.py": 7 } },
    };
    expect(parseAppMessage(bad)).toBeNull();
  });

  it("rejects an app_context missing the token / api_base / app_id", () => {
    for (const drop of ["token", "api_base", "app_id"]) {
      const app_context = { ...validAppPayload.app_context } as Record<string, unknown>;
      delete app_context[drop];
      expect(parseAppMessage({ type: "mewbo-app-payload", payload: { ...validAppPayload, app_context } })).toBeNull();
    }
  });

  it("rejects a missing app_context entirely", () => {
    const { app_context: _drop, ...noCtx } = validAppPayload;
    void _drop;
    expect(parseAppMessage({ type: "mewbo-app-payload", payload: noCtx })).toBeNull();
  });
});

describe("parseHostMessage", () => {
  it("routes both widget and app payloads to their parsers", () => {
    expect(parseHostMessage(validMessage)?.type).toBe("mewbo-widget-payload");
    expect(parseHostMessage(validAppMessage)?.type).toBe("mewbo-app-payload");
  });

  it("rejects anything neither parser accepts", () => {
    expect(parseHostMessage({ type: "mewbo-widget-host-ready" })).toBeNull();
    expect(parseHostMessage(null)).toBeNull();
  });
});

describe("isTrustedOrigin", () => {
  it("accepts the page's own origin — the console frames this page", () => {
    expect(isTrustedOrigin(window.location.origin)).toBe(true);
  });

  it("accepts an empty or null origin, which is how Aura's WebView bridge posts", () => {
    // Aura injects payloads through WebViewCompat's message channel rather
    // than a page-to-page postMessage; those carry no meaningful origin, so
    // rejecting them would break the Android surface outright.
    expect(isTrustedOrigin("")).toBe(true);
    expect(isTrustedOrigin("null")).toBe(true);
  });

  it("rejects a foreign origin framing this page", () => {
    expect(isTrustedOrigin("https://evil.test")).toBe(false);
    expect(isTrustedOrigin("http://localhost:1")).toBe(false);
  });
});
