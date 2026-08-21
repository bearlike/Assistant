/**
 * ConfigureWizard step 2 — generation: wiki depth, language, model, branch
 * picker, and the developer-mode graph-only opt-in.
 */

import { BookOpen, ChevronDown, GitBranch, Globe, Network, Zap } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

import { ModelPicker } from "../ModelPicker";
import { Field } from "./Field";
import type { WizardState } from "./wizardState";

export function StepGeneration({
  state,
  set,
  languages,
  branches,
  defaultBranch,
  branchesLoading,
  developerMode,
  embeddingDefault,
}: {
  state: WizardState;
  set: (patch: Partial<WizardState>) => void;
  languages: Array<{ id: string; label: string; subtle?: string }>;
  /** Remote branches for the branch picker (empty until loaded / on error). */
  branches: string[];
  /** Repo default branch, when known — labels the "Default" option. */
  defaultBranch: string | null;
  /** True while the branch list is in flight. */
  branchesLoading: boolean;
  /** Gated developer-mode features (graph-only indexing). */
  developerMode: boolean;
  /** Server fallback shown when the project leaves this model unset. */
  embeddingDefault?: string;
}) {
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-medium">How should the wiki read?</h2>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
          Pick a depth, language, and the model that does the writing. You can re-generate
          any time.
        </p>
      </div>

      <Field label="Wiki depth" required>
        <div className="grid gap-3 grid-cols-1 sm:grid-cols-2">
          <DepthCard
            icon={<BookOpen className="h-4 w-4" />}
            title="Comprehensive"
            meta="~20–40 pages · 8–15 min"
            desc="Full coverage with architecture, module guides, API reference, and call-graph diagrams."
            tag={{ label: "Recommended", tone: "primary" }}
            selected={state.depth === "comprehensive"}
            onSelect={() => set({ depth: "comprehensive" })}
          />
          <DepthCard
            icon={<Zap className="h-4 w-4" />}
            title="Concise"
            meta="~6–10 pages · 2–4 min"
            desc="Fast tour: what the project does, key entry points, how to run it. Skips deep internals."
            tag={{ label: "Faster", tone: "info" }}
            selected={state.depth === "concise"}
            onSelect={() => set({ depth: "concise" })}
          />
        </div>
      </Field>

      <div className="grid gap-3 grid-cols-1 sm:grid-cols-2">
        <Field label="Wiki language" required>
          <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))] relative">
            <Globe className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
            <select
              value={state.language}
              onChange={(e) => set({ language: e.target.value })}
              className="flex-1 bg-transparent text-sm outline-none appearance-none pr-6 cursor-pointer text-[hsl(var(--foreground))]"
            >
              {languages.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.label}
                  {l.subtle ? ` · ${l.subtle}` : ""}
                </option>
              ))}
            </select>
            <ChevronDown className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] pointer-events-none absolute right-3" />
          </div>
        </Field>

        <Field
          label="Model"
          required
          hint="Used to author every page; add fallbacks for ones to retry if it keeps failing"
        >
          <ModelPicker
            value={state.model}
            onChange={(v) => set({ model: v })}
            variant="full"
            fallbackEnabled={state.fallbackEnabled}
            fallbackModels={state.fallbackModels}
            onFallbackEnabledChange={(on) => set({ fallbackEnabled: on })}
            onFallbackModelsChange={(next) => set({ fallbackModels: next })}
          />
        </Field>
      </div>

      <Field
        label="Embedding model"
        hint="optional · leave blank to inherit the deployment default"
      >
        <Input
          aria-label="Embedding model"
          value={state.embeddingModel}
          onChange={(e) => set({ embeddingModel: e.target.value })}
          placeholder={embeddingDefault ?? "Deployment default"}
        />
      </Field>

      <Field label="Branch">
        <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))] relative">
          <GitBranch className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
          <select
            aria-label="Branch"
            value={state.ref}
            onChange={(e) => set({ ref: e.target.value })}
            disabled={branchesLoading}
            className="flex-1 bg-transparent text-sm outline-none appearance-none pr-6 cursor-pointer text-[hsl(var(--foreground))] disabled:cursor-default"
          >
            {branchesLoading ? (
              <option value="">Loading branches…</option>
            ) : (
              <>
                <option value="">
                  {defaultBranch ? `Default · ${defaultBranch}` : "Default branch"}
                </option>
                {branches.map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
              </>
            )}
          </select>
          <ChevronDown className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] pointer-events-none absolute right-3" />
        </div>
      </Field>

      {/* Both fields are OPTIONAL and inert when blank — an untouched wizard
          submits neither key, leaving the submission byte-identical to one made
          before they existed (`wizardState.ts`'s omit-when-inert rule). */}
      <Field
        label="Indexing instructions"
        hint="optional · standing guidance for every page"
      >
        <Textarea
          value={state.customInstructions}
          onChange={(e) => set({ customInstructions: e.target.value })}
          rows={4}
          maxLength={4000}
          placeholder={
            "This is a Kotlin Android client. Describe the Compose layer in UI terms, and name the config key each page reads."
          }
          className="w-full bg-[hsl(var(--muted))] border-[hsl(var(--border))] rounded-lg p-2.5 resize-none"
        />
      </Field>

      <Field
        label="External MCP servers"
        hint="optional · .mcp.json shape · only add servers you trust"
      >
        <Textarea
          value={state.mcpServers}
          onChange={(e) => set({ mcpServers: e.target.value })}
          rows={5}
          spellCheck={false}
          placeholder={
            '{\n  "schema-registry": {\n    "url": "https://registry.example.com/mcp"\n  }\n}'
          }
          className="w-full font-mono text-field md:text-sm bg-[hsl(var(--muted))] border-[hsl(var(--border))] rounded-lg p-2.5 resize-none"
        />
      </Field>

      {/* Developer-mode opt-in: graph-only indexing. Hidden entirely unless
          ``runtime.developer_mode`` is on. */}
      {developerMode && (
        <div className="space-y-3">
          <div>
            <h3 className="text-sm font-medium">Developer mode</h3>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Settings for inspecting indexing without generating docs.
            </p>
          </div>
          <label className="flex items-start gap-3 p-3.5 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 cursor-pointer">
            <span className="text-[hsl(var(--primary))] mt-0.5 shrink-0">
              <Network className="h-4 w-4" />
            </span>
            <span className="flex-1 min-w-0">
              <span className="block text-sm font-medium">
                Graph only — skip documentation (no LLM)
              </span>
              <span className="block text-2xs text-[hsl(var(--muted-foreground))]">
                Build the AST code graph only; no docs are generated.
              </span>
            </span>
            <Switch
              checked={state.graphOnly}
              onCheckedChange={(v) => set({ graphOnly: v })}
              aria-label="Graph only — skip documentation (no LLM)"
              className="mt-0.5 shrink-0"
            />
          </label>
        </div>
      )}
    </div>
  );
}

