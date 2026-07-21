/**
 * TanStack-Query hooks over the wiki REST surface. The two SSE stream
 * consumers (`useIndexingStream`, `useQaStream`) live in `streamHooks.ts` —
 * this file stays the plain query/mutation surface.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  cancelIndexingJob,
  deleteProject,
  getAnswer,
  getIndexingJob,
  getKnowledgeGraph,
  getPage,
  getProjectFreshness,
  getProjectSettings,
  getSourceExcerpt,
  getWikiDefaults,
  getWikiSessionLink,
  listActiveJobs,
  listBranches,
  listLanguages,
  listPlatforms,
  listProjects,
  listRecoverableJobs,
  requestWikiRefresh,
  resumeIndexingJob,
  submitWizard,
  updateProject,
} from "./client";
import type { ProjectSettingsPatch, WizardSubmission } from "./types";

export function useWikiProjects() {
  return useQuery({
    queryKey: ["wiki", "projects"],
    queryFn: listProjects,
    staleTime: 60_000,
  });
}

/**
 * Lazy per-project freshness (indexed snapshot vs remote HEAD). Long
 * ``staleTime`` (≥5 min, matching the API's TTL cache) so scrolling the
 * gallery doesn't re-probe every card, and ``retry: false`` so a repo whose
 * remote can't be reached fails quietly rather than hammering ``ls-remote``.
 * The badge renders nothing on error/absent, so a failure is invisible.
 */
export function useProjectFreshness(slug: string | null, enabled = true) {
  return useQuery({
    queryKey: ["wiki", "freshness", slug ?? null],
    queryFn: () => getProjectFreshness(slug as string),
    enabled: enabled && slug != null,
    staleTime: 5 * 60_000,
    retry: false,
  });
}

/**
 * Load the persisted code knowledge graph for *slug*. The graph is a
 * derived, idempotent read — long staleTime so a tab keeps cache between
 * navigations. Disabled when no slug is set (e.g. SSR / unmounted).
 */
/**
 * Load the persisted code knowledge graph for *slug*. Pass ``limit`` to
 * cap the node set (degree-ranked truncation on the BE) — omit it to
 * load the full graph. The BE reports ``stats.totalNodes`` and
 * ``stats.truncated`` so the consumer can surface "showing N of M" when
 * a cap kicks in. Pass ``hierarchy`` to fetch the directory-scaffold
 * payload (``Folder`` supernodes) the 3D galaxy collapses on.
 *
 * Backward compatible: the second arg still accepts a bare ``limit``
 * number (legacy call sites), or an options object for ``hierarchy``.
 */
export function useKnowledgeGraph(
  slug: string | null,
  options: number | { limit?: number; hierarchy?: boolean } = {},
) {
  const opts = typeof options === "number" ? { limit: options } : options;
  const { limit, hierarchy } = opts;
  return useQuery({
    queryKey: ["wiki", "graph", slug ?? null, limit ?? null, hierarchy ?? false],
    queryFn: () => getKnowledgeGraph(slug as string, { limit, hierarchy }),
    enabled: slug != null,
    staleTime: 5 * 60_000,
  });
}

/**
 * Active (non-terminal) indexing jobs. Polls every 4 s so the landing
 * page's "Indexing now" surface stays close-to-live without hammering
 * the API. Pauses automatically when the tab is hidden (TanStack Query
 * default `refetchIntervalInBackground: false`).
 */
export function useActiveIndexingJobs() {
  return useQuery({
    queryKey: ["wiki", "jobs", "active"],
    queryFn: listActiveJobs,
    staleTime: 4_000,
    refetchInterval: 4_000,
  });
}

/**
 * Failed / interrupted / cancelled-but-incomplete jobs with reusable work.
 * Powers the landing page's "Incomplete indexes" section. Polled on a slow
 * cadence — these are terminal jobs, so they only change when one resumes.
 */
