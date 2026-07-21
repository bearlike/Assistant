/**
 * Configure wizard. Mandatory before indexing. Three steps:
 *   1. Source        repo URL · platform · access token (optional)
 *   2. Generation    wiki depth · language · model
 *   3. Scope         filter mode · exclude/include paths (optional)
 *
 * Forward navigation is gated by validation; back is free. Summary strip
 * appears from step 2 onward. Submitting transitions to the indexing job.
 *
 * All wizard state/validation/navigation/submit logic lives in
 * `configure-wizard/wizardState.ts`'s `useWizardMachine()` — this file is
 * the render shell only (stepper + active step + summary strip + footer).
 */

import { useLocation } from "wouter";
import { ArrowLeft, BookOpen, ChevronRight, Database, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";

import { ModelChip } from "./ModelPicker";
import { WikiTopBar } from "./WikiTopBar";
import { StepGeneration } from "./configure-wizard/StepGeneration";
import { StepScope } from "./configure-wizard/StepScope";
import { StepSource } from "./configure-wizard/StepSource";
import { PlatformIcon } from "./configure-wizard/PlatformIcon";
import { Stepper } from "./configure-wizard/Stepper";
import { useWizardMachine } from "./configure-wizard/wizardState";
import { buildHref } from "./router";

interface ConfigureWizardProps {
  initialUrl?: string;
}

export function ConfigureWizard({ initialUrl = "" }: ConfigureWizardProps) {
  const [, navigate] = useLocation();
  const {
    platformList,
    languageList,
    modelList,
    developerMode,
    state,
    set,
    errors,
    catalogError,
    platform,
    gitSlug,
    branches,
    step,
    setStep,
    STEPS,
    goNext,
    goBack,
    onSubmit,
    isPending,
  } = useWizardMachine({ initialUrl });

  return (
    <div className="flex flex-col flex-1 overflow-y-auto">
      <WikiTopBar showBackToAll />
      <div className="flex-1 px-4 sm:px-6 py-8">
        <div className="max-w-[980px] mx-auto">
          <button
            type="button"
            onClick={() => navigate(buildHref({ kind: "landing" }))}
            className="inline-flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] mb-3"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Back to all wikis
          </button>

          <div className={cn(cardSurface({ radius: "modal", elevation: "elev-3" }), "overflow-hidden")}>
            {/* Header */}
            <div className="px-6 pt-4 pb-4 border-b border-[hsl(var(--border))]">
              <span className="inline-flex items-center gap-1 px-2 h-6 rounded-full border border-[hsl(var(--primary))]/30 bg-[hsl(var(--primary))]/10 text-2xs text-[hsl(var(--primary-text))]">
                <Sparkles className="h-3 w-3" />
                New wiki
              </span>
              <h1 className="mt-2 text-2xl font-semibold tracking-[-0.02em]">
                Configure indexing
              </h1>
              <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">
                A few quick questions so the wiki matches your repo and your stack.
              </p>
              <Stepper steps={STEPS} current={step} onNavigate={setStep} />
            </div>

            {/* Pane */}
            <div className="px-6 py-6">
              {step === 0 && (
                <StepSource
                  state={state}
                  set={set}
                  errors={errors}
                  platform={platform}
                  platforms={platformList}
                  catalogError={catalogError}
                />
              )}
              {step === 1 && state.sourceType === "git" && (
                <StepGeneration
                  state={state}
                  set={set}
                  languages={languageList}
                  models={modelList}
                  branches={branches.data?.branches ?? []}
                  defaultBranch={branches.data?.defaultBranch ?? null}
                  branchesLoading={branches.isLoading}
                  developerMode={developerMode}
                />
              )}
              {step === 2 && state.sourceType === "git" && (
                <StepScope state={state} set={set} />
              )}
            </div>

            {/* Summary strip — git mode only */}
            {state.sourceType === "git" && step > 0 && platform && state.model && (
              <div className="px-6 py-3 bg-[hsl(var(--muted))]/30 flex items-center gap-3 flex-wrap text-xs">
                <span className="inline-flex items-center gap-1.5">
                  <span
                    aria-hidden
                    className="inline-flex items-center justify-center w-5 h-5 rounded"
                    style={{ background: platform.color }}
                  >
                    <PlatformIcon platformId={platform.id} className="h-3 w-3 text-white" />
                  </span>
                  <span className="font-mono text-[hsl(var(--foreground))]">
                    {gitSlug ?? "—"}
                  </span>
                </span>
                <span aria-hidden className="text-[hsl(var(--muted-foreground))]">·</span>
                <span className="inline-flex items-center gap-1.5 text-[hsl(var(--muted-foreground))]">
                  <BookOpen className="h-3 w-3" />
                  {state.depth === "comprehensive" ? "Comprehensive" : "Concise"}
                </span>
                <span aria-hidden className="text-[hsl(var(--muted-foreground))]">·</span>
                <ModelChip modelId={state.model} />
              </div>
            )}

            {/* Footer */}
            <div className="px-6 py-3 border-t border-[hsl(var(--border))] flex items-center justify-between">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={step === 0 ? () => navigate(buildHref({ kind: "landing" })) : goBack}
                leadingIcon={<ArrowLeft className="h-3.5 w-3.5" />}
              >
                {step === 0 ? "Cancel" : "Back"}
              </Button>

              <div className="inline-flex items-center gap-1.5" aria-hidden>
                {STEPS.map((_, i) => (
                  <span
                    key={i}
                    className={cn(
                      "h-1.5 rounded-full transition-all",
                      i === step
                        ? "w-6 bg-[hsl(var(--primary))]"
                        : i < step
                        ? "w-1.5 bg-[hsl(var(--primary))]/60"
                        : "w-1.5 bg-[hsl(var(--border-strong))]"
                    )}
                  />
                ))}
              </div>

              {step < STEPS.length - 1 ? (
                <Button
                  type="button"
                  variant="primary"
                  size="sm"
                  onClick={goNext}
                  trailingIcon={<ChevronRight className="h-3.5 w-3.5" />}
                >
                  Continue
                </Button>
              ) : state.sourceType === "catalog" ? (
                <Button
                  type="button"
                  variant="primary"
                  size="sm"
                  onClick={onSubmit}
                  disabled={isPending}
                  leadingIcon={<Database className="h-3.5 w-3.5" />}
                >
                  {isPending ? "Ingesting…" : "Create workspace"}
                </Button>
              ) : (
                <Button
                  type="button"
                  variant="primary"
                  size="sm"
                  onClick={onSubmit}
                  disabled={isPending}
                  leadingIcon={<Sparkles className="h-3.5 w-3.5" />}
                >
                  Generate Wiki
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
