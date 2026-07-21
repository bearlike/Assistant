import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * The ONE module that knows where captured images land and what the seeded data
 * is called. Specs import from here; nothing else hardcodes a path or a title.
 */

// __dirname equivalent for this ESM package ("type":"module"):
//   apps/mewbo_console/tests/demo/
const HERE = dirname(fileURLToPath(import.meta.url));
// Four up: tests/demo -> tests -> mewbo_console -> apps -> <repo root>, then into
// the docs asset dir. Resolved absolutely so the run is invariant to the cwd
// Playwright was launched from.
const IMG_DIR = resolve(HERE, "../../../../docs/assets/img");

export type ShotName =
  // console (Phase 2)
  | "front"
  | "tasks"
  | "shellLog"
  | "fileReadLog"
  | "fileEdit"
  | "widgets"
  // wiki (Phase 3 — this extension)
  | "wikiLanding"
  | "wikiOverview"
  | "wikiIndexSource"
  | "wikiIndexGeneration"
  | "wikiIndexScope"
  | "wikiBadge"
  | "wikiQna"
  | "wikiIndexingProgress"
  // agentic search (Phase 3 — this extension)
  | "searchLanding"
  | "searchResults"
  | "searchAgentTrace"
  // settings + plan mode (Phase 4 — legacy hand-captures brought into the pipeline)
  | "settingsModels"
  | "settingsSecurity"
  | "planApproval";

/**
 * Overwrite the docs images IN PLACE: same basenames and extensions the docs
 * `<img>` / video-poster tags already reference (verified in docs/). Console
 * 01/02/07 and the wiki indexing-progress shot are PNG; every other capture
 * keeps the JPEG the hand-authored original used (docs render these <=720px
 * wide, and JPEG-at-90 is byte-deterministic given an identical raster + the
 * pinned Playwright encoder — the same property the console closeups already
 * rely on).
 */
export const SHOTS: Record<ShotName, { file: string; type: "png" | "jpeg" }> = {
  front: { file: "mewbo-console-01-front.png", type: "png" },
  tasks: { file: "mewbo-console-02-tasks.png", type: "png" },
  shellLog: { file: "mewbo-console-shell-log.jpg", type: "jpeg" },
  fileReadLog: { file: "mewbo-console-file-read-log.jpg", type: "jpeg" },
  fileEdit: { file: "mewbo-console-04-file-edit.jpg", type: "jpeg" },
  widgets: { file: "mewbo-console-07-widgets.png", type: "png" },
  wikiLanding: { file: "mewbo-wiki-01-landing.jpg", type: "jpeg" },
  wikiOverview: { file: "mewbo-wiki-02-overview.jpg", type: "jpeg" },
  wikiIndexSource: { file: "mewbo-wiki-04-index-source.jpg", type: "jpeg" },
  wikiIndexGeneration: { file: "mewbo-wiki-05-index-generation.jpg", type: "jpeg" },
  wikiIndexScope: { file: "mewbo-wiki-06-index-scope.jpg", type: "jpeg" },
  wikiBadge: { file: "mewbo-wiki-07-badge.jpg", type: "jpeg" },
  wikiQna: { file: "mewbo-wiki-08-qna.jpg", type: "jpeg" },
  wikiIndexingProgress: {
    file: "mewbo-wiki-09-indexing-progress.png",
    type: "png",
  },
  searchLanding: { file: "mewbo-search-01-landing.jpg", type: "jpeg" },
  searchResults: { file: "mewbo-search-02-results.jpg", type: "jpeg" },
  searchAgentTrace: { file: "mewbo-search-04-agent-trace.jpg", type: "jpeg" },
  settingsModels: { file: "mewbo-settings-01-models.jpg", type: "jpeg" },
  settingsSecurity: { file: "mewbo-settings-02-security.jpg", type: "jpeg" },
  planApproval: { file: "mewbo-console-03-plan-approval.jpg", type: "jpeg" },
};

export function shotPath(name: ShotName): string {
  return resolve(IMG_DIR, SHOTS[name].file);
}

// High enough that syntax-highlight edges stay crisp; matches the source
// rasters' apparent quality. (docs render these at <=720px CSS width anyway.)
export const JPEG_QUALITY = 90;

/**
 * The canonical seeded data this harness captures against (owned by the
 * seeder). These strings are the contract with the seeder: the
 * flows anchor on them, so a title/command/path change is the intended
 * "the demo is stale" signal (script breakage == staleness).
 */
export const SEED = {
  session: {
    // The session whose detail view backs shot 02 and whose two tool steps back
    // the shell-log / file-read-log closeups.
    authRefactorTitle: "Refactor the auth middleware",
    infraReportTitle: "Weekly infra health report",
    dinnerTitle: "Plan a birthday dinner menu",
    // The session whose rendered stlite widget backs the widgets shot (07).
    widgetTitle: "Trending LLM agent harness repositories",
    // The two-revision plan session backing the plan-approval shot (03).
    planTitle: "Scope API keys to one workspace",
  },
  // Shell tool step inside the auth-refactor session.
  shellCommand: "npm test",
  // File-read tool step inside the auth-refactor session.
  readPath: "src/middleware/auth.ts",
  readFile: "auth.ts",
  // Issued API keys backing the Security settings shot. These labels are the
  // seed↔flow contract (bundle `api_keys[].label`); they are invented demo
  // strings and must never carry a real deployment's key naming.
  apiKeyLabels: ["laptop-cli", "ci-pipeline (nightly docs build)"],
  // The fictional proxy base rendered by the Models settings shot — mirrors
  // `demo/configs/app.json` -> llm.api_base. Never a real internal host.
  llmApiBase: "https://llm.example.com/v1",
} as const;
