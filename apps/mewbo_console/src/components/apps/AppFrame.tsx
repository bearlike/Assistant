import { useEffect, useRef, useState } from "react";

import { cn } from "../../lib/utils";
import { useConsoleTheme } from "../stlitePanel";
import type { AppContext, AppFrontend } from "../../types/apps";

/**
 * Where the framework-free stlite host page lives — and it is NOT the same path
 * in dev and prod, which is a trap worth stating plainly because both paths
 * return HTTP 200.
 *
 * The widget host is a SEPARATE Vite build (`vite.widget-host.config.ts`,
 * relative base) emitting `dist/widget-host/`, so in production it is served at
 * `/widget-host/widget-host.html`. The dev server has no such build: it serves
 * root HTML files by name, so the page is at `/widget-host.html`, and a request
 * for the production path falls through Vite's SPA fallback to `index.html` —
 * i.e. framing the prod path in dev would silently load the whole console
 * recursively inside the iframe rather than erroring.
 *
 * Deliberately NOT exported: this file exports a component, and adding a second
 * export kind trips `react-refresh/only-export-components`, which CI runs at
 * `--max-warnings=0`. Tests assert the `src` the iframe actually receives,
 * which is the observable behaviour anyway.
 */
const WIDGET_HOST_URL = import.meta.env.DEV
  ? "/widget-host.html"
  : "/widget-host/widget-host.html";

/** The host's boot signal. Payloads posted before it arrive are dropped. */
const HOST_READY_TYPE = "mewbo-widget-host-ready";

/** The frozen cross-stream payload message type (Aura posts this same shape). */
const APP_PAYLOAD_TYPE = "mewbo-app-payload";

/**
 * `sandbox` — deliberate, and deliberately NOT a security boundary.
 *
 * `allow-same-origin` is mandatory here, and granting it alongside
 * `allow-scripts` is what makes this a CONTAMINATION boundary only: a
 * same-origin frame can reach `parent.document`, so nothing here defends
 * against hostile app code. It is required because the frame must behave like
 * an ordinary same-origin page for three separate reasons — the injected SDK
 * `fetch`es `api_base` on the console's own origin (an opaque origin would make
 * every such call cross-origin, and the API sends no CORS headers), Pyodide
 * fetches its vendored runtime and wheels the same way, and its worker +
 * WASM/storage bootstrap is unavailable to opaque origins.
 *
 * What the attribute DOES buy is exactly the contamination containment this
 * frame exists for: with no `allow-top-navigation*`, embedded app code cannot
 * retarget the console's own URL — the same class of host hijacking (an
 * embedded Streamlit app driving the console's location) that motivated moving
 * the app into a frame in the first place. The rest are enabled because
 * Streamlit genuinely uses them: `allow-forms` for its form widgets,
 * `allow-popups` (+ escape-sandbox, so an opened tab is not itself sandboxed)
 * for `st.link_button`, `allow-downloads` for `st.download_button`, and
 * `allow-modals` because Python `input()` under Pyodide surfaces as `prompt()`.
 */
const SANDBOX = [
  "allow-scripts",
  "allow-same-origin",
  "allow-forms",
  "allow-popups",
  "allow-popups-to-escape-sandbox",
  "allow-downloads",
  "allow-modals",
].join(" ");

interface AppFrameProps {
  frontend: AppFrontend;
  /** Render-scoped context injected as `_app_context.json` (token + api base). */
  appContext: AppContext;
  /** Accessible name for the frame; falls back to a generic label. */
  title?: string;
  className?: string;
}

/**
 * Full-page render of a Mewbo App's multi-file stlite frontend, inside an
 * iframe pointed at the standalone widget-host page.
 *
 * This replaced an in-document `@stlite/react` mount. The concrete bug: stlite
 * runs Streamlit in the HOST document, and Streamlit's multipage navigation
 * calls `history.pushState(..., basePath + "/" + pageName)` on every page
 * switch. wouter monkey-patches `history.pushState` globally, so it observed
 * those pushes, re-ran the console's route match against a path it could not
 * parse, and unmounted the app the user was looking at. A frame owns its own
 * `window.history`, `document.title` and style scope, so the fix generalizes
 * past that one symptom: the title guard and the CSS style-scope guard that the
 * in-document panels need have nothing to defend against here, because nothing
 * born inside the frame can reach the console document at all.
 *
 * It also converges surfaces. Aura already renders apps by posting
 * `mewbo-app-payload` into this same page; the console was the last consumer
 * mounting stlite in-document, so there is now ONE render path for both.
 */
export function AppFrame({ frontend, appContext, title, className }: AppFrameProps) {
  const theme = useConsoleTheme();
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [ready, setReady] = useState(false);

  // Read at post time, not depended on. Within one mount these cannot change in
  // a way that matters: kernel options are init-only, so a new token or a rolled
  // back version arrives as a REMOUNT (the caller keys this component on
  // `token_id`). Holding them in a ref keeps the post effect off object
  // identity — the caller builds `appContext` inline, so depending on it would
  // re-post, and therefore tear down and reboot the Pyodide kernel, on every
  // render of the parent.
  const payloadRef = useRef({ frontend, appContext });
  payloadRef.current = { frontend, appContext };

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;

    const onMessage = (event: MessageEvent) => {
      if (event.source !== frame.contentWindow) return;
      if (event.origin && event.origin !== window.location.origin) return;
      const data = event.data as { type?: unknown } | null;
      if (typeof data === "object" && data !== null && data.type === HOST_READY_TYPE) {
        setReady(true);
      }
    };

    window.addEventListener("message", onMessage);
    // Assign `src` HERE rather than in JSX, and only after the listener is
    // live. This is what closes the ready-before-listener race: `src` in JSX
    // starts the frame loading during React's commit, i.e. before effects run,
    // so a fast (cached) host could post its one ready signal into a window
    // that is not listening yet — and since a pre-ready payload post is
    // silently dropped by the host, the app would then hang forever with no
    // error. Ordering it this way makes the handshake deterministic instead of
    // relying on a retry timer.
    frame.src = WIDGET_HOST_URL;

    return () => window.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => {
    if (!ready) return;
    const target = frameRef.current?.contentWindow;
    if (!target) return;

    const { frontend: fe, appContext: ctx } = payloadRef.current;
    // The wire contract is frozen cross-stream — Aura posts these exact field
    // names — so this object is spelled out rather than spread from a wider one.
    target.postMessage(
      {
        type: APP_PAYLOAD_TYPE,
        payload: {
          entrypoint: fe.entrypoint,
          files: fe.files,
          requirements: fe.requirements,
          app_context: ctx,
        },
        theme,
      },
      window.location.origin,
    );
    // Re-running on a theme flip is the whole theme update path: the host tears
    // down the running kernel and remounts on a re-posted payload, which is the
    // only way to re-theme an init-only kernel. No separate message type.
  }, [ready, theme]);

  return (
    <div className={cn("relative h-full w-full bg-[hsl(var(--widget-panel-bg))]", className)}>
      <iframe
        ref={frameRef}
        title={title ?? "Mewbo app"}
        sandbox={SANDBOX}
        className="h-full w-full border-0"
      />
      {!ready && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-[hsl(var(--widget-panel-bg))] text-xs text-[hsl(var(--muted-foreground))]">
          Loading app…
        </div>
      )}
    </div>
  );
}
