"use client";

import {
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  useAuiState,
} from "@assistant-ui/react";
import {
  createContext,
  useContext,
  useMemo,
  type FC,
  type ReactNode,
} from "react";
import { cn } from "@/lib/utils";
import { isDefaultVisibleOrigin } from "@/utils/sessionOrigins";
import { groupByDateBucket } from "@/utils/dateBuckets";
import {
  FOCUS_RING,
  RailDateGroup,
  railRowCls,
} from "@/components/nav-rail/rows";
import type { SessionSummary } from "@/types";

/**
 * Mewbo side-rail thread list, built on assistant-ui's `ThreadListPrimitive`.
 *
 * The rail reads its ordered/active thread list straight from the shared
 * external-store runtime (mounted once in `AppLayout` via `MewboRuntimeProvider`),
 * so the active row highlights and row-clicks/New route through the SAME seam the
 * composer uses — no bespoke list state. The runtime's thread data only carries
 * `{id, title}`, though; a row's provenance badge, project label and timestamp
 * come from `SessionSummary`, injected via `RailMetaProvider`. The default origin
 * filter (`isDefaultVisibleOrigin`) and the recents cap are applied here so the
 * rail shows the same user-facing set as the landing page.
 *
 * NB. The vendored assistant-ui source ships Tailwind v4 syntax that silently
 * no-ops on our v3.4 build — translated here: `data-active:` → `data-[active]:`
 * (the primitive emits `data-active="true"` on the active row) and every colour
 * routed through our `hsl(var(--token))` vocabulary.
 */
export type RailMeta = {
  /** session_id → summary, for the origin filter, date bucket and running dot
   *  each row derives (the runtime's thread data only carries `{id, title}`). */
  metaById: Map<string, SessionSummary>;
};

const RailMetaContext = createContext<RailMeta | null>(null);

export function RailMetaProvider({
  value,
  children,
}: {
  value: RailMeta;
  children: ReactNode;
}) {
  return (
    <RailMetaContext.Provider value={value}>{children}</RailMetaContext.Provider>
  );
}

function useRailMeta(): RailMeta | null {
  return useContext(RailMetaContext);
}

/** One recents row: the vendor primitive owns click→switch + the active tint;
 *  we render Mewbo's compact single-line title with a trailing liveness dot.
 *
 *  Geometry, spacing, resting type and the focus ring come from the SHARED rail
 *  kit (`railRowCls` + `FOCUS_RING` in `nav-rail/rows.tsx`) — the same constants
 *  `RailRow` composes, so this row cannot drift from its Wiki/Search/Apps
 *  neighbours again. It used to re-spell them, and duly fell out of sync: it sat
 *  at `h-8` against the rail's `h-7` after a density change, rendering Tasks
 *  recents 4px taller than the rows directly above them.
 *
 *  Only COLOUR is supplied here, and that is the kit's design, not a shortfall:
 *  the assistant-ui primitive splits a row across `Root` (which owns the
 *  background, so it can carry `data-[active]`) and `Trigger` (which owns the
 *  text), so a row's idle/hover/active colours cannot live in one class string.
 *  The kit deliberately leaves colour out so this file COMPOSES it rather than
 *  overriding a baked-in default — overriding is how the drift would return.
 *
 *  What stays bespoke: this is the one rail list whose row must BE the
 *  assistant-ui primitive (to get click→switch and the active tint for free),
 *  so it cannot simply render `RailRow`. Structure differs; vocabulary no
 *  longer does. */
const RailThreadListItem: FC = () => {
  const meta = useRailMeta();
  const id = useAuiState((s) => s.threadListItem.id);
  const session = meta?.metaById.get(id);

  return (
    <ThreadListItemPrimitive.Root
      data-slot="rail-thread-item"
      className="group rounded-lg transition-colors hover:bg-[hsl(var(--rail-selected))]/60 data-[active]:bg-[hsl(var(--rail-selected))]"
    >
      <ThreadListItemPrimitive.Trigger
        data-slot="rail-thread-item-trigger"
        className={cn(
          railRowCls,
          // Colour only — the Root above owns the background so it can react to
          // `data-active`; the Trigger owns the text.
          "text-[hsl(var(--muted-foreground))] group-hover:text-[hsl(var(--foreground))] group-data-[active]:text-[hsl(var(--foreground))]",
          FOCUS_RING,
        )}
      >
        <span className="flex-1 truncate">
          <ThreadListItemPrimitive.Title fallback="Untitled" />
        </span>
        {/* Liveness dot — gated on the live `running` flag, never a derived
            status. Matches the Aura drawer's trailing 8px --primary dot. */}
        {session?.running && (
          <span
            className="size-2 shrink-0 rounded-full bg-[hsl(var(--primary))]"
            aria-hidden
          />
        )}
      </ThreadListItemPrimitive.Trigger>
    </ThreadListItemPrimitive.Root>
  );
};

/**
 * The rail's recents list. Reads the runtime's ordered threads, keeps only the
 * default-visible origins (via injected meta), caps at `limit`, then partitions
 * the survivors into Today / Previous 7 days / Older buckets (shared
 * `groupByDateBucket`). Each survivor renders through the vendor `ItemByIndex`
 * primitive so the active-thread highlight and switch behaviour come for free.
 */
export const RailThreadList: FC<{ limit?: number; className?: string }> = ({
  limit,
  className,
}) => {
  const meta = useRailMeta();
  // `ItemByIndex` indexes into `threadIds` — index off the same list (not
  // `threadItems`, which isn't guaranteed 1:1) or a survivor index can point
  // past the valid range and the item lookup throws.
  const threadIds = useAuiState((s) => s.threads.threadIds);

  const indices = useMemo(() => {
    const visible: number[] = [];
    threadIds.forEach((id, index) => {
      if (isDefaultVisibleOrigin(meta?.metaById.get(id)?.origin)) {
        visible.push(index);
      }
    });
    return typeof limit === "number" ? visible.slice(0, limit) : visible;
  }, [threadIds, meta, limit]);

  // Group by the session's created_at (the runtime keeps newest-first order, so
  // each bucket stays newest-first). Empty buckets drop out; an unparseable /
  // absent date sorts into Older.
  const groups = useMemo(
    () =>
      groupByDateBucket(
        indices,
        (index) => meta?.metaById.get(threadIds[index])?.created_at,
      ),
    [indices, meta, threadIds],
  );

  return (
    <ThreadListPrimitive.Root
      data-slot="rail-thread-list"
      className={cn("flex flex-col", className)}
    >
      {groups.map((group) => (
        <RailDateGroup key={group.bucket} label={group.bucket}>
          {group.items.map((index) => (
            <ThreadListPrimitive.ItemByIndex
              key={threadIds[index]}
              index={index}
              components={{ ThreadListItem: RailThreadListItem }}
            />
          ))}
        </RailDateGroup>
      ))}
    </ThreadListPrimitive.Root>
  );
};
