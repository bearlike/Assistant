/**
 * Q&A screen — DeepWiki's faithful two-column streaming layout.
 *
 *   Left  (sticky)  : back link · question · "Generated with [model]" pill ·
 *                     summary card (skeleton → resolved)
 *   Right            : skeleton stack → typewriter answer with caret
 *
 * The model in the URL is authoritative; if it differs from the local
 * persisted one we sync to the URL value so a shared link always shows the
 * same authoring badge.
 */

import { useEffect, useMemo } from "react";
import { useLocation } from "wouter";
import { ArrowLeft, Github, Sparkles } from "lucide-react";

import { cn } from "@/lib/utils";

import { LiveBlocks } from "./LiveBlocks";
import { ModelChip } from "./ModelPicker";
import { QADock } from "./QADock";
import { WikiTopBar } from "./WikiTopBar";
import { useQaStream, useWikiPage } from "./api/hooks";
import { buildHref } from "./router";
import { useStoredModel } from "./useStoredModel";

interface QAScreenProps {
  question: string;
  pageId: string;
  slug?: string;
  model?: string;
}

export function QAScreen({ question, pageId, slug, model: urlModel }: QAScreenProps) {
  const [, navigate] = useLocation();
  const [storedModel, setStoredModel] = useStoredModel();
  const repoSlug = slug ?? "bearlike/Assistant";
  const fromPageQuery = useWikiPage(pageId, repoSlug);

  // Sync local picker to the URL model when present.
  useEffect(() => {
    if (urlModel && urlModel !== storedModel) {
      setStoredModel(urlModel);
    }
    // Only when urlModel arrives initially.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlModel]);

  const answeringModel = urlModel || storedModel;

  // Subscribe to the QA event stream. The stream emits `meta` →
  // `summary_ready` → `block_open` / `block_delta` / `block_close` × N →
  // `complete`, so we can render the left summary the moment it arrives
  // and the right column grows naturally without an extra typewriter.
  const stream = useQaStream({
    question,
    fromPageId: pageId,
    model: answeringModel,
    slug: repoSlug,
  });
  const leftReady = stream.summarySources !== null;
  const hasBlocks = stream.blocks.length > 0;

  const fromPageTitle = fromPageQuery.data?.title ?? pageId;
  const sources = stream.summarySources ?? [
    "docs/core-orchestration.md",
    "CLAUDE.md",
  ];

  const onAsk = (q: string) => {
    navigate(
      buildHref({ kind: "qa", question: q, pageId, slug, model: storedModel })
    );
  };

  const skeletonWidths = useMemo(
    () => [80, 90, 60, 85, 70, 95, 50, 88, 72, 92, 64, 78, 86, 58, 90, 68],
    []
  );

  return (
    <div className="flex flex-col flex-1 overflow-hidden">
      <WikiTopBar repo={repoSlug} showBackToAll />
      <div id="wiki-scroller" className="flex-1 overflow-y-auto pb-32">
        <div className="max-w-[1200px] mx-auto px-4 sm:px-6 py-8 grid grid-cols-1 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)] gap-8">
          {/* Left column */}
          <div className="lg:sticky lg:top-6 self-start">
            <button
              type="button"
              onClick={() =>
                navigate(buildHref({ kind: "page", pageId, slug }))
              }
              className="inline-flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              {repoSlug}
            </button>

            <h1 className="mt-3 text-[clamp(20px,2.4vw,26px)] font-semibold tracking-[-0.02em] [text-wrap:balance]">
              {question}
            </h1>

            <div className="mt-2 inline-flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))]">
              <Sparkles className="h-3 w-3 text-[hsl(var(--primary))]" />
              <span>Generated with</span>
              <ModelChip modelId={answeringModel} />
            </div>

            {leftReady ? (
              <div className="mt-5 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-3.5">
                <div className="inline-flex items-center gap-1.5 mb-2">
                  <Sparkles className="h-3.5 w-3.5 text-[hsl(var(--primary))]" />
                  <span className="text-sm font-medium">Summary</span>
                </div>
                <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed">
                  Generated from{" "}
                  <code className="font-mono text-[hsl(var(--foreground))] bg-[hsl(var(--muted))]/60 px-1 rounded">
                    {fromPageTitle}
                  </code>{" "}
                  and related sources.
                </p>
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {sources.map((s) => (
                    <span
                      key={s}
                      className="inline-flex items-center gap-1 px-1.5 py-px rounded font-mono text-[10px] bg-[hsl(var(--muted))]/60 text-[hsl(var(--muted-foreground))]"
                    >
                      <Github className="h-2.5 w-2.5" />
                      {s}
                    </span>
                  ))}
                </div>
              </div>
            ) : (
              <div className="mt-5 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-3.5 space-y-2">
                {[60, 70, 45, 50].map((w, i) => (
                  <SkeletonLine key={i} width={w} />
                ))}
              </div>
            )}
          </div>

          {/* Right column */}
          <div>
            {hasBlocks ? (
              <article className="prose-wiki">
                <LiveBlocks
                  blocks={stream.blocks}
                  onNavigatePage={(p) =>
                    navigate(buildHref({ kind: "page", pageId: p, slug }))
                  }
                />
              </article>
            ) : stream.error ? (
              <div className="text-sm text-[hsl(var(--destructive))]">
                {stream.error.message}
              </div>
            ) : (
              <div className="space-y-2.5">
                {skeletonWidths.map((w, i) => (
                  <SkeletonLine key={i} width={w} />
                ))}
              </div>
            )}
          </div>
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
