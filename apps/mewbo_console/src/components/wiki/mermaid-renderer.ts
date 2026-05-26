/**
 * Shared Mermaid renderer — single source for lazy-loading mermaid, theme
 * config, init coordination, and the rendered-SVG cache.
 *
 * Both `MermaidBlock` (inline body) and `DiagramZoom` (modal) consume this
 * module so they share the cache and never re-render the same source twice
 * per theme. Keeping it here also kills the `mermaid.initialize()` race
 * that happens when many blocks each call init in their own effects.
 */

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

function buildThemeConfig(theme: Theme) {
  return {
    startOnLoad: false as const,
    securityLevel: "loose" as const,
    fontFamily: "Inter, system-ui, sans-serif",
    theme: (theme === "light" ? "default" : "dark") as "default" | "dark",
    themeVariables:
      theme === "light"
        ? {
            background: "#f0eee6",
            primaryColor: "#ffffff",
            primaryTextColor: "#0a0a0a",
            primaryBorderColor: "#d6d3c5",
            lineColor: "#a8a59a",
            fontFamily: "Inter, system-ui, sans-serif",
          }
        : {
            background: "#1f1e1c",
            primaryColor: "#2c2b29",
            primaryTextColor: "#f5f4ef",
            primaryBorderColor: "#3d3b37",
            lineColor: "#6b6963",
            fontFamily: "Inter, system-ui, sans-serif",
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
