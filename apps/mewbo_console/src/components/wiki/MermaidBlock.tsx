/**
 * Inline Mermaid diagram block. Consumes the shared `mermaid-renderer`
 * module so init/cache/lazy-load happen exactly once across all mounts.
 *
 * Invariants for jank-free scrolling across many diagrams (see
 * `mermaid-renderer.ts` for the cache + init coordination):
 *   1. Diagram ids are stable hashes of the source (set by the renderer
 *      caller), so React reconciler keeps the SVG node across re-renders.
 *   2. `React.memo` on `(diagramId, inlineSource)` skips the whole subtree
 *      when parents re-render for unrelated reasons (scroll-spy, etc.).
 *   3. `setState` is skipped if the SVG hasn't actually changed.
 *
 * Theme: pulled from the `light` class on <html>. Listens for
 * `wiki:theme-change` so theme flips re-render with the right palette.
 */

import { memo, useEffect, useRef, useState } from "react";
import { Maximize2 } from "lucide-react";

import {
  currentTheme,
  diagramRegistry,
  getCachedSvg,
  renderToSvg,
} from "./mermaid-renderer";

interface MermaidBlockProps {
  diagramId: string;
  /** Mermaid source from a fenced code block; cached in `diagramRegistry`. */
  inlineSource: string;
  onZoom?: () => void;
}

function MermaidBlockInner({ diagramId, inlineSource, onZoom }: MermaidBlockProps) {
  // Make the source available to the zoom modal by diagram id.
  diagramRegistry[diagramId] = inlineSource;
  const [svg, setSvg] = useState<string>(() =>
    getCachedSvg(currentTheme(), inlineSource) ?? ""
  );
  const [error, setError] = useState<string | null>(null);
  // Tracks the most recent render request so an earlier (slower) theme
  // flip can't clobber a fresher SVG.
  const generationRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const myGen = ++generationRef.current;
    const theme = currentTheme();
    const cached = getCachedSvg(theme, inlineSource);
    if (cached) {
      setSvg((prev) => (prev === cached ? prev : cached));
      setError(null);
    }
    void (async () => {
      try {
        const next = await renderToSvg(theme, inlineSource, `mmd-${diagramId}`);
        if (cancelled || generationRef.current !== myGen) return;
        setSvg((prev) => (prev === next ? prev : next));
        setError(null);
      } catch (e) {
        if (cancelled || generationRef.current !== myGen) return;
        // eslint-disable-next-line no-console
        console.warn("mermaid render failed", e);
        setError("Failed to render diagram.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [diagramId, inlineSource]);

  // Re-render on theme flip. Cache hits short-circuit the await chain.
  useEffect(() => {
    const onThemeChange = () => {
      const theme = currentTheme();
      const cached = getCachedSvg(theme, inlineSource);
      if (cached) {
        setSvg((prev) => (prev === cached ? prev : cached));
        return;
      }
      void (async () => {
        try {
          const next = await renderToSvg(theme, inlineSource, `mmd-${diagramId}`);
          setSvg((prev) => (prev === next ? prev : next));
        } catch (e) {
          // eslint-disable-next-line no-console
          console.warn("mermaid theme re-render failed", e);
        }
      })();
    };
    window.addEventListener("wiki:theme-change", onThemeChange);
    return () => window.removeEventListener("wiki:theme-change", onThemeChange);
  }, [diagramId, inlineSource]);

  return (
    <button
      type="button"
      onClick={onZoom}
      className="group relative w-full my-6 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))]/30 p-4 cursor-zoom-in hover:border-[hsl(var(--border-strong))] transition-colors overflow-hidden"
      title="Click to zoom"
    >
      <div className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium bg-[hsl(var(--muted))]/80 text-[hsl(var(--muted-foreground))] opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none">
        <Maximize2 className="h-2.5 w-2.5" />
        Click to zoom
      </div>
      {error ? (
        <div className="text-xs text-[hsl(var(--destructive))] py-6 text-center">{error}</div>
      ) : svg ? (
        <div
          className="flex items-center justify-center [&_svg]:max-w-full [&_svg]:h-auto"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      ) : (
        <div className="text-xs text-[hsl(var(--muted-foreground))] py-6 text-center">
          Rendering diagram…
        </div>
      )}
    </button>
  );
}

/** Memoised: parent re-renders (scroll-spy ticks) skip this subtree. */
export const MermaidBlock = memo(MermaidBlockInner, (prev, next) =>
  prev.diagramId === next.diagramId && prev.inlineSource === next.inlineSource
);
