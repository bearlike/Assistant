import { Pause, Play, X, TriangleAlert } from "lucide-react";
import type { TriggerDTO } from "../../api/triggers";
import { isActiveTrigger } from "../../api/triggers";
import type { SessionSummary } from "../../types";
import { Badge } from "../agents";
import { Button } from "../ui/button";
import { RelativeTime } from "../../utils/relativeTime";
import {
  ACTION_LABEL,
  KIND_META,
  STATUS_META,
  firesLabel,
  triggerArgsSummary,
} from "./triggerFormat";

export interface TriggerRowProps {
  trigger: TriggerDTO;
  /** The joined target session, if it's in the loaded session lists. */
  session?: SessionSummary;
  /** Target session is terminated/archived → the trigger is orphaned. */
  dangling: boolean;
  loading: boolean;
  onNavigateSession: (sessionId: string) => void;
  onPauseResume: (trigger: TriggerDTO) => void;
  /** Opens the confirm dialog (normal cancel). */
  onCancel: (trigger: TriggerDTO) => void;
  /** One-click cancel for an orphaned trigger — no confirm. */
  onCancelDangling: (trigger: TriggerDTO) => void;
}

/**
 * One trigger row — compact, border-separated (the list's own `divide-y` in
 * `TriggersPane`), no heavy card. Three lines, one per hierarchy tier: state
 * (badges + actions), identity (the wake prompt, the row's one emphasized
 * line), metadata (everything else, muted). Hierarchy comes from weight and
 * colour, never a size step — see the console CLAUDE.md type-scale law.
 */
export function TriggerRow({
  trigger,
  session,
  dangling,
  loading,
  onNavigateSession,
  onPauseResume,
  onCancel,
  onCancelDangling,
}: TriggerRowProps) {
  const kind = KIND_META[trigger.kind] ?? KIND_META["time.at"];
  const status = STATUS_META[trigger.status] ?? STATUS_META.armed;
  const KindIcon = kind.icon;
  const active = isActiveTrigger(trigger);
  const argsSummary = triggerArgsSummary(trigger);
  const sessionLabel = session?.title?.trim() || `${trigger.session_id.slice(0, 8)}…`;

  return (
    <div className="px-1 py-3 hover:bg-[hsl(var(--accent))]/30 transition-colors">
      {/* Row 1 — STATE: kind + status badges (colour AND text, never colour
          alone), pause/cancel pinned right. Nothing here competes with the
          row's identity, so it can wrap freely at narrow widths instead of
          fighting a long wake prompt for space on one line. */}
      <div className="flex flex-wrap items-center gap-2">
        <Badge color={kind.color}>
          <KindIcon className="w-3 h-3 mr-1 inline-block align-[-1px]" aria-hidden />
          {kind.label}
        </Badge>
        <Badge color={status.color}>
          {status.live && trigger.status === "armed" && (
            <span
              className="mr-1 inline-block w-1.5 h-1.5 rounded-full bg-[hsl(var(--success))] align-[0px] animate-pulse"
              aria-hidden
            />
          )}
          {status.label}
        </Badge>
        <div className="ml-auto flex items-center gap-1 shrink-0">
          {active && !dangling && (
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              disabled={loading}
              onClick={() => onPauseResume(trigger)}
              aria-label={trigger.status === "paused" ? "Resume trigger" : "Pause trigger"}
              title={trigger.status === "paused" ? "Resume" : "Pause"}
            >
              {trigger.status === "paused" ? (
                <Play className="w-3.5 h-3.5" />
              ) : (
                <Pause className="w-3.5 h-3.5" />
              )}
            </Button>
          )}
          {active && (
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              disabled={loading}
              onClick={() => (dangling ? onCancelDangling(trigger) : onCancel(trigger))}
              aria-label="Cancel trigger"
              title={dangling ? "Cancel orphaned trigger" : "Cancel trigger"}
              className="hover:bg-[hsl(var(--destructive)/0.1)] hover:text-[hsl(var(--destructive-text))]"
            >
              <X className="w-3.5 h-3.5" />
            </Button>
          )}
        </div>
      </div>

      {/* Row 2 — IDENTITY: the one line that answers "what does this trigger
          actually do". This is the row's single font-medium/foreground line —
          everything above is state (a badge), everything below is metadata
          (muted). Weight and colour carry the hierarchy; the size stays the
          same body step as before. */}
      <p
        className="mt-1.5 text-sm font-medium text-[hsl(var(--foreground))] truncate"
        title={trigger.wake_prompt}
      >
        {trigger.wake_prompt || (
          <span className="font-normal italic text-[hsl(var(--muted-foreground))]">No wake prompt</span>
        )}
      </p>

      {/* Row 3 — METADATA: how it re-engages, the target session, its
          schedule/condition, how often it has fired, and when. Muted and
          small, deliberately below the identity line. Mono stays reserved for
          the two genuinely machine-shaped values here — the cron/date args
          summary and (further down) a session-id fallback — a re-engage
          LABEL and a fire COUNT are prose, not code, so they read in the body
          font; `tabular-nums` keeps the count from jittering the layout as it
          ticks up instead of borrowing a second typeface for the job. */}
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[hsl(var(--muted-foreground))]">
        <span title={`Firing ${ACTION_LABEL[trigger.action].toLowerCase()}s the session`}>
          {ACTION_LABEL[trigger.action]}
        </span>
        <span aria-hidden className="opacity-40">·</span>
        <button
          type="button"
          onClick={() => onNavigateSession(trigger.session_id)}
          className="inline-flex items-center gap-1 rounded px-1 -mx-1 hover:text-[hsl(var(--foreground))] hover:underline transition-colors"
          title="Open the target session"
        >
          {sessionLabel}
        </button>
        {argsSummary && (
          <>
            <span aria-hidden className="opacity-40">·</span>
            {/* Was a fixed 240px cap, sized for the old max-w-5xl page. The
                Settings shell is max-w-3xl (narrower card), so this scales
                with the row instead of eating a fixed chunk of a smaller
                budget — flex-wrap on the parent still lets it drop to its own
                line rather than overflow. */}
            <span className="font-mono truncate max-w-[45%]" title={argsSummary}>{argsSummary}</span>
          </>
        )}
        <span aria-hidden className="opacity-40">·</span>
        <span className="tabular-nums" title="Fires so far / max">{firesLabel(trigger)} fired</span>
        {trigger.next_fire_at && status.live && (
          <>
            <span aria-hidden className="opacity-40">·</span>
            <span title={RelativeTime.tooltip(trigger.next_fire_at)}>
              next {RelativeTime.format(trigger.next_fire_at)}
            </span>
          </>
        )}
        {trigger.last_fired_at && (
          <>
            <span aria-hidden className="opacity-40">·</span>
            <span title={RelativeTime.tooltip(trigger.last_fired_at)}>
              last {RelativeTime.format(trigger.last_fired_at)}
            </span>
          </>
        )}
        {dangling && (
          <span className="inline-flex items-center gap-1 text-[hsl(var(--warning))]" title="The session this trigger targets is terminated or archived">
            <TriangleAlert className="w-3 h-3" aria-hidden />
            Target session unavailable
          </span>
        )}
      </div>

      {trigger.last_error && (
        <p className="mt-1.5 text-xs font-mono text-[hsl(var(--destructive)/0.85)] break-words" title="Last error">
          {trigger.last_error}
        </p>
      )}
    </div>
  );
}
