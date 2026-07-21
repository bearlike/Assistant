/**
 * Agentic Search-specific navigation constants shared by the demo capture
 * specs. Composes the shared `DemoHelper` (fixtures.ts,
 * owned by the orchestrator) rather than duplicating it — this file owns only
 * the seeded route/id contract, mirroring `wiki-helpers.ts`'s role for the
 * wiki specs.
 *
 * `AgenticSearchView` treats the URL as the single source of truth for
 * {workspace, active run} (`ws` / `run` query params — see
 * `components/agentic_search/CLAUDE.md` "Landing inertness + URL-as-source-
 * of-truth"), so every href here is a plain query string, not a router
 * helper: unlike the wiki (`router.ts:buildHref`), Agentic Search has no
 * route-builder module — the view reads `useSearchParams` directly.
 */

// ── Seeded identities (orchestrator brief — "Seeded content contract") ────
// A change to any of these strings is the intended "the demo is stale"
// signal (script breakage == staleness, per this harness's convention).

export const OSS_REPO_SCOUT_WS = "ws-oss-repo-scout";
export const RUN_OSS_AGENTS = "run-oss-agents";
export const RUN_CC_PLUGINS = "run-cc-plugins";

/**
 * The Agentic Search landing, scoped to one workspace via `?ws=`. The `ws`
 * param always wins over localStorage (`AgenticSearchView`'s resolution
 * order), so this deterministically selects the active workspace without
 * depending on a workspace-picker click or on incidental seed ordering.
 */
export function searchLandingHref(workspaceId: string = OSS_REPO_SCOUT_WS): string {
  return `/search?ws=${encodeURIComponent(workspaceId)}`;
}

/**
 * A completed run's deep link (`/search?ws=...&run=...`). Per the verified
 * deep-link contract, opening this fires exactly one
 * `GET /api/agentic_search/runs/<id>` snapshot + an SSE attach that replays
 * the stored event log and closes — never a `POST /runs`, never a live LLM
 * call.
 */
export function searchRunHref(workspaceId: string, runId: string): string {
  return `/search?ws=${encodeURIComponent(workspaceId)}&run=${encodeURIComponent(runId)}`;
}
