/**
 * The indexer's own output, in its own bounded scroll container.
 *
 * Three rules, each of them a defect measured on a live run rather than a
 * preference:
 *
 *   - It scrolls itself, never the page. A loading screen that grows taller as
 *     it works pushes its own controls off-screen.
 *   - It renders a bounded tail and SAYS the window is a tail. The unbounded
 *     version put 5,468 rows and 22,205 DOM nodes on screen; a silent trim is
 *     what makes two visits look like two different runs, so the count is
 *     printed rather than hidden.
 *   - Only the live edge animates. Every row previously carried a spinning
 *     glyph, so a long run left thousands of animations running at once for no
 *     information at all.
 */
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { AlertTriangle, CircleAlert } from "lucide-react";

import { cn } from "@/lib/utils";

import type { IndexingLogEntry } from "../api/types";
import { ActivityFeed, RENDER_LIMIT } from "./activityModel";

export function ActivityLog({
  entries,
  labels,
  live,
  emptyMessage,
  className,
}: {
  entries: IndexingLogEntry[];
  /** Step key to declared label, for the run headers. */
  labels: Map<string, string>;
  /** Whether the run is still producing output — drives the live edge marker. */
  live: boolean;
  emptyMessage: string;
  className?: string;
}) {
  const feed = useMemo(() => ActivityFeed.from(entries, labels), [entries, labels]);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Pinned unless the reader has scrolled away. Yanking someone back to the
  // bottom while they are reading history is worse than losing the tail.
  const pinnedRef = useRef(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [feed]);

  return (
    <div className={cn("flex min-h-0 flex-col", className)} data-region="activity">
      <div className="flex shrink-0 items-baseline justify-between gap-2 px-4 pb-2 pt-3">
        <h2 className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          Activity
        </h2>
        {feed.truncated && (
          <span className="text-2xs tabular-nums text-[hsl(var(--muted-foreground))]">
            last {RENDER_LIMIT.toLocaleString()} of {feed.total.toLocaleString()} lines
          </span>
        )}
      </div>

      <div
        ref={scrollRef}
        role="log"
        aria-live="polite"
        aria-label="Indexer activity"
        className="min-h-0 flex-1 overflow-y-auto px-2 pb-3"
        data-scroll="pane"
      >
        {feed.total === 0 ? (
          <p className="px-2 py-6 text-center text-xs text-[hsl(var(--muted-foreground))]">
            {emptyMessage}
          </p>
        ) : (
          feed.runs.map((run, runIndex) => {
            const lastRun = runIndex === feed.runs.length - 1;
            // Naming the gap is deliberate: among attributed runs, a line with
            // no step is work that ran outside its declared scope, and a
            // silently unlabelled block is how that stays unnoticed.
            const heading = run.label ?? (feed.attributed ? "Outside a declared step" : null);
            return (
              <section key={`${run.step ?? "unscoped"}-${runIndex}`} className="mb-1.5">
                {heading && (
                  <h3
                    className={cn(
                      // Edge-to-edge background (`-mx-2 px-4` against the
                      // pane's `px-2`): a sticky heading narrower than its
                      // scroll container lets lines bleed past it at the top.
                      "sticky top-0 z-10 -mx-2 truncate bg-[hsl(var(--card))] px-4 py-1.5 text-2xs font-medium",
                      run.label
                        ? "text-[hsl(var(--muted-foreground))]"
                        : "text-[hsl(var(--muted-foreground))] italic opacity-70",
                    )}
                  >
                    {heading}
                  </h3>
                )}
                <ul className="space-y-px">
                  {run.entries.map((entry, index) => {
                    const liveEdge = live && lastRun && index === run.entries.length - 1;
                    return (
                      <li
                        key={`${runIndex}-${index}-${entry.ts ?? 0}`}
                        className={cn(
                          "flex items-start gap-2 rounded-md px-2 py-1 text-xs",
                          entry.level === "error"
                            ? "bg-[hsl(var(--destructive))]/10 text-[hsl(var(--destructive-text))]"
                            : entry.level === "warn"
                              ? "bg-[hsl(var(--warning))]/10 text-[hsl(var(--warning-text))]"
                              : "text-[hsl(var(--muted-foreground))]",
                        )}
                      >
                        {entry.level === "error" ? (
                          <CircleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                        ) : entry.level === "warn" ? (
                          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                        ) : (
                          <span
                            className={cn(
                              "mt-[0.4rem] h-1 w-1 shrink-0 rounded-full bg-current opacity-50",
                              liveEdge && "session-cmp-pulse bg-[hsl(var(--primary))] opacity-100",
                            )}
                            aria-hidden
                          />
                        )}
                        <span className="min-w-0 flex-1 break-words font-mono leading-relaxed">
                          {entry.text}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })
        )}
      </div>
    </div>
  );
}
