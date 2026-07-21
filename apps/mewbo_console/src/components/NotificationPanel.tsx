import { ReactNode } from 'react';
import { X } from 'lucide-react';
import { NotificationItem } from '../types';
import { getStatusConfig } from '../lib/notifications';
import { RelativeTime } from '../utils/relativeTime';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';

interface NotificationPanelProps {
  notifications: NotificationItem[];
  onClose: (id: string) => void;
  onClearAll: () => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The trigger element (e.g. the bell button). Wrapped via PopoverTrigger asChild. */
  trigger: ReactNode;
  /** Popover direction. Defaults to a top-bar bell (`bottom`/`end`); the NavRail
   *  footer bell sits bottom-left, so it opens to the right instead. */
  side?: "top" | "right" | "bottom" | "left";
  align?: "start" | "center" | "end";
}

/**
 * Bell popover: a 320px surface listing every live notification.
 *
 * Typography here is deliberately flat. The panel previously ran FOUR sizes
 * (12 / 11 / 10 / 10px) inside one small popover, all at weight 500 — at that
 * scale a 1px step is invisible, so four sizes read as one blurry size and the
 * hierarchy did no work. It now uses exactly two adjacent steps (`text-xs` for
 * content, `text-2xs` for the metadata line) and lets WEIGHT and COLOUR carry
 * the hierarchy instead: title is medium/foreground, message is normal/muted,
 * status keeps medium plus its state colour, timestamp is normal/muted.
 * Adding a third size back is the regression to watch for.
 */
export function NotificationPanel({
  notifications,
  onClose,
  onClearAll,
  open,
  onOpenChange,
  trigger,
  side = "bottom",
  align = "end",
}: NotificationPanelProps) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        side={side}
        align={align}
        className="w-80 p-0 overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-3 py-2.5 border-b border-[hsl(var(--border))]">
          <span className="text-xs font-medium text-[hsl(var(--foreground))]">
            Notifications
          </span>
          {notifications.length > 0 && (
            <span className="text-2xs font-medium text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded-full">
              {notifications.length}
            </span>
          )}
        </div>

        {/* List */}
        <div className="max-h-[320px] overflow-y-auto">
          {notifications.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-xs text-[hsl(var(--muted-foreground))]">
                No notifications
              </p>
            </div>
          ) : (
            notifications.map((n) => {
              const config = getStatusConfig(n);
              const Icon = config.icon;
              return (
                <div
                  key={n.id}
                  className="group flex items-start gap-2.5 px-3 py-2.5 hover:bg-[hsl(var(--accent))] transition-colors border-b border-[hsl(var(--border))] last:border-b-0">
                  <Icon className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${config.color}`} />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-medium text-[hsl(var(--foreground))] truncate">
                      {n.title}
                    </p>
                    {n.message && (
                      <p className="text-xs font-normal text-[hsl(var(--muted-foreground))] truncate">
                        {n.message}
                      </p>
                    )}
                    <div className="flex items-center gap-1.5 mt-0.5">
                      {/* Status is the semantic anchor of the row, so it keeps
                          weight AND its state colour; the timestamp beside it
                          drops to plain muted. Same size, different job. */}
                      <span className={`text-2xs font-medium ${config.color}`}>
                        {config.label}
                      </span>
                      <span className="text-2xs font-normal text-[hsl(var(--muted-foreground))]">·</span>
                      <span className="text-2xs font-normal text-[hsl(var(--muted-foreground))]">
                        {RelativeTime.format(n.created_at)}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onClose(n.id);
                    }}
                    className="p-0.5 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))] opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 transition-all shrink-0"
                    aria-label="Dismiss notification">
                    <X className="w-3 h-3" />
                  </button>
                </div>
              );
            })
          )}
        </div>

        {/* Footer */}
        {notifications.length > 0 && (
          <div className="border-t border-[hsl(var(--border))] px-3 py-2">
            <button
              onClick={onClearAll}
              className="w-full text-center text-xs font-normal text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors py-0.5">
              Clear all notifications
            </button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
