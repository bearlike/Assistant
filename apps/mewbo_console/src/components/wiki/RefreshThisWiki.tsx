/**
 * Wiki index-status card — answers ONE question at a glance: "is this wiki
 * current, and do I need to re-index?"
 *
 * It leads with a VERDICT (up to date / N behind / update available / indexing
 * / failed, plus the honest "checking" / "unknown" fallbacks), derived once by
 * `deriveWikiStatus` from the existing data contracts — `useProjectFreshness`
 * for the drift comparison, the active/recoverable job lists for the
 * indexing/failed states. The supporting numbers (branch, commit, when it was
 * indexed) sit BELOW as quiet mono reference metadata, and the one real action
 * — re-index — is the only Button on the card, so it never blurs into the
 * static facts the way the old all-pills layout did.
 *
 * State-aware prominence: calm when the wiki is current (a success glyph, a
 * neutral re-index button), attention when it isn't (a tinted verdict, a
 * primary button). Calm is not silent — the card always states its verdict
 * (the `AppFreshness` "always says something" precedent), it just doesn't raise
 * its voice when there's nothing to do.
 *
 * One refresh path: the re-index Button IS the project's single re-index CTA —
 * the same one `FreshnessBadge` (in `WikiTopBar`) and the settings dialog hand
 * off to via `openSignal`. No second mutation is minted here.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  ArrowUpCircle,
  CheckCircle2,
  GitBranch,
  GitCommitHorizontal,
  HelpCircle,
  History,
  Loader2,
  RefreshCw,
  type LucideIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";

import type { IndexedSnapshot } from "./indexedSnapshot";
import { buildHref } from "./router";
import {
  useActiveIndexingJobs,
  useProjectFreshness,
  useRecoverableJobs,
  useRequestWikiRefresh,
} from "./api/hooks";
import {
  deriveWikiStatus,
  type WikiStatusKind,
  type WikiStatusTone,
} from "./wikiStatus";

interface RefreshThisWikiProps {
  slug: string;
  /** Indexed provenance; null while the project record loads. */
  snapshot?: IndexedSnapshot | null;
  /**
   * Bump to jump straight to the confirm step — lets another surface (the
   * settings dialog's "takes effect next index" hand-off, `WikiTopBar`'s
   * `FreshnessBadge`) reach this card's existing CTA instead of minting a
   * second refresh path.
   */
  openSignal?: number;
  className?: string;
}

type Phase = "idle" | "confirming" | "queued";

/** Verdict glyph per state — pairs with the headline WORD so status is never
 *  carried by colour alone. */
const KIND_ICON: Record<WikiStatusKind, LucideIcon> = {
  indexing: Loader2,
  failed: AlertTriangle,
  behind: History,
  "update-available": ArrowUpCircle,
  "up-to-date": CheckCircle2,
  checking: Loader2,
  unknown: HelpCircle,
};

/** Tinted glyph tile per tone. Status tokens carry no embedded alpha, so the
 *  `/12` tint is legitimate; text-on-tint uses the `-text` variants (which is
 *  why the tile glyph, not the tile itself, wears the colour). */
const TONE_TILE: Record<WikiStatusTone, string> = {
  success: "bg-[hsl(var(--success))]/12 text-[hsl(var(--success))]",
  warning: "bg-[hsl(var(--warning))]/12 text-[hsl(var(--warning-text))]",
  info: "bg-[hsl(var(--info))]/12 text-[hsl(var(--info-text))]",
  destructive: "bg-[hsl(var(--destructive))]/12 text-[hsl(var(--destructive-text))]",
  neutral: "bg-[hsl(var(--muted))]/50 text-[hsl(var(--muted-foreground))]",
};

/** Headline colour: reinforce the attention tones, stay foreground when calm
 *  (success/neutral) so a current wiki reads quiet. */
const TONE_HEADLINE: Record<WikiStatusTone, string> = {
  success: "text-[hsl(var(--foreground))]",
  warning: "text-[hsl(var(--warning-text))]",
  info: "text-[hsl(var(--info-text))]",
  destructive: "text-[hsl(var(--destructive-text))]",
  neutral: "text-[hsl(var(--foreground))]",
};

