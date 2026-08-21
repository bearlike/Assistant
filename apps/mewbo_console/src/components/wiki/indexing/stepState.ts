/**
 * The one place a step or phase state becomes a tone and a word.
 *
 * Three surfaces read a state — the phase bar, the plan outline and the header
 * — and a state that renders as three different colours across them is worse
 * than no colour at all.
 *
 * Kept apart from `StepGlyph.tsx` because a `.tsx` exporting both components
 * and plain functions trips `react-refresh/only-export-components`, and the
 * console's lint gate runs at `--max-warnings=0`.
 */
import type { PhaseState } from "./planModel";

const WORD: Record<PhaseState, string> = {
  pending: "Not started",
  running: "In progress",
  done: "Done",
  skipped: "Skipped",
  failed: "Failed",
};

/** Text colour for a state — the tone shared by a glyph and its row. */
export function stepTone(state: PhaseState): string {
  switch (state) {
    case "failed":
      return "text-[hsl(var(--destructive-text))]";
    case "running":
      return "text-[hsl(var(--primary-text))]";
    case "done":
      return "text-[hsl(var(--success))]";
    default:
      return "text-[hsl(var(--muted-foreground))]";
  }
}

/** The state as words, so a readout is never colour or glyph alone. */
export function stepWord(state: PhaseState): string {
  return WORD[state];
}
