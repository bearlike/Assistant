import { ChevronDown } from 'lucide-react';

/**
 * Floating scroll-to-bottom button — shared by ConversationTimeline and
 * LogsView. Positioned absolute within a relative parent.
 *
 * - 36×36 pill (one of the few legitimate "pill = status indicator"
 *   exceptions; everything else in the design uses a 6/8 px radius scale).
 * - Anchored just above the composer's outer band-glow padding. The
 *   bottom offset adapts to composer height: 96 px when idle (sits over
 *   the band's bottom gradient padding, above the composer shell at
 *   ~112 px), 152 px when running (clears the run-indicator strip while
 *   still reading as bottom-anchored).
 * - Slides up + fades in (180ms / 220ms cubic-bezier) on appearance —
 *   parent controls visibility by toggling render.
 * - 92% opacity at rest → 100% on hover; fills `--primary` on hover with
 *   a -2px lift to read as "available, not demanding".
 */
export function ScrollToBottom({ onClick, label = 'Jump to bottom', isRunning = false }: {
  onClick: () => void;
  label?: string;
  isRunning?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={[
        `absolute right-5 ${isRunning ? 'bottom-[152px]' : 'bottom-[96px]'} z-[6]`,
        'inline-flex h-9 w-9 items-center justify-center rounded-full',
        'border border-[hsl(var(--border-strong))] bg-[hsl(var(--card))]',
        'text-[hsl(var(--muted-foreground))] shadow-md opacity-[0.92]',
        'transition-all duration-200 ease-out',
        'hover:-translate-y-0.5 hover:opacity-100',
        'hover:bg-[hsl(var(--primary))] hover:text-[hsl(var(--primary-foreground))]',
        'hover:border-[hsl(var(--primary))]',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]/45',
        'animate-in fade-in slide-in-from-bottom-2 duration-200',
      ].join(' ')}
    >
      <ChevronDown className="h-4 w-4" aria-hidden />
    </button>
  );
}
