/**
 * SettingsView — the section pane for whichever facet the URL names.
 *
 * Used to also render its own facet sidebar; that's gone. The NavRail's
 * Settings zone (`nav-rail/settingsSection.tsx`) is the one facet picker now,
 * on every viewport, so this shell only READS `?facet=` — it never writes it.
 * The shell still OWNS edit state (a sectionId → formData map) so it can
 * drive the @modified filter and per-section Save/Reset; all grouping /
 * slicing / search / diff logic stays in `SettingsModel` — the shell never
 * recomputes it. Per-section patches are produced by `model.patchFor` and
 * persisted via `useConfig.savePatch`.
 *
 * The shell knows NO facet by name. A facet may carry schema-driven sections,
 * custom panes (`settings/panes.ts`), or both; panes render above sections,
 * each behind its own `<Suspense>`. Adding a pane is one line in `panes.ts`.
 *
 * The active facet is read from the URL as `?facet=<id>` (deep-linkable), and
 * a change re-focuses the pane heading — see the `previousFacetRef` effect.
 */
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "wouter";
import { Loader2, Search, SlidersHorizontal } from "lucide-react";
import { useConfig } from "../hooks/useConfig";
import { SettingsModel } from "./settings/SettingsModel";
import { SettingsSection } from "./settings/SettingsSection";
import { FACET_PANES, PANE_COUNTS } from "./settings/panes";
import { ConfigJsonViewer } from "./settings/ConfigJsonViewer";
import { FieldHelp } from "./settings/fields/FieldHelp";
import { ErrorAlert } from "./ErrorAlert";
import { Input } from "./ui/input";
import { Switch } from "./ui/switch";

// The `iconName` → lucide map moved to `settings/facetIcons.ts`. This shell no
// longer renders facet glyphs at all — the NavRail's Settings zone is the only
// facet navigation now — so keeping a copy here left a map nothing read.

/** The URL param carrying the active facet id. */
const FACET_PARAM = "facet";

