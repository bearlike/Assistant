import { useEffect, useMemo, useState } from "react";
import { LayoutDashboard } from "lucide-react";
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

type Theme = "dark" | "light";

/**
 * Keep stlite from hijacking the browser tab title. When a widget's
 * `app.py` doesn't call `st.set_page_config(page_title=...)`, Streamlit
 * forces `document.title = "Streamlit"` during Pyodide boot. We observe
 * `<title>` and revert to the last non-"Streamlit" value — which lets
 * legitimate App.tsx updates (session renames, etc.) flow through while
 * pinning stlite's default out.
 */
function useTitleGuard() {
  useEffect(() => {
    const titleEl = document.querySelector("title");
    if (!titleEl) return;
    let lastGood = document.title;
    const obs = new MutationObserver(() => {
      if (document.title === "Streamlit") {
        document.title = lastGood;
      } else {
        lastGood = document.title;
      }
    });
    obs.observe(titleEl, { childList: true });
    return () => obs.disconnect();
  }, []);
}

/**
 * Subscribe to the Meeseeks console's theme. App.tsx toggles by
 * adding/removing the `light` class on `<html>` (dark is the default, with
 * no class); a MutationObserver on that single attribute is cheaper than a
 * React context + provider and fires exactly once per toggle.
 */
function useConsoleTheme(): Theme {
  const read = (): Theme =>
    document.documentElement.classList.contains("light") ? "light" : "dark";
  const [theme, setTheme] = useState<Theme>(read);
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(read()));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => obs.disconnect();
  }, []);
  return theme;
}

interface StliteWidgetPanelProps {
  widget: WidgetReadyPayload;
  className?: string;
}

/**
 * Outer wrapper — reads the console theme and remounts the inner kernel
 * when it flips. stlite's `streamlitConfig` is init-only (see
 * `@stlite/react/useKernel.ts`: *"options are only read on mount"*); the
 * StliteKernel exposes no `setConfig` or `setTheme`. Keying the inner on
 * `theme` is the canonical React idiom for a forced re-mount — the only
 * supported way to change theme at runtime. Remount cost = one Pyodide
 * boot; theme toggles are rare enough that we take the hit rather than
 * pre-warming a second hidden kernel.
 */
