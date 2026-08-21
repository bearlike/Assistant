/**
 * ConfigureWizard's non-JSX orchestration: wizard state, validation, step
 * navigation, and submit — everything the wizard decides that isn't markup.
 * `ConfigureWizard.tsx` calls `useWizardMachine()` and renders what it
 * returns; the individual `Step*` components stay dumb `{state, set}`
 * consumers. Mirrors the `useQaConversation` pattern used for QAScreen.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "wouter";

import { hostOf } from "../../../api/git";
import { useConfig } from "../../../hooks/useConfig";
import { useModels } from "../../../hooks/useModels";
import { useRepository } from "../../../hooks/useRepositories";

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
import { parseMcpServers, splitLines } from "../projectSettingsForm";
import { buildHref } from "../router";
import { slugFromRepoUrl } from "../slug";

/**
 * Exact host → platform, matched against the SERVER's catalogue.
 *
 * The one rule the backend uses (`mewbo_core.repositories.PLATFORM_HOSTS`):
 * exact host, or a dot-subdomain of one, so an Azure DevOps org on the older
 * `acme.visualstudio.com` domain resolves. Never a substring. An unrecognised host is
 * `"git"` — the honest answer, since a self-hosted forge is unknowable.
 *
 * The mapping itself is never written down here: `platforms` comes from
 * `/v1/wiki/platforms`, whose `hosts` are built from that same core constant,
 * so there is exactly one place a host is bound to a platform.
 */
function platformForHost(host: string, platforms: Platform[]): Platform["id"] {
  for (const p of platforms) {
    for (const h of p.hosts) {
      if (host === h || host.endsWith("." + h)) return p.id;
    }
  }
  return "git";
}

/**
 * The platform the REGISTRY will store for a repo URL, or `null` if no host
 * can be read from it.
 *
 * Deliberately heuristic-free, unlike `detectPlatformFromUrl` below. This
 * answer is shown next to "will be registered as", so it is a claim about a
 * value the server is about to persist and render back: it must be the
 * server's rule or nothing at all. `hostOf` is the console's one host
 * extractor and agrees with the backend grammar on every URL shape that can
 * reach this function (pinned against the shared fixture in
 * `registryPlatform.test.ts`).
 */
export function registryPlatformFromUrl(
  url: string,
  platforms: Platform[],
): Platform["id"] | null {
  const host = hostOf(url);
  return host === null ? null : platformForHost(host, platforms);
}

/**
 * The platform tile the WIZARD pre-selects. Guesses, and that is correct here:
 * the tile is a starting point the user can click away from, and self-hosted
 * forges are the whole reason the guess exists. Do NOT reuse this for anything
 * that gets persisted — use `registryPlatformFromUrl` for that.
 */
export function detectPlatformFromUrl(url: string, platforms: Platform[]): Platform["id"] {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    const exact = platformForHost(host, platforms);
    if (exact !== "git") return exact;
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
  /** Empty = inherit the deployment's default embedding model. */
  embeddingModel: string;
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
  /** Free-text operator guidance appended to the indexer's playbook; empty =
   *  none. Capped at 4000 chars server-side — it is paid once per page. */
  customInstructions: string;
  /** External MCP servers to attach for the index, edited as raw `.mcp.json`
   *  JSON; empty = attach none. Parsed at submit, never stored parsed. */
  mcpServers: string;
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
  /**
   * Slug (`host/owner/repo`) of an already-registered repository, off the
   * configure route's `?repo=`. When it resolves, seeds `url`/`platform`
   * from the registry record — a SEED, not a lock: every field stays
   * editable exactly as when the wizard opens cold, and an unknown or
   * unreachable slug just leaves the wizard cold (no error surfaced here).
   * Registering a repository stays inert; this is how an operator opts
   * into indexing one without retyping a URL the product already holds.
   */
  initialRepo?: string;
}

