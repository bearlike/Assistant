import { useCallback, useState } from "react";
import {
  Activity,
  CalendarClock,
  ChevronDown,
  GitBranch,
  History,
  PanelRightClose,
  PanelRightOpen,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { FOCUS_RING, RAIL_CONTENT_START, RAIL_GUTTER, RAIL_INDENT, RAIL_PAD_X, railLabelIconCls, railRowIconCls } from "@/components/nav-rail/rows";
import { cn } from "@/lib/utils";
import type { AppSystemHealth } from "../../../api/apps";
import type { AppSpec, AppVersion } from "../../../types/apps";
import { HealthBody } from "./Health";
import { PipelinesBody } from "./Pipelines";
import { RunsBody } from "./Runs";
import { TriggersBody } from "./Schedule";
import { VersionsBody } from "./Versions";

const RAIL_KEY = "mewbo:apps-rail-open";

/** Every rule the rail draws is the kit's rail divider — the same token the
 *  NavRail's own zone dividers use, so a boundary reads identically on both
 *  sides of the app. It already embeds its alpha; never append another. */
const RAIL_DIVIDER = "border-[hsl(var(--rail-border))]";

/** SectionId is the shared vocabulary for the collapsed icon strip and the
 *  expanded Collapsible cluster, so the two views can never drift. */
type SectionId = "health" | "runs" | "pipelines" | "schedule" | "versions";

/** Status-first order: live state (Health) and activity (Recent runs) lead;
 *  configuration/history (Pipelines, Schedule, Versions) follow. Each carries
 *  the glyph the collapsed strip and the expanded header both render. Data,
 *  not logic. */
const SECTION_META: { id: SectionId; label: string; icon: LucideIcon }[] = [
  { id: "health", label: "Health", icon: Activity },
  { id: "runs", label: "Recent runs", icon: History },
  { id: "pipelines", label: "Pipelines", icon: Workflow },
  { id: "schedule", label: "Schedule", icon: CalendarClock },
  { id: "versions", label: "Versions", icon: GitBranch },
];

/** Whether the Health section should read as degraded — stale/never-refreshed
 *  data, no next refresh scheduled, pipelines the server flagged as
 *  unscheduled, or a declared collection the latest run left untouched.
 *  Mirrors the per-row warn tones in HealthBody so the header pip and the
 *  body agree. */
function healthHasWarning(system: AppSystemHealth | undefined): boolean {
  const f = system?.freshness;
  if (!f) return false;
  const unscheduled = system?.unscheduled_pipelines;
  return (
    f.stale ||
    !f.last_success_at ||
    !f.next_fire_at ||
    (unscheduled != null && unscheduled.length > 0) ||
    (f.unwritten_collections != null && f.unwritten_collections.length > 0)
  );
}

/**
 * The whole right-hand instrument rail. Owns the persisted whole-rail
 * open/collapsed state (localStorage `mewbo:apps-rail-open`, mirroring
 * `mewbo:sidebar-open`); per-section open state stays ephemeral in each
 * InstrumentSection. At lg the rail collapses to a slim icon strip; below lg it
 * always stacks under the app and cannot collapse.
 *
 * Its section chrome is a persisted Collapsible cluster with a collapsed
 * icon-strip mode — axes the NavRail kit's static `RailSection` deliberately
 * does not model — so it is composed here rather than imported. What it DOES
 * share with the kit is the type ramp, the glyph sizes (`railLabelIconCls`),
 * the horizontal inset (`RAIL_PAD_X`), the focus ring and the divider token, so
 * borders, spacing, type and focus read identically to the navigation side.
 */
export function InstrumentRail({
  spec,
  versions,
  system,
  loading,
}: {
  spec: AppSpec;
  versions: AppVersion[];
  system: AppSystemHealth | undefined;
  loading: boolean;
}) {
  const [open, setOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem(RAIL_KEY) !== "0";
    } catch {
      return true;
    }
  });
  const setRail = useCallback((next: boolean) => {
    setOpen(next);
    try {
      localStorage.setItem(RAIL_KEY, next ? "1" : "0");
    } catch {
      /* storage unavailable — state still changes for the session */
    }
  }, []);

  const healthWarn = healthHasWarning(system);
  const runsCount = system?.runs?.length ?? 0;
  const versionsCount = versions.length;
  // The pipelines liveness field is additive: an older server
  // omits it, and absence is NOT "no pipelines" (the app may well have some).
  // So the Pipelines section — and its strip glyph — appear only when the
  // server actually reported rows, matching the original null-render.
  const showPipelines = loading || (system?.pipelines?.length ?? 0) > 0;

  return (
    <aside
      aria-label="App instruments"
      className={cn(
        // Only the LEFT edge is the aside's, and only at lg: stacked under the
        // app the first section's own top rule already draws that boundary, and
        // an aside border there would double it into a 2px line.
        "shrink-0 lg:border-l",
        RAIL_DIVIDER,
        open ? "lg:w-[340px] lg:overflow-y-auto" : "lg:w-[52px] lg:overflow-visible",
      )}
    >
      {/* Collapsed icon strip — lg only, when the rail is collapsed. Any glyph
          re-expands the rail; a Health warning surfaces as a dot so a degraded
          state stays visible even fully collapsed. */}
      <div className={cn("hidden flex-col items-center gap-1 p-2", !open && "lg:flex")}>
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          onClick={() => setRail(true)}
          aria-label="Expand panel"
          title="Expand panel"
          className="text-[hsl(var(--muted-foreground))]"
        >
          <PanelRightOpen className={railRowIconCls} />
        </Button>
        {SECTION_META.filter((s) => s.id !== "pipelines" || showPipelines).map(
          ({ id, label, icon: Icon }) => {
            const warn = id === "health" && healthWarn;
            return (
              <button
                key={id}
                type="button"
                onClick={() => setRail(true)}
                // The warning has to survive into the accessible name too — a
                // bare amber dot is state conveyed by colour alone.
                aria-label={
                  warn ? `${label}, needs attention, expand panel` : `${label}, expand panel`
                }
                title={warn ? `${label} — needs attention` : label}
                className={cn(
                  "relative flex h-7 w-7 items-center justify-center rounded-md text-[hsl(var(--muted-foreground))] transition-colors hover:bg-[hsl(var(--rail-selected))]/60 hover:text-[hsl(var(--foreground))]",
                  FOCUS_RING,
                )}
              >
                <Icon className={railRowIconCls} aria-hidden />
                {warn && (
                  <span
                    aria-hidden
                    className="absolute right-0.5 top-0.5 h-1.5 w-1.5 rounded-full bg-[hsl(var(--warning))]"
                  />
                )}
              </button>
            );
          },
        )}
      </div>

      {/* Expanded cluster — always below lg (the rail stacks under the app);
          at lg only when open. Every section draws its own top rule and the
          cluster closes with a bottom one, so each section reads as bounded
          above AND below without any section hand-rolling an edge. */}
      <div className={cn(!open && "lg:hidden")}>
        <div className={cn("hidden items-center justify-end py-1 lg:flex", RAIL_PAD_X)}>
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={() => setRail(false)}
            aria-label="Collapse panel"
            title="Collapse panel"
            className="text-[hsl(var(--muted-foreground))]"
          >
            <PanelRightClose className={railLabelIconCls} />
          </Button>
        </div>

        <div className={cn("border-b", RAIL_DIVIDER)}>
          <InstrumentSection
            title="Health"
            icon={Activity}
            defaultOpen
            pip={healthWarn ? <WarnPip /> : undefined}
          >
            <HealthBody appId={spec.app_id} system={system} loading={loading} />
          </InstrumentSection>

          <InstrumentSection title="Recent runs" icon={History} defaultOpen pip={<CountPip n={runsCount} />}>
            <RunsBody runs={system?.runs ?? []} loading={loading} />
          </InstrumentSection>

          {showPipelines && (
            <InstrumentSection title="Pipelines" icon={Workflow}>
              <PipelinesBody appId={spec.app_id} pipelines={system?.pipelines} loading={loading} />
            </InstrumentSection>
          )}

          <InstrumentSection title="Schedule" icon={CalendarClock}>
            <TriggersBody status={spec.status} system={system} />
          </InstrumentSection>

          <InstrumentSection title="Versions" icon={GitBranch} pip={<CountPip n={versionsCount} />}>
            <VersionsBody appId={spec.app_id} activeVersion={spec.version} versions={versions} />
          </InstrumentSection>
        </div>
      </div>
    </aside>
  );
}

