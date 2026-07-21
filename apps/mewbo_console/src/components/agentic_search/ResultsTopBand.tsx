import type { RefObject } from "react"
import { AlertTriangle, CircleSlash, ExternalLink, Layers } from "lucide-react"

import { CopyButton } from "../CopyButton"
import type {
  RunPayload,
  SourceCatalogEntry,
  Workspace,
} from "../../types/agenticSearch"
import { RunStats } from "./ResultsPanel"
import { SearchBar } from "./SearchBar"
import { type SearchScope } from "./SearchScopeControl"

interface ResultsTopBandProps {
  workspace: Workspace
  workspaces: Workspace[]
  sources: SourceCatalogEntry[]
  /** Budget tier + model override, bundled with their setters. */
  scope: SearchScope
  /** A run submission is in flight (mutation pending). */
  submitting?: boolean
  /** The composer's controlled value + setter — pre-filled by follow-up/refine. */
  pending: string
  onPendingChange: (value: string) => void
  onSubmit: (query: string) => void
  onOpenRun?: (runId: string) => void
  onSelectWorkspace: (workspace: Workspace) => void
  onOpenCreate: () => void
  onOpenConfig: (workspace: Workspace) => void
  /** Root's ref onto the composer wrapper — `focusBar()` queries it for the
   *  `<input>` on follow-up/refine/card-follow-up, so the ref stays owned by
   *  the root and is threaded down rather than duplicated here. */
  barRef: RefObject<HTMLDivElement>
  run: RunPayload
  resultCount: number
  elapsedMs: number
  done: boolean
  failed: boolean
  cancelled: boolean
  onCancel?: () => void
  onOpenTrace: () => void
}

/**
 * Results-page top band — the sticky search surface in the landing
 * composer's visual language: the bar is the focal point (capped + centered
 * like the hero), then the load-bearing no-sources warning, then a single
 * muted meta row (RunStats + trace/session/cancel/copy-link). The query
 * appears ONCE, in the input — editing/refining is the input's job, not a
 * redundant echo. Status reads in exactly one place (`RunStats`).
 */
export function ResultsTopBand({
  workspace,
  workspaces,
  sources,
  scope,
  submitting = false,
  pending,
  onPendingChange,
  onSubmit,
  onOpenRun,
  onSelectWorkspace,
  onOpenCreate,
  onOpenConfig,
  barRef,
  run,
  resultCount,
  elapsedMs,
  done,
  failed,
  cancelled,
  onCancel,
  onOpenTrace,
}: ResultsTopBandProps) {
  return (
    <div className="sticky top-0 z-10 bg-[hsl(var(--background)/0.92)] backdrop-blur-md border-b border-[hsl(var(--border))]">
      <div className="mx-auto max-w-[1320px] px-6 py-3">
        <div ref={barRef} className="mx-auto max-w-[570px]">
          <SearchBar
            value={pending}
            onChange={onPendingChange}
            onSubmit={onSubmit}
            onReplay={onOpenRun}
            workspace={workspace}
            workspaces={workspaces}
            onSelectWorkspace={onSelectWorkspace}
            onNewWorkspace={onOpenCreate}
            sources={sources}
            onOpenConfig={onOpenConfig}
            scope={scope}
            submitting={submitting}
            variant="compact"
          />
          {workspace.sources.length === 0 && (
            <div className="mt-2 flex items-center gap-2 text-xs text-[hsl(var(--destructive-text))]">
              <AlertTriangle className="h-3.5 w-3.5 flex-none" />
              <span>
                This workspace has no sources — new searches can't run.{" "}
                <button
                  type="button"
                  onClick={() => onOpenConfig(workspace)}
                  className="underline underline-offset-2 hover:opacity-80"
                >
                  Add sources
                </button>
              </span>
            </div>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
            <RunStats
              count={resultCount}
              elapsedMs={elapsedMs}
              done={done}
              failed={failed}
              cancelled={cancelled}
            />
            {/* Agent-trace trigger lives in the meta row too, so it's reachable
                at EVERY width (the right rail — which holds the only at-rest
                trigger — is hidden below 1100px). */}
            <button
              type="button"
              onClick={onOpenTrace}
              className="inline-flex items-center gap-1 hover:text-[hsl(var(--primary-text))] transition-colors"
            >
              <Layers className="h-3 w-3" />
              Agent trace
              {!done && (
                <span className="h-1.5 w-1.5 rounded-full bg-[hsl(var(--primary))] animate-pulse" />
              )}
            </button>
            {/* The backing agent session (deep-dive into the orchestrator's
                conversation) — only when the BE stamped a session id. */}
            {run.session_id && (
              <a
                href={`/s/${encodeURIComponent(run.session_id)}`}
                className="inline-flex items-center gap-1 hover:text-[hsl(var(--primary-text))] transition-colors"
              >
                <ExternalLink className="h-3 w-3" />
                Open agent session
              </a>
            )}
            {/* Steering: cancel an in-flight run. Mirrors the composer Stop —
                fire-and-forget; the stream's `cancelled` frame flips the view. */}
            {!done && onCancel && (
              <button
                type="button"
                onClick={onCancel}
                className="inline-flex items-center gap-1 text-[hsl(var(--destructive-text))] hover:opacity-80 transition-opacity"
              >
                <CircleSlash className="h-3 w-3" />
                Cancel
              </button>
            )}
            {/* Sources config now lives in the composer's scope control —
                the band keeps only the share affordance (DRY: status reads
                in RunStats, config reads in one place). */}
            {run.run_id && (
              <CopyButton
                text={`${window.location.origin}/search?run=${encodeURIComponent(run.run_id)}`}
                className="ml-auto h-7 px-2 text-2xs"
              >
                Copy link
              </CopyButton>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