export function RefreshThisWiki({
  slug,
  snapshot,
  openSignal,
  className,
}: RefreshThisWikiProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const mutate = useRequestWikiRefresh();

  const freshnessQuery = useProjectFreshness(slug);
  const activeJobs = useActiveIndexingJobs();
  const recoverableJobs = useRecoverableJobs();

  const activeJob = activeJobs.data?.find((j) => j.slug === slug) ?? null;
  const recoverableJob = recoverableJobs.data?.find((j) => j.slug === slug) ?? null;

  const status = useMemo(
    () =>
      deriveWikiStatus({
        freshness: freshnessQuery.data,
        freshnessPending: freshnessQuery.isLoading,
        activeJob,
        recoverableJob,
      }),
    [freshnessQuery.data, freshnessQuery.isLoading, activeJob, recoverableJob],
  );

  // Another surface asked for the re-index CTA — open the confirm step.
  useEffect(() => {
    if (openSignal === undefined || openSignal === 0) return;
    setPhase("confirming");
  }, [openSignal]);

  // Settle back to idle after the queued confirmation has been read.
  useEffect(() => {
    if (phase !== "queued") return;
    const t = window.setTimeout(() => setPhase("idle"), 4000);
    return () => window.clearTimeout(t);
  }, [phase]);

  const onConfirm = () => {
    if (mutate.isPending) return;
    mutate.mutate(slug, {
      onSuccess: () => setPhase("queued"),
      onError: () => setPhase("idle"),
    });
  };

  const Icon = KIND_ICON[status.kind];
  const spin = status.kind === "indexing" || status.kind === "checking";

  return (
    <div className={cn(cardSurface({ radius: "left" }), "p-3", className)}>
      {/* Verdict — the one thing the reader is here to learn. */}
      <div role="status" data-kind={status.kind} className="flex items-start gap-2">
        <span
          className={cn(
            "mt-px flex h-5 w-5 flex-none items-center justify-center rounded-md",
            TONE_TILE[status.tone],
          )}
        >
          <Icon className={cn("h-3 w-3", spin && "animate-spin")} aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <p className={cn("text-sm font-medium leading-tight", TONE_HEADLINE[status.tone])}>
            {status.headline}
          </p>
          {status.detail && (
            <p className="mt-0.5 text-xs leading-snug text-[hsl(var(--muted-foreground))]">
              {status.detail}
            </p>
          )}
        </div>
      </div>

      {/* Quiet reference metadata — when it was indexed, from which branch/sha.
          Demoted to muted 2xs; branch + commit stay mono (machine text) but
          shed the coloured pills so nothing here competes with the action. */}
      {snapshot && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-2xs text-[hsl(var(--muted-foreground))]">
          <span title={snapshot.indexedTitle()}>{snapshot.indexedLabel()}</span>
          {snapshot.branch && (
            <RefLink
              href={snapshot.branchUrl()}
              title={`Branch: ${snapshot.branch}`}
              icon={<GitBranch className="h-3 w-3 shrink-0" />}
              label={snapshot.branch}
            />
          )}
          {snapshot.commitShort && (
            <RefLink
              href={snapshot.commitUrl()}
              title={snapshot.commitSha ? `Commit: ${snapshot.commitSha}` : undefined}
              icon={<GitCommitHorizontal className="h-3 w-3 shrink-0" />}
              label={snapshot.commitShort}
            />
          )}
        </div>
      )}

      {/* The one action. Suppressed while an index is already running (you view
          its progress instead); otherwise the single re-index path, primary
          when the verdict asks for action, neutral when the wiki is current. */}
      <div className="mt-3">
        {status.busy && activeJob ? (
          <Button asChild variant="neutral" size="sm" className="w-full">
            <a href={buildHref({ kind: "indexing", jobId: activeJob.jobId, slug })}>
              View progress
              <ArrowRight className="h-3 w-3" />
            </a>
          </Button>
        ) : phase === "queued" ? (
          <div className="inline-flex items-center gap-1.5 px-1 text-2xs text-[hsl(var(--success))]">
            <CheckCircle2 className="h-3 w-3" />
            Queued — indexing will start shortly.
          </div>
        ) : phase === "confirming" ? (
          <div className="space-y-2">
            <p className="text-2xs leading-snug text-[hsl(var(--muted-foreground))]">
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
        ) : (
          <Button
            type="button"
            variant={status.attention ? "primary" : "neutral"}
            size="sm"
            className="w-full"
            leadingIcon={<RefreshCw className="h-3 w-3" />}
            onClick={() => setPhase("confirming")}
          >
            Re-index this wiki
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * One quiet reference token (branch or commit). Machine text, so it keeps the
 * mono face; but it is demoted to muted 2xs with no tint or pill — it links out
 * to the host when we can build the URL, and is inert text otherwise.
 */
function RefLink({
  href,
  title,
  icon,
  label,
}: {
  href: string | null;
  title?: string;
  icon: React.ReactNode;
  label: string;
}) {
  const cls =
    "inline-flex min-w-0 items-center gap-1 font-mono leading-none transition-colors hover:text-[hsl(var(--foreground))]";
  const body = (
    <>
      {icon}
      <span className="truncate">{label}</span>
    </>
  );
  if (!href) {
    return (
      <span className={cls} title={title}>
        {body}
      </span>
    );
  }
  return (
    <a href={href} target="_blank" rel="noreferrer" title={title} className={cn(cls, "no-underline")}>
      {body}
    </a>
  );
}
