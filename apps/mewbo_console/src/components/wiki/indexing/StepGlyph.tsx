/**
 * A step or phase state as one glyph.
 *
 * The word travels with the glyph as screen-reader text, so the readout is
 * never colour alone without spending a caps-locked column on every row —
 * twenty rows of `DONE` was the shouting this replaced. The pending marker is
 * a ring rather than an icon so an untouched step reads as an empty slot
 * instead of a symbol to interpret.
 */
import { Check, CircleSlash, Loader2, TriangleAlert } from "lucide-react";

import { cn } from "@/lib/utils";

import type { PhaseState } from "./planModel";
import { stepTone, stepWord } from "./stepState";

export function StepGlyph({
  state,
  className,
}: {
  state: PhaseState;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center justify-center",
        stepTone(state),
        className,
      )}
    >
      <span className="sr-only">{stepWord(state)}</span>
      {state === "running" ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
      ) : state === "done" ? (
        <Check className="h-3.5 w-3.5" aria-hidden />
      ) : state === "failed" ? (
        <TriangleAlert className="h-3.5 w-3.5" aria-hidden />
      ) : state === "skipped" ? (
        <CircleSlash className="h-3.5 w-3.5" aria-hidden />
      ) : (
        <span className="h-2 w-2 rounded-full border border-current opacity-50" aria-hidden />
      )}
    </span>
  );
}
