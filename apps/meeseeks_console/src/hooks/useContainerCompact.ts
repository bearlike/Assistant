import { RefObject, useEffect, useState } from 'react';

export function useContainerCompact(
  ref: RefObject<HTMLElement | null>,
  threshold = 480,
): boolean {
  const [compact, setCompact] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setCompact(entry.contentRect.width < threshold);
      }
    });

    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, threshold]);

  return compact;
}
