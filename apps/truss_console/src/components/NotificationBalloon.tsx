import { useEffect, useRef } from "react";
import { Toaster, toast } from "sonner";
import { NotificationItem } from "../types";

const AUTO_HIDE_MS = 5000;

interface NotificationBalloonProps {
  notifications: NotificationItem[];
}

/**
 * Top-right transient overlay backed by sonner.
 *
 * Listens to the same notifications stream the panel uses; when a new id
 * appears, fires a 5-second toast in the top-right corner. Hovering pauses
 * the timer (sonner default). The toast is purely a transient surface — it
 * does **not** dismiss the underlying NotificationStore record, which still
 * lives in the bell panel until the user clears it.
 *
 * Single source of truth: any notification source (session lifecycle, command
 * results, anything added later) flows through here automatically — DRY
 * across all paths.
 */
export function NotificationBalloon({ notifications }: NotificationBalloonProps) {
  const seenRef = useRef<Set<string>>(new Set());
  const initializedRef = useRef(false);

  useEffect(() => {
    // First mount: seed the seen set so we don't fire balloons for every
    // notification that pre-dates the page load.
    if (!initializedRef.current) {
      initializedRef.current = true;
      notifications.forEach((n) => seenRef.current.add(n.id));
      return;
    }

    for (const n of notifications) {
      if (seenRef.current.has(n.id)) continue;
      seenRef.current.add(n.id);

      const level = String(n.level || "info").toLowerCase();
      const event = String(n.event_type || "").toLowerCase();
      const fn =
        event === "completed"
          ? toast.success
          : event === "failed" || event === "canceled" || level === "error"
            ? toast.error
            : level === "warning"
              ? toast.warning
              : toast.info;

      fn(n.title, {
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
