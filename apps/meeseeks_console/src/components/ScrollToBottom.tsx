/**
 * Floating scroll-to-bottom button — shared by ConversationTimeline and LogsView.
 * Positioned absolute within a relative parent.
 */
export function ScrollToBottom({ onClick, label = 'Jump to bottom' }: {
  onClick: () => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className="absolute bottom-20 right-4 h-10 w-10 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-[hsl(var(--foreground))] shadow-md transition hover:-translate-y-0.5"
    >
      <svg viewBox="0 0 24 24" aria-hidden="true" className="mx-auto h-5 w-5">
        <path fill="currentColor" d="M12 16.5 5 9.5l1.4-1.4L12 13.7l5.6-5.6L19 9.5z" />
      </svg>
    </button>
  );
}