export function useWizardMachine({ initialUrl = "", initialRepo }: UseWizardMachineOptions) {
  const [, navigate] = useLocation();
  const registeredRepo = useRepository(initialRepo);
  const platforms = useWikiPlatforms();
  const languages = useWikiLanguages();
  const { config } = useConfig();
  const developerMode = Boolean(
    (config?.runtime as { developer_mode?: boolean } | undefined)?.developer_mode
  );
  const { defaultModel } = useModels();
  const wikiDefaults = useWikiDefaults();
  const seedModel = wikiDefaults.data?.model || defaultModel;
  const embeddingDefault = wikiDefaults.data?.embeddingModel;
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
    embeddingModel: "",
    fallbackEnabled: false,
    fallbackModels: [],
    ref: "",
    filterMode: "exclude",
    dirs: "",
    files: "",
    graphOnly: false,
    customInstructions: "",
    mcpServers: "",
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

  // Seed url/platform from an already-registered repository (`?repo=`),
  // independent of the model-seed effect above — they touch disjoint
  // fields and can't race each other. Applies AT MOST ONCE (repoSeedApplied
  // ref): the registry list underneath `useRepository` can refetch for
  // reasons that have nothing to do with this wizard instance, and a
  // second application would clobber whatever the user has since typed.
  // An explicit `?url=`, or a url the user typed before the fetch
  // resolved, wins outright — the seed never overwrites a non-empty field.
  const repoSeedApplied = useRef(false);
  const [repoSeedUrl, setRepoSeedUrl] = useState<string | null>(null);
  useEffect(() => {
    if (repoSeedApplied.current) return;
    const repo = registeredRepo.repository;
    if (!repo) return;
    repoSeedApplied.current = true;
    if (state.url.trim()) return;
    setRepoSeedUrl(repo.repoUrl);
    setState((s) => ({ ...s, url: repo.repoUrl, platform: repo.platform }));
  }, [registeredRepo.repository, state.url]);

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

  // Auto-detect platform from URL unless user has explicitly picked one, or
  // the URL still matches the registered repo it was just seeded from — a
  // registered platform is the product's own record and wins over the
  // heuristic host guess. Once the user edits the URL away from the seed,
  // `state.url !== repoSeedUrl` and normal auto-detect resumes untouched.
  useEffect(() => {
    if (!state.url || state.platformLocked) return;
    if (state.url === repoSeedUrl) return;
    if (!platformList.length) return;
    const next = detectPlatformFromUrl(state.url, platformList);
    if (next !== state.platform) {
      setState((s) => ({ ...s, platform: next }));
    }
  }, [state.url, state.platformLocked, platformList, state.platform, repoSeedUrl]);

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
    // Reuses the settings dialog's parser rather than a second one: the wizard
    // and the settings panel edit the SAME field in the same textarea shape, and
    // two parsers would drift on the first change to either.
    const mcpServers = parseMcpServers(state.mcpServers);
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
      ...(state.embeddingModel.trim()
        ? { embeddingModel: state.embeddingModel.trim() }
        : {}),
      // Only forward the developer-mode opt-in when it's actually available
      // and engaged — the field is omitted entirely otherwise.
      ...(developerMode && state.graphOnly ? { graphOnly: true } : {}),
      // Same omit-when-inert rule as `ref` again: an untouched field must leave
      // the submission byte-identical to one made before it existed.
      ...(state.customInstructions.trim()
        ? { customInstructions: state.customInstructions.trim() }
        : {}),
      ...(mcpServers ? { mcpServers } : {}),
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
    developerMode,
    embeddingDefault,
    state,
    set,
    errors,
    catalogError,
    platform,
    gitSlug,
    branches,
    /**
     * Registered default branch, when `initialRepo` resolved one — a
     * DISPLAY fallback only. Never written into `state.ref`: leaving `ref`
     * empty is what lets the branch picker's "Default · …" option keep
     * tracking the git host's *current* default rather than pinning to
     * whatever the registry had cached at registration time.
     */
    seedDefaultBranch: registeredRepo.repository?.defaultBranch ?? null,
    step,
    setStep,
    STEPS,
    goNext,
    goBack,
    onSubmit,
    isPending,
  };
}
