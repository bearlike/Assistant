/**
 * FACET_PANES — the facet → custom-panes registry.
 *
 * Some facets carry surfaces that are NOT driven by the backend config schema
 * (API keys, git credentials, projects, plugins, triggers…). They used to be
 * mounted by a hardcoded `activeGroup.id === "security" ? … : …` ternary in
 * `SettingsView`; copy-pasting that branch per facet is the DRY violation this
 * file removes. The shell now knows no facet by name — it looks the active
 * facet up here and renders whatever it finds ABOVE the schema-driven sections.
 * Adding a pane is ONE line here and zero lines in the shell.
 *
 * ## The pane contract — conform to this or you cannot be registered
 *
 * 1. **Zero props.** A pane is a `ComponentType` with no props, which is
 *    exactly what lets this registry be a plain data map instead of a switch.
 *    A pane fetches its own data via TanStack Query — re-calling a hook the
 *    shell already called is NOT a second fetch (the cache is shared by
 *    queryKey), so there is never a reason to prop-drill into a pane.
 * 2. **`React.lazy`.** Panes pull heavy deps (react-hook-form/zod, extra
 *    queries); they must not land in the Settings chunk until their facet is
 *    opened. Each gets its own `<Suspense>` boundary at the render site, so one
 *    slow chunk never blocks a sibling's fallback.
 * 3. **Chrome-agnostic.** A pane renders bare `<SettingsCard>`s and owns no
 *    page width/padding — the shell supplies the page. (This is what lets a
 *    pane also be mounted standalone by a route.)
 */
import { lazy, type ComponentType, type LazyExoticComponent } from "react";

import type { FacetId } from "./facets";

/** A registered pane: zero-prop, lazily loaded. */
export type SettingsPane = LazyExoticComponent<ComponentType>;

const SecretsSummary = lazy(() =>
  import("./panes/SecretsSummary").then((m) => ({ default: m.SecretsSummary }))
);
const ApiKeysView = lazy(() =>
  import("../ApiKeysView").then((m) => ({ default: m.ApiKeysView }))
);
const GitCredentialsView = lazy(() =>
  import("../GitCredentialsView").then((m) => ({
    default: m.GitCredentialsView,
  }))
);
const ProjectsPane = lazy(() =>
  import("./panes/ProjectsPane").then((m) => ({ default: m.ProjectsPane }))
);
const PluginsPane = lazy(() =>
  import("./panes/PluginsPane").then((m) => ({ default: m.PluginsPane }))
);
const TriggersPane = lazy(() =>
  import("./panes/TriggersPane").then((m) => ({ default: m.TriggersPane }))
);
const SystemInstructionsPane = lazy(() =>
  import("./panes/SystemInstructionsPane").then((m) => ({
    default: m.SystemInstructionsPane,
  }))
);
// The Apps management pane lives in the `apps` product namespace (it shares the
// gallery's hooks, badges, and router), not under `settings/panes/`.
const AppsPane = lazy(() =>
  import("../apps/AppsPane").then((m) => ({ default: m.AppsPane }))
);
const IdentityAccessPane = lazy(() =>
  import("./panes/IdentityAccessPane").then((m) => ({
    default: m.IdentityAccessPane,
  }))
);

/**
 * Facet id → its panes, in render order. Keyed by the `FacetId` union, so a
 * typo or a facet that `facets.ts` doesn't declare is a compile error.
 *
 * Each pane sits in the facet that owns its *settings*, so the live surface and
 * the knobs that govern it read as one page: the plugin marketplace next to
 * `plugins.marketplaces`, the trigger list next to the trigger policy ceilings,
 * managed projects next to the `projects` map. That adjacency is the whole
 * reason these four stopped being standalone pages — and it is why a pane and
 * its config section MOVE TOGETHER. Plugins is the worked example: it got its
 * own top-level facet, so `PluginsPane` and `PluginsConfig`'s `x-group`
 * (core `config.py`) both moved off `agent` in one change.
 */
export const FACET_PANES: Partial<Record<FacetId, SettingsPane[]>> = {
  agent: [SystemInstructionsPane],
  plugins: [PluginsPane],
  automation: [TriggersPane],
  apps: [AppsPane],
  security: [SecretsSummary, ApiKeysView, GitCredentialsView],
  // `access` carries no schema sections at all — identity lives in the IdP and
  // the IAM stores, not in `app.json`. A facet with only panes is a supported
  // shape: the shell's visibility test is "has sections OR has panes".
  access: [IdentityAccessPane],
  workspace: [ProjectsPane],
};

/**
 * Pane COUNT per facet, derived from `FACET_PANES` — the plain-data shape
 * `SettingsModel.visibleGroups()` accepts. The model is React-free by
 * convention (see its own header comment), and `FACET_PANES`'s values are
 * `ComponentType`s, so a count is what crosses that boundary, not the
 * registry itself. Any facet-nav renderer (the Settings shell, the NavRail
 * settings section) computes visibility off this one derivation instead of
 * re-testing "does this facet have panes" its own way.
 */
export const PANE_COUNTS: Partial<Record<FacetId, number>> = Object.fromEntries(
  Object.entries(FACET_PANES).map(([id, panes]) => [id, panes.length])
) as Partial<Record<FacetId, number>>;
