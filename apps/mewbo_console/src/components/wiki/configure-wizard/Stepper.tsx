/**
 * Three-step header chips. Numbered chips become a checkmark when complete;
 * the active chip gets the clay halo. Finished steps can be clicked to jump
 * back; future steps are disabled.
 */

import { Check } from "lucide-react";

import { cn } from "@/lib/utils";

interface StepperProps {
  steps: Array<{ id: string; label: string; sub: string }>;
  current: number;
  onJump: (index: number) => void;
}

export function Stepper({ steps, current, onJump }: StepperProps) {
  return (
    <div className="flex items-center gap-2 mt-4" role="tablist">
      {steps.map((s, i) => {
        const state: "future" | "active" | "done" =
          i < current ? "done" : i === current ? "active" : "future";
        return (
          <div key={s.id} className="flex items-center gap-2 flex-1">
            <button
              type="button"
              role="tab"
              aria-selected={i === current}
              disabled={state === "future"}
              onClick={() => i < current && onJump(i)}
              className={cn(
                "inline-flex items-center gap-2.5 px-2 py-1 rounded-md transition-colors text-left",
                state === "future" && "opacity-50 cursor-not-allowed",
                state !== "future" && "cursor-pointer hover:bg-[hsl(var(--accent))]/30"
              )}
            >
              <span
                className={cn(
                  "inline-flex items-center justify-center w-6 h-6 rounded-full text-[11px] font-semibold shrink-0",
                  state === "done" && "bg-[hsl(var(--primary))]/15 text-[hsl(var(--primary))]",
                  state === "active" &&
                    "bg-[hsl(var(--primary))] text-white shadow-[0_0_0_4px_hsl(var(--primary)/0.15)]",
                  state === "future" &&
                    "bg-[hsl(var(--muted))]/60 text-[hsl(var(--muted-foreground))]"
                )}
              >
                {state === "done" ? <Check className="h-3 w-3" /> : i + 1}
              </span>
              <span className="leading-tight">
                <span
                  className={cn(
                    "block text-xs font-medium",
                    state === "active" && "text-[hsl(var(--primary))]"
                  )}
                >
                  {s.label}
                </span>
                <span className="block text-[10px] text-[hsl(var(--muted-foreground))]">
                  {s.sub}
                </span>
              </span>
            </button>
            {i < steps.length - 1 && (
              <span
                aria-hidden
                className={cn(
                  "flex-1 h-px",
                  i < current ? "bg-[hsl(var(--primary))]/30" : "bg-[hsl(var(--border))]"
                )}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
