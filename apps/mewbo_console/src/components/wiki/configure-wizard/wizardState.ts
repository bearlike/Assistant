/**
 * ConfigureWizard's non-JSX orchestration: wizard state, validation, step
 * navigation, and submit — everything the wizard decides that isn't markup.
 * `ConfigureWizard.tsx` calls `useWizardMachine()` and renders what it
 * returns; the individual `Step*` components stay dumb `{state, set}`
 * consumers. Mirrors the `useQaConversation` pattern used for QAScreen.
 */

import { useEffect, useMemo, useState } from "react";
import { useLocation } from "wouter";

import { useConfig } from "../../../hooks/useConfig";
import { useModels } from "../../../hooks/useModels";

import { uploadCatalogDocuments } from "../api/client";
import {
  useBranches,
  useSubmitWizard,
  useWikiDefaults,
  useWikiLanguages,
  useWikiPlatforms,
} from "../api/hooks";
import type {
  CatalogDocument,
  Platform,
  WizardSourceType,
  WizardSubmission,
} from "../api/types";
import { splitLines } from "../projectSettingsForm";
import { buildHref } from "../router";
import { slugFromRepoUrl } from "../slug";

export function detectPlatformFromUrl(url: string, platforms: Platform[]): Platform["id"] {
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

// Slug parsing is centralised in ../slug.ts so the wizard, the landing
// page and the rest of the FE all agree on the canonical
// ``host/owner/repo`` shape.

export interface WizardState {
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
  /** Opt-in cross-model fallback for the indexing run. `fallbackEnabled` gates
   *  the control; `fallbackModels` is the ordered ladder. Only a non-empty
   *  ladder is submitted — there is no separate "enabled" wire field. */
  fallbackEnabled: boolean;
  fallbackModels: string[];
  /** Branch/tag to clone; empty = repo default branch. */
  ref: string;
  filterMode: "exclude" | "include";
  dirs: string;
  files: string;
  /** Developer-mode opt-in: build only the AST graph, skip docs + LLM. */
  graphOnly: boolean;
  // ── catalog fields ────────────────────────────────────────────────
  /** Human-readable workspace name; slugified to produce the project slug. */
  catalogName: string;
  catalogDocs: CatalogDocument[];
}

/** Slugify a free-form workspace name into a valid project slug. */
export function slugifyName(name: string): string {
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

export interface UseWizardMachineOptions {
  initialUrl?: string;
}

export function useWizardMachine({ initialUrl = "" }: UseWizardMachineOptions) {
  const [, navigate] = useLocation();
  const platforms = useWikiPlatforms();
  const languages = useWikiLanguages();
  const { config } = useConfig();
  const developerMode = Boolean(
    (config?.runtime as { developer_mode?: boolean } | undefined)?.developer_mode
  );
  const { models: modelList, defaultModel } = useModels();
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
    fallbackEnabled: false,
    fallbackModels: [],
    ref: "",
    filterMode: "exclude",
    dirs: "",
    files: "",
    graphOnly: false,
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

  // Branch list for the generation step's branch picker. Disabled until the
  // URL looks like a git URL (the hook gates internally).
  const branches = useBranches({
    repoUrl: state.url,
    token: state.token || undefined,
    slug: gitSlug ?? undefined,
  });

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
    const fallbackLadder = state.fallbackModels.filter((m) => m !== state.model);
    const payload: WizardSubmission = {
      repoUrl: state.url,
      slug: gitSlug,
      platform: state.platform,
      token: state.token || undefined,
      depth: state.depth,
      language: state.language,
      model: state.model,
      filterMode: state.filterMode,
      dirs: splitLines(state.dirs),
      files: splitLines(state.files),
      // Forward the chosen branch/tag only when set — empty means default.
      ...(state.ref ? { ref: state.ref } : {}),
      // Same omit-when-inert rule as `ref`: the primary is already tried
      // first, so it never belongs in its own ladder.
      ...(state.fallbackEnabled && fallbackLadder.length > 0
        ? { fallbackModels: fallbackLadder }
        : {}),
      // Only forward the developer-mode opt-in when it's actually available
      // and engaged — the field is omitted entirely otherwise.
      ...(developerMode && state.graphOnly ? { graphOnly: true } : {}),
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

  const isPending = submit.isPending || catalogPending;

  return {
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
  };
}
