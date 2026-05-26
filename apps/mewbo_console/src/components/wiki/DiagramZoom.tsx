/**
 * Diagram zoom modal. Reuses the diagram source from `diagramRegistry`,
 * renders via the shared mermaid renderer (cache shared with inline blocks),
 * and opens at a fit-to-stage scale so the diagram fills the modal nicely
 * regardless of its natural mermaid-emitted size.
 *
 * The shadcn `<DialogContent>` ships its own absolute-positioned close
 * button — we don't render a second one (DRY).
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Maximize2, Minus, Plus, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";

import {
  currentTheme,
  diagramRegistry,
  getCachedSvg,
  renderToSvg,
} from "./mermaid-renderer";

interface DiagramZoomProps {
  diagramId: string | null;
  onClose: () => void;
}

const MIN_SCALE = 0.2;
const MAX_SCALE = 6;

export function DiagramZoom({ diagramId, onClose }: DiagramZoomProps) {
  const [svg, setSvg] = useState<string>("");
  const [fitScale, setFitScale] = useState(1);
  const [scale, setScale] = useState(1);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const dragRef = useRef<{ x: number; y: number; sx: number; sy: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const innerRef = useRef<HTMLDivElement | null>(null);

  // Load the SVG (cache hit when the inline block already rendered it).
  useEffect(() => {
    if (!diagramId) {
      setSvg("");
      setPos({ x: 0, y: 0 });
      setScale(1);
      setFitScale(1);
      return;
    }
    const source = diagramRegistry[diagramId];
    if (!source) return;
    const theme = currentTheme();
    const cached = getCachedSvg(theme, source);
    if (cached) setSvg(cached);
    void (async () => {
      const rendered = await renderToSvg(theme, source, `mmd-zoom-${diagramId}`);
      setSvg(rendered);
    })();
  }, [diagramId]);

  // After the SVG paints, measure it against the stage and pick a scale
  // that fills the stage with a comfortable margin. Re-run on window
  // resize so the modal stays correctly fit when the viewport changes.
  const recomputeFit = useCallback(() => {
    if (!svg || !stageRef.current || !innerRef.current) return;
    const stage = stageRef.current.getBoundingClientRect();
    const innerSvg = innerRef.current.querySelector("svg");
    if (!innerSvg) return;
    const svgRect = innerSvg.getBoundingClientRect();
    // Use the SVG's natural size (post-transform / browser-paint).
    // We divide by the *current* scale so the math is in natural units.
    const naturalW = svgRect.width / scale;
    const naturalH = svgRect.height / scale;
    if (naturalW <= 0 || naturalH <= 0) return;
    const fit = Math.min(
      (stage.width - 48) / naturalW,
      (stage.height - 48) / naturalH
    );
    const next = Math.max(MIN_SCALE, Math.min(MAX_SCALE, fit));
    setFitScale(next);
    setScale(next);
    setPos({ x: 0, y: 0 });
    // We intentionally don't list `scale` as a dep — it would cause an
    // infinite recompute loop. The natural-size division above uses the
    // current scale value via closure capture; the result is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [svg]);

  useLayoutEffect(() => {
    recomputeFit();
  }, [recomputeFit]);

  useEffect(() => {
    const onResize = () => recomputeFit();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [recomputeFit]);

  const onWheel = useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.92 : 1.08;
    setScale((s) => Math.max(MIN_SCALE, Math.min(MAX_SCALE, s * delta)));
  }, []);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      setDragging(true);
      dragRef.current = { x: e.clientX, y: e.clientY, sx: pos.x, sy: pos.y };
      e.currentTarget.setPointerCapture(e.pointerId);
    },
    [pos.x, pos.y]
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!dragging || !dragRef.current) return;
      setPos({
        x: dragRef.current.sx + (e.clientX - dragRef.current.x),
        y: dragRef.current.sy + (e.clientY - dragRef.current.y),
      });
    },
    [dragging]
  );

  const onPointerUp = useCallback(() => {
    setDragging(false);
    dragRef.current = null;
  }, []);

  // Reset returns to the fit-to-stage scale rather than 1× (which was the
  // mermaid-natural size and tended to look tiny inside the modal).
  const reset = () => {
    setScale(fitScale);
    setPos({ x: 0, y: 0 });
  };

  return (
    <Dialog open={diagramId !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="!max-w-[1100px] !w-[min(95vw,1100px)] h-[min(78vh,800px)] p-0 overflow-hidden rounded-xl border-[hsl(var(--border-strong))] bg-[hsl(var(--card))] grid-cols-1 grid-rows-[auto_1fr] gap-0">
        {/* Top bar — diagram id on the left, zoom controls on the right.
            Close is provided by DialogContent itself (top-right X). */}
        <div className="flex items-center justify-between pl-4 pr-12 h-11 border-b border-[hsl(var(--border))]">
          <span className="inline-flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
            <Maximize2 className="h-3.5 w-3.5" />
            <span className="font-mono text-[hsl(var(--foreground))]">{diagramId}</span>
          </span>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              onClick={() =>
                setScale((s) => Math.max(MIN_SCALE, s * 0.9))
              }
              aria-label="Zoom out"
            >
              <Minus className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="sm" iconOnly onClick={reset} aria-label="Reset zoom">
              <RotateCcw className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              onClick={() => setScale((s) => Math.min(MAX_SCALE, s * 1.1))}
              aria-label="Zoom in"
            >
              <Plus className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
        <div
          ref={stageRef}
          className="relative overflow-hidden touch-none select-none"
          onWheel={onWheel}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          style={{
            backgroundImage:
              "radial-gradient(circle, hsl(var(--border-strong)) 1px, transparent 1px)",
            backgroundSize: "20px 20px",
            cursor: dragging ? "grabbing" : "grab",
          }}
        >
          <div
            ref={innerRef}
            className="absolute left-1/2 top-1/2 [&_svg]:max-w-none [&_svg]:h-auto"
            style={{
              transform: `translate(calc(-50% + ${pos.x}px), calc(-50% + ${pos.y}px)) scale(${scale})`,
              transformOrigin: "center",
              transition: dragging ? "none" : "transform 80ms ease-out",
            }}
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
