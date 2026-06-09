/**
 * Configure wizard. Mandatory before indexing. Three steps:
 *   1. Source        repo URL · platform · access token (optional)
 *   2. Generation    wiki depth · language · model
 *   3. Scope         filter mode · exclude/include paths (optional)
 *
 * Forward navigation is gated by validation; back is free. Summary strip
 * appears from step 2 onward. Submitting transitions to the indexing job.
 */

import { useEffect, useMemo, useState } from "react";
import { useLocation } from "wouter";
import {
  ArrowLeft,
  BookOpen,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Database,
  Eye,
  EyeOff,
  GitFork,
  Globe,
  Info,
  KeyRound,
  RotateCcw,
  Sparkles,
  XCircle,
  CheckCircle2,
  ExternalLink,
  Zap,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { ModelChip, ModelPicker } from "./ModelPicker";
import { WikiTopBar } from "./WikiTopBar";
import { CatalogDocsForm } from "./CatalogDocsForm";
import { Field } from "./configure-wizard/Field";
import { PlatformIcon } from "./configure-wizard/PlatformIcon";
import { PlatformTile } from "./configure-wizard/PlatformTile";
import { Stepper } from "./configure-wizard/Stepper";
import { getDefaultExclusions, uploadCatalogDocuments } from "./api/client";
import {
  useSubmitWizard,
  useWikiDefaults,
  useWikiLanguages,
  useWikiPlatforms,
} from "./api/hooks";
import { useModels } from "../../hooks/useModels";
import { buildHref } from "./router";
import { slugFromRepoUrl } from "./slug";
import type {
  CatalogDocument,
  Platform,
  WizardSourceType,
  WizardSubmission,
} from "./api/types";

function detectPlatformFromUrl(url: string, platforms: Platform[]): Platform["id"] {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    for (const p of platforms) {
      for (const h of p.hosts) {
        if (host === h || host.endsWith("." + h)) return p.id;
      }
    }
    // Heuristic fallbacks for self-hosted Git servers — the catalog only
    // lists the cloud-default host for each platform, so anything else
    // falls through to a pattern guess.
    if (host.includes("gitea") || host.startsWith("git.") || host === "git") {
      return "gitea";
    }
    if (host.includes("gitlab")) return "gitlab";
    if (host.includes("forgejo") || host.includes("codeberg")) return "gitea";
    // Unknown host → generic git (user can still click another tile).
    return "git";
  } catch {
    // ignore
  }
  return "github";
}

// Slug parsing is centralised in ./slug.ts so the wizard, the landing
// page and the rest of the FE all agree on the canonical
// ``host/owner/repo`` shape.

interface ConfigureWizardProps {
  initialUrl?: string;
}

interface WizardState {
  /** Whether this is a git-backed or catalog (non-git) workspace. */
  sourceType: WizardSourceType;
  // ── git fields ────────────────────────────────────────────────────
  url: string;
  platform: Platform["id"];
  platformLocked: boolean;
  token: string;
  depth: "comprehensive" | "concise";
  language: string;
  model: string;
  filterMode: "exclude" | "include";
  dirs: string;
  files: string;
  // ── catalog fields ────────────────────────────────────────────────
  /** Human-readable workspace name; slugified to produce the project slug. */
  catalogName: string;
  catalogDocs: CatalogDocument[];
}

