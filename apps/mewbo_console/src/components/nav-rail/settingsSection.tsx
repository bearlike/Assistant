import { useMemo } from "react";
import { useSearchParams } from "wouter";
import { SlidersHorizontal } from "lucide-react";

import { useConfig } from "@/hooks/useConfig";
import { SettingsModel } from "@/components/settings/SettingsModel";
import { FACET_PANES } from "@/components/settings/panes";
import { facetIcon } from "@/components/settings/facetIcons";
import { RailActionRow, RailEmpty, RailSection } from "./rows";

/** The URL param carrying the active facet id — the Settings shell's own key. */
const FACET_PARAM = "facet";

// The `iconName` → lucide resolution lives in `settings/facetIcons.ts`. It used
// to be a private copy here and a second one in the Settings shell, and the
// glyph was the one thing that could silently lag between them (the FACET LIST
// never could — it is read from the model). One module now owns both the map
// and the unmapped-name fallback.

/**
 * Settings occupies zone 3 the same way a product's recents do: the zone shows
 * what is INSIDE the current scope, and for Settings that is its facets.
 *
 * This is why `/settings` marks no product row current and still has primary
 * navigation — these rows ARE it. It also stops the rail from advertising
 * another product's recents while the user is somewhere else entirely, and lets
 * the Settings page stop spending its own width on a second nav column.
 *
 * Self-contained per the section contract: it calls `useConfig` itself rather
 * than taking facets as props, and the TanStack cache dedupes that against the
 * shell's identical call, so mounting this costs no extra request.
 */
export function SettingsFacetsSection() {
  const [params, setParams] = useSearchParams();
  const { schema, config, loading } = useConfig();

  const model = useMemo(
    () => (schema && config ? new SettingsModel(schema, config) : null),
    [schema, config],
  );

  // The SAME visibility rule the shell applies: a facet renders if it has
  // schema sections OR custom panes. Offering a facet the shell would refuse to
  // show would strand the row on a page that silently redirects elsewhere. No
  // facet is named here either — "has panes" stays a registry lookup.
  const facets = useMemo(
    () =>
      (model?.groups() ?? []).filter(
        (g) => g.sections.length > 0 || (FACET_PANES[g.id]?.length ?? 0) > 0,
      ),
    [model],
  );

  // An absent or unknown `?facet=` resolves to the first visible facet, because
  // that is what the shell lands on — read the param alone and the rail would
  // mark nothing current on the very first paint of `/settings`.
  const facetParam = params.get(FACET_PARAM);
  const currentFacet =
    facets.find((f) => f.id === facetParam)?.id ?? facets[0]?.id;

  const selectFacet = (id: string) => {
    // Rebuild from `prev` and set only our own key, mirroring the shell: a
    // `?session=` deep-link into the Automation pane must survive a facet
    // switch. `replace` keeps facet clicks out of the history stack, so Back
    // leaves Settings instead of walking facets.
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(FACET_PARAM, id);
        return next;
      },
      { replace: true },
    );
  };

  return (
    // `divider={false}`: this header OPENS zone 3 (Settings has no action row
    // above it), so a rule here would double the zone divider already there.
    <RailSection label="Settings" icon={SlidersHorizontal} divider={false}>
      {loading && facets.length === 0 && <RailEmpty>Loading settings…</RailEmpty>}
      {!loading && facets.length === 0 && (
        <RailEmpty>Settings are unavailable.</RailEmpty>
      )}
      {facets.map((facet) => (
        <RailActionRow
          key={facet.id}
          icon={facetIcon(facet.iconName)}
          label={facet.title}
          current={currentFacet === facet.id}
          onClick={() => selectFacet(facet.id)}
        />
      ))}
    </RailSection>
  );
}
