import { Check, Loader2, Settings2 } from "lucide-react"
import { useLocation } from "wouter"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

import {
  isMapJobActive,
  useMapJobs,
  useMapJobStream,
  useScgStatus,
  useStartMapJob,
} from "../../hooks/useAgenticSearch"
import type { MapJobPhase, SourceCatalogEntry } from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"

// Human labels for the five-phase SCG map pipeline (mirrors the wiki's
// PHASE_LABEL idiom — labels live beside the surface that renders them).
const MAP_PHASE_LABEL: Record<MapJobPhase, string> = {
  connect: "Connecting",
  introspect: "Introspecting schema",
  parse: "Parsing capabilities",
  link: "Linking types",
  finalize: "Embedding & finalizing",
}

interface SourcesDialogProps {
  open: boolean
  sources: SourceCatalogEntry[]
  onClose: () => void
}

/**
 * Source catalog + SCG mapping surface. Lists every configured connector with
 * availability, shows which are already mapped into the Source Capability
 * Graph (`GET /scg`), and offers a "Map" action per source with live phase
 * progress over the map-events SSE stream (reload-safe via the jobs snapshot).
 */
export function SourcesDialog({ open, sources, onClose }: SourcesDialogProps) {
  const [, navigate] = useLocation()
  const scgQuery = useScgStatus(open)
  const scg = scgQuery.data
  const enabled = scg?.enabled ?? false
  const mappedIds = new Set((scg?.sources ?? []).map((s) => s.source_id))

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto shadow-[var(--elev-3)]">
        <DialogHeader>
          <DialogTitle>Sources</DialogTitle>
          <DialogDescription>
            Your configured MCP connectors. Mapping a source indexes its schemas and
            tools into the Source Capability Graph so searches can route to it.
          </DialogDescription>
        </DialogHeader>

        {scgQuery.isLoading ? (
          <div className="flex items-center gap-2 py-4 text-sm text-[hsl(var(--muted-foreground))]">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Checking graph status…
          </div>
        ) : scgQuery.isError ? (
          <div className="text-[13px] text-[hsl(var(--destructive))]">
            Couldn't read graph status: {scgQuery.error?.message ?? "unknown error"}
          </div>
        ) : enabled ? (
          scg?.counts && (
            <div className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
              {scg.counts.sources} mapped · {scg.counts.nodes} nodes ·{" "}
              {scg.counts.edges} edges · {scg.counts.recipes} recipes
            </div>
          )
        ) : (
          <div className="flex items-start gap-2.5 p-3 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted)/0.4)] text-[13px]">
            <Settings2 className="h-4 w-4 flex-none mt-0.5 text-[hsl(var(--muted-foreground))]" />
            <div className="flex-1">
              Source mapping is off. Turn on <code className="font-mono text-xs">scg.enabled</code>{" "}
              in Settings to build the Source Capability Graph and route searches through it.
            </div>
            <Button variant="neutral" size="sm" onClick={() => navigate("/settings")}>
              Open Settings
            </Button>
          </div>
        )}

        <div className="space-y-2 mt-1">
          {sources.map((s) => (
            <SourceRow
              key={s.id}
              source={s}
              scgEnabled={enabled}
              mapped={mappedIds.has(s.id)}
            />
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}

interface SourceRowProps {
  source: SourceCatalogEntry
  scgEnabled: boolean
  mapped: boolean
}

function SourceRow({ source, scgEnabled, mapped }: SourceRowProps) {
  const available = source.available !== false
  // Job state is only meaningful for mappable sources; skip the fetch otherwise.
  const jobsQuery = useMapJobs(scgEnabled && available ? source.id : null)
  const latest = jobsQuery.data?.[0]
  const active = isMapJobActive(latest)
  const stream = useMapJobStream(
    active ? source.id : null,
    active ? latest?.job_id ?? null : null
  )
  const startMap = useStartMapJob()

  // Live SSE phase wins; the polled snapshot is the reload-safe fallback. A
  // refused POST (422/503) never persists a job, so surface the mutation error.
  const phase = stream.phase ?? latest?.phase ?? null
  const failed = latest?.status === "failed" || startMap.isError
  const failure =
    startMap.error?.message ?? stream.error?.message ?? latest?.error?.message

  return (
    <div
      title={!available ? source.unavailable_reason ?? "Source unavailable" : undefined}
      className={cn(
        "flex items-center gap-2.5 p-2.5 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))]",
        !available && "opacity-50"
      )}
    >
      <SrcAvatar source={source} size={26} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          <span className="truncate">{source.name}</span>
          {!available && (
            <span className="text-[10px] uppercase tracking-wider font-mono text-[hsl(var(--muted-foreground))]">
              unavailable
            </span>
          )}
        </div>
        <div className="text-xs text-[hsl(var(--muted-foreground))] truncate">
          {active && phase ? (
            <span className="inline-flex items-center gap-1.5 text-[hsl(var(--primary))]">
              <Loader2 className="h-3 w-3 animate-spin" />
              {MAP_PHASE_LABEL[phase]}…
            </span>
          ) : active ? (
            <span className="inline-flex items-center gap-1.5">
              <Loader2 className="h-3 w-3 animate-spin" />
              Starting…
            </span>
          ) : failed && failure ? (
            <span className="text-[hsl(var(--destructive))]">Map failed: {failure}</span>
          ) : (
            source.desc
          )}
        </div>
      </div>
      {mapped && (
        <span className="inline-flex items-center gap-1 text-[11px] font-mono text-[hsl(var(--success))]">
          <Check className="h-3 w-3" />
          Mapped
        </span>
      )}
      {scgEnabled && available && !active && (
        <Button
          variant="ghost"
          size="sm"
          disabled={startMap.isPending}
          onClick={() =>
            startMap.mutate({
              sourceId: source.id,
              sourceType: source.source_type ?? "mcp_tool_list",
            })
          }
        >
          {mapped || failed ? "Re-map" : "Map"}
        </Button>
      )}
    </div>
  )
}
