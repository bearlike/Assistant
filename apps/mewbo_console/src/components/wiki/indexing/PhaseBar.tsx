/**
 * The run's shape in one strip: seven named segments, filled as each phase
 * settles. It is the ONLY positional progress readout on the screen — the
 * screen previously carried a plain bar AND a dotted phase strip saying the
 * same thing twice, which is what made the header read as scattered.
 *
 * Segments are equal width on purpose. They answer "which stage am I in, and
 * how many are left" — a question about ORDER. The precise share of the run is
 * the percentage in the header, which comes from the backend's own weights;
 * sizing segments by weight would put a second, disagreeing number on screen.
 */
import { cn } from "@/lib/utils";

import { stepTone } from "./stepState";
import type { PhaseEntry } from "./planModel";

/** Fill for one segment: solid when settled, partial while its steps run. */
function fillFraction(phase: PhaseEntry): number {
  if (phase.state === "done" || phase.state === "skipped") return 1;
  if (phase.state === "failed") return 1;
  if (phase.state !== "running") return 0;
  if (phase.steps.length === 0) return 0.5;
  return Math.min(0.95, Math.max(0.08, phase.settled / phase.steps.length));
}

export function PhaseBar({
  phases,
  stopped,
  className,
}: {
  phases: PhaseEntry[];
  /** A run that ended incomplete freezes amber, so the fill reads as
   *  "what completed" rather than as live progress. */
  stopped: boolean;
  className?: string;
}) {
  return (
    <ol className={cn("flex items-stretch gap-1", className)}>
      {phases.map((phase) => {
        const fill = fillFraction(phase);
        const active = phase.state === "running";
        return (
          <li key={phase.key} className="min-w-0 flex-1">
            <div
              className="h-1.5 overflow-hidden rounded-full bg-[hsl(var(--muted))]/60"
              role="presentation"
            >
              <div
                className={cn(
                  "h-full rounded-full transition-[width] duration-500",
                  stopped
                    ? "bg-[hsl(var(--warning))]/70"
                    : phase.state === "failed"
                      ? "bg-[hsl(var(--destructive))]/70"
                      : phase.state === "skipped"
                        ? "bg-[hsl(var(--muted-foreground))]/40"
                        : "bg-[hsl(var(--primary))]",
                )}
                style={{ width: `${Math.round(fill * 100)}%` }}
              />
            </div>
            <div
              className={cn(
                "mt-1.5 truncate text-2xs",
                active
                  ? cn("font-medium", stopped ? "text-[hsl(var(--warning))]" : stepTone("running"))
                  : phase.state === "pending"
                    ? "text-[hsl(var(--muted-foreground))] opacity-60"
                    : "text-[hsl(var(--muted-foreground))]",
              )}
              title={phase.label}
            >
              {phase.label}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