/**
 * The ONE instrument-section chrome. Every rail section wears it: a top rule on
 * the kit's rail-divider token, a 32px heading row at the kit's horizontal
 * inset with the section's glyph at the shared label size, and a body whose
 * bottom padding is the only gap to the rule below. A section that draws its own
 * heading, border or padding after this is the defect — a body renders CONTENT
 * and nothing else.
 *
 * It is the instrument analog of the kit's `RailSection`, kept local because it
 * carries axes the kit deliberately does not model — a persisted collapse and
 * an icon-strip mode. The heading is the Radix trigger (`aria-expanded` for
 * free); the chevron rotates on state and the body height/opacity is the rail's
 * single deliberate motion, gated by motion-safe.
 */
function InstrumentSection({
  title,
  icon: Icon,
  pip,
  defaultOpen = false,
  children,
}: {
  title: string;
  icon: LucideIcon;
  pip?: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Collapsible open={open} onOpenChange={setOpen} className={cn("border-t", RAIL_DIVIDER)}>
      <CollapsibleTrigger
        className={cn(
          "group flex h-8 w-full items-center gap-2 text-left transition-colors",
          "hover:bg-[hsl(var(--rail-selected))]/40",
          "focus-visible:ring-inset",
          FOCUS_RING,
          // Heading icon begins on the 12px content column, matching the body
          // rows below (which reach it via RAIL_GUTTER + RAIL_PAD_X) instead of
          // sitting 4px left of them at the bare RAIL_PAD_X.
          RAIL_CONTENT_START,
          "pr-2",
        )}
      >
        {/* Leading glyph recedes WITH its label onto `--rail-heading` rather
            than competing with it (the chevron below keeps the fuller muted tone
            as the collapse affordance). */}
        <Icon className={cn(railLabelIconCls, "text-[hsl(var(--rail-heading))]")} aria-hidden />
        {/* A zone label, not a headline, and the LIGHTEST tier of the rail's
            300/400/500 weight ramp so it recedes below the rows it heads instead
            of reading as another clickable one. Colour recedes too, onto
            `--rail-heading`. The WORD itself takes no hover/active promotion; the
            collapse affordance this trigger carries lives on the row tint + the
            rotating chevron, never on the label. */}
        <span className="truncate text-sm font-light text-[hsl(var(--rail-heading))]">
          {title}
        </span>
        {pip}
        <ChevronDown
          aria-hidden
          className={cn(
            railLabelIconCls,
            "ml-auto text-[hsl(var(--muted-foreground))] transition-transform duration-200 group-data-[state=open]:rotate-180",
          )}
        />
      </CollapsibleTrigger>
      <CollapsibleContent className="overflow-hidden motion-safe:data-[state=closed]:animate-collapsible-up motion-safe:data-[state=open]:animate-collapsible-down">
        {/* Same composed rule as a labelled NavRail section: `RAIL_GUTTER` floats
            the row fills off the aside's left border, then `RAIL_INDENT` steps the
            body in under its heading. They sit on SEPARATE wrappers so the two
            stack (4px + 6px) instead of colliding on `padding-left` — the icons
            land 10px in, matching the nav side row-for-row. */}
        <div className={cn(RAIL_GUTTER, "pb-2")}>
          <div className={RAIL_INDENT}>{children}</div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

/** 4px count pip (workspace-tab vocabulary). Renders nothing at zero. The
 *  numeric STATE-PILL exception to the rail's single-size law — it stays
 *  `text-2xs tabular-nums` because a 13px numeral balloons an overlay pill. */
function CountPip({ n }: { n: number }) {
  if (n <= 0) return null;
  return (
    <span className="inline-flex flex-none items-center justify-center rounded-[4px] bg-[hsl(var(--muted))] px-1.5 py-0.5 text-2xs leading-none tabular-nums text-[hsl(var(--muted-foreground))]">
      {n}
    </span>
  );
}

/** Warning state dot on the Health header. rounded-full is the sanctioned
 *  state-container shape; the body (open by default) carries the detail, so
 *  the dot is decorative — the sr-only sibling is what a screen reader hears,
 *  and it is also what makes the state readable without the colour. */
function WarnPip() {
  return (
    <span className="flex flex-none items-center">
      <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-[hsl(var(--warning))]" />
      <span className="sr-only">needs attention</span>
    </span>
  );
}