/** A section's config value as a plain object (or `{}` for scalars/missing). */
function normalizeSection(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** Shared fallback for a pane whose lazy chunk is still loading. */
function PaneFallback() {
  return (
    <div className="flex items-center justify-center py-8">
      <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------

export function SettingsView() {
  const {
    schema,
    config,
    secrets,
    loading,
    error,
    savePatch,
  } = useConfig();

  const model = useMemo(
    () => (schema && config ? new SettingsModel(schema, config) : null),
    [schema, config]
  );

  // Shell-owned edit state: one formData blob per section, but ONLY for a
  // section actually edited — a section absent from `formState` reads
  // straight from `config` via `sectionValue` below, live, every render.
  //
  // This used to be pre-seeded from `config` by a `useEffect` that copied
  // every section in once config arrived. That left a real window on a
  // section's FIRST render, before that effect could run: `SettingsSection`
  // received a bare `{}` for `value`, RJSF's `<Form>` computed ITS OWN schema
  // defaults for any array field the section has (an empty `list[str]`
  // defaults to `[]`), and fired `onChange` with that computed shape — which
  // looks exactly like a real edit and got written into `formState`,
  // permanently shadowing the real config value (e.g. `plugins.marketplaces`
  // stuck at `[]` forever, no matter what was actually saved). Effects run
  // child-before-parent, so RJSF's onChange — fired from the child `Form` —
  // always won that race against this component's own seed effect; it only
  // ever surfaced for a section landed on DIRECTLY (as the very first active
  // facet) that also has an array field, which is exactly what exposed it:
  // the default "models" facet's `llm` section has none.
  //
  // Resolving the fallback INLINE at read time instead closes the window
  // outright: a section's first render already sees real data, so RJSF's
  // computed defaults match it and its onChange is a no-op. `formState` now
  // holds ONLY genuine edits (from `onChange` below, or a post-save re-seed
  // in `handleSectionSave`), never a copy of pristine config.
  const [formState, setFormState] = useState<Record<string, Record<string, unknown>>>({});
  const sectionValue = (id: string): Record<string, unknown> =>
    formState[id] ?? normalizeSection(config?.[id]);

  // `setSearchParams` isn't needed here anymore — nothing in this component
  // writes `?facet=` now that the rail owns facet selection; this shell only
  // reads it.
  const [searchParams] = useSearchParams();
  const facetParam = searchParams.get(FACET_PARAM);

  const [query, setQuery] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [modifiedOnly, setModifiedOnly] = useState(false);

  const paneHeadingRef = useRef<HTMLHeadingElement>(null);

  // Visible facets: a facet renders if it has schema sections OR custom panes.
  // (No facet is named here — the rule itself lives on the model now, so this
  // shell and the NavRail settings section can't drift by each re-deriving it.)
  const visibleGroups = useMemo(
    () => model?.visibleGroups(PANE_COUNTS) ?? [],
    [model]
  );

  // The active facet is derived from the URL directly — no local state, no
  // seed-once effect to fall out of sync with it. `?facet=` is therefore
  // authoritative for EVERY writer — the NavRail's facet rows (the only picker
  // left; see the removed `<aside>` below) or an external link — not just
  // whichever one happened to run first. An unknown/absent facet falls back
  // to the first visible one.
  const activeFacet = visibleGroups.some((g) => g.id === facetParam)
    ? (facetParam as string)
    : visibleGroups[0]?.id;

  // Move focus to the pane heading whenever the active facet actually
  // CHANGES — a rail click, an external `?facet=` link, browser back/forward —
  // but never on first mount, which would otherwise steal focus the instant
  // Settings opens. This used to live in this shell's own `selectFacet`
  // click handler; now that facet selection itself moved to the NavRail, the
  // shell can only learn about a change by watching the URL-derived value.
  const previousFacetRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (
      previousFacetRef.current !== undefined &&
      previousFacetRef.current !== activeFacet
    ) {
      paneHeadingRef.current?.focus();
    }
    previousFacetRef.current = activeFacet;
  }, [activeFacet]);

  const searchResult = useMemo(
    () => (model && query.trim() ? model.search(query) : null),
    [model, query]
  );

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  if (!model || !config) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-sm text-[hsl(var(--destructive-text))]">
          Failed to load configuration.
        </p>
      </div>
    );
  }

  const activeGroup =
    visibleGroups.find((g) => g.id === activeFacet) ?? visibleGroups[0];

  // Custom panes for the active facet (empty for a purely schema-driven facet).
  const activePanes = activeGroup ? FACET_PANES[activeGroup.id] ?? [] : [];

  // Sections to render for the active facet, filtered by search + modified.
  const sectionsForActive = (activeGroup?.sections ?? []).filter((s) => {
    if (searchResult && !searchResult.sectionIds.has(s.id)) return false;
    if (modifiedOnly && !model.isDirty(s.id, sectionValue(s.id))) return false;
    return true;
  });

  const handleSectionSave = async (sectionId: string) => {
    const patch = model.patchFor(sectionId, sectionValue(sectionId));
    if (!patch) return;
    // `savePatch`'s `onSuccess` already updates the ["config"] cache; re-seed
    // ONLY the saved section so it becomes non-dirty without touching siblings.
    const updated = await savePatch(patch);
    if (updated) {
      setFormState((prev) => ({
        ...prev,
        [sectionId]: normalizeSection(updated.config[sectionId]),
      }));
    }
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Header — no facet-nav trigger of any kind here anymore. The
          NavRail's own Settings zone (`nav-rail/settingsSection.tsx`) is now
          the ONE facet picker, on every viewport; it wraps the rail's own
          desktop aside / mobile off-canvas Sheet swap, so this shell has no
          mobile path of its own to reproduce. */}
      <div className="border-b border-[hsl(var(--border))] px-6 py-4">
        <h1 className="text-base font-semibold text-[hsl(var(--foreground))]">
          Settings
        </h1>
        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          Some changes take effect on the next session.
        </p>
      </div>

      <div className="flex-1 flex overflow-hidden">
        {/* Main pane */}
        <div className="flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-6 py-6 space-y-6">
            {error && <ErrorAlert error={error} fallback="Failed to load settings" />}

            {/* Toolbar */}
            <div className="flex flex-wrap items-center gap-3">
              <div className="relative flex-1 min-w-[12rem]">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-[hsl(var(--muted-foreground))]" />
                <Input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search settings…"
                  aria-label="Search settings"
                  className="pl-8"
                />
              </div>
              <label className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
                <SlidersHorizontal className="w-3.5 h-3.5" />
                Advanced
                <Switch
                  checked={advanced}
                  onCheckedChange={setAdvanced}
                  aria-label="Show advanced settings"
                />
              </label>
              <label className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]">
                Modified
                <Switch
                  checked={modifiedOnly}
                  onCheckedChange={setModifiedOnly}
                  aria-label="Show only modified settings"
                />
              </label>
            </div>

            {/* Active facet pane heading (focus target on facet switch) + blurb */}
            <div>
              <h2
                ref={paneHeadingRef}
                tabIndex={-1}
                className="text-base font-semibold text-[hsl(var(--foreground))] outline-none"
              >
                {activeGroup?.title}
              </h2>
              {activeGroup?.blurb && (
                <div className="mt-1">
                  <FieldHelp text={activeGroup.blurb} />
                </div>
              )}
            </div>

            <div className="space-y-4">
              {/* Custom panes (registry-driven) sit above the schema sections.
                  One Suspense each: a slow chunk must not block its sibling.
                  Key by facet id + index, not bare index — a bare index key
                  is stable within one facet's render but COLLIDES across a
                  facet switch (facet A's pane 0 and facet B's pane 0 both key
                  `0`), so React can treat two unrelated Suspense subtrees as
                  "the same" mid-transition. */}
              {activePanes.map((Pane, i) => (
                <Suspense key={`${activeGroup?.id ?? "facet"}:${i}`} fallback={<PaneFallback />}>
                  <Pane />
                </Suspense>
              ))}

              {/* Schema-driven sections */}
              {sectionsForActive.map((s) => (
                <SettingsSection
                  key={s.id}
                  model={model}
                  sectionId={s.id}
                  value={sectionValue(s.id)}
                  original={normalizeSection(config[s.id])}
                  advanced={advanced}
                  secrets={secrets}
                  onChange={(next) =>
                    setFormState((prev) => ({ ...prev, [s.id]: next }))
                  }
                  onSave={() => handleSectionSave(s.id)}
                />
              ))}

              {activePanes.length === 0 && sectionsForActive.length === 0 && (
                <p className="text-sm text-[hsl(var(--muted-foreground))]">
                  {modifiedOnly
                    ? "No modified settings in this section."
                    : "No settings match your search."}
                </p>
              )}
            </div>

            {/* Read-only "View as JSON" escape hatch */}
            <ConfigJsonViewer config={config} />
          </div>
        </div>
      </div>
    </div>
  );
}