export function useRecoverableJobs() {
  return useQuery({
    queryKey: ["wiki", "jobs", "recoverable"],
    queryFn: listRecoverableJobs,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}

/**
 * Resume a recoverable indexing job. On success the job re-enters the active
 * set, so invalidate the recoverable + active lists AND the per-job snapshot
 * (whose polling stopped on the terminal status) so the indexing screen sees
 * the job is live again. The caller navigates / re-subscribes to the stream.
 */
export function useResumeIndexing() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => resumeIndexingJob(jobId),
    onSuccess: (_res, jobId) => {
      qc.invalidateQueries({ queryKey: ["wiki", "jobs", "recoverable"] });
      qc.invalidateQueries({ queryKey: ["wiki", "jobs", "active"] });
      qc.invalidateQueries({ queryKey: ["wiki", "indexing", jobId] });
    },
    // A 400/404/409 (already running, not resumable, …) must surface — the
    // button re-enables on its own (mutation settles), the toast tells why.
    onError: (err) => {
      toast.error(
        `Resume failed — ${err instanceof Error ? err.message : String(err)}`,
      );
    },
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => deleteProject(slug),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["wiki", "projects"] });
    },
  });
}

/**
 * A project's editable indexing settings (``GET …/settings``).
 * ``retry: false`` so an unknown slug / absent route fails once and renders the
 * dialog's error state instead of hammering. Pass ``enabled`` so the dialog
 * only reads while it's open.
 */
export function useProjectSettings(slug: string | null, enabled = true) {
  return useQuery({
    queryKey: ["wiki", "settings", slug ?? null],
    queryFn: () => getProjectSettings(slug as string),
    enabled: enabled && slug != null,
    staleTime: 30_000,
    retry: false,
  });
}

/**
 * PATCH a project's settings with the changed subset.
 *
 * Invalidation: the projects list (cards show model/desc) and this project's
 * settings. A changed ``ref`` ALSO invalidates freshness — the badge compares
 * the indexed snapshot against the pinned branch's remote HEAD, so a re-pointed
 * ref leaves it reporting drift against the old branch until it re-probes.
 *
 * No toast: the caller renders `mutation.error` inline (a 403 dev-mode gate or
 * a 409 identity-edit belongs next to the field that caused it), and success is
 * signalled by the dialog closing + the list refreshing.
 */
export function useUpdateProject(slug: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: ProjectSettingsPatch) => updateProject(slug, patch),
    onSuccess: (settings, patch) => {
      qc.setQueryData(["wiki", "settings", slug], settings);
      qc.invalidateQueries({ queryKey: ["wiki", "projects"] });
      if ("ref" in patch) {
        qc.invalidateQueries({ queryKey: ["wiki", "freshness", slug] });
      }
    },
  });
}

export function useWikiPlatforms() {
  return useQuery({
    queryKey: ["wiki", "platforms"],
    queryFn: listPlatforms,
    staleTime: Infinity,
  });
}

export function useWikiLanguages() {
  return useQuery({
    queryKey: ["wiki", "languages"],
    queryFn: listLanguages,
    staleTime: Infinity,
  });
}

/**
 * Wiki-specific defaults from app.json (``wiki.default_model`` etc.).
 * The wiki picker prefers these over the global /api/models default so
 * operators can pin a fast/cheap/stable model for indexing without
 * affecting the rest of the app. Missing keys fall back to FE defaults.
 */
export function useWikiDefaults() {
  return useQuery({
    queryKey: ["wiki", "defaults"],
    queryFn: getWikiDefaults,
    staleTime: 5 * 60_000,
  });
}

export function useWikiPage(pageId: string | null, slug?: string) {
  return useQuery({
    queryKey: ["wiki", "page", slug ?? null, pageId],
    queryFn: () =>
      pageId && slug ? getPage(slug, pageId) : Promise.resolve(null),
    enabled: Boolean(pageId) && Boolean(slug),
    staleTime: 5 * 60_000,
  });
}

/**
 * Snapshot-only indexing job lookup. Polling fallback for SSE; the
 * function-form `refetchInterval` pauses automatically when the live
 * status reaches a terminal state.
 */
export function useIndexingJob(jobId: string | null) {
  return useQuery({
    queryKey: ["wiki", "indexing", jobId],
    queryFn: () => (jobId ? getIndexingJob(jobId) : Promise.resolve(null)),
    enabled: Boolean(jobId),
    refetchInterval: (q) => {
      const last = q.state.data;
      if (!last) return 500;
      return last.status === "complete" ||
        last.status === "cancelled" ||
        last.status === "failed" ||
        last.status === "interrupted"
        ? false
        : 500;
    },
  });
}

