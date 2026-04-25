import { useEffect, useRef, useState } from 'react';
import { RotateCcw } from 'lucide-react';
import { Button } from './ui/button';

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
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="neutral"
              size="sm"
              tone="info"
              onClick={() => {
                setOpen(false);
                onConfirm();
              }}
            >
              Confirm
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
