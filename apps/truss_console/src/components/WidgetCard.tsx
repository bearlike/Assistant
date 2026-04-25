import { Suspense, lazy, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "../lib/utils";
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
const MIN_CARD_HEIGHT_PX = 120;
const MAX_CARD_HEIGHT_PX = 600;
const MAX_CARD_HEIGHT_VH = 70;
const HEIGHT_BUFFER_PX = 8;

const loadingFallback = (
  <div className="flex items-center justify-center h-full text-xs text-[hsl(var(--muted-foreground))]">
    Loading widget…
  </div>
);

/**
 * Inline container for a `widget_ready` timeline entry.
 *
 * The widget DOM lives in `document.body` via `createPortal`, ALWAYS — even
 * when visually inline. The card here is a placeholder that reserves space
 * in the chat flow; we mirror its bounding rect to the portalled wrapper so
 * it visually appears to be inline. When maximized, we just swap the
 * portalled wrapper's CSS to viewport-centered. The widget instance never
 * moves, never re-mounts, never re-inits — Pyodide kernel, Streamlit
 * session_state, widget values, scroll position all preserved
 * automatically. Resizing is the only thing that happens.
 *
 * Why portal-to-body and not inline-fixed: when inline, `position: fixed`
 * with z-index gets clipped by deep ancestor flex/overflow combinations in
 * Chromium even though the spec says it shouldn't. Body-portal sidesteps
 * the issue entirely — root stacking context, viewport-relative containing
 * block, no flex parent to confuse the renderer.
 */
export function WidgetCard({ widget }: WidgetCardProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const widgetRef = useRef<HTMLDivElement>(null);
  const [contentHeight, setContentHeight] = useState<number | null>(null);
  const [isMaximized, setIsMaximized] = useState(false);
  const [cardRect, setCardRect] = useState<DOMRect | null>(null);
  const [cardVisible, setCardVisible] = useState(true);
  // `transitioning` gates the CSS transition on left/top/width/height. It is
  // ONLY true during the ~300ms expand/minimize animation. The widget mirrors
  // the card's bounding rect during chat scroll (cardRect → setState → new
  // style.left/top each frame); if the transition were always-on, every
  // scroll update would animate over 300ms and the widget would chase the
  // chat at half speed — a literal echo. Gating on a separate flag instead
  // of `isMaximized` keeps scroll tracking instant.
  const [transitioning, setTransitioning] = useState(false);
  const transitionTimeoutRef = useRef<number | null>(null);

  // Track the card's bounding rect so we can mirror it onto the portalled
  // widget wrapper. ResizeObserver covers card resize; scroll listeners on
  // every scrollable ancestor cover the chat scroll case; window resize
  // covers viewport changes. All updates batched on rAF — at 60fps the
  // widget tracks the card without visible lag.
  useLayoutEffect(() => {
    const card = cardRef.current;
    if (!card) return;

    let raf = 0;
    const update = () => {
      raf = 0;
      if (cardRef.current) {
        setCardRect(cardRef.current.getBoundingClientRect());
      }
    };
    const schedule = () => {
      if (raf) return;
      raf = requestAnimationFrame(update);
    };
    update();

    const ro = new ResizeObserver(schedule);
    ro.observe(card);

    // Find every scrollable ancestor and listen on it. We can't just listen
    // on window because the chat pane has its own internal scroll container.
    const scrollables: Element[] = [];
    let p: Element | null = card.parentElement;
    while (p) {
      const cs = window.getComputedStyle(p);
      if (
        cs.overflow !== "visible" ||
        cs.overflowY !== "visible" ||
        cs.overflowX !== "visible"
      ) {
        scrollables.push(p);
      }
      p = p.parentElement;
    }
    scrollables.forEach((s) => s.addEventListener("scroll", schedule, { passive: true }));
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);

    return () => {
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
      scrollables.forEach((s) => s.removeEventListener("scroll", schedule));
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, []);

  // Hide the portalled widget when the card scrolls fully out of view —
  // otherwise it keeps floating over whatever the user scrolls TO. Skip
  // this gating while maximized (modal should show regardless of card
  // visibility).
  useEffect(() => {
    const card = cardRef.current;
    if (!card) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) setCardVisible(e.isIntersecting);
      },
      { threshold: 0, rootMargin: "100px" }
    );
    io.observe(card);
    return () => io.disconnect();
  }, []);

  // Observe the widget's `.stMainBlockContainer` for content-height changes
  // so the card placeholder grows/shrinks to match. Mounted on widgetRef
  // (the portalled wrapper) since the widget DOM lives there now.
  useEffect(() => {
    const root = widgetRef.current;
    if (!root) return;

    let resizeObserver: ResizeObserver | undefined;
    let observedTarget: HTMLElement | null = null;

    const attach = (target: HTMLElement) => {
      if (observedTarget === target) return;
      resizeObserver?.disconnect();
      observedTarget = target;
      resizeObserver = new ResizeObserver(() => {
        const h = target.scrollHeight;
        if (h > 0) setContentHeight(h);
      });
      resizeObserver.observe(target);
      const initial = target.scrollHeight;
      if (initial > 0) setContentHeight(initial);
    };

    const sync = () => {
      const target = root.querySelector(".stMainBlockContainer");
      if (target instanceof HTMLElement) attach(target);
    };

    sync();
    const mo = new MutationObserver(sync);
    mo.observe(root, { childList: true, subtree: true });

    return () => {
      mo.disconnect();
      resizeObserver?.disconnect();
    };
  }, [widget.widget_id]);

  // Toggle maximize state with a real CSS transition. Two non-obvious
  // requirements:
  //
  // 1. The transition class must be in the element's computed styles BEFORE
  //    the position values change. If the class and the new style values are
  //    committed in the same paint, the browser sees no "from" state and
  //    skips the animation. Double-rAF guarantees one full paint with the
  //    transition class active before we flip `isMaximized` (which changes
  //    the style).
  //
  // 2. `transitioning` is only true for ~350ms (300ms transition + a little
  //    slack). Once it flips back off, the transition class is removed, so
  //    the next batch of scroll-driven cardRect updates lands instantly
  //    instead of animating.
  const toggleMaximize = (next: boolean) => {
    if (next === isMaximized) return;
    if (transitionTimeoutRef.current) {
      window.clearTimeout(transitionTimeoutRef.current);
    }
    setTransitioning(true);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        setIsMaximized(next);
        transitionTimeoutRef.current = window.setTimeout(() => {
          setTransitioning(false);
          transitionTimeoutRef.current = null;
        }, 350);
      });
    });
  };

  useEffect(() => {
    return () => {
      if (transitionTimeoutRef.current) {
        window.clearTimeout(transitionTimeoutRef.current);
      }
    };
  }, []);

  // Escape key + scroll-lock for the maximized state.
  useEffect(() => {
    if (!isMaximized) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") toggleMaximize(false); };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMaximized]);

  // Card placeholder height — minimum while maximized (placeholder is just a
  // "widget open in expanded view" hint), content-driven otherwise.
  const cardStyle = isMaximized
    ? { height: `${MIN_CARD_HEIGHT_PX}px` }
    : contentHeight != null
    ? {
        height: `clamp(${MIN_CARD_HEIGHT_PX}px, ${
          contentHeight + HEIGHT_BUFFER_PX
        }px, min(${MAX_CARD_HEIGHT_PX}px, ${MAX_CARD_HEIGHT_VH}vh))`,
      }
    : { height: `${MIN_CARD_HEIGHT_PX}px` };

  // Inline-mirror style: position: fixed at the card's exact viewport rect.
  // Switches to centered 90vw × 85vh when maximized. Both states use the
  // same `position: fixed` so transitions on width/height/top/left animate
  // smoothly between them.
  const widgetStyle: React.CSSProperties = isMaximized
    ? { left: "5vw", top: "7.5vh", width: "90vw", height: "85vh" }
    : cardRect
    ? { left: cardRect.left, top: cardRect.top, width: cardRect.width, height: cardRect.height }
    : { display: "none" };

  // Hide the portalled widget when the card scrolls offscreen and we're not
  // maximized. visibility:hidden + pointer-events:none keeps the React tree
  // mounted (kernel preserved) while removing it from view + interaction.
  const hidePortal = !isMaximized && !cardVisible;

  return (
    <>
      <div
        ref={cardRef}
        style={cardStyle}
        className="relative rounded-lg border border-[hsl(var(--border))] shadow-sm overflow-hidden bg-[hsl(var(--widget-panel-bg))] transition-[height] duration-200"
      >
        {isMaximized && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-[hsl(var(--muted-foreground))] pointer-events-none">
            Widget open in expanded view…
          </div>
        )}
      </div>

      {/* Backdrop — always portalled to body. The transition-opacity class is
        always present, so opacity changes in either direction interpolate
        smoothly. We never gate the mount so React doesn't unmount the node
        mid-fade-out (which would cut the transition short and pop the
        backdrop away).  */}
      {createPortal(
        <div
          className={cn(
            "fixed inset-0 z-40 bg-black/80 transition-opacity duration-300 ease-out",
            isMaximized ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none"
          )}
          onClick={() => toggleMaximize(false)}
        />,
        document.body
      )}

      {/*
        Widget — ALWAYS portalled to body, single createPortal at a stable
        JSX position with a stable target. React reconciler treats this as
        one Portal element across all renders → never unmounts, never
        re-attaches DOM, never reboots the Pyodide kernel. The wrapper's
        position/size animate smoothly between inline-mirror and maximized
        via CSS transitions — Streamlit just sees a resize, which it handles
        natively without re-init.
      */}
      {createPortal(
        <div
          ref={widgetRef}
          style={{
            ...widgetStyle,
            visibility: hidePortal ? "hidden" : "visible",
            pointerEvents: hidePortal ? "none" : "auto",
          }}
          className={cn(
            // `bg-[--widget-panel-bg]` MUST be on the always-on classes, not
            // gated on isMaximized. The portal div is z-50 above the
            // backdrop's z-40 — but during the resize animation, the inner
            // StliteWidgetPanel's `h-full` chain (flex-1 → Streamlit's
            // absolute-positioned .stApp) lags the interpolating container
            // by a frame and leaves transient transparent gaps. With no bg
            // on the portal div those gaps reveal the fading-in backdrop
            // BEHIND the widget, making the widget appear to go black for
            // the first ~150ms of the expand. Painting the portal div with
            // the panel color matches the StliteWidgetPanel's own bg so the
            // gaps are invisible.
            "fixed overflow-hidden bg-[hsl(var(--widget-panel-bg))]",
            // Transition is gated on `transitioning`, NOT `isMaximized`. See
            // the comment on the state declaration — gating on isMaximized
            // (or making the transition always-on) makes scroll tracking
            // animate every frame, dragging the widget behind the chat.
            transitioning && "transition-[left,top,width,height] duration-300 ease-out",
            isMaximized
              ? "z-50 rounded-xl shadow-2xl"
              : "z-[5] rounded-lg border border-[hsl(var(--border))] shadow-sm"
          )}
        >
          <Suspense fallback={loadingFallback}>
            <StliteWidgetPanel
              widget={widget}
              zoom={isMaximized ? 1 : 0.85}
              className="h-full"
              onMaximize={isMaximized ? undefined : () => toggleMaximize(true)}
              onClose={isMaximized ? () => toggleMaximize(false) : undefined}
            />
          </Suspense>
        </div>,
        document.body
      )}
    </>
  );
}
