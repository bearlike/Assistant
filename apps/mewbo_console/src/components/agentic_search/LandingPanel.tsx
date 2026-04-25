import { useMemo, useState } from "react"
import { ChevronDown, Clock, Plus } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { SourceCatalogEntry, Workspace } from "../../types/agenticSearch"
import { SearchBar } from "./SearchBar"
import { SrcAvatar } from "./SrcAvatar"

interface LandingPanelProps {
  workspace: Workspace
  workspaces: Workspace[]
  sources: SourceCatalogEntry[]
  onPickWorkspace: (workspace: Workspace) => void
  onSubmit: (query: string) => void
  onOpenCreate: () => void
  onOpenConfig: (workspace: Workspace) => void
}

type Tab = "workspaces" | "recent"

/**
 * Landing surface — hero rhythm matched to HomeView (logo+halo, ~48px title,
 * balanced 480px subtitle), then a soft section anchor and the workspace
 * grid with Workspaces / Recent tabs that mirror the Sessions / Archive
 * pattern in HomeView.
 */
export function LandingPanel({
  workspace,
  workspaces,
  sources,
  onPickWorkspace,
  onSubmit,
  onOpenCreate,
  onOpenConfig,
}: LandingPanelProps) {
  const [value, setValue] = useState("")
  const [tab, setTab] = useState<Tab>("workspaces")

  const examples = (workspace.past_queries ?? []).slice(0, 3)

  // "Recent" surfaces only workspaces with query history, ranked by activity.
  // Backend prepends new past_queries so length is a good recency proxy.
  const sortedWorkspaces = useMemo(() => {
    if (tab === "workspaces") return workspaces
    return workspaces
      .filter((w) => (w.past_queries?.length ?? 0) > 0)
      .sort(
        (a, b) => (b.past_queries?.length ?? 0) - (a.past_queries?.length ?? 0)
      )
  }, [tab, workspaces])

  return (
    <main className="flex-1 overflow-y-auto">
      {/* Hero column — same vertical rhythm as HomeView so the two landings read as one product family. */}
      <section className="mx-auto max-w-[720px] w-full px-4 sm:px-6 flex flex-col items-center text-center pt-[clamp(56px,12vh,140px)] pb-[clamp(32px,6vh,64px)]">
        <img
          src="/logo-transparent.svg"
          alt=""
          aria-hidden
          className="w-14 h-14 mb-5 drop-shadow-[0_0_40px_hsl(var(--primary)/0.30)]"
        />
        <h1 className="text-4xl sm:text-5xl font-semibold tracking-tight mb-2.5 [text-wrap:balance]">
          Agentic search
        </h1>
        <p className="max-w-[480px] mb-6 text-[15px] leading-[1.5] text-[hsl(var(--muted-foreground))] [text-wrap:balance]">
          Ask a question. Sub-agents fan out across your workspace's connected MCPs and bring back ranked results.
        </p>

        <SearchBar
          value={value}
          onChange={setValue}
          onSubmit={onSubmit}
          workspace={workspace}
          workspaces={workspaces}
          onPickWorkspace={onPickWorkspace}
          onNewWorkspace={onOpenCreate}
          variant="hero"
          sources={sources}
          onOpenConfig={onOpenConfig}
          autoFocus
        />

        {examples.length > 0 && (
          <div className="mt-5 flex flex-wrap items-center justify-center gap-2 max-w-[720px] px-2">
            {examples.map((e) => (
              <button
                key={e.q}
                type="button"
                onClick={() => onSubmit(e.q)}
                className="inline-flex items-center gap-1.5 px-3 h-7 rounded-full border border-[hsl(var(--border))] text-xs text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors"
              >
                <Clock className="h-3 w-3" />
                {e.q}
              </button>
            ))}
          </div>
        )}
      </section>

      {/* Soft anchor — visual rhythm match to HomeView's "Recent sessions ⌄" affordance. */}
      <div
        aria-hidden
        className="flex flex-col items-center gap-0.5 text-[11px] text-[hsl(var(--muted-foreground))] opacity-70 my-2 mb-[clamp(20px,3vw,28px)]"
      >
        <span>Your workspaces</span>
        <ChevronDown className="h-3.5 w-3.5 opacity-60" />
      </div>

      {/* Workspaces grid — tabs mirror HomeView's Sessions / Archive treatment. */}
      <div className="mx-auto max-w-[1080px] w-full px-4 sm:px-6 pb-20">
        <div className="flex items-center justify-between mb-3.5 gap-3 border-b border-[hsl(var(--border))] pb-2.5">
          <div className="flex gap-6">
            {(["workspaces", "recent"] as const).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={cn(
                  "pb-2.5 -mb-[11px] text-sm font-medium border-b-2 transition-colors capitalize cursor-pointer",
                  tab === t
                    ? "text-[hsl(var(--foreground))] border-[hsl(var(--foreground))]"
                    : "text-[hsl(var(--muted-foreground))] border-transparent hover:text-[hsl(var(--foreground))]"
                )}
              >
                {t}
              </button>
            ))}
          </div>
          <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs" onClick={onOpenCreate}>
            <Plus className="h-3.5 w-3.5" />
            New workspace
          </Button>
        </div>

        <div
          className="grid gap-2.5"
          style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}
        >
          {sortedWorkspaces.map((w) => (
            <button
              key={w.id}
              type="button"
              onClick={() => onPickWorkspace(w)}
              className={cn(
                "group flex flex-col gap-2.5 p-3.5 rounded-xl border text-left min-h-[120px]",
                "shadow-[var(--elev-1)] hover:shadow-[var(--elev-2)] hover:-translate-y-px",
                "transition-[box-shadow,transform,background-color,border-color] duration-200",
                w.id === workspace.id
                  ? "border-[hsl(var(--primary)/0.5)] bg-[hsl(var(--primary)/0.04)]"
                  : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)]"
              )}
            >
              <div className="flex flex-col gap-1 flex-1">
                <h4 className="text-sm font-semibold leading-tight">{w.name}</h4>
                <p className="text-xs text-[hsl(var(--muted-foreground))] [text-wrap:pretty] line-clamp-2">
                  {w.desc}
                </p>
              </div>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-1 flex-wrap">
                  {w.sources.slice(0, 5).map((sid) => (
                    <SrcAvatar
                      key={sid}
                      source={sources.find((s) => s.id === sid)}
                      size={20}
                    />
                  ))}
                  {w.sources.length > 5 && (
                    <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] ml-1">
                      +{w.sources.length - 5}
                    </span>
                  )}
                </div>
                <span className="text-[11px] font-mono text-[hsl(var(--muted-foreground))]">
                  {w.past_queries?.length ?? 0} past
                </span>
              </div>
            </button>
          ))}
          <button
            type="button"
            onClick={onOpenCreate}
            className="flex flex-col items-center justify-center gap-1.5 p-6 rounded-xl border border-dashed border-[hsl(var(--border-strong))] text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] hover:border-[hsl(var(--primary)/0.5)] transition-colors min-h-[120px]"
          >
            <Plus className="h-5 w-5" />
            <span className="text-[13px] font-medium">New workspace</span>
            <span className="text-[11px]">Scope MCPs for a topic</span>
          </button>
        </div>
      </div>
    </main>
  )
}
