import { useState, useEffect } from 'react';
import {
  Loader2,
  AlertTriangle,
  Puzzle,
  Trash2,
  Download,
  CheckCircle2,
  Search,
  Tag,
} from 'lucide-react';
import {
  listPlugins,
  listMarketplacePlugins,
  installPlugin,
  uninstallPlugin,
} from '../api/client';
import type { MarketplacePlugin, PluginSummary } from '../api/client';
import { Button } from './ui/Button';

function Badge({ children, variant = 'default' }: { children: React.ReactNode; variant?: 'default' | 'success' | 'muted' }) {
  const cls =
    variant === 'success'
      ? 'bg-[hsl(var(--success))]/15 text-[hsl(var(--success))] border-[hsl(var(--success))]/20'
      : variant === 'muted'
      ? 'bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] border-[hsl(var(--border))]'
      : 'bg-[hsl(var(--primary))]/15 text-[hsl(var(--primary))] border-[hsl(var(--primary))]/20';
  return (
    <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium leading-none border ${cls}`}>
      {children}
    </span>
  );
}

function CountBadge({ label, count }: { label: string; count: number }) {
  if (count === 0) return null;
  return <Badge variant="muted">{count} {label}</Badge>;
}

export default function PluginsView() {
  const [plugins, setPlugins] = useState<PluginSummary[]>([]);
  const [marketplace, setMarketplace] = useState<MarketplacePlugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);

  useEffect(() => {
    void loadData();
  }, []);

  async function loadData() {
    setLoading(true);
    setError(null);
    try {
      const [installed, available] = await Promise.all([
        listPlugins(),
        listMarketplacePlugins(),
      ]);
      setPlugins(installed);
      const installedNames = new Set(installed.map((p) => p.name));
      setMarketplace(
        available.map((p) => ({ ...p, installed: installedNames.has(p.name) }))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load plugins');
    } finally {
      setLoading(false);
    }
  }

  function showSuccess(msg: string) {
    setActionSuccess(msg);
    window.setTimeout(() => setActionSuccess(null), 3000);
  }

  async function handleUninstall(name: string) {
    setPendingAction(name);
    setActionError(null);
    try {
      await uninstallPlugin(name);
      showSuccess(`Uninstalled "${name}".`);
      await loadData();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : `Failed to uninstall "${name}"`);
    } finally {
      setPendingAction(null);
    }
  }

  async function handleInstall(name: string, mp: string) {
    const key = `install:${name}`;
    setPendingAction(key);
    setActionError(null);
    try {
      await installPlugin(name, mp);
      showSuccess(`Installed "${name}".`);
      await loadData();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : `Failed to install "${name}"`);
    } finally {
      setPendingAction(null);
    }
  }

  const categories = Array.from(new Set(marketplace.map((p) => p.category).filter(Boolean))).sort();

  const filteredMarketplace = marketplace.filter((p) => {
    const q = search.toLowerCase();
    const matchesSearch = !q || p.name.toLowerCase().includes(q) || p.description.toLowerCase().includes(q);
    const matchesCategory = !categoryFilter || p.category === categoryFilter;
    return matchesSearch && matchesCategory;
  });

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-6 py-8 space-y-8">
        {/* Page header */}
        <div>
          <h1 className="text-lg font-semibold text-[hsl(var(--foreground))] flex items-center gap-2">
            <Puzzle className="w-5 h-5" />
            Plugins
          </h1>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
            Manage installed plugins and discover new ones from the marketplace.
          </p>
        </div>

        {/* Global error */}
        {error && (
          <div className="flex items-start gap-2 rounded-lg border border-[hsl(var(--destructive))]/30 bg-[hsl(var(--destructive))]/10 px-3 py-2.5">
            <AlertTriangle className="w-4 h-4 text-[hsl(var(--destructive))] shrink-0 mt-0.5" />
            <p className="text-xs text-[hsl(var(--destructive))]">{error}</p>
          </div>
        )}

        {/* Action feedback */}
        {actionError && (
          <div className="flex items-start gap-2 rounded-lg border border-[hsl(var(--destructive))]/30 bg-[hsl(var(--destructive))]/10 px-3 py-2.5">
            <AlertTriangle className="w-4 h-4 text-[hsl(var(--destructive))] shrink-0 mt-0.5" />
            <p className="text-xs text-[hsl(var(--destructive))]">{actionError}</p>
          </div>
        )}
        {actionSuccess && (
          <div className="flex items-center gap-2 rounded-lg border border-[hsl(var(--success))]/30 bg-[hsl(var(--success))]/10 px-3 py-2.5">
            <CheckCircle2 className="w-4 h-4 text-[hsl(var(--success))] shrink-0" />
            <p className="text-xs text-[hsl(var(--success))]">{actionSuccess}</p>
          </div>
        )}

        {/* ── Section 1: Installed Plugins ── */}
        <section>
          <h2 className="text-sm font-semibold text-[hsl(var(--foreground))] mb-3">
            Installed
            {plugins.length > 0 && (
              <span className="ml-2 text-xs font-normal text-[hsl(var(--muted-foreground))]">
                ({plugins.length})
              </span>
            )}
          </h2>

          {plugins.length === 0 ? (
            <p className="text-sm text-[hsl(var(--muted-foreground))]">No plugins installed.</p>
          ) : (
            <div className="space-y-2">
              {plugins.map((plugin) => {
                const isPending = pendingAction === plugin.name;
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
                        {plugin.version && <Badge variant="muted">v{plugin.version}</Badge>}
                        <Badge variant="muted">{plugin.marketplace}</Badge>
                        {plugin.scope && plugin.scope !== 'user' && <Badge variant="muted">{plugin.scope}</Badge>}
                        {plugin.has_hooks && <Badge variant="success">hooks</Badge>}
                      </div>
                      {plugin.description && (
                        <p className="text-xs text-[hsl(var(--muted-foreground))] leading-snug">
                          {plugin.description}
                        </p>
                      )}
                      <div className="flex items-center gap-1.5 flex-wrap pt-0.5">
                        <CountBadge label="skills" count={plugin.skills} />
                        <CountBadge label="agents" count={plugin.agents} />
                        <CountBadge label="commands" count={plugin.commands} />
                        <CountBadge label="MCP" count={plugin.mcp_servers} />
                      </div>
                    </div>
                    <Button
                      variant="neutral"
                      size="sm"
                      tone="danger"
                      onClick={() => void handleUninstall(plugin.name)}
                      disabled={isPending}
                      aria-label={`Uninstall ${plugin.name}`}
                      leadingIcon={
                        isPending ? (
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
        </section>

        {/* ── Section 2: Marketplace ── */}
        <section>
          <h2 className="text-sm font-semibold text-[hsl(var(--foreground))] mb-3">Marketplace</h2>

          {/* Search + category filter */}
          <div className="flex items-center gap-2 mb-3 flex-wrap">
            <div className="relative flex-1 min-w-[160px]">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[hsl(var(--muted-foreground))]" />
              <input
                type="text"
                placeholder="Search plugins…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full pl-8 pr-3 py-1.5 text-xs rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] text-[hsl(var(--foreground))] placeholder-[hsl(var(--muted-foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--ring))]"
              />
            </div>
            {categories.length > 0 && (
              <div className="relative">
                <Tag className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[hsl(var(--muted-foreground))]" />
                <select
                  value={categoryFilter}
                  onChange={(e) => setCategoryFilter(e.target.value)}
                  className="pl-8 pr-3 py-1.5 text-xs rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] text-[hsl(var(--foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--ring))] appearance-none"
                >
                  <option value="">All categories</option>
                  {categories.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          {filteredMarketplace.length === 0 ? (
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              {marketplace.length === 0 ? 'No marketplace plugins available.' : 'No plugins match your search.'}
            </p>
          ) : (
            <div className="space-y-2">
              {filteredMarketplace.map((plugin) => {
                const actionKey = `install:${plugin.name}`;
                const isPending = pendingAction === actionKey;
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
                        {plugin.category && <Badge variant="muted">{plugin.category}</Badge>}
                        <Badge variant="muted">{plugin.marketplace}</Badge>
                        {plugin.installed && <Badge variant="success">Installed</Badge>}
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
                        onClick={() => void handleInstall(plugin.name, plugin.marketplace)}
                        disabled={isPending}
                        aria-label={`Install ${plugin.name}`}
                        leadingIcon={
                          isPending ? (
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
        </section>
      </div>
    </div>
  );
}
