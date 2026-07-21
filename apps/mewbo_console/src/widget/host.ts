// Standalone stlite widget host — the entry behind `widget-host.html`.
//
// Unlike `StliteWidgetPanel.tsx` (which mounts inside the React console), this
// page has NO React and NO app chrome. It exists for embedders that only have a
// browser context and a `postMessage` channel. Two consumers, one render path:
//   - the Aura Android client, which loads this page inside a `WebView` served
//     from the synthetic origin `https://appassets.androidplatform.net/` and
//     hands it a widget payload over the JS bridge;
//   - the web console's `AppFrame`, which frames this page to render a Mewbo
//     App. The console used to mount stlite in-document and paid for it: an
//     embedded Streamlit multipage app pushes its own URLs onto
//     `window.history`, which the console's router observed and reacted to by
//     navigating away from the app. A frame gives the embedded app its own
//     history, title and style scope, so that whole class of host
//     contamination stops at the frame boundary.
//
// Contract (see apps/mewbo_console/CLAUDE.md → "widget-host entry"):
//   - On boot the page posts `{type:"mewbo-widget-host-ready"}` to its host so
//     the embedder knows when to send a payload.
//   - The embedder posts a `message` whose `data` is ONE of:
//       * `{type:"mewbo-widget-payload", payload: WidgetReadyPayload, theme?}`
//         — the original fixed `{app.py, data.json}` widget bundle.
//       * `{type:"mewbo-app-payload", payload: AppFrontendPayload, theme?}`
//         — a multi-file Mewbo App (arbitrary path→source map + entrypoint +
//         requirements + `app_context`), added for the Apps sub-product. This
//         is a FIXED cross-stream interface (Aura posts the exact same shape),
//         so the field names must never change.
//   - Any other message shape is ignored silently (defensive — a WebView shares
//     one `message` bus with everything else on the page).
//   - Re-posting a payload tears down the running kernel and remounts.
//
// Relocatable-base: this bundle is built with a RELATIVE Vite base
// (`vite.widget-host.config.ts`), so its own JS/CSS/wheel assets resolve
// against the page URL and work from any mount path. The one asset Vite does
// not fingerprint — the vendored Pyodide runtime — is resolved at runtime from
// `document.baseURI` so it too follows the page wherever it is served.

import { mount } from "@stlite/browser";
// @stlite/browser ships its Streamlit theme/layout/@font-face CSS as a separate
// stylesheet and does NOT auto-inject it (the browser build expects the
// embedder to include it, unlike the styled-components rules Streamlit injects
// at runtime). Without this the widget renders as unstyled HTML. Imported by
// relative filesystem path rather than the package specifier because
// @stlite/browser's `exports` map only exposes `.` and `./wheels/*.whl`, so
// `@stlite/browser/build/stlite.css` is not resolvable under Vite 8's strict
// exports enforcement.
import "../../node_modules/@stlite/browser/build/stlite.css";

import { buildAppKernelOptions, buildKernelOptions, type WidgetTheme } from "./stliteBoot";
import type { WidgetReadyPayload } from "../types"
import type { AppFrontendPayload } from "../types/apps";

// The bundled streamlit + stlite-lib wheels. Globbed (rather than hard-coding
// version-stamped filenames like `streamlit-1.57.0-…`) so an @stlite/browser
// bump doesn't silently break the vendor reference. `?url` yields a
// base-relative asset URL; the relative-base build makes it self-locating.
const wheelModules = import.meta.glob<string>(
  "/node_modules/@stlite/browser/build/wheels/*.whl",
  { query: "?url", import: "default", eager: true },
);

