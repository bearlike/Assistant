import { useMemo, useRef } from "react";
import { LayoutDashboard, Maximize2, X } from "lucide-react";
import { StliteAppWithToast, useKernel } from "@stlite/react";
import { wheelUrls } from "@stlite/react/vite-utils";
// Stlite ships its Streamlit theme, layout, and @font-face rules as a
// separate stylesheet — @stlite/react does NOT auto-inject it. Without this
// side-effect import the widget renders as unstyled HTML (no fonts, no
// layout chrome, no buttons/inputs styling). Vite copies the referenced
// .woff2/.ttf assets into dist/assets/ automatically.
import "@stlite/react/stlite.css";

import { WidgetReadyPayload } from "../types";
import { cn } from "../lib/utils";
import { buildKernelOptions } from "../widget/stliteBoot";
// The theme/title/scrollbar chrome lives in `stlitePanel.ts`. This is now the
// ONLY in-document stlite surface — the app renderer moved into an iframe
// (`apps/AppFrame.tsx`), where a frame's own document makes the title guard and
// style scoping unnecessary. Those guards still matter HERE, because this panel
// really does mount Streamlit into the console's document.
// See `stlitePanel.ts`. Font injection was removed in a later change (it was
// inert for visible text and mis-fonted code chips) — this panel intentionally
// does not import `useFontInjection`.
import {
  PYODIDE_URL,
  STLITE_STREAMLIT_OVERRIDES,
  useConsoleTheme,
  useStliteStyleScope,
  useTitleGuard,
  type StliteTheme as Theme,
} from "./stlitePanel";

interface StliteWidgetPanelProps {
  widget: WidgetReadyPayload;
  className?: string;
  onMaximize?: () => void;
  onClose?: () => void;
}

/**
 * Outer wrapper — reads the console theme and passes it to the inner kernel.
 * Intentionally does NOT key on `theme`: keying would kill and reboot the
 * Pyodide worker on every theme toggle, breaking the single-persistent-kernel
 * contract. stlite's `streamlitConfig` is init-only so the widget stays in
 * its initial theme — the surrounding chrome adapts via CSS variables instead.
 */
export function StliteWidgetPanel(props: StliteWidgetPanelProps) {
  const theme = useConsoleTheme();
  useTitleGuard();
  return <StliteWidgetPanelInner theme={theme} {...props} />;
}

interface InnerProps extends StliteWidgetPanelProps {
  theme: Theme;
}

/**
 * Mounts a stlite (Streamlit-in-WASM) widget in-browser using @stlite/react.
 *
 * The widget's `app.py` and `data.json` are passed INLINE via the `files`
 * option — no CORS, no server fetch. `requirements` are installed via
 * micropip inside Pyodide. The whole thing runs in a sandboxed Web Worker.
 *
 * `streamlitConfig` dotted keys mirror `.streamlit/config.toml`. Two keys
 * matter here:
 *
 *   - `client.toolbarMode: "viewer"` hides the Deploy + hamburger + Rerun
 *     band Streamlit renders at the top of every app. Inside a chat
 *     widget that band is pure noise and overlapped the widget content.
 *   - `theme.base` flips Streamlit's bundled stylesheet between its dark
 *     and light defaults. The extended theme object (STLITE_THEME) overrides
 *     background, text, and primary colors to match our CSS variable palette
 *     so the widget reads as part of the same design system.
 */

