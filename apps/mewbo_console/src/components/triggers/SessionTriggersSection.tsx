import { useLocation } from "wouter";
import { ChevronRight } from "lucide-react";
import { isActiveTrigger, type TriggerDTO } from "../../api/triggers";
import { useSessionTriggers } from "../../hooks/useTriggers";
import { RelativeTime } from "../../utils/relativeTime";
import { KIND_META, STATUS_META } from "./triggerFormat";

/**
 * Armed-triggers strip on a session view. Shows the session's
 * still-firing triggers (armed / paused) as compact chips — kind glyph + next
 * fire — and links to the management page filtered to this session. Renders
 * nothing when the session has no live triggers, so a session without any stays
 * uncluttered.
 */
export function SessionTriggersSection({ sessionId }: { sessionId: string }) {
  const [, navigate] = useLocation();
  const { triggers } = useSessionTriggers(sessionId);
  const live = triggers.filter(isActiveTrigger);
  if (live.length === 0) return null;

  const goToManage = () =>
    navigate(`/settings?facet=automation&session=${encodeURIComponent(sessionId)}`);

  return (
    <div className="px-6 pt-4">
      <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 px-3 py-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-medium text-[hsl(var(--muted-foreground))]">
            Armed triggers
          </span>
          {live.map((t) => (
            <TriggerChip key={t.id} trigger={t} onClick={goToManage} />
          ))}
          <button
            type="button"
            onClick={goToManage}
            className="ml-auto inline-flex items-center gap-0.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
          >
            Manage
            <ChevronRight className="w-3 h-3" />
          </button>
        </div>
      </div>
    </div>
  );
}

function TriggerChip({ trigger, onClick }: { trigger: TriggerDTO; onClick: () => void }) {
  const kind = KIND_META[trigger.kind] ?? KIND_META["time.at"];
  const status = STATUS_META[trigger.status];
  const KindIcon = kind.icon;
  const next = trigger.next_fire_at ? RelativeTime.format(trigger.next_fire_at) : null;
  const paused = trigger.status === "paused";
  return (
    <button
      type="button"
      onClick={onClick}
      title={`${kind.label}${next ? ` · next ${next}` : ""}${paused ? " · paused" : ""}`}
      className="inline-flex items-center gap-1.5 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-2 py-0.5 text-xs text-[hsl(var(--foreground))] hover:border-[hsl(var(--primary))]/40 transition-colors"
    >
      <KindIcon className={`w-3 h-3 ${paused ? "text-[hsl(var(--muted-foreground))]" : "text-[hsl(var(--primary))]"}`} aria-hidden />
      <span className="font-medium">{kind.label}</span>
      {next && (
        <span className="text-[hsl(var(--muted-foreground))]">· {next}</span>
      )}
      {paused && (
        <span className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">{status?.label}</span>
      )}
    </button>
  );
}
