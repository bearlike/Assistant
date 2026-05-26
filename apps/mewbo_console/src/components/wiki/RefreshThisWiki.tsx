/**
 * "Refresh this wiki" card in the right rail.
 *
 * Two-step inline confirm: idle → warning + Re-index / Cancel → queued.
 * No email collection; the backend queues a fresh indexing run on click
 * and the user navigates to whichever screen they want.
 */

import { useEffect, useState } from "react";
import { CheckCircle2, RefreshCw, X } from "lucide-react";

import { Button } from "@/components/ui/button";

import { useRequestWikiRefresh } from "./api/hooks";

interface RefreshThisWikiProps {
  slug: string;
  onDismiss: () => void;
}

type Phase = "idle" | "confirming" | "queued";

export function RefreshThisWiki({ slug, onDismiss }: RefreshThisWikiProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const mutate = useRequestWikiRefresh();

  // Auto-dismiss the queued confirmation after a beat so the rail returns
  // to its usual layout without the user having to click again.
  useEffect(() => {
    if (phase !== "queued") return;
    const t = window.setTimeout(onDismiss, 1600);
    return () => window.clearTimeout(t);
  }, [phase, onDismiss]);

  const onConfirm = () => {
    if (mutate.isPending) return;
    mutate.mutate(slug, {
      onSuccess: () => setPhase("queued"),
      onError: () => setPhase("idle"),
    });
  };

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-2.5 relative">
      <div className="flex items-center gap-1.5 mb-1.5">
        <RefreshCw className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />
        <div className="text-xs font-medium">Refresh this wiki</div>
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss"
          className="ml-auto inline-flex items-center justify-center w-5 h-5 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]"
        >
          <X className="h-3 w-3" />
        </button>
      </div>

      {phase === "queued" && (
        <div className="inline-flex items-center gap-1.5 text-[11px] text-emerald-500 px-1 py-1">
          <CheckCircle2 className="h-3 w-3" />
          Queued — indexing will start shortly.
        </div>
      )}

      {phase === "idle" && (
        <Button
          type="button"
          variant="primary"
          size="sm"
          className="w-full"
          onClick={() => setPhase("confirming")}
        >
          Re-index this wiki
        </Button>
      )}

      {phase === "confirming" && (
        <div className="space-y-2">
          <p className="text-[11px] text-[hsl(var(--muted-foreground))] leading-snug">
            Re-indexing can take several minutes. You can keep using the wiki
            while it runs.
          </p>
          <div className="flex items-center gap-1.5">
            <Button
              type="button"
              variant="primary"
              size="sm"
              className="flex-1"
              disabled={mutate.isPending}
              onClick={onConfirm}
            >
              {mutate.isPending ? "Queueing…" : "Re-index"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setPhase("idle")}
              disabled={mutate.isPending}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
