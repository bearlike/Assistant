import { Clock } from "lucide-react";

import type { TriggerDTO } from "../../../api/triggers";
import type { AppSystemHealth } from "../../../api/apps";
import type { AppSpec } from "../../../types/apps";
import { RelativeTime } from "../../../utils/relativeTime";
import { KIND_META, STATUS_META, triggerArgsSummary } from "../../triggers/triggerFormat";
import { RailEmpty, RailList, RailMeta, RailNote, RailRow, railLabelIconCls } from "./rows";

export function TriggersBody({
  status,
  system,
}: {
  status: AppSpec["status"];
  system: AppSystemHealth | undefined;
}) {
  const triggers = system?.triggers ?? [];
  return (
    <>
      {status === "paused" && (
        <RailNote tone="warning" wrap className="px-2 pb-1.5">
          Paused — triggers won't fire until you resume.
        </RailNote>
      )}
      {triggers.length === 0 ? (
        <RailEmpty>No triggers armed.</RailEmpty>
      ) : (
        <RailList>
          {triggers.map((t) => (
            <TriggerRow key={t.id} trigger={t} />
          ))}
        </RailList>
      )}
    </>
  );
}

function TriggerRow({ trigger }: { trigger: TriggerDTO }) {
  const nextFire = trigger.next_fire_at;
  const paused = trigger.status === "paused";
  // The console already owns ONE trigger vocabulary — reuse it rather than
  // teaching this rail a second one. `KIND_META`/`STATUS_META` give the human
  // label ("Cron schedule", not the raw `time.cron`), and the raw args summary
  // is where the machine text actually lives. Both are indexed by a closed
  // union, but a server ahead of this build could still send an unknown value,
  // so each read falls back to the wire string rather than crashing the rail.
  const kindLabel = KIND_META[trigger.kind]?.label ?? trigger.kind;
  const statusLabel = STATUS_META[trigger.status]?.label ?? trigger.status;
  const args = triggerArgsSummary(trigger);
  return (
    <RailRow
      label={kindLabel}
      labelTitle={kindLabel}
      trailing={
        paused ? (
          <RailMeta tone="warning">{statusLabel}</RailMeta>
        ) : nextFire ? (
          <RailMeta
            tone="muted"
            title={RelativeTime.tooltip(nextFire)}
            icon={<Clock aria-hidden className={railLabelIconCls} />}
          >
            {RelativeTime.format(nextFire)}
          </RailMeta>
        ) : (
          <RailMeta tone="muted">{statusLabel}</RailMeta>
        )
      }
    >
      {/* The cron expression / repo · workflow pair — the one string in this
          rail a user would genuinely select and paste, so the one that earns
          monospace. Empty when the kind carries nothing summarisable. */}
      {args && (
        <RailNote title={args} className="font-mono">
          {args}
        </RailNote>
      )}
    </RailRow>
  );
}
