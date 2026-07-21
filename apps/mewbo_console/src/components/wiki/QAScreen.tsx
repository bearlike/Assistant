/**
 * Q&A screen — a faithful two-column streaming layout.
 *
 * Per turn (``TurnView``), below a shared back-link header:
 *   Left  (sticky)  : question · "Generated with [model]" pill · summary card ·
 *                     cited-source cards (lazy file excerpts) · demoted
 *                     retrieval-details footer (accessed + models)
 *   Right            : skeleton stack → streamed markdown answer
 *
 * The model in the URL is authoritative; if it differs from the local
 * persisted one we sync to the URL value so a shared link always shows the
 * same authoring badge.
 *
 * Source cards: the card set is the unique citations from the terminal
 * ``sources`` block + the LLM-curated ``summarySources``, kind-tagged by
 * ``parseCitations`` so file / wiki-page / graph-node refs each render their
 * own card. Inline citation chips in the answer scroll to the matching card
 * via the shared ``citationDomId``. The ``sources`` block is therefore NOT
 * rendered inline in the answer column — LiveBlocks paints prose only.
 *
 * Idempotent URL: when an ``answerId`` is present the screen renders the
 * persisted snapshot (``GET /v1/wiki/qa/<id>``) and never opens a live stream,
 * so a refresh / shared link re-reads the saved answer instead of POSTing a
 * fresh LLM run. Without one it streams as before, then folds the freshly
 * assigned id into the URL (``?answer=<id>``, replace) so the next refresh is
 * idempotent — the already-streamed content keeps painting, no refetch flash.
 *
 * Multi-turn: a follow-up does NOT navigate to a new route. It reuses the same
 * ``answerId`` (backend continues the session, appending a turn) and stacks
 * each turn — question · summary/cited-sources · answer prose — as its own
 * ``TurnView`` block, divider-separated, within this one screen. The rendered
 * turn list is: prior turns from a shared-link snapshot (``QaAnswer.turns``) ∪
 * turns completed earlier this browser session ∪ the current live/in-progress
 * turn. The ``answerId`` (and its URL fold) is per-conversation, not per-turn.
 */

import { useMemo } from "react";
import type { ReactNode } from "react";
import { useLocation } from "wouter";
import { ArrowLeft, ChevronRight, Cpu, FileText, Route, Sparkles } from "lucide-react";

import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";

import { LiveBlocks } from "./LiveBlocks";
import { ModelChip } from "./ModelPicker";
import { QADock } from "./QADock";
import { SourceCard } from "./SourceCard";
import { SourceHrefProvider } from "./markdownComponents";
import { WikiTopBar } from "./WikiTopBar";
import { IndexedSnapshot } from "./indexedSnapshot";
import { parseCitations, type Citation } from "./citations";
import { buildHref } from "./router";
import { useQaConversation, type RenderedTurn } from "./useQaConversation";

interface QAScreenProps {
  question: string;
  pageId: string;
  slug?: string;
  model?: string;
  /**
   * Persisted answer id (from ``?answer=`` in the URL). When set the screen
   * renders that saved answer and skips the live stream entirely — a refresh
   * never re-invokes the LLM.
   */
  answerId?: string;
}

const SKELETON_WIDTHS = [80, 90, 60, 85, 70, 95, 50, 88, 72, 92, 64, 78, 86, 58, 90, 68];

export function QAScreen({ question, pageId, slug, model: urlModel, answerId }: QAScreenProps) {
  const [, navigate] = useLocation();
  const {
    repoSlug,
    fromPageTitle,
    repoSnapshot,
    resolveSourceHref,
    storedModel,
    setStoredModel,
    renderedTurns,
    onAsk,
  } = useQaConversation({ question, pageId, slug, model: urlModel, answerId });

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      <WikiTopBar repo={repoSlug} showBackToAll showSettings />
      <div id="wiki-scroller" className="flex-1 overflow-y-auto pb-32">
        <div className="max-w-[1200px] mx-auto px-4 sm:px-6">
          <div className="pt-8">
            <button
              type="button"
              onClick={() => navigate(buildHref({ kind: "page", pageId, slug }))}
              className="inline-flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              {repoSlug}
            </button>
          </div>

          {renderedTurns.map((turn, i) => (
            <div
              key={i}
              className={cn(
                "grid grid-cols-1 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)] gap-6 py-8",
                i > 0 && "border-t border-[hsl(var(--border))]",
              )}
            >
              <TurnView
                turn={turn}
                repoSlug={repoSlug}
                fromPageTitle={fromPageTitle}
                repoSnapshot={repoSnapshot}
                resolveSourceHref={resolveSourceHref}
                onNavigatePage={(p) => navigate(buildHref({ kind: "page", pageId: p, slug }))}
              />
            </div>
          ))}
        </div>
      </div>

      <QADock
        placeholder="Ask a follow-up question"
        model={storedModel}
        onModelChange={setStoredModel}
        onAsk={onAsk}
      />
    </div>
  );
}

