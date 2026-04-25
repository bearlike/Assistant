import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { WidgetReadyPayload } from "../types";

// Lazy-load the stlite panel — @stlite/react pulls in Pyodide wheel wrappers
// and ~1 MB of WASM glue. Gating the import behind the first widget keeps
// the console's initial bundle lean.
const StliteWidgetPanel = lazy(() =>
  import("./StliteWidgetPanel").then((m) => ({ default: m.StliteWidgetPanel }))
);

interface WidgetCardProps {
  widget: WidgetReadyPayload;
}

// --- Sizing knobs ---------------------------------------------------------
// A widget is a chat attachment, not a page. The cap is the *smaller* of
// an absolute ceiling (so a 1920×1080 monitor doesn't swallow the chat)
// and a viewport-relative ceiling (so a 375×667 phone doesn't get a
// card that eats 60% of the screen). Whichever is more conservative for
// the current device wins. When content exceeds the cap, the card
// scrolls internally — the chat pane stays the chat pane.
//
// The cap is generous on purpose: combined with the `zoom: 0.85` on the
// stlite content in StliteWidgetPanel, an 800px natural widget collapses
// to ~680px rendered and fits most caps with little-to-no scroll. Going
// smaller here would re-introduce the "scroll inside every widget"
// complaint; going larger would let tall widgets dominate the chat pane.
//
// MIN keeps a tiny loading placeholder from collapsing during Pyodide
// boot. The observer below reads `.stMainBlockContainer.scrollHeight`
// (streamlit's content-truthful node — excludes sidebar/toolbar chrome
// which `.stAppViewContainer` would include). Because `zoom` is part of
// the layout tree (not a paint effect like `transform: scale`), descendant
// `scrollHeight` values come back already scaled — we size against the
// rendered height, not a natural one.
const MIN_CARD_HEIGHT_PX = 120;
const MAX_CARD_HEIGHT_PX = 600;
const MAX_CARD_HEIGHT_VH = 70;
// Tiny buffer so we never fight the ResizeObserver with a 1px scroll bar.
const HEIGHT_BUFFER_PX = 8;

/**
 * Inline container for a `widget_ready` timeline entry.
 *
 * Sits in the conversation stream exactly where the submit landed — same
 * placement model as every other tool card. Sizes to its content so the
 * widget looks native to the chat, not boxed into a fixed-height frame.
 */
export function WidgetCard({ widget }: WidgetCardProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const [contentHeight, setContentHeight] = useState<number | null>(null);

  useEffect(() => {
    const card = cardRef.current;
    if (!card) return;

    // We observe `.stMainBlockContainer` (not the card itself) because its
    // `scrollHeight` reflects the content's natural height even while the
    // parent card is clamping it.  That target is unstable: a theme toggle
    // remounts the whole stlite subtree (see `key={theme}` in
    // StliteWidgetPanel) and the container is destroyed-then-recreated.
    // So the MutationObserver has to stay alive for the lifetime of the
    // widget, re-attaching the ResizeObserver each time a fresh container
    // appears, and never collapse the card to a degenerate height in the
    // gap — the old measurement is a better guess than zero.
    let resizeObserver: ResizeObserver | undefined;
    let observedTarget: HTMLElement | null = null;

    const attach = (target: HTMLElement) => {
      if (observedTarget === target) return;
      resizeObserver?.disconnect();
      observedTarget = target;
      resizeObserver = new ResizeObserver(() => {
        // During tear-down a detached node fires with scrollHeight = 0;
        // ignoring those keeps the last valid height on screen until the
        // remounted kernel has real content to measure.
        const h = target.scrollHeight;
        if (h > 0) setContentHeight(h);
      });
      resizeObserver.observe(target);
      const initial = target.scrollHeight;
      if (initial > 0) setContentHeight(initial);
    };

    const syncTarget = () => {
      const target = card.querySelector(".stMainBlockContainer");
      if (target instanceof HTMLElement) {
        attach(target);
      } else if (observedTarget) {
        // Old container was removed during remount — disconnect the now-
        // orphaned ResizeObserver but LEAVE `contentHeight` alone so the
        // card holds its previous size through the Pyodide reboot.
        resizeObserver?.disconnect();
        resizeObserver = undefined;
        observedTarget = null;
      }
    };

    syncTarget();
    const mutationObserver = new MutationObserver(syncTarget);
    mutationObserver.observe(card, { childList: true, subtree: true });

    return () => {
      mutationObserver.disconnect();
      resizeObserver?.disconnect();
    };
  }, [widget.widget_id]);

  const style =
    contentHeight != null
      ? {
          // Cap = min(absolute, viewport-relative). Mobile 667h → 333px;
          // desktop 1080h → 400px. Nested min() inside clamp() is
          // CSS Values L4 (all evergreen browsers, Safari 14.4+).
          height: `clamp(${MIN_CARD_HEIGHT_PX}px, ${
            contentHeight + HEIGHT_BUFFER_PX
          }px, min(${MAX_CARD_HEIGHT_PX}px, ${MAX_CARD_HEIGHT_VH}vh))`,
        }
      : { height: `${MIN_CARD_HEIGHT_PX}px` };

  return (
    <div
      ref={cardRef}
      style={style}
      className="rounded-lg border border-[hsl(var(--border))] shadow-sm overflow-hidden bg-[hsl(var(--widget-panel-bg))] transition-[height] duration-200 [&_.stAppViewContainer]:!overflow-y-scroll"
    >
      <Suspense
        fallback={
          <div className="flex items-center justify-center h-full text-xs text-[hsl(var(--muted-foreground))]">
            Loading widget…
          </div>
        }
      >
        <StliteWidgetPanel widget={widget} className="h-full" />
      </Suspense>
    </div>
  );
}