/** Slugify a free-form workspace name into a valid project slug. */
function slugifyName(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

const GIT_STEPS = [
  { id: "source", label: "Source", sub: "Where the code lives" },
  { id: "generation", label: "Generation", sub: "Depth · language · model" },
  { id: "scope", label: "Scope", sub: "What to index" },
];

const CATALOG_STEPS = [
  { id: "source", label: "Source", sub: "Workspace name & documents" },
];

export function ConfigureWizard({ initialUrl = "" }: ConfigureWizardProps) {
  const [, navigate] = useLocation();
  const platforms = useWikiPlatforms();
  const languages = useWikiLanguages();
  const { defaultModel } = useModels();
  const wikiDefaults = useWikiDefaults();
  const seedModel = wikiDefaults.data?.model || defaultModel;
  const submit = useSubmitWizard();
  const [step, setStep] = useState(0);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [catalogPending, setCatalogPending] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  const platformList = useMemo(() => platforms.data ?? [], [platforms.data]);
  const languageList = useMemo(() => languages.data ?? [], [languages.data]);

  const [state, setState] = useState<WizardState>(() => ({
    sourceType: "git",
    url: initialUrl,
    platform: "github",
    platformLocked: false,
    token: "",
    depth: "comprehensive",
    language: "en",
    model: "",
    filterMode: "exclude",
    dirs: "",
    files: "",
    catalogName: "",
    catalogDocs: [],
  }));

  // Seed the model from wiki.default_model if pinned, else fall back to
  // /api/models' global default. User selection in the picker overrides.
  useEffect(() => {
    if (!state.model && seedModel) {
      setState((s) => ({ ...s, model: seedModel }));
    }
  }, [seedModel, state.model]);

  const gitSlug = useMemo(() => slugFromRepoUrl(state.url), [state.url]);
  const catalogSlug = useMemo(() => slugifyName(state.catalogName), [state.catalogName]);

  const platform =
    platformList.find((p) => p.id === state.platform) ?? platformList[0];

  // Auto-detect platform from URL unless user has explicitly picked one.
  useEffect(() => {
    if (!state.url || state.platformLocked) return;
    if (!platformList.length) return;
    const next = detectPlatformFromUrl(state.url, platformList);
    if (next !== state.platform) {
      setState((s) => ({ ...s, platform: next }));
    }
  }, [state.url, state.platformLocked, platformList, state.platform]);

  const set = (patch: Partial<WizardState>) => setState((s) => ({ ...s, ...patch }));

  // Which steps to show depends on source type.
  const STEPS = state.sourceType === "catalog" ? CATALOG_STEPS : GIT_STEPS;

  const validate = (n: number): boolean => {
    const e: Record<string, string> = {};
    if (n === 0) {
      if (state.sourceType === "git") {
        if (!state.url.trim()) e.url = "Repository URL is required.";
        else if (!gitSlug) e.url = "Doesn't look like a valid repository URL.";
      } else {
        if (!state.catalogName.trim()) e.catalogName = "Workspace name is required.";
        else if (!catalogSlug) e.catalogName = "Name must contain at least one letter or number.";
        if (state.catalogDocs.length === 0) e.catalogDocs = "Add at least one document.";
        else if (state.catalogDocs.some((d) => !d.text.trim())) {
          e.catalogDocs = "All documents must have content.";
        }
      }
    }
    setErrors(e);
    return Object.keys(e).length === 0;
  };

  const goNext = () => {
    if (!validate(step)) return;
    setStep((s) => Math.min(STEPS.length - 1, s + 1));
  };
  const goBack = () => setStep((s) => Math.max(0, s - 1));

  const onSubmit = () => {
    if (state.sourceType === "catalog") {
      onSubmitCatalog();
      return;
    }
    // git path
    if (!validate(0)) {
      setStep(0);
      return;
    }
    if (!gitSlug || !platform) return;
    const payload: WizardSubmission = {
      repoUrl: state.url,
      slug: gitSlug,
      platform: state.platform,
      token: state.token || undefined,
      depth: state.depth,
      language: state.language,
      model: state.model,
      filterMode: state.filterMode,
      dirs: state.dirs.split("\n").map((s) => s.trim()).filter(Boolean),
      files: state.files.split("\n").map((s) => s.trim()).filter(Boolean),
    };
    submit.mutate(payload, {
      onSuccess: (job) => {
        navigate(
          buildHref({
            kind: "indexing",
            jobId: job.jobId,
            slug: gitSlug,
            platform: state.platform,
          })
        );
      },
    });
  };

  const onSubmitCatalog = () => {
    if (!validate(0)) return;
    const slug = catalogSlug;
    if (!slug) return;
    setCatalogPending(true);
    setCatalogError(null);
    uploadCatalogDocuments(slug, state.catalogDocs)
      .then((report) => {
        // Navigate to the Q&A screen for this workspace using the landing page.
        navigate(
          buildHref({
            kind: "page",
            pageId: report.landingPageId,
            slug: report.slug,
          })
        );
      })
      .catch((err: unknown) => {
        const msg =
          err instanceof Error ? err.message : "Upload failed. Please try again.";
        setCatalogError(msg);
        setCatalogPending(false);
      });
  };

  const isPending = submit.isPending || catalogPending;

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

          <div className="rounded-2xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-[0_6px_22px_rgba(0,0,0,0.12)] overflow-hidden">
            {/* Header */}
            <div className="px-6 pt-5 pb-4 border-b border-[hsl(var(--border))]">
              <span className="inline-flex items-center gap-1 px-2 h-6 rounded-full border border-[hsl(var(--primary))]/30 bg-[hsl(var(--primary))]/10 text-[10px] text-[hsl(var(--primary))]">
                <Sparkles className="h-3 w-3" />
                New wiki
              </span>
              <h1 className="mt-2 text-[26px] font-semibold tracking-[-0.02em]">
                Configure indexing
              </h1>
              <p className="mt-1 text-sm text-[hsl(var(--muted-foreground))]">
                A few quick questions so the wiki matches your repo and your stack.
              </p>
              <Stepper steps={STEPS} current={step} onJump={setStep} />
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
                />
              )}
              {step === 2 && state.sourceType === "git" && (
                <StepScope state={state} set={set} />
              )}
            </div>

            {/* Summary strip — git mode only */}
            {state.sourceType === "git" && step > 0 && platform && state.model && (
              <div className="px-6 py-3 border-t border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 flex items-center gap-3 flex-wrap text-xs">
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

// ── Step 1 ──────────────────────────────────────────────────────────

function StepSource({
  state,
  set,
  errors,
  platform,
  platforms,
  catalogError,
}: {
  state: WizardState;
  set: (patch: Partial<WizardState>) => void;
  errors: Record<string, string>;
  platform: Platform | undefined;
  platforms: Platform[];
  catalogError?: string | null;
}) {
  const [reveal, setReveal] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);

  return (
    <div className="space-y-5">
      {/* ── Source type toggle ──────────────────────────────────── */}
      <div>
        <h2 className="text-base font-semibold">What are you indexing?</h2>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
          Point to a code repository or upload a document collection.
        </p>
        <div className="mt-3 grid gap-2 grid-cols-1 sm:grid-cols-2">
          <SourceTypeCard
            icon={<GitFork className="h-4 w-4" />}
            title="Git repository"
            desc="Index a GitHub, GitLab, Gitea, or any hosted repo."
            selected={state.sourceType === "git"}
            onSelect={() => set({ sourceType: "git" })}
          />
          <SourceTypeCard
            icon={<Database className="h-4 w-4" />}
            title="Document catalog"
            desc="Paste or upload text/Markdown files — no git URL needed."
            selected={state.sourceType === "catalog"}
            onSelect={() => set({ sourceType: "catalog" })}
          />
        </div>
      </div>

      {/* ── Git fields ─────────────────────────────────────────── */}
      {state.sourceType === "git" && (
        <>
          <Field
            label="Repository URL"
            hint="HTTPS or SSH"
            required
            error={errors.url}
          >
            <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--input))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]/30">
              <GitFork className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
              <input
                type="text"
                placeholder="https://github.com/owner/repo"
                value={state.url}
                onChange={(e) => set({ url: e.target.value })}
                spellCheck={false}
                autoFocus
                className="flex-1 bg-transparent text-sm font-mono outline-none placeholder:text-[hsl(var(--muted-foreground))]"
              />
            </div>
          </Field>

          <Field
            label="Platform"
            required
            hint="We auto-detect from the URL — change if needed"
          >
            <div className="grid gap-2 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3">
              {platforms.map((p) => (
                <PlatformTile
                  key={p.id}
                  platform={p}
                  selected={state.platform === p.id}
                  onSelect={() => set({ platform: p.id, platformLocked: true })}
                />
              ))}
            </div>
          </Field>

          {platform && (
            <Field
              label={platform.tokenLabel}
              hint="Optional · only needed for private repos"
            >
              <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--input))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]/30">
                <KeyRound className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
                <input
                  type={reveal ? "text" : "password"}
                  placeholder={`Paste your ${platform.name} token`}
                  value={state.token}
                  onChange={(e) => set({ token: e.target.value })}
                  spellCheck={false}
                  autoComplete="off"
                  className="flex-1 bg-transparent text-sm outline-none placeholder:text-[hsl(var(--muted-foreground))]"
                />
                <button
                  type="button"
                  onClick={() => setReveal((r) => !r)}
                  aria-label={reveal ? "Hide token" : "Show token"}
                  className="inline-flex items-center justify-center w-7 h-7 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]"
                >
                  {reveal ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                </button>
              </div>
              <div className="mt-2 flex items-start justify-between gap-3 flex-wrap">
                <div className="inline-flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))]">
                  <Info className="h-3 w-3" />
                  Token is held in memory for this session only — never persisted.
                </div>
                <button
                  type="button"
                  onClick={() => setHelpOpen((s) => !s)}
                  className="inline-flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                >
                  <BookOpen className="h-3 w-3" />
                  How do I create a {platform.name} token?
                  {helpOpen ? (
                    <ChevronUp className="h-3 w-3" />
                  ) : (
                    <ChevronDown className="h-3 w-3" />
                  )}
                </button>
              </div>
              {helpOpen && (
                <div className="mt-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 p-3">
                  <div className="flex items-start gap-3 flex-wrap">
                    <span
                      aria-hidden
                      className="inline-flex items-center justify-center w-8 h-8 rounded shrink-0"
                      style={{ background: platform.color }}
                    >
                      <PlatformIcon platformId={platform.id} className="h-4 w-4 text-white" />
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium">
                        Create a token on {platform.name}
                      </div>
                      <div className="text-[11px] text-[hsl(var(--muted-foreground))] mt-0.5">
                        {platform.tokenScope}
                      </div>
                    </div>
                    {platform.tokenUrl && (
                      <a
                        href={platform.tokenUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-[11px] text-[hsl(var(--primary))] hover:underline"
                      >
                        Open token settings
                        <ExternalLink className="h-2.5 w-2.5" />
                      </a>
                    )}
                  </div>
                  <ol className="mt-2.5 space-y-1.5 text-xs text-[hsl(var(--muted-foreground))] list-decimal pl-5">
                    {platform.tokenSteps.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ol>
                </div>
              )}
            </Field>
          )}
        </>
      )}

      {/* ── Catalog fields ─────────────────────────────────────── */}
      {state.sourceType === "catalog" && (
        <>
          <Field
            label="Workspace name"
            hint="Becomes the project slug"
            required
            error={errors.catalogName}
          >
            <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--input))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]/30">
              <Database className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
              <input
                type="text"
                placeholder="my-knowledge-base"
                value={state.catalogName}
                onChange={(e) => set({ catalogName: e.target.value })}
                spellCheck={false}
                autoFocus
                className="flex-1 bg-transparent text-sm outline-none placeholder:text-[hsl(var(--muted-foreground))]"
              />
            </div>
            {state.catalogName.trim() && (
              <p className="mt-1 text-[11px] text-[hsl(var(--muted-foreground))]">
                Slug:{" "}
                <span className="font-mono text-[hsl(var(--foreground))]">
                  {slugifyName(state.catalogName) || "—"}
                </span>
              </p>
            )}
          </Field>

          <Field
            label="Documents"
            required
            error={errors.catalogDocs}
            hint="Paste content or upload .txt / .md files"
          >
            <CatalogDocsForm
              docs={state.catalogDocs}
              onChange={(docs) => set({ catalogDocs: docs })}
              error={catalogError ?? undefined}
            />
          </Field>
        </>
      )}
    </div>
  );
}

