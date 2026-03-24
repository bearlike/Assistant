import { useRef, useEffect } from 'react';
import { X, CheckCircle2, XCircle, Square } from 'lucide-react';
import { NotificationItem } from '../types';
interface NotificationPanelProps {
  notifications: NotificationItem[];
  onDismiss: (id: string) => void;
  onClearAll: () => void;
  onClose: () => void;
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
const levelColors: Record<string, string> = {
  info: 'text-[hsl(var(--muted-foreground))]',
  warning: 'text-amber-600',
  error: 'text-red-600'
};

function getStatusConfig(notification: NotificationItem) {
  const eventType = String(notification.event_type || '').toLowerCase();
  if (eventType === 'completed') {
    return { icon: CheckCircle2, label: 'Completed', color: 'text-emerald-600' };
  }
  if (eventType === 'failed') {
    return { icon: XCircle, label: 'Failed', color: 'text-red-600' };
  }
  if (eventType === 'canceled') {
    return { icon: XCircle, label: 'Canceled', color: 'text-amber-600' };
  }
  if (eventType === 'stopped') {
    return {
      icon: Square,
      label: 'Stopped',
      color: 'text-[hsl(var(--muted-foreground))]'
    };
  }
  const level = String(notification.level || 'info').toLowerCase();
  const color = levelColors[level] || levelColors.info;
  const label =
  level === 'warning' ? 'Warning' : level === 'error' ? 'Error' : 'Info';
  return { icon: Square, label, color };
}
export function NotificationPanel({
  notifications,
  onDismiss,
  onClearAll,
  onClose
}: NotificationPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        onClose();
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [onClose]);
  return (
    <div
      ref={panelRef}
      className="absolute top-full right-0 mt-2 w-80 bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-lg shadow-2xl shadow-black/40 overflow-hidden z-50 ring-1 ring-white/[0.03]">

      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-[hsl(var(--border))] bg-[hsl(var(--popover))]">
        <span className="text-xs font-semibold text-[hsl(var(--foreground))]">
          Notifications
        </span>
        {notifications.length > 0 &&
        <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded-full">
            {notifications.length}
          </span>
        }
      </div>

      {/* List */}
      <div className="max-h-[320px] overflow-y-auto">
        {notifications.length === 0 ?
        <div className="px-4 py-8 text-center">
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              No notifications
            </p>
          </div> :

        notifications.map((n) => {
          const config = getStatusConfig(n);
          const Icon = config.icon;
          return (
            <div
              key={n.id}
              className="group flex items-start gap-2.5 px-3 py-2.5 hover:bg-[hsl(var(--accent))] transition-colors border-b border-[hsl(var(--border))]/50 last:border-b-0">

                <Icon
                className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${config.color}`} />

                <div className="flex-1 min-w-0">
                  <p className="text-xs text-[hsl(var(--foreground))] font-medium truncate">
                    {n.title}
                  </p>
                  {n.message &&
                  <p className="text-[11px] text-[hsl(var(--muted-foreground))] truncate">
                      {n.message}
                    </p>
                  }
                  <div className="flex items-center gap-1.5 mt-0.5">
                    <span className={`text-[10px] font-medium ${config.color}`}>
                      {config.label}
                    </span>
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                      ·
                    </span>
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
              </div>);

        })
        }
      </div>

      {/* Footer */}
      {notifications.length > 0 &&
      <div className="border-t border-[hsl(var(--border))] px-3 py-2 bg-[hsl(var(--popover))]">
          <button
          onClick={onClearAll}
          className="w-full text-center text-[11px] font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors py-0.5">

            Clear all notifications
          </button>
        </div>
      }
    </div>);

}
