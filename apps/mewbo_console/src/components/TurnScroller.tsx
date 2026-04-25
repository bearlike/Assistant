import { useEffect, useMemo, useRef, useState } from 'react';
import { TimelineEntry } from '../types';
import { formatTokens } from '../utils/time';

// Why no visible "01 12k" labels next to each segment: they were absolute-
// positioned at `left-full ml-2`, so they extended past the pill into the
// message column. The token/turn info is preserved on `aria-label` for
// screen readers; the dot-rail itself is the visual indicator.

/**
 * Vertical, token-weighted "where am I in the conversation" indicator that
 * lives on the left edge of the conversation pane. Replaces a literal
 * scrollbar with something that doubles as a cost map: heavier turns occupy
 * more horizontal width when the rail is expanded.
 *
 * Behavior:
 *   - At rest: tiny monochrome dots, one per turn, opacity-keyed by activity.
 *   - Auto-peeks while the user is scrolling (~900ms after last scroll).
 *   - Peeks while the cursor is over the rail or the wide invisible hit-zone.
 *   - Click any segment → smooth-scroll to the corresponding row by
 *     `data-turn-idx`, accounting for the scroll container's top.
 *   - Below ~360px pane width, segments stay as 3px dots.
 *
 * The `scrollRef` is the conversation scroll container. Rows in that
 * container must carry `data-turn-idx={i}` for IntersectionObserver to
 * track which turn currently dominates the viewport.
 */
interface TurnScrollerProps {
  scrollRef: React.RefObject<HTMLDivElement | null>;
  timeline: TimelineEntry[];
}

const PEEK_TIMEOUT_MS = 900;
const NARROW_BREAKPOINT = 360;

function turnWidth(entry: TimelineEntry): number {
  if (entry.role === 'user') return 4;
  // Total tokens for the turn — peak input is "context pressure", output
  // is "work produced". Use peak as the cost proxy (matches how the
  // workspace footer talks about cost per turn).
  const usage = entry.turn?.tokenUsage;
  const tokens = usage ? Math.max(usage.inputTokens, usage.subInputTokens) : 0;
  if (!tokens) return 4;
  // 0 → 4px, ~16k → ~18px, log-ramped so a 200k turn isn't 10x a 20k turn.
  // Capped tight so the rail never reaches into the text column even when
  // expanded — the cost map stays at the viewport margin.
  return Math.round(4 + Math.min(14, Math.log2(1 + tokens / 500) * 2.6));
}

export function TurnScroller({ scrollRef, timeline }: TurnScrollerProps) {
  const railRef = useRef<HTMLDivElement | null>(null);
  const peekTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const [hovering, setHovering] = useState(false);
  const [scrolling, setScrolling] = useState(false);
  const [narrow, setNarrow] = useState(false);

  // Token-weighted width per turn — recomputed only when timeline shape changes.
  const widths = useMemo(() => timeline.map(turnWidth), [timeline]);

  // Track which row currently dominates the viewport. The rootMargin biases
  // detection toward the upper-third of the pane so the active segment
  // matches what the reader is actually looking at.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root || timeline.length === 0) return;
    const rows = root.querySelectorAll<HTMLElement>('[data-turn-idx]');
    if (!rows.length) return;
    const obs = new IntersectionObserver(
      (entries) => {
        let best: IntersectionObserverEntry | null = null;
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          if (!best || e.intersectionRatio > best.intersectionRatio) best = e;
        }
        if (best) {
          const raw = (best.target as HTMLElement).dataset.turnIdx;
          const idx = raw != null ? parseInt(raw, 10) : NaN;
          if (!Number.isNaN(idx)) setActiveIdx(idx);
        }
      },
      { root, threshold: [0.25, 0.5, 0.75], rootMargin: '-20% 0px -40% 0px' },
    );
    rows.forEach((r) => obs.observe(r));
    return () => obs.disconnect();
  }, [scrollRef, timeline.length]);

  // Auto-peek while the user is actively scrolling.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const onScroll = () => {
      setScrolling(true);
      if (peekTimeoutRef.current) clearTimeout(peekTimeoutRef.current);
      peekTimeoutRef.current = setTimeout(() => setScrolling(false), PEEK_TIMEOUT_MS);
    };
    root.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      root.removeEventListener('scroll', onScroll);
      if (peekTimeoutRef.current) clearTimeout(peekTimeoutRef.current);
    };
  }, [scrollRef]);

  // Collapse to dot mode below a narrow pane width.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const measure = () => setNarrow(root.clientWidth < NARROW_BREAKPOINT);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(root);
    return () => ro.disconnect();
  }, [scrollRef]);

  const jumpTo = (i: number) => {
    const root = scrollRef.current;
    if (!root) return;
    const target = root.querySelector<HTMLElement>(`[data-turn-idx="${i}"]`);
    if (!target) return;
    const rootTop = root.getBoundingClientRect().top;
    const targetTop = target.getBoundingClientRect().top;
    root.scrollTo({ top: root.scrollTop + (targetTop - rootTop) - 16, behavior: 'smooth' });
  };

  if (timeline.length === 0) return null;
  const expanded = !narrow && (hovering || scrolling);

  return (
    <div
      ref={railRef}
      className="fixed left-2 top-1/2 -translate-y-1/2 z-10 select-none"
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
      aria-label="Turn navigator"
    >
      {/* Wide invisible hit-zone so cursor proximity counts as peek */}
      <div className="absolute -inset-y-2 -left-2 w-7" aria-hidden />
      <div
        className={[
          'session-ts-rail relative flex flex-col gap-1.5 rounded-full py-2 px-1.5 transition-all duration-200',
          expanded ? 'is-expanded' : '',
          narrow ? 'is-collapsed' : '',
        ].join(' ').trim()}
      >
        {timeline.map((entry, i) => {
          const isActive = i === activeIdx;
          const w = expanded ? widths[i] : 2;
          const tokens = entry.turn?.tokenUsage
            ? Math.max(entry.turn.tokenUsage.inputTokens, entry.turn.tokenUsage.subInputTokens)
            : 0;
          return (
            <button
              key={entry.id}
              type="button"
              onClick={() => jumpTo(i)}
              aria-label={`Jump to turn ${i + 1}${tokens ? `, ${formatTokens(tokens)} tokens` : ''}`}
              aria-current={isActive ? 'true' : undefined}
              className={`session-ts-seg ${isActive ? 'is-active' : ''}`}
              style={{ ['--ts-w' as string]: `${w}px` }}
            />
          );
        })}
      </div>
    </div>
  );
}
