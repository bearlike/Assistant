// TanStack Query hooks for the Mewbo Apps surface. All server state flows
// through here so the view layer never touches fetch directly (console house
// rule). Mirrors `useAgenticSearch.ts`: read hooks with `staleTime`, write
// hooks that invalidate the affected keys by prefix.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  archiveApp,
  createApp,
  firePipeline,
  getApp,
  getAppSystem,
  listApps,
  mintAppToken,
  pauseApp,
  rearmApp,
  resumeApp,
  rollbackApp,
  type CreateAppInput,
  type RearmInput,
} from "../api/apps";
import type { AppReadTokenScope, AppStatus } from "../types/apps";

const APPS_KEY = ["apps"] as const;
const appKey = (appId: string | null) => ["apps", "app", appId] as const;
const appSystemKey = (appId: string | null) => ["apps", "system", appId] as const;

/** Statuses whose app is still converging — the detail view polls through them
 *  (a draft becomes building becomes live as the builder session runs). */
const TRANSIENT_STATUSES: ReadonlySet<AppStatus> = new Set(["draft", "building"]);

/** `GET /api/apps` — the gallery. */
export function useApps() {
  return useQuery({
    queryKey: APPS_KEY,
    queryFn: listApps,
    staleTime: 30_000,
  });
}

/**
 * `GET /api/apps/<id>` — the app manifest + version history. Polls itself while
 * the app is still building (draft/building) so the detail view flips from the
 * build-progress state to the live app without a manual refresh, then stops.
 *
 * Also polls gently (30s) once settled, and refetches on window focus: a new
 * version (repair, rollback, or an agent editing the app) has no push signal
 * to an already-open detail screen, so without this a user watching the app
 * sees no change until they reload. Scoped to this hook alone — its only
 * consumer is the detail screen, so this is not a global polling default.
 */
export function useApp(appId: string | null) {
  return useQuery({
    queryKey: appKey(appId),
    queryFn: () => getApp(appId as string),
    enabled: Boolean(appId),
    refetchInterval: (query) =>
      query.state.data && TRANSIENT_STATUSES.has(query.state.data.spec.status) ? 2_000 : 30_000,
    refetchOnWindowFocus: true,
  });
}

/**
 * `GET /api/apps/<id>/system` — freshness + triggers + runs + maintainer.
 * Pass `enabled: false` to keep it idle (gallery cards enable it lazily). Polls
 * while the app is live so the health strip's freshness/next-fire stay current.
 */
export function useAppSystem(appId: string | null, enabled = true) {
  return useQuery({
    queryKey: appSystemKey(appId),
    queryFn: () => getAppSystem(appId as string),
    enabled: Boolean(appId) && enabled,
    staleTime: 30_000,
    refetchInterval: (query) =>
      query.state.data?.status === "live" ? 30_000 : false,
  });
}

/** Create a draft app + spawn its builder session. Invalidates the gallery. */
export function useCreateApp() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateAppInput) => createApp(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: APPS_KEY }),
  });
}

/**
 * The render token for an open app, keyed by `(app_id, version, scope)` so it
 * mints exactly once per opened version+scope and a rollback re-mints (which the
 * render panel keys off to remount with the new frontend). Minting is a POST, but
 * it is safe to retry and effectively "get me a token", so a cached query reads
 * cleaner than a mutation-in-effect. Enabled only once the app is renderable.
 *
 * `writable` requests a `write` token IFF the app declares a
 * `user_writable` pipeline — the one path the served frontend's
 * `app.pipelines.submit` form write-back needs. The scope is in the query key so
 * flipping it re-mints; the console holds the master key (server-proxied), which
 * is what a `write` mint requires.
 */
export function useAppRenderToken(
  appId: string | null,
  version: number,
  enabled: boolean,
  writable = false,
) {
  const scope: AppReadTokenScope = writable ? "write" : "read";
  return useQuery({
    queryKey: ["apps", "token", appId, version, scope] as const,
    queryFn: () => mintAppToken(appId as string, scope),
    enabled: Boolean(appId) && enabled,
    // A token is single-use-per-open; don't auto-refetch it out from under a
    // mounted kernel (kernel options are init-only). Re-mint happens by version.
    staleTime: Infinity,
    gcTime: 5 * 60_000,
    retry: 1,
  });
}

/** Shared invalidation for the lifecycle mutations below — a status change
 *  ripples into the gallery card, the detail manifest, and the health strip. */
function useAppLifecycleInvalidation() {
  const qc = useQueryClient();
  return (appId: string) => {
    void qc.invalidateQueries({ queryKey: APPS_KEY });
    void qc.invalidateQueries({ queryKey: appKey(appId) });
    void qc.invalidateQueries({ queryKey: appSystemKey(appId) });
  };
}

/** Pause an app (pauses its triggers). */
export function usePauseApp() {
  const invalidate = useAppLifecycleInvalidation();
  return useMutation({
    mutationFn: (appId: string) => pauseApp(appId),
    onSuccess: (spec) => invalidate(spec.app_id),
  });
}

/** Resume a paused app (re-arms its triggers). */
export function useResumeApp() {
  const invalidate = useAppLifecycleInvalidation();
  return useMutation({
    mutationFn: (appId: string) => resumeApp(appId),
    onSuccess: (spec) => invalidate(spec.app_id),
  });
}

/** Archive an app (removes it from the gallery). */
export function useArchiveApp() {
  const invalidate = useAppLifecycleInvalidation();
  return useMutation({
    mutationFn: (appId: string) => archiveApp(appId),
    onSuccess: (spec) => invalidate(spec.app_id),
  });
}

/** Repoint the active version (rollback). Returns the refreshed detail. */
export function useRollbackApp() {
  const invalidate = useAppLifecycleInvalidation();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ appId, version }: { appId: string; version: number }) =>
      rollbackApp(appId, version),
    onSuccess: (detail) => {
      // Seed the fresh detail so the version list re-renders without a refetch
      // gap, then invalidate the rest of the app's keys.
      qc.setQueryData(appKey(detail.spec.app_id), detail);
      invalidate(detail.spec.app_id);
    },
  });
}

/**
 * Run one pipeline on demand ("Run now"). Shape mirrors
 * `useRollbackApp` — `{appId, pipeline}` at `mutate()` time, not a hook param,
 * so a caller with several pipeline rows can mount one instance per row and
 * still get independent `isPending`/`data` per row. Settling (success OR
 * error) invalidates the system query: a 200 (`mode:"code"`) landed
 * synchronously, a 202 (`mode:"agentic"`) lands later via the EXISTING 30s
 * `/system` poll (`useAppSystem`) — never add a second poller here.
 */
export function useFirePipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ appId, pipeline }: { appId: string; pipeline: string }) =>
      firePipeline(appId, pipeline),
    onSettled: (_data, _error, variables) => {
      void qc.invalidateQueries({ queryKey: appSystemKey(variables.appId) });
    },
  });
}

/**
 * Re-arm schedule triggers for pipelines the server flagged unscheduled
 * (health strip's "Re-arm schedules" action). Same `{appId, ...}`-at-mutate
 * shape as `useFirePipeline`/`useRollbackApp`.
 */
export function useRearmApp() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ appId, seed }: { appId: string } & RearmInput) => rearmApp(appId, { seed }),
    onSettled: (_data, _error, variables) => {
      void qc.invalidateQueries({ queryKey: appSystemKey(variables.appId) });
    },
  });
}
