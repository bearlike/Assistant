/**
 * Shared Mermaid renderer — single source for lazy-loading mermaid, theme
 * config, init coordination, and the rendered-SVG cache.
 *
 * Both `MermaidBlock` (inline body) and `DiagramZoom` (modal) consume this
 * module so they share the cache and never re-render the same source twice
 * per theme. Keeping it here also kills the `mermaid.initialize()` race
 * that happens when many blocks each call init in their own effects.
 */

import { cssVarColor } from "./graphTheme";

type MermaidLib = typeof import("mermaid")["default"];

let mermaidLibPromise: Promise<MermaidLib> | null = null;

/** Lazy-load the mermaid bundle. Subsequent callers reuse the same promise. */
export async function loadMermaid(): Promise<MermaidLib> {
  if (!mermaidLibPromise) {
    mermaidLibPromise = import("mermaid").then((m) => m.default);
  }
  return mermaidLibPromise;
}

export type Theme = "light" | "dark";

export function currentTheme(): Theme {
  return typeof document !== "undefined" &&
    document.documentElement.classList.contains("light")
    ? "light"
    : "dark";
}

/**
 * Mermaid needs an actual resolved font stack, not a live `var(--font-sans)`
 * reference: it feeds this string into Canvas 2D text measurement
 * (`ctx.font = …`) to lay out diagram boxes, and a CSS custom property
 * doesn't resolve there the way it does in a stylesheet — only in the SVG's
 * own inline styles, which is half the story. Reading the computed value
 * mirrors `cssVarColor` below and keeps this from ever hardcoding (and
 * outliving) a specific font name again.
 */
function resolveFontFamily(): string {
  if (typeof window === "undefined") return "system-ui, sans-serif";
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue("--font-sans")
    .trim();
  return raw || "system-ui, sans-serif";
}

// Colours are live token reads (via `graphTheme.ts:cssVarColor`), not a
// hardcoded per-theme palette — `getComputedStyle` picks up whichever of
// `:root` / `.light` is active on `<html>` at call time, so the two themes
// share one mapping and never drift out of sync with `index.css`. Only
// mermaid's own built-in theme name still branches on `theme` below.
function buildThemeConfig(theme: Theme) {
  const fontFamily = resolveFontFamily();
  return {
    startOnLoad: false as const,
    securityLevel: "loose" as const,
    fontFamily,
    theme: (theme === "light" ? "default" : "dark") as "default" | "dark",
    themeVariables: {
      background: cssVarColor("--surface"),
      primaryColor: cssVarColor("--card"),
      primaryTextColor: cssVarColor("--foreground"),
      primaryBorderColor: cssVarColor("--border-strong"),
      lineColor: cssVarColor("--border-strong"),
      // Edge labels (a flowchart branch's "yes"/"no") are drawn as a filled
      // chip, and mermaid derives that fill from its OWN palette rather than
      // from `background` — so overriding the surface alone left the chip at
      // mermaid's light default, grey-on-grey against our dark diagram. Both
      // halves have to be stated: the chip fill and the text sitting on it.
      edgeLabelBackground: cssVarColor("--surface"),
      textColor: cssVarColor("--foreground"),
      fontFamily,
    },
  };
}

// One initialise() call per theme. A second mount on the same theme reuses
// the same promise and never re-races `mermaid.initialize()`.
let initialisedTheme: Theme | null = null;
async function ensureInitialised(theme: Theme): Promise<MermaidLib> {
  const mermaid = await loadMermaid();
  if (initialisedTheme !== theme) {
    mermaid.initialize(buildThemeConfig(theme));
    initialisedTheme = theme;
  }
  return mermaid;
}

// Render cache keyed by `theme|source`. Identical content + theme returns
// the same SVG immediately and `mermaid.render()` is not called again.
const svgCache = new Map<string, string>();

export function getCachedSvg(theme: Theme, source: string): string | undefined {
  return svgCache.get(`${theme}|${source}`);
}

/**
 * Render `source` to an SVG string. Cached per `(theme, source)` — repeat
 * calls bypass mermaid entirely. `domId` controls the temporary id mermaid
 * uses internally for its render container.
 */
export async function renderToSvg(
  theme: Theme,
  source: string,
  domId: string
): Promise<string> {
  const cacheKey = `${theme}|${source}`;
  const hit = svgCache.get(cacheKey);
  if (hit) return hit;
  const mermaid = await ensureInitialised(theme);
  const { svg } = await mermaid.render(domId, source);
  svgCache.set(cacheKey, svg);
  return svg;
}

/**
 * Source registry by diagram id, populated by the inline `MermaidBlock`
 * mounts. The zoom modal reads from here to render the same diagram at a
 * larger scale without re-walking the markdown tree.
 */
export const diagramRegistry: Record<string, string> = {};
