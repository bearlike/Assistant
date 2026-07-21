/**
 * PluginsPane — installed plugins + marketplace browsing (Plugins facet).
 *
 * A pane in the facet registry (`../panes.ts`): zero props, fetches its own
 * data via `usePlugins()` (TanStack Query — see that hook for the queryKey /
 * mutation contract), chrome-agnostic (bare `<SettingsCard>`s, no page width/
 * padding/header). Replaces the old standalone `PluginsView` route.
 *
 * Plugins is its OWN top-level facet, so this pane sits directly above the
 * schema-driven "Plugins" section (`PluginsConfig`, `x-group: "plugins"`) that
 * holds the knobs governing it — the marketplace list this pane browses, the
 * enabled-plugins allowlist, the install path. Pane and section moved off the
 * `agent` facet together; per the pane contract they always must.
 */
import { useState } from "react";
import { Download, Loader2, Search, Trash2 } from "lucide-react";

import { Badge } from "../../agents";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";
import { ErrorAlert } from "../../ErrorAlert";
import { usePlugins } from "../../../hooks/usePlugins";
import { FilterDropdown } from "../FilterDropdown";
import { SettingsCard } from "../SettingsCard";

/** "1 skill" / "2 skills" — a bare `{count} {label}` rendered "1 agents". */
function CountBadge({ singular, count }: { singular: string; count: number }) {
  if (count === 0) return null;
  return (
    <Badge color="muted">
      {count} {count === 1 ? singular : `${singular}s`}
    </Badge>
  );
}

const INSTALLED_DESCRIPTION =
  "The plugins active in this deployment. Each one gives the agent new capabilities.\n\n" +
  "Installing a plugin is not cosmetic. Its **skills, agent definitions, hooks, and MCP " +
  "tools** all become available to the agent, and a hook can run on this machine, so install " +
  "only what you trust. Uninstalling takes those capabilities away again, from every session " +
  "started afterwards.";

const MARKETPLACE_DESCRIPTION =
  "The plugins on offer from the marketplaces you have configured.\n\n" +
  "Every listing here comes from a catalog repository named in the **Plugins → marketplaces** " +
  "setting further down this page. Add a marketplace there and its plugins appear here.";

export function PluginsPane() {
  const {
    plugins,
    marketplace,
    loading,
    error,
    install,
    isInstalling,
    uninstall,
    isUninstalling,
  } = usePlugins();
  const [search, setSearch] = useState("");
  // `"all"` is the shared no-filter sentinel of `<FilterDropdown>`.
  const [categoryFilter, setCategoryFilter] = useState("all");

  const categoryOptions = Array.from(
    new Set(marketplace.map((p) => p.category).filter(Boolean))
  )
    .sort()
    .map((c) => ({ value: c as string, label: c as string }));

  const filteredMarketplace = marketplace.filter((p) => {
    const q = search.toLowerCase();
    const matchesSearch =
      !q || p.name.toLowerCase().includes(q) || p.description.toLowerCase().includes(q);
    const matchesCategory = categoryFilter === "all" || p.category === categoryFilter;
    return matchesSearch && matchesCategory;
  });

  return (
    <div className="space-y-4">
      <SettingsCard
        id="settings-plugins-installed"
        title="Installed plugins"
        description={INSTALLED_DESCRIPTION}
      >
        {error && (
          <ErrorAlert error={error} fallback="Failed to load plugins" className="mb-3" />
        )}

        {loading ? (
          <div className="flex items-center justify-center py-6">
            <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
          </div>
        ) : plugins.length === 0 ? (
          <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
            <p className="text-sm text-[hsl(var(--muted-foreground))]">No plugins installed.</p>
            <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
              The agent runs on its built-in tools alone. Install one from the marketplace below
              to give it new skills, hooks, or MCP tools.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {plugins.map((plugin) => {
              const pending = isUninstalling(plugin.name);
              return (
                <div
                  key={plugin.name}
                  className="flex items-start justify-between gap-4 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-4 py-3"
                >
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-[hsl(var(--foreground))]">
                        {plugin.name}
                      </span>
                      {plugin.version && <Badge color="muted">v{plugin.version}</Badge>}
                      <Badge color="muted">{plugin.marketplace}</Badge>
                      {plugin.scope && plugin.scope !== "user" && (
                        <Badge color="muted">{plugin.scope}</Badge>
                      )}
                      {plugin.has_hooks && <Badge color="emerald">hooks</Badge>}
                    </div>
                    {plugin.description && (
                      <p className="text-xs text-[hsl(var(--muted-foreground))] leading-snug">
                        {plugin.description}
                      </p>
                    )}
                    <div className="flex items-center gap-1.5 flex-wrap pt-0.5">
                      <CountBadge singular="skill" count={plugin.skills} />
                      <CountBadge singular="agent" count={plugin.agents} />
                      <CountBadge singular="command" count={plugin.commands} />
                      <CountBadge singular="MCP server" count={plugin.mcp_servers} />
                    </div>
                  </div>
                  <Button
                    variant="neutral"
                    size="sm"
                    tone="danger"
                    onClick={() => uninstall(plugin.name)}
                    disabled={pending}
                    aria-label={`Uninstall ${plugin.name}`}
                    leadingIcon={
                      pending ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <Trash2 className="w-3.5 h-3.5" />
                      )
                    }
                    className="shrink-0"
                  >
                    Uninstall
                  </Button>
                </div>
              );
            })}
          </div>
        )}
      </SettingsCard>

      <SettingsCard
        id="settings-plugins-marketplace"
        title="Marketplace"
        description={MARKETPLACE_DESCRIPTION}
      >
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <div className="relative flex-1 min-w-[160px]">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[hsl(var(--muted-foreground))]" />
            <Input
              type="search"
              placeholder="Search plugins…"
              aria-label="Search marketplace plugins"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8"
            />
          </div>
          {categoryOptions.length > 0 && (
            <FilterDropdown
              label="Category"
              value={categoryFilter}
              options={categoryOptions}
              onChange={setCategoryFilter}
            />
          )}
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-6">
            <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
          </div>
        ) : filteredMarketplace.length === 0 ? (
          marketplace.length === 0 ? (
            <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                No marketplace plugins available.
              </p>
              <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
                Add a catalog repository under Plugins → marketplaces, below, and its plugins show
                up here.
              </p>
            </div>
          ) : (
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              No plugins match your search.
            </p>
          )
        ) : (
          <div className="space-y-2">
            {filteredMarketplace.map((plugin) => {
              const pending = isInstalling(plugin.name);
              return (
                <div
                  key={`${plugin.marketplace}:${plugin.name}`}
                  className="flex items-start justify-between gap-4 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-4 py-3"
                >
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-[hsl(var(--foreground))]">
                        {plugin.name}
                      </span>
                      {plugin.category && <Badge color="muted">{plugin.category}</Badge>}
                      <Badge color="muted">{plugin.marketplace}</Badge>
                      {plugin.installed && <Badge color="emerald">Installed</Badge>}
                    </div>
                    {plugin.description && (
                      <p className="text-xs text-[hsl(var(--muted-foreground))] leading-snug">
                        {plugin.description}
                      </p>
                    )}
                  </div>
                  {!plugin.installed && (
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => install(plugin.name, plugin.marketplace)}
                      disabled={pending}
                      aria-label={`Install ${plugin.name}`}
                      leadingIcon={
                        pending ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        ) : (
                          <Download className="w-3.5 h-3.5" />
                        )
                      }
                      className="shrink-0"
                    >
                      Install
                    </Button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </SettingsCard>
    </div>
  );
}
