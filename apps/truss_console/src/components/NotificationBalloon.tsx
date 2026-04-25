import { useEffect, useRef } from "react";
import { Toaster } from "sonner";
import { pickToastFn } from "../lib/notifications";
import { NotificationItem } from "../types";

const AUTO_HIDE_MS = 5000;

/**
 * Captured at module load. Notifications older than this came from a previous
 * page lifetime — they're already in the bell panel and must NOT replay as
 * toasts on refresh / reload / re-mount.
 */
const PAGE_LOAD_AT = Date.now();

interface NotificationBalloonProps {
  notifications: NotificationItem[];
}

/**
 * Top-right transient overlay backed by sonner.
 *
 * Toasts fire only for notifications created AFTER this page loaded. Backlog
 * still lives in the bell panel via NotificationPanel — the toaster is
 * strictly for in-flight events. `seenRef` de-dupes across react-query
 * refetches so the same notification never fires twice.
 */
export function NotificationBalloon({ notifications }: NotificationBalloonProps) {
  const seenRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    for (const n of notifications) {
      if (seenRef.current.has(n.id)) continue;
      seenRef.current.add(n.id);

      const createdAt = Date.parse(n.created_at);
      if (!Number.isFinite(createdAt) || createdAt <= PAGE_LOAD_AT) continue;

      pickToastFn(n)(n.title, {
        description: n.message,
        duration: AUTO_HIDE_MS,
      });
    }
  }, [notifications]);

  return (
    <Toaster
      position="top-right"
      richColors
      closeButton
      duration={AUTO_HIDE_MS}
      toastOptions={{
        classNames: {
          toast:
            "bg-[hsl(var(--popover))] text-[hsl(var(--popover-foreground))] border border-[hsl(var(--border))] shadow-lg",
        },
      }}
    />
  );
}