function resolveWheelUrls(): { streamlit: string; stliteLib: string } {
  let streamlit = "";
  let stliteLib = "";
  for (const [path, url] of Object.entries(wheelModules)) {
    // ABSOLUTIZE against the page. These URLs are consumed by micropip inside
    // the Pyodide WEB WORKER, which has no document base to resolve against, so
    // `new Request(url)` throws "Failed to parse URL" on anything relative.
    //
    // The production build hides this: its emitted asset URL happens to resolve
    // where the worker runs. `vite dev` returns the raw `/node_modules/...`
    // path, which the main thread resolves fine and the worker cannot — so the
    // host page loads, Pyodide boots, and the app dies at "Installing
    // packages". Resolving here is correct on BOTH paths and keeps the
    // relative-base build self-locating (`document.baseURI` is the host page's
    // own URL wherever it is mounted).
    const absolute = new URL(url, document.baseURI).href;
    if (path.includes("/streamlit-")) streamlit = absolute;
    else if (path.includes("/stlite_lib-")) stliteLib = absolute;
  }
  if (!streamlit || !stliteLib) {
    throw new Error("widget-host: could not resolve bundled stlite wheels");
  }
  return { streamlit, stliteLib };
}

// Vendored Pyodide runtime, resolved relative to THIS page (not the JS module,
// which lives under assets/). `document.baseURI` is the widget-host.html URL,
// so `./pyodide/pyodide.mjs` follows the page to any origin/base.
const PYODIDE_URL = new URL("./pyodide/pyodide.mjs", document.baseURI).href;

const HOST_READY = { type: "mewbo-widget-host-ready" } as const;
const PAYLOAD_TYPE = "mewbo-widget-payload";
const APP_PAYLOAD_TYPE = "mewbo-app-payload";

/**
 * stlite's `basePath` — pinned rather than left to its `window.location.pathname`
 * default, because on EVERY surface that loads this page, the page's own
 * pathname is not the app's.
 *
 * Concretely, the console frames this page at `/widget-host/widget-host.html`.
 * Left defaulted, a Streamlit multipage switch pushes
 * `/widget-host/widget-host.html/<Page>` — and that path is served by the SPA
 * fallback, so a reload silently returns the console shell instead of the app.
 * That is precisely the failure stlite's own `basePath` docs warn about. A
 * stable pinned prefix keeps the pushed URL meaningless-but-harmless instead.
 */
const APP_BASE_PATH = "mewbo-app";

/**
 * Whether a `message` event may be trusted to carry a payload.
 *
 * `host.ts` historically did NO origin check, with a note that one becomes
 * load-bearing the moment this page is framed in a normal browser context.
 * The console now frames it, so here it is. The empty/`"null"` case is not an
 * oversight: Aura's Android WebView injects payloads through WebViewCompat's
 * message channel rather than a page-to-page `postMessage`, and those arrive
 * with no meaningful origin — rejecting them would break that surface outright.
 * `parseHostMessage`'s shape validation remains the real defense; this narrows
 * who may reach it in the browser-embed case.
 */
export function isTrustedOrigin(origin: string): boolean {
  if (!origin || origin === "null") return true;
  return origin === window.location.origin;
}

/** A validated inbound message — a fixed-shape widget OR a multi-file app,
 *  discriminated by the wire `type` (the same key the embedder posts). */
type WidgetMessage = { type: typeof PAYLOAD_TYPE; payload: WidgetReadyPayload; theme?: WidgetTheme };
type AppMessage = { type: typeof APP_PAYLOAD_TYPE; payload: AppFrontendPayload; theme?: WidgetTheme };
type HostMessage = WidgetMessage | AppMessage;

function parseTheme(value: unknown): WidgetTheme | undefined {
  return value === "light" || value === "dark" ? value : undefined;
}

/** True iff `value` is a `Record<string, string>` (the stlite file map shape). */
function isStringMap(value: unknown): value is Record<string, string> {
  if (typeof value !== "object" || value === null) return false;
  return Object.values(value as Record<string, unknown>).every((v) => typeof v === "string");
}

