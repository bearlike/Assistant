import { cn } from '../utils/cn';

type PopoverProps = {
  direction?: 'up' | 'down';
  width?: string;
  maxHeight?: string;
  className?: string;
  children: React.ReactNode;
};

export function Popover({
  direction = 'up',
  width = 'w-60',
  maxHeight = 'max-h-[320px]',
  className,
  children,
}: PopoverProps) {
  return (
    <div
      className={cn(
        'absolute',
        direction === 'up' ? 'bottom-full left-0 mb-2' : 'top-full left-0 mt-2',
        width,
        maxHeight,
        'bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-lg',
        'shadow-2xl shadow-black/40 ring-1 ring-white/[0.03]',
        'overflow-hidden overflow-y-auto z-50 text-left flex flex-col',
        className,
      )}
    >
      {children}
    </div>
  );
}