export function StliteWidgetPanel(props: StliteWidgetPanelProps) {
  const theme = useConsoleTheme();
  useTitleGuard();
  return <StliteWidgetPanelInner key={theme} theme={theme} {...props} />;
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

// Palette-aligned hex values keyed by theme.
// Dark: --widget-panel-bg / --muted / --foreground / --primary
// Light: same semantic roles, different values.
const STLITE_THEME: Record<
  "dark" | "light",
  Pick<
    Record<string, string>,
    | "theme.backgroundColor"
    | "theme.secondaryBackgroundColor"
    | "theme.textColor"
  >
> = {
  dark: {
    "theme.backgroundColor": "#191a1d",        // hsl(220 5% 10%) = --widget-panel-bg
    "theme.secondaryBackgroundColor": "#363533", // hsl(60 3% 21%) = --muted
    "theme.textColor": "#faf9f0",              // hsl(60 14% 97%) = --foreground
  },
  light: {
    "theme.backgroundColor": "#f4f4f6",        // hsl(220 5% 96%) = --widget-panel-bg
    "theme.secondaryBackgroundColor": "#f0eee6", // hsl(48 25% 92.2%) = --muted
    "theme.textColor": "#0a0a0a",              // hsl(0 0% 4%) = --foreground
  },
};

function StliteWidgetPanelInner({ widget, className, theme }: InnerProps) {
  const kernelOptions = useMemo(
    () => ({
      entrypoint: "app.py",
      files: Object.fromEntries(
        Object.entries(widget.files).map(([name, content]) => [name, { data: content }]),
      ),
      requirements: widget.requirements,
      prebuiltPackageNames: [] as string[],
      archives: [] as never[],
      wheelUrls,
      streamlitConfig: {
        "client.toolbarMode": "viewer",
        "theme.base": theme,
        // Brand clay (#D97757 = hsl(14.8 63.1% 59.6%) = --primary) for interactive elements.
        "theme.primaryColor": "#D97757",
        // Sans-serif matches the console's Inter stack.
        "theme.font": "sans serif",
        ...STLITE_THEME[theme],
      },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [], // options are only read on mount per useKernel contract
  );

  const kernel = useKernel(kernelOptions);

  return (
    <div
      className={cn(
        // NOTE: this outer wrapper is deliberately NOT `position: relative`.
        // streamlit's `.stApp` is `position: absolute; inset: 0`, so it
        // fills its nearest positioned ancestor. If this wrapper were
        // `relative`, stApp would fill the entire card — title bar included
        // — and paint over the macOS chrome. Instead the inner widget-area
        // div below owns `relative`, which anchors stApp below the title bar.
        "flex flex-col h-full bg-[hsl(var(--widget-panel-bg))]",
        // Belt-and-braces chrome hiding: `client.toolbarMode: "viewer"`
        // drops most widgets but Streamlit still ships a sticky
        // `<header data-testid="stHeader">` (Running indicator) and a
        // `<div data-testid="stDecoration">` (top gradient bar) that
        // visually overlap the first line of our content. Nuking them
        // here reclaims the space and makes WidgetCard's
        // `scrollHeight` measurement honest.
        "[&_[data-testid='stHeader']]:!hidden",
        "[&_[data-testid='stToolbar']]:!hidden",
        "[&_[data-testid='stDecoration']]:!hidden",
        // Force Streamlit's block container to use the full card width.
        // By default Streamlit caps `.stMainBlockContainer` at ~736px
        // (equivalent to st.set_page_config(layout="centered")). Removing
        // that cap and zeroing the side padding makes widgets fill the card
        // edge-to-edge so the `zoom: 0.85` wrapper uses all available space.
        "[&_.stMainBlockContainer]:!max-w-none",
        "[&_.stMainBlockContainer]:!w-full",
        "[&_.stMainBlockContainer]:!px-4",
        "[&_.stMainBlockContainer]:!pt-4",
        "[&_.stMainBlockContainer]:!pb-4",
        // Kill the INNER scrollbar. Streamlit's <section data-testid="stMain">
        // ships with `overflow: auto` on both axes, so a tall widget ends up
        // with two nested scrollbars (stMain + stAppViewContainer). Users
        // scroll the inner one, hit its end, and don't realize the outer
        // still has more content. Letting stMain overflow visibly passes
        // the scroll up to .stAppViewContainer, leaving exactly one
        // (styled, always-visible) scrollbar at the card edge.
        "[&_[data-testid='stMain']]:!overflow-visible",
        className,
      )}
    >
      {/* macOS-style chrome title bar — same pattern as TerminalCard */}
      <div className="flex items-center gap-2 px-3 py-1.5 bg-[hsl(var(--code-chrome))] border-b border-[hsl(var(--border))] shrink-0">
        {/* macOS traffic-light dots — fixed brand colors, same in both themes */}
        <div className="flex items-center gap-1.5 shrink-0">
          <span className="w-2.5 h-2.5 rounded-full bg-[#FF5F57]" />
          <span className="w-2.5 h-2.5 rounded-full bg-[#FFBD2E]" />
          <span className="w-2.5 h-2.5 rounded-full bg-[#28C840]" />
        </div>
        <span className="flex-1 text-[11px] text-[hsl(var(--code-fg-muted))] truncate min-w-0">
          {widget.summary || widget.widget_id}
        </span>
        <LayoutDashboard className="w-3 h-3 shrink-0 text-[hsl(var(--code-fg-subtle))]/60" />
      </div>

      {/*
        Widget area — `zoom: 0.85` scales the whole stlite subtree down
        so the widget reads as a *component inside* the chat, not a page
        rendered at the same weight as the surrounding UI. Chosen over
        `transform: scale(0.85)` because `zoom` is part of the CSS layout
        tree: descendant `scrollHeight` values come back already scaled,
        so WidgetCard's ResizeObserver measures what the user sees.
        `transform` would paint smaller but keep the natural layout,
        leaving the card oversized by ~15%.
      */}
      <div className="relative flex-1 overflow-hidden [zoom:0.85]">
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