function DepthCard({
  icon,
  title,
  meta,
  desc,
  tag,
  selected,
  onSelect,
}: {
  icon: React.ReactNode;
  title: string;
  meta: string;
  desc: string;
  tag: { label: string; tone: "primary" | "info" };
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={cn(
        "flex flex-col gap-1.5 text-left p-3.5 rounded-lg border transition-all",
        selected
          ? "border-[hsl(var(--primary))] bg-[hsl(var(--primary))]/[0.06] ring-1 ring-[hsl(var(--primary))]/30"
          : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--border-strong))] hover:bg-[hsl(var(--accent))]/40"
      )}
    >
      <div className="flex items-center gap-2 text-[hsl(var(--foreground))]">
        <span className="text-[hsl(var(--primary))]">{icon}</span>
        <span className="text-sm font-medium">{title}</span>
        <span
          className={cn(
            "ml-auto inline-flex items-center gap-1 px-1.5 h-5 rounded-full text-2xs font-medium",
            tag.tone === "primary"
              ? "bg-[hsl(var(--primary))]/15 text-[hsl(var(--primary-text))]"
              : "bg-[hsl(var(--info))]/15 text-[hsl(var(--info-text))]"
          )}
        >
          {tag.label}
        </span>
      </div>
      <div className="text-2xs text-[hsl(var(--muted-foreground))]">{meta}</div>
      <p className="text-xs text-[hsl(var(--muted-foreground))] [text-wrap:pretty]">{desc}</p>
    </button>
  );
}