// ── SourceTypeCard ───────────────────────────────────────────────────

function SourceTypeCard({
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
          : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--border-strong))] hover:bg-[hsl(var(--accent))]/40"
      )}
    >
      <span className="text-[hsl(var(--primary))] mt-0.5 shrink-0">{icon}</span>
      <span className="flex-1 min-w-0">
        <span className="block text-sm font-medium">{title}</span>
        <span className="block text-[11px] text-[hsl(var(--muted-foreground))]">{desc}</span>
      </span>
    </button>
  );
}

// ── Step 2 ──────────────────────────────────────────────────────────

function StepGeneration({
  state,
  set,
  languages,
}: {
  state: WizardState;
  set: (patch: Partial<WizardState>) => void;
  languages: Array<{ id: string; label: string; subtle?: string }>;
}) {
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-base font-semibold">How should the wiki read?</h2>
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
            tag={{ label: "Faster", tone: "blue" }}
            selected={state.depth === "concise"}
            onSelect={() => set({ depth: "concise" })}
          />
        </div>
      </Field>

      <div className="grid gap-3 grid-cols-1 sm:grid-cols-2">
        <Field label="Wiki language" required>
          <div className="flex items-center gap-2 h-11 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--input))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]/30 relative">
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

        <Field label="Model" required hint="Used to author every page">
          <ModelPicker value={state.model} onChange={(v) => set({ model: v })} variant="full" />
        </Field>
      </div>
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
  tag: { label: string; tone: "primary" | "blue" };
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
            "ml-auto inline-flex items-center gap-1 px-1.5 h-5 rounded-full text-[10px] font-medium",
            tag.tone === "primary"
              ? "bg-[hsl(var(--primary))]/15 text-[hsl(var(--primary))]"
              : "bg-blue-500/15 text-blue-400"
          )}
        >
          {tag.label}
        </span>
      </div>
      <div className="font-mono text-[11px] text-[hsl(var(--muted-foreground))]">{meta}</div>
      <p className="text-xs text-[hsl(var(--muted-foreground))] [text-wrap:pretty]">{desc}</p>
    </button>
  );
}

