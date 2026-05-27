/**
 * Faceted settings IA — the top-level groups (models, agent, integrations,
 * interface, server, security, workspace; plus an "other" fallback) that the
 * Settings shell renders as a sidebar.
 *
 * This file is intentionally React-free: icons are referenced by their
 * lucide-react *name* (a string) so the model and its tests stay pure and
 * trivially unit-testable. The shell maps `iconName` → a lucide component.
 *
 * Facet ids are matched against the schema's `x-group` def metadata by
 * `SettingsModel`. The `security` facet carries no config sections — it is
 * declared here so the shell can render it and later populate it with API
 * tokens + a secrets summary.
 */

/** One presentation facet — id, human title, lucide icon name, sort order. */
export interface FacetMeta {
  id: string;
  title: string;
  iconName: string;
  order: number;
}

/** Ordered facet list — the canonical Settings information architecture. */
export const FACETS: readonly FacetMeta[] = [
  { id: "models", title: "Models & Inference", iconName: "Cpu", order: 1 },
  { id: "agent", title: "Agent & Tools", iconName: "Wrench", order: 2 },
  { id: "integrations", title: "Integrations", iconName: "Plug", order: 3 },
  { id: "interface", title: "Interface", iconName: "Monitor", order: 4 },
  { id: "server", title: "Server & Storage", iconName: "Server", order: 5 },
  { id: "security", title: "Security & Access", iconName: "Shield", order: 6 },
  { id: "workspace", title: "Workspace", iconName: "FolderGit2", order: 7 },
  { id: "other", title: "Other", iconName: "Settings2", order: 99 },
] as const;

/** The fallback facet id for sections with no `x-group` annotation. */
export const FALLBACK_FACET_ID = "other";
