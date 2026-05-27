/**
 * SettingsView — the faceted Settings shell.
 *
 * Replaces the flat RJSF dump with a sidebar of facets (from `SettingsModel`)
 * and a section pane. The shell OWNS edit state (a sectionId → formData map)
 * so it can drive the @modified filter and per-section Save/Reset; all
 * grouping / slicing / search / diff logic stays in `SettingsModel` — the
 * shell never recomputes it. Per-section patches are produced by
 * `model.patchFor` and persisted via `useConfig.savePatch`.
 */
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  Cpu,
  FolderGit2,
  Loader2,
  Menu,
  Minus,
  Monitor,
  Plug,
  Search,
  Server,
  Settings2,
  Shield,
  SlidersHorizontal,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useConfig } from "../hooks/useConfig";
import { useIsMobile } from "../hooks/useIsMobile";
import { SettingsModel } from "./settings/SettingsModel";
import { SettingsSection } from "./settings/SettingsSection";
import { CopyButton } from "./CopyButton";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Switch } from "./ui/switch";
import { cn } from "@/lib/utils";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "./ui/sheet";

// ApiKeysView is self-contained; lazy-load it so the Security facet doesn't
// pull react-hook-form/zod into the main Settings chunk until needed.
const ApiKeysView = lazy(() =>
  import("./ApiKeysView").then((m) => ({ default: m.ApiKeysView }))
);

// ---------------------------------------------------------------------------
// iconName (string from facets.ts) → lucide component
// ---------------------------------------------------------------------------

const FACET_ICONS: Record<string, LucideIcon> = {
  Cpu,
  Wrench,
  Plug,
  Monitor,
  Server,
  Shield,
  FolderGit2,
  Settings2,
};

const SECURITY_FACET_ID = "security";

