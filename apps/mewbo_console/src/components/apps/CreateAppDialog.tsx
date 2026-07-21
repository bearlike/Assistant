import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { ChevronDown, FolderGit2, Loader2, Sparkles, User, Users } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { FOCUS_RING } from "@/components/ui/focus-ring";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { useProjects } from "../../hooks/useProjects";
import type { CreateAppInput } from "../../api/apps";
import type { AppWorkspaceKind } from "../../types/apps";

const schema = z.object({
  intent: z.string().min(1, "Describe what the app should do"),
});

type FormValues = z.infer<typeof schema>;

interface CreateAppDialogProps {
  open: boolean;
  onClose: () => void;
  onSubmit: (input: CreateAppInput) => void;
  submitting?: boolean;
  /** Seed the intent field (e.g. from the landing hero composer). */
  initialIntent?: string;
}

/**
 * The creation flow's first step: an intent composer plus the own/shared
 * workspace choice. On submit the caller POSTs `/api/apps` and hands off to the
 * live build progress (streaming the builder session's SSE) — this dialog owns
 * only the intent + workspace-ref inputs. Own-workspace is the default (spec
 * §2.3); shared binds the app's agents to an existing project you pick.
 */
export function CreateAppDialog({
  open,
  onClose,
  onSubmit,
  submitting,
  initialIntent = "",
}: CreateAppDialogProps) {
  const { projects, loading: projectsLoading } = useProjects();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { intent: "" },
  });

  const [kind, setKind] = useState<AppWorkspaceKind>("own");
  // The shared-workspace key is a project the app's agents anchor to (the same
  // `context.project` scope sessions use). Empty until the user picks one.
  const [projectKey, setProjectKey] = useState("");

  useEffect(() => {
    if (!open) return;
    reset({ intent: initialIntent });
    setKind("own");
    setProjectKey("");
  }, [open, initialIntent, reset]);

  const submit = handleSubmit((values) => {
    // A shared app with no project chosen falls back to "own" rather than
    // sending an empty key the backend would reject.
    const useShared = kind === "shared" && projectKey !== "";
    onSubmit({
      intent: values.intent,
      workspace: useShared
        ? { kind: "shared", key: projectKey }
        : { kind: "own", key: "" },
    });
  });

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-xl [box-shadow:var(--elev-3)]">
        <DialogHeader>
          <DialogTitle>New app</DialogTitle>
          <DialogDescription>
            Describe the app in plain language. Mewbo designs the data model, writes the
            frontend and the pipelines that keep it fresh, and opens it here when it is ready.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="mt-2 space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="app-intent">What should it do?</Label>
            <Textarea
              id="app-intent"
              rows={4}
              autoFocus
              placeholder={
                "e.g. Group my unread emails into tasks by sender and topic, and show them as a checklist I can work through."
              }
              {...register("intent")}
            />
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Mewbo designs the frontend and data pipelines from this description; pipelines
              refresh the data automatically, and builds usually take a few minutes.
            </p>
            {errors.intent && (
              <p className="text-xs text-[hsl(var(--destructive-text))]">{errors.intent.message}</p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label>Workspace</Label>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Where the app's builder and maintainer agents run. Their workspace gives them
              its instructions, tools, and memory; the served app never inherits it.
            </p>
            <div className="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-2">
              <WorkspaceOption
                selected={kind === "own"}
                onSelect={() => setKind("own")}
                icon={<User className="h-4 w-4" />}
                title="Own workspace"
                desc="A fresh, app-scoped workspace. Recommended."
              />
              <WorkspaceOption
                selected={kind === "shared"}
                onSelect={() => setKind("shared")}
                icon={<Users className="h-4 w-4" />}
                title="Shared workspace"
                desc="Reuse an existing project's tools and memory."
              />
            </div>

            {kind === "shared" && (
              <div className="relative mt-1">
                <FolderGit2 className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[hsl(var(--muted-foreground))]" />
                <select
                  value={projectKey}
                  onChange={(e) => setProjectKey(e.target.value)}
                  aria-label="Shared workspace project"
                  // `text-field md:text-sm` for the same reason the Input and
                  // Textarea primitives carry it: mobile Safari zooms the
                  // viewport for any focused form control under 16px. The
                  // intent field directly above already floors at 16px, so a
                  // 13px select beside it both zoomed and read as a different
                  // control family.
                  className={`h-9 w-full appearance-none rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] pl-8 pr-8 text-field ${FOCUS_RING} md:text-sm`}
                >
                  <option value="">
                    {projectsLoading ? "Loading projects…" : "Select a project…"}
                  </option>
                  {projects.map((p) => (
                    <option key={p.project_id ?? p.name} value={p.name}>
                      {p.name}
                    </option>
                  ))}
                </select>
                <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[hsl(var(--muted-foreground))]" />
              </div>
            )}
          </div>

          {submitting && (
            <div className="flex items-center gap-2 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted)/0.3)] px-3 py-2 text-xs text-[hsl(var(--muted-foreground))]">
              <Loader2 className="h-3.5 w-3.5 flex-none animate-spin text-[hsl(var(--primary))]" />
              Builder is designing your app — usually a few minutes. Opening it now…
            </div>
          )}

          {/* Footer button sizing follows the vendored `ConfirmDialog` — the
              console's other dialog footer — so a dialog is a dialog whichever
              one opened: ghost cancel + accented confirm, both `size="md"`. */}
          <DialogFooter>
            <Button type="button" variant="ghost" size="md" onClick={onClose} disabled={submitting}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="md"
              disabled={submitting}
              leadingIcon={
                submitting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5" />
                )
              }
            >
              {submitting ? "Starting…" : "Build app"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** One own/shared choice tile — a selectable card, not a radio-group primitive
 *  (none is vendored; a tile reads clearer for a two-option pick anyway). */
function WorkspaceOption({
  selected,
  onSelect,
  icon,
  title,
  desc,
}: {
  selected: boolean;
  onSelect: () => void;
  icon: React.ReactNode;
  title: string;
  desc: string;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex flex-col gap-1 rounded-md border p-3 text-left transition-colors",
        FOCUS_RING,
        selected
          ? "border-[hsl(var(--primary)/0.5)] bg-[hsl(var(--primary)/0.06)]"
          : "border-[hsl(var(--border))] bg-[hsl(var(--background))] hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)]",
      )}
    >
      <span
        className={cn(
          "inline-flex items-center gap-1.5 text-sm font-medium",
          selected ? "text-[hsl(var(--primary-text))]" : "text-[hsl(var(--foreground))]",
        )}
      >
        {icon}
        {title}
      </span>
      <span className="text-xs text-[hsl(var(--muted-foreground))]">{desc}</span>
    </button>
  );
}
