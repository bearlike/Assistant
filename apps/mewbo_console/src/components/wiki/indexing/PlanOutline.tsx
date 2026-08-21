/**
 * The declared plan, disclosed one phase at a time.
 *
 * Rendering every declared step flat is what turned this surface into a wall:
 * twenty-odd rows on a mid-size repository, growing with each phase, each one
 * shouting its state in a caps-locked column. A reader watching an index wants
 * one thing — what is happening now — and occasionally a second — what is left.
 * So a phase is a single row until it is the one doing work, and the run
 * carries the outline with it as phases advance.
 *
 * The whole outline lives inside its own bounded scroll container, never the
 * page: a loading screen that grows a scrollbar as it works is the defect this
 * screen was rebuilt to remove.
 */
import { useEffect, useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

import { IndexingProgress } from "../progress";
import { StepGlyph } from "./StepGlyph";
import { stepTone } from "./stepState";
import { IndexingPlan } from "./planModel";
import type { PhaseEntry } from "./planModel";

export function PlanOutline({
  plan,
  now,
  className,
}: {
  plan: IndexingPlan;
  /** Injected clock — a caller ticking it is what advances the elapsed
   *  readouts, and a test can pass a fixed value instead of faking timers. */
  now: number;
  className?: string;
}) {
  const activeKey = plan.activePhaseKey;
  // Following the run is the default: the outline re-points at the phase doing
  // work each time that changes. A reader who opens another phase keeps it
  // until the run moves on, which is minutes — long enough to read, short
  // enough that the outline never drifts away from the live work.
  const [openKeys, setOpenKeys] = useState<ReadonlySet<string>>(
    () => new Set(activeKey ? [activeKey] : []),
  );
  useEffect(() => {
    if (activeKey) setOpenKeys(new Set([activeKey]));
  }, [activeKey]);

  const phases = useMemo(() => plan.phases, [plan]);

  return (
    <div className={cn("flex min-h-0 flex-col", className)} data-region="plan">
      <div className="flex shrink-0 items-baseline justify-between gap-2 px-4 pb-2 pt-3">
        <h2 className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          Plan
        </h2>
        <span className="text-2xs tabular-nums text-[hsl(var(--muted-foreground))]">
          {phases.filter((phase) => phase.state === "done" || phase.state === "skipped").length} of{" "}
          {phases.length} stages
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3" data-scroll="pane">
        <ul className="space-y-0.5">
          {phases.map((phase) => (
            <PhaseRow
              key={phase.key}
              phase={phase}
              now={now}
              open={openKeys.has(phase.key)}
              onToggle={() =>
                setOpenKeys((prev) => {
                  const next = new Set(prev);
                  if (next.has(phase.key)) next.delete(phase.key);
                  else next.add(phase.key);
                  return next;
                })
              }
            />
          ))}
        </ul>
      </div>
    </div>
  );
}

function PhaseRow({
  phase,
  now,
  open,
  onToggle,
}: {
  phase: PhaseEntry;
  now: number;
  open: boolean;
  onToggle: () => void;
}) {
  const expandable = phase.steps.length > 0;
  const elapsed =
    phase.elapsedSeconds == null ? "" : IndexingProgress.formatElapsed(phase.elapsedSeconds);

  return (
    <li>
      <button
        type="button"
        onClick={expandable ? onToggle : undefined}
        aria-expanded={expandable ? open : undefined}
        disabled={!expandable}
        className={cn(
          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors",
          expandable
            ? "hover:bg-[hsl(var(--muted))]/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[hsl(var(--ring))]"
            : "cursor-default",
        )}
      >
        <ChevronRight
          className={cn(
            "h-3 w-3 shrink-0 text-[hsl(var(--muted-foreground))] transition-transform",
            open && "rotate-90",
            !expandable && "opacity-0",
          )}
          aria-hidden
        />
        <StepGlyph state={phase.state} />
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-sm",
            phase.state === "running"
              ? cn("font-medium", stepTone("running"))
              : phase.state === "pending"
                ? "text-[hsl(var(--muted-foreground))]"
                : "text-[hsl(var(--foreground))]",
          )}
        >
          {phase.label}
        </span>
        <span className="shrink-0 text-2xs tabular-nums text-[hsl(var(--muted-foreground))]">
          {expandable ? `${phase.settled}/${phase.steps.length}` : ""}
          {elapsed && <span className="ml-1.5 opacity-70">{elapsed}</span>}
        </span>
      </button>

      {open && expandable && (
        <ul className="mb-1 ml-[1.4rem] space-y-px border-l border-[hsl(var(--border))] pl-2">
          {phase.steps.map((step) => {
            const detail = IndexingPlan.stepDetail(step, now);
            const running = step.state === "running";
            return (
              <li
                key={step.key}
                className={cn(
                  "flex gap-2 rounded-md px-2 py-1",
                  // The open step stacks its counter under its label. Only one
                  // step runs at a time, so the extra row is cheap — and it is
                  // the one row whose label must never be cut, since it is the
                  // answer to "what is happening right now".
                  running ? "flex-col items-stretch" : "items-center",
                )}
                title={step.note ? `${step.label} — ${step.note}` : step.label}
              >
                <span className="flex min-w-0 items-center gap-2">
                  <StepGlyph state={step.state} className="scale-[0.85]" />
                  <span
                    className={cn(
                      "min-w-0 flex-1 truncate text-xs",
                      running
                        ? cn("font-medium", stepTone("running"))
                        : step.state === "pending"
                          ? "text-[hsl(var(--muted-foreground))] opacity-70"
                          : "text-[hsl(var(--muted-foreground))]",
                    )}
                  >
                    {step.label}
                  </span>
                  {detail && !running && (
                    <span className="shrink-0 text-2xs tabular-nums text-[hsl(var(--muted-foreground))]">
                      {detail}
                    </span>
                  )}
                </span>
                {detail && running && (
                  <span className="ml-[1.35rem] text-2xs tabular-nums text-[hsl(var(--muted-foreground))]">
                    {detail}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}