/**
 * A single Q&A turn: left column (sticky) holds the question, "Generated with"
 * pill, summary card, cited-source cards, and the demoted retrieval footer;
 * the right column paints the streamed/persisted answer prose (or skeleton /
 * error / empty). Rendered identically for the live turn and every prior one.
 */
function TurnView({
  turn,
  repoSlug,
  fromPageTitle,
  repoSnapshot,
  resolveSourceHref,
  onNavigatePage,
}: {
  turn: RenderedTurn;
  repoSlug: string;
  fromPageTitle: string;
  repoSnapshot: IndexedSnapshot | null;
  resolveSourceHref: (c: Citation) => string | null;
  onNavigatePage: (pageId: string) => void;
}) {
  const { question, blocks, summarySources, done, errorMessage, accessedSources, modelsUsed, model } =
    turn;

  // Terminal-aware readiness: the summary card resolves once `summary_ready`
  // lands, OR once the answer settles (so it can't dangle a skeleton forever
  // on a zero-source answer).
  const leftReady = summarySources !== null || done;
  const hasBlocks = blocks.some((b) => b.kind !== "sources" && b.kind !== "accordion");

  // The cited-source card set: unique citations from the terminal ``sources``
  // block + the curated ``summarySources``, kind-tagged so file / wiki-page /
  // graph-node refs each render their own card. Deduped + first-seen ordered by
  // ``parseCitations``.
  const cards = useMemo(() => {
    const sourceBlock = blocks.find(
      (b): b is Extract<typeof b, { kind: "sources" }> => b.kind === "sources",
    );
    return parseCitations([...(sourceBlock?.items ?? []), ...(summarySources ?? [])]);
  }, [blocks, summarySources]);

  return (
    <>
      {/* Left column */}
      <div className="lg:sticky lg:top-6 self-start max-h-[calc(100vh-3rem)] overflow-y-auto pr-1">
        <h1 className="text-2xl font-semibold tracking-[-0.02em] [text-wrap:balance]">
          {question}
        </h1>

        <div className="mt-2 inline-flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
          <Sparkles className="h-3 w-3 text-[hsl(var(--primary))]" />
          <span>Generated with</span>
          <ModelChip modelId={model} />
        </div>

        {leftReady ? (
          <div className={cn(cardSurface({ radius: "left" }), "mt-5 p-3.5")}>
            <div className="inline-flex items-center gap-1.5 mb-2">
              <Sparkles className="h-3.5 w-3.5 text-[hsl(var(--primary))]" />
              <span className="text-sm font-medium">Summary</span>
            </div>
            <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed">
              Generated from{" "}
              <code className="text-[hsl(var(--foreground))] bg-[hsl(var(--muted))]/60 px-1 rounded">
                {fromPageTitle}
              </code>{" "}
              and related sources.
            </p>
          </div>
        ) : (
          <div className={cn(cardSurface({ radius: "left" }), "mt-5 p-3.5 space-y-2")}>
            {[60, 70, 45, 50].map((w, i) => (
              <SkeletonLine key={i} width={w} />
            ))}
          </div>
        )}

        {/* Cited sources — the primary right-panel content. */}
        {cards.length > 0 && (
          <div className="mt-5">
            <div className="inline-flex items-center gap-1.5 mb-2.5 text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
              <FileText className="h-3 w-3" />
              Cited sources
            </div>
            <div className="space-y-2.5">
              {cards.map((c) => (
                <SourceCard key={c.raw} citation={c} slug={repoSlug} snapshot={repoSnapshot} />
              ))}
            </div>
          </div>
        )}

        <ProvenanceFooter accessedSources={accessedSources} modelsUsed={modelsUsed} />
      </div>

      {/* Right column — answer prose. Skeleton, answer, error, and the
          terminal empty-state all occupy the same slot. */}
      <div>
        {hasBlocks ? (
          <article className="prose-wiki">
            <SourceHrefProvider resolve={resolveSourceHref} platform={repoSnapshot?.source ?? null}>
              <LiveBlocks blocks={blocks} onNavigatePage={onNavigatePage} />
            </SourceHrefProvider>
          </article>
        ) : errorMessage ? (
          <div className="text-sm text-[hsl(var(--destructive-text))]">{errorMessage}</div>
        ) : done ? (
          <div className="text-sm text-[hsl(var(--muted-foreground))]">
            No answer was generated for this question.
          </div>
        ) : (
          <div className="space-y-2.5">
            {SKELETON_WIDTHS.map((w, i) => (
              <SkeletonLine key={i} width={w} />
            ))}
          </div>
        )}
      </div>
    </>
  );
}

