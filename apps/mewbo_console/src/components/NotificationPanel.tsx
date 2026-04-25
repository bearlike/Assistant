import { ReactNode } from 'react';
import { X } from 'lucide-react';
import { NotificationItem } from '../types';
import { getStatusConfig } from '../lib/notifications';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';

interface NotificationPanelProps {
  notifications: NotificationItem[];
  onDismiss: (id: string) => void;
  onClearAll: () => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The trigger element (e.g. the bell button). Wrapped via PopoverTrigger asChild. */
  trigger: ReactNode;
}

function timeAgo(timestamp: string): string {
  const diff = Date.now() - new Date(timestamp).getTime();
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function NotificationPanel({
  notifications,
  onDismiss,
  onClearAll,
  open,
  onOpenChange,
  trigger,
}: NotificationPanelProps) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        side="bottom"
        align="end"
        className="w-80 p-0 overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-3 py-2.5 border-b border-[hsl(var(--border))]">
          <span className="text-xs font-semibold text-[hsl(var(--foreground))]">
            Notifications
          </span>
          {notifications.length > 0 && (
            <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded-full">
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
                  className="group flex items-start gap-2.5 px-3 py-2.5 hover:bg-[hsl(var(--accent))] transition-colors border-b border-[hsl(var(--border))]/50 last:border-b-0">
                  <Icon className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${config.color}`} />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-[hsl(var(--foreground))] font-medium truncate">
                      {n.title}
                    </p>
                    {n.message && (
                      <p className="text-[11px] text-[hsl(var(--muted-foreground))] truncate">
                        {n.message}
                      </p>
                    )}
                    <div className="flex items-center gap-1.5 mt-0.5">
                      <span className={`text-[10px] font-medium ${config.color}`}>
                        {config.label}
                      </span>
                      <span className="text-[10px] text-[hsl(var(--muted-foreground))]">·</span>
                      <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                        {timeAgo(n.created_at)}
                      </span>
                    </div>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDismiss(n.id);
                    }}
                    className="p-0.5 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))] opacity-0 group-hover:opacity-100 transition-all shrink-0"
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
              className="w-full text-center text-[11px] font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors py-0.5">
              Clear all notifications
            </button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
