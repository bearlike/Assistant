/**
 * Faceted settings IA — the top-level groups (models, agent, plugins,
 * automation, integrations, interface, server, security, workspace; plus an
 * "other" fallback) that the Settings shell renders as a sidebar.
 *
 * This file is intentionally React-free: icons are referenced by their
 * lucide-react *name* (a string) so the model and its tests stay pure and
 * trivially unit-testable. The shell maps `iconName` → a lucide component.
 *
 * Facet ids are matched against the schema's `x-group` def metadata by
 * `SettingsModel`. A facet with no config sections is still declared here (and
 * still renders) when it carries custom panes — see `panes.ts`, whose registry
 * is keyed by the `FacetId` union below, so an unknown facet id is a TS error.
 */

/**
 * The closed set of facet ids. This is the contract two other files key off:
 * `panes.ts` (facet → custom panes) and core's `x-group` annotations
 * (`config.py`). A section whose `x-group` is not in this union is silently
 * bucketed into `other` by `SettingsModel` — so adding an `x-group` in core
 * means adding its id HERE, or the section vanishes into the fallback facet.
 */
export type FacetId =
  | "models"
  | "agent"
  | "plugins"
  | "automation"
  | "apps"
  | "integrations"
  | "interface"
  | "server"
  | "security"
  | "access"
  | "workspace"
  | "other";

/** One presentation facet — id, human title, blurb, lucide icon name, order. */
export interface FacetMeta {
  id: FacetId;
  title: string;
  /**
   * One or two plain sentences: what a user configures here and why they'd come
   * to this page. Rendered ONCE by the shell under the facet heading (through
   * `FieldHelp`, the surface's only description renderer) — never per-card.
   *
   * Keep it a single line under `FieldHelp`'s 140-char threshold. A break-free
   * description longer than that renders popover-only (case 3), so the facet
   * heading would silently lose its blurb. No em dashes: connected prose, in
   * full sentences, is the copy standard for this surface.
   */
  blurb: string;
  iconName: string;
  order: number;
}

/** Ordered facet list — the canonical Settings information architecture. */
export const FACETS: readonly FacetMeta[] = [
  {
    id: "models",
    title: "Models & Inference",
    blurb:
      "Pick which model answers you, how much conversation it can hold, and when older turns get summarized away.",
    iconName: "Cpu",
    order: 1,
  },
  {
    id: "agent",
    title: "Agent & Tools",
    blurb:
      "Decide what the agent may do on your machine: which tools it can reach, and what it has to ask your permission for first.",
    iconName: "Wrench",
    order: 2,
  },
  {
    id: "plugins",
    title: "Plugins",
    blurb:
      "Extend the agent with plugins. A plugin brings its own skills, agent definitions, hooks, and MCP tools, installed from a marketplace.",
    iconName: "Puzzle",
    order: 3,
  },
  {
    id: "automation",
    title: "Automation",
    blurb:
      "Let Mewbo start a session on its own, later: at a set time, on a repeating schedule, or when CI, a pull request, or a webhook says so.",
    iconName: "AlarmClock",
    order: 4,
  },
  {
    id: "apps",
    title: "Apps",
    blurb:
      "Manage the small tools Mewbo builds and keeps running for you: pause or resume their scheduled refreshes, open them, or archive ones you no longer need.",
    iconName: "AppWindow",
    order: 5,
  },
  {
    id: "integrations",
    title: "Integrations",
    blurb:
      "Connect Mewbo to the outside services it talks to, such as Home Assistant, Langfuse tracing, and your own event hooks.",
    iconName: "Plug",
    order: 6,
  },
  {
    id: "interface",
    title: "Interface",
    blurb: "Change how the chat and terminal surfaces look and behave.",
    iconName: "Monitor",
    order: 7,
  },
  {
    id: "server",
    title: "Server & Storage",
    blurb:
      "Set where the API listens and where sessions, events, and working files are kept.",
    iconName: "Server",
    order: 8,
  },
  {
    id: "security",
    title: "Security & Access",
    blurb:
      "Manage what can call Mewbo and what Mewbo can reach: API keys, git credentials, and the secrets it already holds.",
    iconName: "Shield",
    order: 9,
  },
  {
    id: "access",
    title: "Identity & Access",
    blurb:
      "See who can sign in, which teams they belong to, and what their roles let them do.",
    iconName: "Users",
    order: 10,
  },
  {
    id: "workspace",
    title: "Workspace",
    blurb:
      "Point Mewbo at the directories it works in, both the ones you register and the ones it creates itself, and set how it indexes them.",
    iconName: "FolderGit2",
    order: 11,
  },
  {
    id: "other",
    title: "Other",
    blurb: "Settings that don't belong to any facet yet.",
    iconName: "Settings2",
    order: 99,
  },
] as const;

/** The fallback facet id for sections with no (or an unknown) `x-group`. */
export const FALLBACK_FACET_ID: FacetId = "other";