// ── Step 3 ──────────────────────────────────────────────────────────

function StepScope({
  state,
  set,
}: {
  state: WizardState;
  set: (patch: Partial<WizardState>) => void;
}) {
  const defaults = getDefaultExclusions();
  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-base font-semibold">Anything to skip?</h2>
        <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
          Trim the indexer to focus on what matters. Defaults already exclude lockfiles,
          build output, and vendored code.
        </p>
        <span className="inline-flex items-center gap-1 mt-2 px-2 h-5 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-[10px] text-[hsl(var(--muted-foreground))]">
          <Sparkles className="h-2.5 w-2.5 text-[hsl(var(--primary))]" />
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
          <textarea
            rows={7}
            spellCheck={false}
            placeholder={
              state.filterMode === "include" ? "src/\napps/\nlibs/" : "node_modules\ndist\n.git"
            }
            value={state.dirs}
            onChange={(e) => set({ dirs: e.target.value })}
            className="w-full font-mono text-xs bg-[hsl(var(--input))] border border-[hsl(var(--border))] rounded-lg p-3 outline-none focus-visible:border-[hsl(var(--border-strong))] focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]/30 resize-none"
          />
          <button
            type="button"
            onClick={() => set({ dirs: defaults.dirs })}
            className="inline-flex items-center gap-1 text-[11px] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] mt-1.5"
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
          <textarea
            rows={7}
            spellCheck={false}
            placeholder={
              state.filterMode === "include"
                ? "**/*.py\n**/*.ts\n**/*.md"
                : "*.lock\n*.min.js\n*.map"
            }
            value={state.files}
            onChange={(e) => set({ files: e.target.value })}
            className="w-full font-mono text-xs bg-[hsl(var(--input))] border border-[hsl(var(--border))] rounded-lg p-3 outline-none focus-visible:border-[hsl(var(--border-strong))] focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]/30 resize-none"
          />
          <button
            type="button"
            onClick={() => set({ files: defaults.files })}
            className="inline-flex items-center gap-1 text-[11px] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] mt-1.5"
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
        <span className="block text-[11px] text-[hsl(var(--muted-foreground))]">{desc}</span>
      </span>
    </button>
  );
}
