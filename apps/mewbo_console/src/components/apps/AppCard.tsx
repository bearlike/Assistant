import { Sparkles, Users } from "lucide-react";

import { cardSurface } from "@/components/ui/card-surface";
import { FOCUS_RING } from "@/components/ui/focus-ring";
import { cn } from "@/lib/utils";
import type { AppSummary } from "../../types/apps";
import { AppFreshness } from "./AppFreshness";
import { AppStatusBadge } from "./AppStatusBadge";

/** The gallery cell's floor height. Exported because the landing's dashed
 *  "New app" tile and its loading skeleton are the SAME grid cell and must
 *  agree — three hand-copied literals is how a grid goes ragged. (A plain
 *  const export needs no react-refresh escape hatch: the rule runs with
 *  `allowConstantExport`.) */
export const APP_CARD_MIN_H = "min-h-[132px]";

/**
 * One gallery card. Mirrors the Agentic Search workspace-card shape vocabulary
 * (rounded-xl, elev-on-hover, name > summary hierarchy, a `mt-auto` meta shelf
 * pinned to the bottom so equal-height grid rows read as one shelf). The emoji
 * icon is the app's own glyph; status + freshness + next-fire come from the
 * durable summary and the lazy system endpoint (`AppFreshness`).
 *
 * Chrome comes from `cardSurface({radius:"panel"})` — unlike the search
 * `WorkspaceCard` this card has no selected state, so nothing about its border
 * or fill is conditional and it fits the builder's chrome-only contract. The
 * hover treatment layers on top as its own variants.
 *
 * Type tiers, top to bottom: title `text-sm font-medium` (identity) > summary
 * `text-xs` muted (secondary) > meta shelf `text-2xs` muted. Hierarchy is
 * weight and colour; the sizes never step by 1px.
 */
export function AppCard({
  app,
  onOpen,
}: {
  app: AppSummary;
  onOpen: (appId: string) => void;
}) {
  const shared = app.workspace_ref.kind === "shared";
  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Open app ${app.title}`}
      onClick={() => onOpen(app.app_id)}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(app.app_id);
        }
      }}
      className={cn(
        cardSurface({ radius: "panel", elevation: "elev-1" }),
        APP_CARD_MIN_H,
        "group flex cursor-pointer flex-col gap-2.5 p-3.5 text-left",
        "transition-[box-shadow,transform,background-color,border-color] duration-200",
        "hover:-translate-y-px hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)] hover:[box-shadow:var(--elev-2)]",
        FOCUS_RING,
      )}
    >
      <div className="flex items-start gap-2.5">
        <span
          aria-hidden
          className="flex h-9 w-9 flex-none items-center justify-center rounded-lg bg-[hsl(var(--muted))] text-lg leading-none"
        >
          {app.icon || "✨"}
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-medium leading-tight" title={app.title}>
            {app.title}
          </h3>
          <p className="mt-0.5 line-clamp-2 text-xs text-[hsl(var(--muted-foreground))] [text-wrap:pretty]">
            {app.summary}
          </p>
        </div>
        <AppStatusBadge status={app.status} className="flex-none" />
      </div>

      {/* Meta shelf — freshness (or build progress while status=building) on the
          left, workspace-scope hint on the right, pinned to the bottom (`mt-auto`)
          so cards line up as a shelf. The left cell is `min-w-0` and the right
          one `flex-none`: at a 260px grid track the freshness chips are what
          gives, never the scope hint. */}
      <div className="mt-auto flex items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          {app.status === "building" ? (
            // Short enough to sit on ONE line in a 260px grid track — the
            // previous phrasing measured wider than the cell and wrapped under
            // its own icon.
            <span
              className="inline-flex items-center gap-1.5 text-2xs text-[hsl(var(--primary-text))]"
              title="The agent is designing this app's frontend and data pipelines"
            >
              <Sparkles className="h-3 w-3 flex-none motion-safe:animate-pulse" />
              Building frontend and pipelines
            </span>
          ) : (
            <AppFreshness appId={app.app_id} />
          )}
        </div>
        {shared && (
          <span
            className="inline-flex flex-none items-center gap-1 whitespace-nowrap text-2xs text-[hsl(var(--muted-foreground))]"
            title="Runs against a shared workspace"
          >
            <Users className="h-3 w-3 flex-none" />
            Shared
          </span>
        )}
      </div>
    </div>
  );
}