export function useCancelIndexing() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => cancelIndexingJob(jobId),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["wiki", "jobs", "active"] });
      qc.invalidateQueries({ queryKey: ["wiki", "projects"] });
    },
  });
}

export function useSubmitWizard() {
  return useMutation({
    mutationFn: (input: WizardSubmission) => submitWizard(input),
  });
}

/**
 * List the repo's remote branches for the wizard branch picker. Enabled only
 * once the URL looks like a git URL (so a half-typed value doesn't hit the
 * endpoint); ``retry: false`` keeps a bad URL/token from hammering it. The
 * token-presence flag (never the raw token) keys the cache so adding a token
 * to a private repo re-fetches.
 */
export function useBranches(input: { repoUrl: string; token?: string; slug?: string }) {
  const enabled = /:\/\/|git@/.test(input.repoUrl.trim());
  return useQuery({
    queryKey: ["wiki", "branches", input.repoUrl, Boolean(input.token)],
    queryFn: () => listBranches(input),
    enabled,
    retry: false,
    staleTime: 5 * 60_000,
  });
}

export function useRequestWikiRefresh() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => requestWikiRefresh(slug),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["wiki", "jobs", "active"] });
      qc.invalidateQueries({ queryKey: ["wiki", "projects"] });
    },
  });
}

/** Atomic-class consumers (sidebar caption, landing card) read the
 *  matching Project off the projects list — no per-slug endpoint exists
 *  yet and one is not required for these UI surfaces. */
export function useWikiProjectBySlug(slug: string | undefined) {
  return useQuery({
    queryKey: ["wiki", "projects"],
    queryFn: listProjects,
    staleTime: 60_000,
    select: (projects) => projects.find((p) => p.slug === slug),
    enabled: Boolean(slug),
  });
}

/**
 * Snapshot lookup for a completed answer (``GET /v1/wiki/qa/<answerId>``).
 * The live ``useQaStream`` drives the typewriter, but the deterministic
 * provenance fields (``accessedSources`` / ``modelsUsed``) live only on the
 * snapshot — the stream's internal ``access`` events are intentionally
 * ignored. Enable this once the stream has assigned an ``answerId`` to fold
 * those telemetry fields in. Idempotent read → long staleTime.
 */
export function useQaAnswerSnapshot(answerId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["wiki", "qa", answerId],
    queryFn: () => getAnswer(answerId as string),
    enabled: Boolean(answerId) && enabled,
    staleTime: 5 * 60_000,
  });
}

/**
 * Resolve a Mewbo session id to the wiki project it belongs to (backs
 * SessionHeader's "Open wiki" jump). The linkage is immutable once a
 * session is created — a job/answer never re-points at a different
 * project — so `staleTime: Infinity` and no retry: a 404 means "not a
 * wiki session," which is a real answer, not a transient failure.
 * `enabled` is caller-gated (e.g. `session.origin === "wiki"`) so a plain
 * session never fires this fetch at all.
 */
export function useWikiSessionLink(sessionId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["wiki", "sessionLink", sessionId ?? null],
    queryFn: () => getWikiSessionLink(sessionId as string),
    enabled: enabled && Boolean(sessionId),
    staleTime: Infinity,
    retry: false,
  });
}

/**
 * Lazily fetch the file excerpt for one cited Q&A source card. Keyed by
 * slug + path + range so each distinct card caches independently; the file
 * content is idempotent, so a long staleTime keeps it cached across
 * re-renders / navigations. Disabled until a slug + path are known.
 */
export function useSourceExcerpt(
  slug: string | null,
  path: string | null,
  start?: number | null,
  end?: number | null,
) {
  return useQuery({
    queryKey: ["wiki", "source", slug, path, start ?? null, end ?? null],
    queryFn: () => getSourceExcerpt(slug as string, path as string, start, end),
    enabled: Boolean(slug) && Boolean(path),
    staleTime: 5 * 60_000,
  });
}
