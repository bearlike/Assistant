/**
 * ConfigureWizard step 3 (git only) — scope: include/exclude filter mode
 * and the dirs/files textareas.
 */

import { CheckCircle2, RotateCcw, Sparkles, XCircle } from "lucide-react";

import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

import { getDefaultExclusions } from "../api/client";
import { Field } from "./Field";
import type { WizardState } from "./wizardState";

export function StepScope({
  state,
  set,
}: {
  state: WizardState;
  set: (patch: Partial<WizardState>) => void;
}) {
  const defaults = getDefaultExclusions();
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-medium">Anything to skip?</h2>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
          Trim the indexer to focus on what matters. Defaults already exclude lockfiles,
          build output, and vendored code.
        </p>
        <span className="inline-flex items-center gap-1 mt-2 px-2 h-5 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-2xs text-[hsl(var(--muted-foreground))]">
          <Sparkles className="size-3 text-[hsl(var(--primary))]" />
          Optional — defaults are fine for most repos
        </span>
      </div>

      <Field label="Filter mode" required>
        <div className="grid gap-3 grid-cols-1 sm:grid-cols-2">
          <ModeCard
            icon={<XCircle className="h-4 w-4" />}
            title="Exclude paths"
            desc="Default — process everything except matches"
            selected={state.filterMode === "exclude"}
            onSelect={() => set({ filterMode: "exclude" })}
          />
          <ModeCard
            icon={<CheckCircle2 className="h-4 w-4" />}
            title="Include only"
            desc="Process only paths that match"
            selected={state.filterMode === "include"}
            onSelect={() => set({ filterMode: "include" })}
          />
        </div>
      </Field>

      <div className="grid gap-3 grid-cols-1 sm:grid-cols-2">
        <Field
          label={
            state.filterMode === "include"
              ? "Directories to include"
              : "Directories to exclude"
          }
          hint="One per line · glob or path prefix"
        >
          <Textarea
            rows={7}
            spellCheck={false}
            placeholder={
              state.filterMode === "include" ? "src/\napps/\nlibs/" : "node_modules\ndist\n.git"
            }
            value={state.dirs}
            onChange={(e) => set({ dirs: e.target.value })}
            className="w-full font-mono shadow-none bg-[hsl(var(--muted))] border border-[hsl(var(--border))] rounded-lg p-3 focus-visible:border-[hsl(var(--border-strong))] resize-none"
          />
          <button
            type="button"
            onClick={() => set({ dirs: defaults.dirs })}
            className="inline-flex items-center gap-1 text-2xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] mt-1.5"
          >
            <RotateCcw className="h-3 w-3" />
            Load default exclusions
          </button>
        </Field>
        <Field
          label={
            state.filterMode === "include" ? "Files to include" : "Files to exclude"
          }
          hint="One per line · glob accepted"
        >
          <Textarea
            rows={7}
            spellCheck={false}
            placeholder={
              state.filterMode === "include"
                ? "**/*.py\n**/*.ts\n**/*.md"
                : "*.lock\n*.min.js\n*.map"
            }
            value={state.files}
            onChange={(e) => set({ files: e.target.value })}
            className="w-full font-mono shadow-none bg-[hsl(var(--muted))] border border-[hsl(var(--border))] rounded-lg p-3 focus-visible:border-[hsl(var(--border-strong))] resize-none"
          />
          <button
            type="button"
            onClick={() => set({ files: defaults.files })}
            className="inline-flex items-center gap-1 text-2xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] mt-1.5"
          >
            <RotateCcw className="h-3 w-3" />
            Load default exclusions
          </button>
        </Field>
      </div>
    </div>
  );
}

function ModeCard({
  icon,
  title,
  desc,
  selected,
  onSelect,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={cn(
        "flex items-start gap-3 p-3.5 rounded-lg border text-left transition-all",
        selected
          ? "border-[hsl(var(--primary))] bg-[hsl(var(--primary))]/[0.06] ring-1 ring-[hsl(var(--primary))]/30"
          : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--border-strong))]"
      )}
    >
      <span className="text-[hsl(var(--primary))] mt-0.5">{icon}</span>
      <span className="flex-1 min-w-0">
        <span className="block text-sm font-medium">{title}</span>
        <span className="block text-2xs text-[hsl(var(--muted-foreground))]">{desc}</span>
      </span>
    </button>
  );
}
