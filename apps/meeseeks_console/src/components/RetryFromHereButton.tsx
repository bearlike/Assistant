import { useEffect, useRef, useState } from 'react';
import { RotateCcw } from 'lucide-react';

export function RetryFromHereButton({
  onConfirm,
  className = '',
}: {
  onConfirm: () => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className={className}
        aria-label="Retry from here"
      >
        <RotateCcw className="w-3 h-3" />
        <span className="hidden text-[10px] group-hover:inline-block">
          Retry from here
        </span>
      </button>

      {open && (
        <div className="absolute left-0 top-full mt-2 z-50 w-72 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--popover))] shadow-2xl shadow-black/40 ring-1 ring-white/[0.03] p-3">
          <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed mb-3">
            This will restart the conversation from this point. Changes made by
            the agent will not be reverted.
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={() => setOpen(false)}
              className="px-2.5 py-1 text-xs rounded-md text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))] transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                setOpen(false);
                onConfirm();
              }}
              className="px-2.5 py-1 text-xs font-medium rounded-md bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 transition-colors"
            >
              Confirm
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
