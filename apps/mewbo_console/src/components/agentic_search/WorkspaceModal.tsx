import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"
import { Check } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"

import type { SourceCatalogEntry, Workspace, WorkspaceInput } from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"

const schema = z.object({
  name: z.string().min(1, "Name is required"),
  desc: z.string(),
  instructions: z.string(),
})

type FormValues = z.infer<typeof schema>

interface WorkspaceModalProps {
  open: boolean
  initial: Workspace | null
  sources: SourceCatalogEntry[]
  onClose: () => void
  onSubmit: (values: WorkspaceInput) => void
  submitting?: boolean
}

export function WorkspaceModal({
  open,
  initial,
  sources,
  onClose,
  onSubmit,
  submitting,
}: WorkspaceModalProps) {
  const isNew = !initial
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: "", desc: "", instructions: "" },
  })

  const [enabled, setEnabled] = useState<Record<string, boolean>>({})

  useEffect(() => {
    if (!open) return
    reset({
      name: initial?.name ?? "",
      desc: initial?.desc ?? "",
      instructions: initial?.instructions ?? "",
    })
    const nextEnabled: Record<string, boolean> = {}
    for (const s of sources) {
      nextEnabled[s.id] = (initial?.sources ?? []).includes(s.id)
    }
    setEnabled(nextEnabled)
  }, [open, initial, sources, reset])

  const enabledCount = Object.values(enabled).filter(Boolean).length

  const submit = handleSubmit((values) => {
    const pickedSources = sources.map((s) => s.id).filter((id) => enabled[id])
    onSubmit({
      name: values.name,
      desc: values.desc,
      sources: pickedSources,
      instructions: values.instructions,
    })
  })

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto shadow-[var(--elev-3)]">
        <DialogHeader>
          <DialogTitle>{isNew ? "New workspace" : "Configure workspace"}</DialogTitle>
          <DialogDescription>
            Workspaces scope which of your connected MCPs the search agent can reach. Same MCPs
            you use in Sessions — just grouped for a topic.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-5 mt-2">
          <div className="space-y-1.5">
            <Label htmlFor="ws-name">Name</Label>
            <Input id="ws-name" placeholder="e.g. Engineering docs" {...register("name")} />
            {errors.name && (
              <p className="text-xs text-[hsl(var(--destructive))]">{errors.name.message}</p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ws-desc">Description</Label>
            <Input
              id="ws-desc"
              placeholder="One line — what's in here?"
              {...register("desc")}
            />
          </div>

          <div className="space-y-1.5">
            <div className="flex items-baseline gap-2">
              <Label>Connections</Label>
              <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
                {enabledCount} of {sources.length} enabled
              </span>
            </div>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Your installed MCPs. Toggle which ones this workspace can query.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-1">
              {sources.map((s) => {
                const on = !!enabled[s.id]
                return (
                  <div
                    key={s.id}
                    className={cn(
                      "flex items-center gap-2.5 p-2.5 rounded-md border transition-colors",
                      on
                        ? "border-[hsl(var(--border-strong))] bg-[hsl(var(--accent))]"
                        : "border-[hsl(var(--border))] bg-[hsl(var(--background))]"
                    )}
                  >
                    <SrcAvatar source={s} size={26} />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium truncate">{s.name}</div>
                      <div className="text-xs text-[hsl(var(--muted-foreground))] truncate">
                        {s.desc}
                      </div>
                    </div>
                    <Switch
                      checked={on}
                      onCheckedChange={(v) => setEnabled((prev) => ({ ...prev, [s.id]: v }))}
                      aria-label={`Toggle ${s.name}`}
                    />
                  </div>
                )
              })}
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ws-instructions">Instructions</Label>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Guidance the search agent follows when querying this workspace's connections.
            </p>
            <Textarea
              id="ws-instructions"
              rows={4}
              placeholder={"e.g. Prefer RFCs over chat threads.\nDe-dupe results pointing to the same feature."}
              className="font-mono text-xs"
              {...register("instructions")}
            />
          </div>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={submitting}>
              Cancel
            </Button>
            <Button type="submit" disabled={submitting}>
              <Check className="h-3.5 w-3.5 mr-1.5" />
              {isNew ? "Create workspace" : "Save changes"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