function SkeletonLine({ width }: { width: number }) {
  return (
    <div
      className={cn(
        "h-3 rounded-md overflow-hidden",
        "bg-gradient-to-r from-[hsl(var(--muted))]/40 via-[hsl(var(--muted))]/70 to-[hsl(var(--muted))]/40",
        "bg-[length:200%_100%] animate-[wiki-shimmer_1500ms_linear_infinite]"
      )}
      style={{ width: `${width}%` }}
    />
  );
}

/**
 * Secondary provenance / telemetry block, demoted BELOW the cited-source
 * cards. Shows the deterministic retrieval trail (``accessedSources``) and
 * the distinct models that ran (``modelsUsed``) — both from the answer
 * snapshot. A collapsed ``<details>`` (the wiki's established collapsible) so
 * it never competes with the answer or the source cards. Renders nothing
 * when both trails are empty.
 */
export function ProvenanceFooter({
  accessedSources,
  modelsUsed,
}: {
  accessedSources: string[];
  modelsUsed: string[];
}) {
  if (accessedSources.length === 0 && modelsUsed.length === 0) return null;

  return (
    <details className="group mt-4 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))]/40 overflow-hidden">
      <summary className="flex items-center gap-1.5 px-3 py-2 cursor-pointer select-none list-none text-2xs font-medium text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))]/30">
        <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />
        Retrieval details
      </summary>
      <div className="border-t border-[hsl(var(--border))] px-3 py-2.5 space-y-3">
        {accessedSources.length > 0 && (
          <ProvenanceGroup
            icon={<Route className="h-3 w-3" />}
            label="Accessed"
          >
            {accessedSources.map((s) => (
              <span
                key={s}
                title={s}
                className="inline-flex items-center px-1.5 py-px rounded font-mono text-2xs bg-[hsl(var(--muted))]/60 text-[hsl(var(--muted-foreground))]"
              >
                {shortenCitation(s)}
              </span>
            ))}
          </ProvenanceGroup>
        )}
        {modelsUsed.length > 0 && (
          <ProvenanceGroup icon={<Cpu className="h-3 w-3" />} label="Models">
            {modelsUsed.map((m) => (
              <ModelChip
                key={m}
                modelId={m}
                className="px-1.5 py-px rounded bg-[hsl(var(--muted))]/60 [&_span]:text-2xs [&_span]:text-[hsl(var(--muted-foreground))]"
              />
            ))}
          </ProvenanceGroup>
        )}
      </div>
    </details>
  );
}

function ProvenanceGroup({
  icon,
  label,
  children,
}: {
  icon: ReactNode;
  label: string;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="inline-flex items-center gap-1 mb-1.5 text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
        {icon}
        {label}
      </div>
      <div className="flex flex-wrap gap-1.5">{children}</div>
    </div>
  );
}

/**
 * Trim the citation-id grammar to a readable label while keeping it
 * unambiguous: ``graph:<id>`` / ``wiki:<id>`` drop their scheme prefix
 * (the icon + group already mark them as provenance ids); plain
 * ``<path>#L<a>-<b>`` source refs pass through verbatim. The full id is
 * preserved in the chip's ``title`` for hover.
 */
function shortenCitation(id: string): string {
  if (id.startsWith("graph:")) return id.slice("graph:".length);
  if (id.startsWith("wiki:")) return id.slice("wiki:".length);
  return id;
}