/** A section's config value as a plain object (or `{}` for scalars/missing). */
function normalizeSection(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

// ---------------------------------------------------------------------------
// Secrets summary (read-only) — for the Security facet
// ---------------------------------------------------------------------------

function SecretsSummary({ secrets }: { secrets: Record<string, boolean> }) {
  const paths = Object.keys(secrets).sort();
  if (paths.length === 0) {
    return (
      <p className="text-sm text-[hsl(var(--muted-foreground))]">
        No secrets are configured.
      </p>
    );
  }
  return (
    <ul className="space-y-1.5">
      {paths.map((path) => {
        const set = secrets[path];
        return (
          <li
            key={path}
            className="flex items-center justify-between gap-3 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-3 py-2"
          >
            <code className="text-xs font-mono text-[hsl(var(--foreground))] break-all">
              {path}
            </code>
            {set ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-[hsl(var(--success))]/15 px-2 py-0.5 text-[10px] font-medium leading-none text-[hsl(var(--success))] border border-[hsl(var(--success))]/20">
                <Check className="w-3 h-3" /> set
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 rounded-full bg-[hsl(var(--muted))]/40 px-2 py-0.5 text-[10px] font-medium leading-none text-[hsl(var(--muted-foreground))] border border-[hsl(var(--border))]">
                <Minus className="w-3 h-3" /> not set
              </span>
            )}
          </li>
        );
      })}
    </ul>
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
  const isMobile = useIsMobile();

  const model = useMemo(
    () => (schema && config ? new SettingsModel(schema, config) : null),
    [schema, config]
  );

  // Shell-owned edit state: one formData blob per section. We seed ONLY
  // sections missing from `formState` (initial load + any newly-appearing
  // config key) and never overwrite a section already present — a section in
  // `formState` may hold unsaved edits, and saving one section changes the
  // `config` identity, which must not clobber edits in sibling sections.
  // After a save, `handleSectionSave` re-seeds just the saved section.
  const [formState, setFormState] = useState<Record<string, Record<string, unknown>>>({});
  useEffect(() => {
    if (!config) return;
    setFormState((prev) => {
      const next = { ...prev };
      for (const key of Object.keys(config)) {
        if (!(key in next)) next[key] = normalizeSection(config[key]);
      }
      return next;
    });
  }, [config]);

  const [activeFacet, setActiveFacet] = useState<string>("");
  const [query, setQuery] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [modifiedOnly, setModifiedOnly] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  const paneHeadingRef = useRef<HTMLHeadingElement>(null);

  // Visible facets: render a facet only if it has sections OR it is security.
  const visibleGroups = useMemo(() => {
    if (!model) return [];
    return model
      .groups()
      .filter((g) => g.sections.length > 0 || g.id === SECURITY_FACET_ID);
  }, [model]);

  // Default the active facet to the first visible one once the model loads.
  useEffect(() => {
    if (!activeFacet && visibleGroups.length > 0) {
      setActiveFacet(visibleGroups[0].id);
    }
  }, [activeFacet, visibleGroups]);

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
        <p className="text-sm text-[hsl(var(--destructive))]">
          Failed to load configuration.
        </p>
      </div>
    );
  }

  const activeGroup =
    visibleGroups.find((g) => g.id === activeFacet) ?? visibleGroups[0];

  const selectFacet = (id: string) => {
    setActiveFacet(id);
    setNavOpen(false);
    // Move focus to the section pane heading on facet switch.
    requestAnimationFrame(() => paneHeadingRef.current?.focus());
  };

  // Sections to render for the active facet, filtered by search + modified.
  const sectionsForActive = (activeGroup?.sections ?? []).filter((s) => {
    if (searchResult && !searchResult.sectionIds.has(s.id)) return false;
    if (modifiedOnly && !model.isDirty(s.id, formState[s.id] ?? {})) return false;
    return true;
  });

  const handleSectionSave = async (sectionId: string) => {
    const patch = model.patchFor(sectionId, formState[sectionId] ?? {});
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

  // -- nav (shared between desktop column and mobile sheet) -----------------
  const NavList = (
    <nav aria-label="Settings" className="space-y-1">
      {visibleGroups.map((g) => {
        const Icon = FACET_ICONS[g.iconName] ?? Settings2;
        const dimmed =
          searchResult && !searchResult.groupIds.has(g.id) && g.id !== SECURITY_FACET_ID;
        const isActive = activeGroup?.id === g.id;
        return (
          <button
            key={g.id}
            type="button"
            aria-current={isActive ? "page" : undefined}
            onClick={() => selectFacet(g.id)}
            className={cn(
              "flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]/40",
              isActive
                ? "bg-[hsl(var(--accent))] text-[hsl(var(--foreground))] font-medium"
                : "text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]",
              dimmed && "opacity-40"
            )}
          >
            <Icon className="w-4 h-4 shrink-0" />
            <span className="truncate">{g.title}</span>
          </button>
        );
      })}
    </nav>
  );

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="border-b border-[hsl(var(--border))] px-6 py-4">
        <div className="flex items-center gap-3">
          {isMobile && (
            <Sheet open={navOpen} onOpenChange={setNavOpen}>
              <SheetTrigger asChild>
                <Button
                  variant="ghost"
                  size="md"
                  iconOnly
                  aria-label="Open settings navigation"
                  title="Open settings navigation"
                  leadingIcon={<Menu className="w-4 h-4" />}
                />
              </SheetTrigger>
              <SheetContent side="left" className="w-72 p-0">
                <SheetHeader className="border-b border-[hsl(var(--border))] px-4 py-3">
                  <SheetTitle>Settings</SheetTitle>
                </SheetHeader>
                <div className="p-3">{NavList}</div>
              </SheetContent>
            </Sheet>
          )}
          <div>
            <h1 className="text-lg font-semibold text-[hsl(var(--foreground))]">
              Settings
            </h1>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Some changes take effect on the next session.
            </p>
          </div>
        </div>
      </div>

      <div className="flex-1 flex overflow-hidden">
        {/* Desktop left nav */}
        {!isMobile && (
          <aside className="w-60 shrink-0 border-r border-[hsl(var(--border))] overflow-y-auto p-3">
            {NavList}
          </aside>
        )}

        {/* Main pane */}
        <div className="flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-6 py-6 space-y-5">
            {error && (
              <div className="flex items-start gap-2 rounded-lg border border-[hsl(var(--destructive))]/30 bg-[hsl(var(--destructive))]/10 px-3 py-2.5">
                <AlertTriangle className="w-4 h-4 text-[hsl(var(--destructive))] shrink-0 mt-0.5" />
                <p className="text-xs text-[hsl(var(--destructive))]">{error}</p>
              </div>
            )}

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

            {/* Active facet pane heading (focus target on facet switch) */}
            <h2
              ref={paneHeadingRef}
              tabIndex={-1}
              className="text-base font-semibold text-[hsl(var(--foreground))] outline-none"
            >
              {activeGroup?.title}
            </h2>

            {/* Security facet — reuse ApiKeysView + secrets summary */}
            {activeGroup?.id === SECURITY_FACET_ID ? (
              <div className="space-y-6">
                <section
                  aria-labelledby="settings-secrets-summary"
                  className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-5"
                >
                  <h3
                    id="settings-secrets-summary"
                    className="text-sm font-semibold text-[hsl(var(--foreground))] mb-3"
                  >
                    Configured secrets
                  </h3>
                  <SecretsSummary secrets={secrets} />
                </section>

                <Suspense
                  fallback={
                    <div className="flex items-center justify-center py-8">
                      <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
                    </div>
                  }
                >
                  <ApiKeysView />
                </Suspense>
              </div>
            ) : (
              <div className="space-y-4">
                {sectionsForActive.length === 0 ? (
                  <p className="text-sm text-[hsl(var(--muted-foreground))]">
                    {modifiedOnly
                      ? "No modified settings in this section."
                      : "No settings match your search."}
                  </p>
                ) : (
                  sectionsForActive.map((s) => (
                    <SettingsSection
                      key={s.id}
                      model={model}
                      sectionId={s.id}
                      value={formState[s.id] ?? {}}
                      original={normalizeSection(config[s.id])}
                      advanced={advanced}
                      secrets={secrets}
                      onChange={(next) =>
                        setFormState((prev) => ({ ...prev, [s.id]: next }))
                      }
                      onSave={() => handleSectionSave(s.id)}
                    />
                  ))
                )}
              </div>
            )}

            {/* Read-only "View as JSON" escape hatch */}
            <details className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))]">
              <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium text-[hsl(var(--foreground))]">
                View as JSON (read-only)
              </summary>
              <div className="relative border-t border-[hsl(var(--border))]">
                <div className="absolute right-2 top-2 z-10">
                  <CopyButton text={JSON.stringify(config, null, 2)}>
                    Copy
                  </CopyButton>
                </div>
                <pre className="overflow-x-auto px-4 py-3 text-xs font-mono text-[hsl(var(--code-fg))] bg-[hsl(var(--code-body))]">
                  {JSON.stringify(config, null, 2)}
                </pre>
              </div>
            </details>
          </div>
        </div>
      </div>
    </div>
  );
}
