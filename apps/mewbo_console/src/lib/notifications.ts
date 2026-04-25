import { CheckCircle2, Sparkles, Square, XCircle, type LucideIcon } from "lucide-react";
import { toast } from "sonner";
import type { NotificationItem } from "../types";

/**
 * Single source of truth for mapping a NotificationItem to its visual status
 * (icon, label, themed text color). Consumed by both NotificationPanel (the
 * persistent center) and NotificationBalloon (the transient toaster), so the
 * two surfaces stay in lockstep — no parallel mappings to drift.
 */
export type NotificationStatus = {
  icon: LucideIcon;
  label: string;
  /** Tailwind text-color class (CSS variable backed) */
  color: string;
};

const LEVEL_COLOR: Record<string, string> = {
  info: "text-[hsl(var(--muted-foreground))]",
  warning: "text-amber-600",
  error: "text-red-600",
};

const LEVEL_LABEL: Record<string, string> = {
  info: "Info",
  warning: "Warning",
  error: "Error",
};

export function getStatusConfig(notification: NotificationItem): NotificationStatus {
  const eventType = String(notification.event_type ?? "").toLowerCase();
  switch (eventType) {
    case "created":
      return { icon: Sparkles, label: "New session", color: "text-sky-600" };
    case "completed":
      return { icon: CheckCircle2, label: "Completed", color: "text-emerald-600" };
    case "failed":
      return { icon: XCircle, label: "Failed", color: "text-red-600" };
    case "canceled":
      return { icon: XCircle, label: "Canceled", color: "text-amber-600" };
    case "stopped":
      return { icon: Square, label: "Stopped", color: "text-[hsl(var(--muted-foreground))]" };
  }
  const level = String(notification.level ?? "info").toLowerCase();
  return {
    icon: Square,
    label: LEVEL_LABEL[level] ?? LEVEL_LABEL.info,
    color: LEVEL_COLOR[level] ?? LEVEL_COLOR.info,
  };
}

/**
 * Pick the sonner toast variant for a notification. Maps the same
 * event_type/level taxonomy as getStatusConfig — kept colocated so adding a
 * new status only touches this file.
 */
export function pickToastFn(notification: NotificationItem): typeof toast.info {
  const eventType = String(notification.event_type ?? "").toLowerCase();
  if (eventType === "completed") return toast.success;
  if (eventType === "failed" || eventType === "canceled") return toast.error;
  const level = String(notification.level ?? "info").toLowerCase();
  if (level === "error") return toast.error;
  if (level === "warning") return toast.warning;
  return toast.info;
}
