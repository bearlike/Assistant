import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Auto-scroll-to-bottom behavior for scrollable containers.
 * Stays pinned to the bottom when new content arrives,
 * but detaches when the user scrolls up.
 *
 * Returns a ref for the scroll container, an `isAtBottom` flag,
 * a manual `scrollToBottom` function, and an `onScroll` handler.
 */
export function useAutoScroll(dep: unknown) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(() => {
    if (isAtBottom) scrollToBottom();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dep, isAtBottom]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const threshold = 64;
    setIsAtBottom(el.scrollTop + el.clientHeight >= el.scrollHeight - threshold);
  }, []);

  return { scrollRef, isAtBottom, scrollToBottom, onScroll };
}
