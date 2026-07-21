import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { useLocation } from "wouter";
import { ExternalLink, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";
import { useSessionEvents } from "../../hooks/useSessionEvents";
import type { EventRecord } from "../../types"
import type { AppReadyEvent } from "../../types/apps";

/**
 * Live build-progress view. Tails the builder session's EXISTING event stream
 * (no bespoke transport — the same `useSessionEvents` poll every session view
 * uses) and shows the agent working until the `app_ready` terminal event. It
 * never fabricates progress: the readout is the real event log, and the terminal
 * signal is the real event. When `app_ready` lands it calls `onReady`, but the
 * parent's polled manifest (status → live) is the durable trigger, so a missed
 * event still resolves on the next poll.
 */
export function BuildProgress({
  sessionId,
  title,
  onReady,
}: {
  sessionId: string;
  title?: string;
  onReady?: (event: AppReadyEvent) => void;
}) {
  const [, setLocation] = useLocation();
  const { events, running } = useSessionEvents(sessionId);

  // Fire `onReady` once, the first time an `app_ready` event folds into the log.
  const firedRef = useRef(false);
  useEffect(() => {
    if (firedRef.current) return;
    const ready = events.find((e) => e.type === "app_ready");
    if (ready) {
      firedRef.current = true;
      onReady?.(ready.payload as unknown as AppReadyEvent);
    }
  }, [events, onReady]);

  const activity = useMemo(() => events.slice(-8).map(describeEvent).filter(Boolean), [events]);

  // Keep the newest activity line in view.
  const scrollRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [activity.length]);

  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <div className="w-full max-w-md text-center">
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-[hsl(var(--primary)/0.12)]">
          <Loader2 className="h-5 w-5 animate-spin text-[hsl(var(--primary-text))]" />
        </div>
        {/* `text-sm font-medium` is the ONE tier every in-pane state title in
            Apps uses (gallery empty state, archived, load failure, settings
            pane). It was `text-base` here, a 1px step above the rest and above
            the detail header's own h1 — a size difference that small reads as
            noise, not as hierarchy. */}
        <h2 className="text-sm font-medium">
          {title ? `Building ${title}` : "Building your app"}
        </h2>
        <p className="mx-auto mt-1 max-w-[340px] text-xs text-[hsl(var(--muted-foreground))] [text-wrap:balance]">
          Mewbo is designing the data model, writing the frontend, and arming the pipelines
          that keep it fresh. This opens automatically when it's ready.
        </p>

        {activity.length > 0 && (
          <div
            ref={scrollRef}
            className={cn(cardSurface({ radius: "left" }), "mt-5 max-h-40 overflow-y-auto p-3 text-left")}
          >
            <ul className="space-y-1.5">
              {activity.map((line, i) => (
                <li
                  key={i}
                  className="flex items-start gap-2 text-xs text-[hsl(var(--muted-foreground))]"
                >
                  <span aria-hidden className="mt-0.5 text-[hsl(var(--primary))]">
                    ·
                  </span>
                  <span className="min-w-0 flex-1 truncate">{line}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {!running && !firedRef.current && (
          // The builder session ended without an `app_ready` we saw — the parent
          // poll will resolve the real status (live, or broken); this is the
          // honest interim note, not a fabricated success.
          <p className="mt-4 text-xs text-[hsl(var(--muted-foreground))]">
            Wrapping up. Finishing the build…
          </p>
        )}

        <div className="mt-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setLocation(`/s/${encodeURIComponent(sessionId)}`)}
            leadingIcon={<ExternalLink className="h-3.5 w-3.5" />}
          >
            Watch the builder session
          </Button>
        </div>
      </div>
    </div>
  );
}

/**
 * Turn a raw builder event into a short human-readable activity line. Best
 * effort and honest: an unrecognized event surfaces its type rather than a
 * fabricated phrase, and a value-less event is dropped (returns "").
 */
function describeEvent(event: EventRecord): string {
  const p = event.payload ?? {};
  switch (event.type) {
    case "user":
      return "Starting from your description";
    case "assistant": {
      const text = typeof p.content === "string" ? p.content : "";
      return text ? truncate(text) : "Thinking through the design";
    }
    case "tool_call":
    case "action": {
      const tool = typeof p.tool_id === "string" ? p.tool_id : typeof p.operation === "string" ? p.operation : "";
      return tool ? `Working: ${tool}` : "Working";
    }
    case "sub_agent":
      return "Delegating to a builder agent";
    case "app_ready":
      return "App ready";
    case "completion":
    case "done":
      return "Finishing up";
    default:
      return "";
  }
}

function truncate(text: string, max = 90): string {
  const trimmed = text.replace(/\s+/g, " ").trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}