function StliteWidgetPanelInner({ widget, className, theme, onMaximize, onClose }: InnerProps) {
  // Theme + kernel-option construction now lives in `widget/stliteBoot.ts` so
  // the framework-free `widget-host.html` page shares the exact same logic.
  // `wheelUrls` (from @stlite/react's vite-utils) and `pyodideUrl` (our
  // self-hosted runtime) are the only surface-specific inputs.
  const kernelOptions = useMemo(
    () => {
      // `wheelUrls` is typed optional (vite-utils resolves the asset imports at
      // build time) but is always present at runtime; guard it so the type
      // narrows and a broken build fails loudly rather than booting without the
      // bundled streamlit/stlite wheels.
      if (!wheelUrls) throw new Error("stlite wheelUrls unavailable");
      return buildKernelOptions(widget, { theme, wheelUrls, pyodideUrl: PYODIDE_URL });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [], // options are only read on mount per useKernel contract
  );

  const kernel = useKernel(kernelOptions);

  // Confine facade / widget-author `<style>` tags to THIS portalled wrapper.
  // The whole panel is portalled to `document.body` by `WidgetCard`, so this
  // div is the container that holds the stlite mount + its injected styles —
  // observe it, not the inline placeholder. See `stlitePanel.ts`.
  const scopeRef = useRef<HTMLDivElement>(null);
  useStliteStyleScope(scopeRef);

  return (
    <div
      ref={scopeRef}
      className={cn(
        // NOTE: this outer wrapper is deliberately NOT `position: relative`.
        // streamlit's `.stApp` is `position: absolute; inset: 0`, so it
        // fills its nearest positioned ancestor. If this wrapper were
        // `relative`, stApp would fill the entire card — title bar included
        // — and paint over the macOS chrome. Instead the inner widget-area
        // div below owns `relative`, which anchors stApp below the title bar.
        "flex flex-col h-full bg-[hsl(var(--widget-panel-bg))]",
        // The Streamlit-DOM overrides (hide the leftover header/toolbar/
        // decoration chrome, un-cap the block container, collapse the double
        // scrollbar) live in `stlitePanel.ts`. The iframed app renderer gets
        // them from the host page instead. See that constant for the rationale.
        STLITE_STREAMLIT_OVERRIDES,
        className,
      )}
    >
      {/* macOS-style chrome title bar — same pattern as TerminalCard.
          `data-widget-chrome` is WidgetCard's measurement hook: the card
          reserves content height + this bar's height (see WidgetCard). */}
      <div data-widget-chrome className="flex items-center gap-2 px-3 py-1.5 bg-[hsl(var(--code-chrome))] border-b border-[hsl(var(--border))] shrink-0">
        {/* macOS traffic-light dots — fixed brand colors, same in both themes */}
        <div className="flex items-center gap-1.5 shrink-0">
          <span className="w-2.5 h-2.5 rounded-full bg-[#FF5F57]" />
          <span className="w-2.5 h-2.5 rounded-full bg-[#FFBD2E]" />
          <span className="w-2.5 h-2.5 rounded-full bg-[#28C840]" />
        </div>
        <span className="flex-1 text-2xs text-[hsl(var(--code-fg-muted))] truncate min-w-0">
          {widget.summary || widget.widget_id}
        </span>
        {onClose ? (
          <button
            onClick={onClose}
            aria-label="Close expanded widget"
            className="shrink-0 text-[hsl(var(--code-fg-subtle))]/60 hover:text-[hsl(var(--code-fg-muted))] transition-colors"
          >
            <X className="w-3 h-3" />
          </button>
        ) : onMaximize ? (
          <button
            onClick={onMaximize}
            aria-label="Maximize widget"
            className="shrink-0 text-[hsl(var(--code-fg-subtle))]/60 hover:text-[hsl(var(--code-fg-muted))] transition-colors"
          >
            <Maximize2 className="w-3 h-3" />
          </button>
        ) : (
          <LayoutDashboard className="w-3 h-3 shrink-0 text-[hsl(var(--code-fg-subtle))]/60" />
        )}
      </div>

      {/*
        Widget area — rendered at NATURAL scale, deliberately unscaled.
        An earlier revision applied `zoom: 0.85` here to make the widget
        read "smaller than the page". Fractional zoom lays glyphs out at
        fractional pixel positions (worse under devicePixelRatio 2 → an
        effective 1.7× raster), which made every word in the widget render
        with subtly irregular intra-word spacing next to the crisp console
        text. Streamlit at native scale looks like Streamlit in a browser
        — that fidelity is the product requirement; size containment is
        the card cap's job (WidgetCard), not a scale hack's.
      */}
      <div className="relative flex-1 overflow-hidden">
        {kernel ? (
          // stlite's boot emits a cascade of Toastify progress notifications
          // ("Loading Pyodide", "Mounting files", …) into a position:absolute
          // container that escapes the card. The library provides a first-
          // party opt-out; we render our own quiet "Loading widget…"
          // placeholder while `kernel` itself is null.
          <StliteAppWithToast
            kernel={kernel}
            disableProgressToasts
            disableErrorToasts
            disableModuleAutoLoadToasts
          />
        ) : (
          <div className="flex items-center justify-center h-full text-xs text-[hsl(var(--muted-foreground))]">
            Loading widget…
          </div>
        )}
      </div>
    </div>
  );
}
