import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  installPlugin,
  listMarketplacePlugins,
  listPlugins,
  uninstallPlugin,
} from "../api/client";
import type { MarketplacePlugin, PluginSummary } from "../api/client";
import { getErrorMessage, logApiError } from "../utils/errors";

/** Root key shared by both plugin queries, so one `invalidateQueries({queryKey: PLUGINS_ROOT})`
 * after install/uninstall refreshes the installed list AND the marketplace's
 * derived `installed` flag together. */
const PLUGINS_ROOT = ["plugins"] as const;
const PLUGINS_KEY = [...PLUGINS_ROOT, "installed"] as const;
const MARKETPLACE_KEY = [...PLUGINS_ROOT, "marketplace"] as const;

/**
 * Installed + marketplace plugin data for the Plugins pane (Agent & Tools
 * facet). Replaces the old `PluginsView`'s hand-rolled `useState`/`useEffect`
 * `loadData()` + a `pendingAction`/`actionError`/`actionSuccess` state machine
 * with `window.setTimeout` as a fake toast — install/uninstall feedback now
 * rides `sonner` (already mounted at the app root via `NotificationBalloon`).
 */
export function usePlugins() {
  const qc = useQueryClient();

  const pluginsQ = useQuery<PluginSummary[]>({
    queryKey: PLUGINS_KEY,
    queryFn: listPlugins,
  });
  const marketplaceQ = useQuery<MarketplacePlugin[]>({
    queryKey: MARKETPLACE_KEY,
    queryFn: listMarketplacePlugins,
  });

  const installedNames = new Set((pluginsQ.data ?? []).map((p) => p.name));
  const marketplace: MarketplacePlugin[] = (marketplaceQ.data ?? []).map((p) => ({
    ...p,
    installed: installedNames.has(p.name),
  }));

  const installM = useMutation({
    mutationFn: (vars: { name: string; marketplace: string }) =>
      installPlugin(vars.name, vars.marketplace),
    onSuccess: (_data, vars) => {
      toast.success(`Installed "${vars.name}".`);
      void qc.invalidateQueries({ queryKey: PLUGINS_ROOT });
    },
    onError: (err, vars) => {
      toast.error(`Failed to install "${vars.name}" — ${getErrorMessage(err)}`);
    },
  });

  const uninstallM = useMutation({
    mutationFn: (name: string) => uninstallPlugin(name),
    onSuccess: (_data, name) => {
      toast.success(`Uninstalled "${name}".`);
      void qc.invalidateQueries({ queryKey: PLUGINS_ROOT });
    },
    onError: (err, name) => {
      toast.error(`Failed to uninstall "${name}" — ${getErrorMessage(err)}`);
    },
  });

  const errSource = pluginsQ.error || marketplaceQ.error;
  return {
    plugins: pluginsQ.data ?? [],
    marketplace,
    loading: pluginsQ.isPending || marketplaceQ.isPending,
    error: errSource ? logApiError("plugins", errSource) : null,
    install: (name: string, marketplaceName: string) =>
      installM.mutate({ name, marketplace: marketplaceName }),
    isInstalling: (name: string) =>
      installM.isPending && installM.variables?.name === name,
    uninstall: (name: string) => uninstallM.mutate(name),
    isUninstalling: (name: string) =>
      uninstallM.isPending && uninstallM.variables === name,
  };
}
