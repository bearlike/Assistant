import { Ban, Zap } from "lucide-react";
import type { TriggerTranscriptMeta } from "../../types";
import type { TriggerKind } from "../../api/triggers";
import { KIND_META } from "./triggerFormat";
import { RelativeTime } from "../../utils/relativeTime";

/**
 * Compact inline transcript rows for the trigger subsystem,
 * rendered by `ConversationTimeline` for `role: "trigger"` /
 * `role: "session_terminated"` entries. Deliberately NOT cards — they read as
 * quiet lifecycle beats in the conversation flow (same weight class as the
 * pending "Working…" line), following the compact console design language.
 */

function kindLabel(kind: string): string {
  return KIND_META[kind as TriggerKind]?.label ?? "Trigger";
}

function KindGlyph({ kind, className }: { kind: string; className?: string }) {
  const Icon = KIND_META[kind as TriggerKind]?.icon;
  return Icon ? <Icon className={className} aria-hidden /> : <Zap className={className} aria-hidden />;
}

export function TriggerTranscriptRow({ trigger }: { trigger: TriggerTranscriptMeta }) {
  const label = kindLabel(trigger.kind);
  const fired = trigger.action === "fired";
  // A fired trigger woke the session — mark it with the accent so it reads as
  // "something happened here"; an armed trigger is a quieter muted note.
  const tone = fired
    ? "text-[hsl(var(--primary-text))]"
    : "text-[hsl(var(--muted-foreground))]";
  // Adjacent same-identity fires are folded into this one row by
  // coalesceAdjacentTriggers (ConversationTimeline) — count is only ever set
  // above 1, so a lone occurrence renders identically to before grouping
  // existed.
  const count = trigger.count ?? 1;
  const suffix = count > 1 ? ` ×${count} times` : "";
  const title =
    count > 1 && trigger.firstTs
      ? `First at ${RelativeTime.tooltip(trigger.firstTs)}`
      : undefined;
  return (
    <div
      className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]"
      role="note"
      title={title}
    >
      <span
        className={`inline-flex items-center gap-1.5 font-medium ${tone}`}
      >
        {fired ? (
          <Zap className="w-3.5 h-3.5" aria-hidden />
        ) : (
          <KindGlyph kind={trigger.kind} className="w-3.5 h-3.5" />
        )}
        {fired ? `${label} fired${suffix}` : `Armed ${label.toLowerCase()}${suffix}`}
      </span>
      {trigger.summary && (
        <>
          <span aria-hidden className="opacity-40">
            ·
          </span>
          <span className="truncate font-mono opacity-80">{trigger.summary}</span>
        </>
      )}
    </div>
  );
}

export function SessionTerminatedDivider({ ts }: { ts?: string }) {
  return (
    <div className="flex items-center gap-3" role="separator">
      <span className="h-px flex-1 bg-[hsl(var(--border))]" aria-hidden />
      <span className="inline-flex items-center gap-1.5 rounded-full border border-[hsl(var(--destructive)/0.3)] bg-[hsl(var(--destructive)/0.05)] px-2.5 py-0.5 text-2xs font-medium text-[hsl(var(--destructive-text))]">
        <Ban className="w-3 h-3" aria-hidden />
        Session terminated
        {ts && (
          <span
            className="text-[hsl(var(--muted-foreground))]"
            title={RelativeTime.tooltip(ts)}
          >
            · {RelativeTime.format(ts)}
          </span>
        )}
      </span>
      <span className="h-px flex-1 bg-[hsl(var(--border))]" aria-hidden />
    </div>
  );
}
