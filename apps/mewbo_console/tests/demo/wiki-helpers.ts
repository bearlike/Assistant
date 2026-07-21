import type { Page } from "@playwright/test";

// Pulls the REAL app router so a spec never hand-rolls a query string — any
// drift in route shape (a renamed param, a new required field) breaks the
// TYPE CHECK here before it ever breaks a capture. `buildHref` is a pure
// function (no React/DOM), so importing it into the Node-side test runner is
// safe; see `src/components/wiki/router.ts`.
import { buildHref, type PlatformId } from "../../src/components/wiki/router";

/**
 * Wiki-specific navigation + network-stub helpers shared by the demo capture
 * specs. Composes the shared `DemoHelper` (fixtures.ts,
 * owned by the orchestrator) rather than duplicating it — this file owns
 * only wiki-route construction and the deterministic stubs for the two
 * outbound/LAN calls the wiki screens make (branch listing, freshness probe).
 */

// ── Seeded identities (orchestrator brief — "Seeded content contract") ────
// These strings are the contract with the seeder: a title/slug change here
// is the intended "the demo is stale" signal, matching the
// "script breakage == staleness" convention used throughout this harness.

export const GROVE_SLUG = "github.com/bearlike/Grove";
export const GROVE_LANDING_PAGE_ID = "grove-overview";

/**
 * The repo the configure-wizard shot indexes. Deliberately NOT one of the
 * seeded projects (the wizard is the "add something new" flow) and
 * deliberately on the self-hosted forge, so the URL agrees with the Gitea
 * platform tile the spec selects.
 */
export const WIZARD_REPO_URL = "https://git.example.com/acme/payments-api";

export const ASSISTANT_SLUG = "github.com/bearlike/Assistant";
export const ASSISTANT_OVERVIEW_PAGE_ID = "overview";

export const BEACON_SLUG = "git.example.com/acme/beacon";

/** The single in-flight (`finalizing`) job the indexing-progress shot captures. */
export const DEMO_JOB = {
  jobId: "job-demo-project-64-0001",
  slug: "git.example.com/acme/demo-project-64",
} as const;

export const QA_ANSWER_ID = "qa-assistant-what-is-this-for-0001";
export const QA_QUESTION = "What is this project for?";

const GITEA: PlatformId = "gitea";

// ── Route builders — thin wrappers over the app's OWN buildHref ───────────

/** A wiki content page (`/wiki/p/<pageId>?slug=...&platform=...`). */
export function wikiPageHref(pageId: string, slug: string, platform: PlatformId = GITEA): string {
  return buildHref({ kind: "page", pageId, slug, platform });
}

/** The configure wizard, prefilled with a repo URL (`/wiki/configure?url=...`). */
export function wikiConfigureHref(url: string): string {
  return buildHref({ kind: "configure", url });
}

/** The live indexing-progress screen for one job. */
export function wikiIndexingHref(jobId: string, slug: string): string {
  return buildHref({ kind: "indexing", jobId, slug });
}

/** The idempotent Q&A snapshot deep link (`?answer=<id>` — no LLM re-invoke). */
export function wikiQaSnapshotHref(args: { answer: string; pageId: string; slug: string }): string {
  return buildHref({
    kind: "qa",
    question: QA_QUESTION,
    pageId: args.pageId,
    slug: args.slug,
    answer: args.answer,
  });
}

// ── Deterministic network stubs — no shot depends on a real LAN call ──────

/**
 * Every visible LandingScreen card AND every in-project `WikiTopBar` (when
 * `showFreshness` is on) fires its own `GET .../freshness` probe on mount —
 * a real `git ls-remote` against the repo's host. Stub it to a fixed
 * "up to date" reading so freshness pixels never depend on real network
 * reachability/timing. Call BEFORE navigating.
 */
export async function stubFreshness(page: Page): Promise<void> {
  await page.route("**/v1/wiki/projects/*/freshness*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        indexedSha: "abc123def456",
        remoteSha: "abc123def456",
        behindBy: 0,
        upToDate: true,
        checkedAt: "2026-07-14T09:00:00Z",
      }),
    }),
  );
}

/**
 * The configure wizard's branch picker POSTs `/v1/wiki/branches` (another
 * real `ls-remote`) the moment the Source step mounts, since `state.url` is
 * prefilled from the query param. Empty branches + null defaultBranch is
 * what yields the deterministic "Default branch" fallback text — the
 * query's error/empty path is a silent swallow by design (see
 * `wiki/CLAUDE.md` "Branch picker (generation step)"). Call BEFORE navigating.
 */
export async function stubBranches(page: Page): Promise<void> {
  await page.route("**/v1/wiki/branches", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ branches: [], defaultBranch: null }),
    }),
  );
}