/** Narrow an untrusted `message` event's data to the widget payload shape. */
export function parseWidgetMessage(data: unknown): WidgetMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const msg = data as Record<string, unknown>;
  if (msg.type !== PAYLOAD_TYPE) return null;

  const payload = msg.payload;
  if (typeof payload !== "object" || payload === null) return null;
  const p = payload as Record<string, unknown>;

  const files = p.files;
  if (typeof files !== "object" || files === null) return null;
  const f = files as Record<string, unknown>;
  if (typeof f["app.py"] !== "string" || typeof f["data.json"] !== "string") return null;

  if (typeof p.widget_id !== "string" || typeof p.session_id !== "string") return null;

  if (!Array.isArray(p.requirements) || !p.requirements.every((r) => typeof r === "string")) {
    return null;
  }

  return { type: PAYLOAD_TYPE, payload: payload as WidgetReadyPayload, theme: parseTheme(msg.theme) };
}

/**
 * Narrow an untrusted `message` event's data to the multi-file app payload
 * shape (`AppFrontendPayload` + `_app_context`). Validates the entrypoint,
 * file map, requirements, and the render context (token + api_base + app_id)
 * — the same defensive stance as `parseWidgetMessage` (a WebView shares one
 * `message` bus, so anything malformed is dropped silently).
 */
export function parseAppMessage(data: unknown): AppMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const msg = data as Record<string, unknown>;
  if (msg.type !== APP_PAYLOAD_TYPE) return null;

  const payload = msg.payload;
  if (typeof payload !== "object" || payload === null) return null;
  const p = payload as Record<string, unknown>;

  if (typeof p.entrypoint !== "string" || !p.entrypoint) return null;
  if (!isStringMap(p.files) || typeof p.files[p.entrypoint] !== "string") return null;
  if (!Array.isArray(p.requirements) || !p.requirements.every((r) => typeof r === "string")) {
    return null;
  }

  const ctx = p.app_context;
  if (typeof ctx !== "object" || ctx === null) return null;
  const c = ctx as Record<string, unknown>;
  if (
    typeof c.token !== "string" ||
    typeof c.api_base !== "string" ||
    typeof c.app_id !== "string"
  ) {
    return null;
  }

  return { type: APP_PAYLOAD_TYPE, payload: payload as AppFrontendPayload, theme: parseTheme(msg.theme) };
}

/** Try both payload shapes, widest-compat first. */
export function parseHostMessage(data: unknown): HostMessage | null {
  return parseWidgetMessage(data) ?? parseAppMessage(data);
}

let current: { unmount: () => void } | null = null;

function render(message: HostMessage, root: HTMLElement): void {
  // Re-posting a payload tears down the previous kernel (a fresh Pyodide worker
  // per mount) before remounting — kernel options are init-only, so there is no
  // in-place update path.
  if (current) {
    current.unmount();
    current = null;
    root.replaceChildren();
  }
  const theme: WidgetTheme = message.theme ?? "dark";
  const bootOpts = {
    theme,
    wheelUrls: resolveWheelUrls(),
    pyodideUrl: PYODIDE_URL,
    basePath: APP_BASE_PATH,
  };
  const options =
    message.type === APP_PAYLOAD_TYPE
      ? buildAppKernelOptions(message.payload, message.payload.app_context, bootOpts)
      : buildKernelOptions(message.payload, bootOpts);
  current = mount(options, root);
}

function main(): void {
  const root = document.getElementById("root");
  if (!root) throw new Error("widget-host: #root not found");

  // Two layers, in order: `isTrustedOrigin` narrows WHO may post (the console
  // frames this page now, which is the condition an earlier revision of this
  // comment named as the point an origin allowlist becomes load-bearing), and
  // `parseHostMessage` shape-validates WHAT they posted. The shape check stays
  // the primary defense — a WebView shares one `message` bus with everything
  // else on the page, so anything unrecognized is dropped silently.
  window.addEventListener("message", (event: MessageEvent) => {
    if (!isTrustedOrigin(event.origin)) return;
    const message = parseHostMessage(event.data);
    if (message) render(message, root);
  });

  // Signal readiness so an embedder knows the listener is live. Android re-posts
  // on a timer regardless, so this is a best-effort hint, not a handshake.
  window.parent?.postMessage(HOST_READY, "*");
}

main();
