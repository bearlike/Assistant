/**
 * Presentation maps for the trigger subsystem — the single home for turning a
 * `TriggerKind` / `TriggerStatus` / `TriggerAction` into a human label, a
 * `Badge` colour (a `BADGE_COLOR_MAP` key), and a lucide glyph. Every map is an
 * exhaustive `Record<Union, …>` so `tsc` flags a missing arm the moment the
 * contract's unions grow — the same closed-union discipline the graph theme and
 * origin badge use.
 *
 * Kept React-free (no JSX) so it stays unit-testable in isolation; the lucide
 * icon is stored as a component *reference*, not rendered here.
 */
import {
  AlarmClock,
  CalendarClock,
  GitPullRequest,
  Webhook,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import type {
  TriggerAction,
  TriggerDTO,
  TriggerKind,
  TriggerStatus,
} from "../../api/triggers";

export interface KindMeta {
  label: string;
  /** `BADGE_COLOR_MAP` key for the kind chip. */
  color: string;
  icon: LucideIcon;
}

/** Kind → label / chip colour / glyph. Colours are grouped by family. */
export const KIND_META: Record<TriggerKind, KindMeta> = {
  "time.at": { label: "Scheduled time", color: "blue", icon: AlarmClock },
  "time.cron": { label: "Cron schedule", color: "blue", icon: CalendarClock },
  "ci.workflow": { label: "CI workflow", color: "violet", icon: Workflow },
  "forge.pr": { label: "Pull request", color: "teal", icon: GitPullRequest },
  webhook: { label: "Webhook", color: "cyan", icon: Webhook },
};

export interface StatusMeta {
  label: string;
  /** `BADGE_COLOR_MAP` key for the status chip. */
  color: string;
  /** Live states still fire — drives the pulse dot + "which actions apply". */
  live: boolean;
}

/**
 * Status → label / chip colour / liveness. `armed` reads live (emerald); the
 * held `paused` state stays amber so it never reads as a grey terminal at a
 * glance; terminal states are subdued grey except `failed`, which stays red
 * because it carries a `last_error` worth surfacing.
 */
export const STATUS_META: Record<TriggerStatus, StatusMeta> = {
  armed: { label: "Armed", color: "emerald", live: true },
  paused: { label: "Paused", color: "amber", live: true },
  completed: { label: "Completed", color: "muted", live: false },
  failed: { label: "Failed", color: "red", live: false },
  cancelled: { label: "Cancelled", color: "muted", live: false },
  expired: { label: "Expired", color: "muted", live: false },
};

export const ACTION_LABEL: Record<TriggerAction, string> = {
  message: "Re-engage",
  start: "New turn",
};

/** Read the first present string among *keys* from a loose args object. */
function firstString(
  args: Record<string, unknown> | undefined,
  keys: string[],
): string | null {
  if (!args) return null;
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  return null;
}

type ArgsSummaryFn = (args: Record<string, unknown> | undefined) => string;

/**
 * Kind → args summariser. A `Record<TriggerKind, …>` alongside `KIND_META` so a
 * new `TriggerKind` fails `tsc` here too, instead of silently falling through to
 * an empty summary.
 */
const ARGS_SUMMARY: Record<TriggerKind, ArgsSummaryFn> = {
  "time.at": (args) => firstString(args, ["at", "when", "run_at", "iso"]) ?? "",
  "time.cron": (args) => {
    const expr = firstString(args, ["cron", "expr", "expression", "schedule"]);
    const tz = firstString(args, ["tz", "timezone"]);
    if (!expr) return "";
    return tz ? `${expr} · ${tz}` : expr;
  },
  "ci.workflow": (args) => {
    const wf = firstString(args, ["workflow", "name", "workflow_name"]);
    const repo = firstString(args, ["repo", "repository", "slug"]);
    return [repo, wf].filter(Boolean).join(" · ");
  },
  "forge.pr": (args) => {
    const repo = firstString(args, ["repo", "repository", "slug"]);
    const ref = firstString(args, ["pr", "number", "ref", "branch"]);
    const event = firstString(args, ["event", "action"]);
    return [repo, ref && `#${ref}`.replace("##", "#"), event]
      .filter(Boolean)
      .join(" · ");
  },
  webhook: (args) => firstString(args, ["name", "id", "hook", "path"]) ?? "",
};

/**
 * A compact one-line summary of a trigger's kind-specific `args` for the row's
 * secondary line — best-effort over the common arg keys each kind carries.
 * Returns "" when nothing summarisable is present (callers render it inline, so
 * an empty string is a safe no-op).
 */
export function triggerArgsSummary(trigger: Pick<TriggerDTO, "kind" | "args">): string {
  return ARGS_SUMMARY[trigger.kind](trigger.args);
}

/** Fires label — "3 / 5" when capped, "3" when unbounded. */
export function firesLabel(trigger: Pick<TriggerDTO, "fires" | "max_fires">): string {
  const fires = Number.isFinite(trigger.fires) ? trigger.fires : 0;
  if (trigger.max_fires != null && trigger.max_fires > 0) {
    return `${fires} / ${trigger.max_fires}`;
  }
  return String(fires);
}
