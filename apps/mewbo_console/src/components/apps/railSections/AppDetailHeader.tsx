import { useState } from "react";
import { useLocation } from "wouter";
import { ArrowLeft, ExternalLink, MoreHorizontal, Pause, Play, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useArchiveApp, usePauseApp, useResumeApp } from "../../../hooks/useApps";
import type { AppSpec } from "../../../types/apps";
import { AppStatusBadge } from "../AppStatusBadge";

export function AppDetailHeader({ spec, onBack }: { spec: AppSpec; onBack: () => void }) {
  const [, setLocation] = useLocation();
  const [confirmArchive, setConfirmArchive] = useState(false);
  const archive = useArchiveApp();

  return (
    // A structural boundary between the detail header and the panes below it,
    // so it rides --border-strong (the "structural dividers — nav, panels"
    // token); plain --border is a hairline tuned for INSIDE a card and measures
    // as absent here. The rail's own rules use --rail-border, the same value
    // under a name scoped to rail edges.
    <header className="flex shrink-0 items-center gap-3 border-b border-[hsl(var(--border-strong))] px-4 py-2.5">
      <Button variant="ghost" size="sm" iconOnly aria-label="All apps" title="All apps" onClick={onBack}>
        <ArrowLeft className="h-3.5 w-3.5" />
      </Button>
      <span aria-hidden className="text-lg leading-none">
        {spec.icon || "✨"}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h1 className="truncate text-sm font-medium">{spec.title}</h1>
          <AppStatusBadge status={spec.status} />
          <span className="hidden text-2xs text-[hsl(var(--muted-foreground))] sm:inline">
            v{spec.version}
          </span>
        </div>
        <p className="truncate text-xs text-[hsl(var(--muted-foreground))]">{spec.summary}</p>
      </div>

      <div className="flex items-center gap-1.5">
        <PauseResumeControl appId={spec.app_id} status={spec.status} />
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          aria-label="Open builder session"
          title="Open the builder/maintainer session"
          onClick={() =>
            setLocation(`/s/${encodeURIComponent(spec.maintainer_session_id ?? spec.owner_session_id)}`)
          }
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" iconOnly aria-label="More actions" title="More actions">
              <MoreHorizontal className="h-3.5 w-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            <DropdownMenuItem
              onSelect={() => setConfirmArchive(true)}
              className="text-[hsl(var(--destructive-text))] focus:bg-[hsl(var(--destructive)/0.1)] focus:text-[hsl(var(--destructive-text))]"
            >
              <Trash2 className="mr-2 h-3.5 w-3.5" />
              Archive app
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <ConfirmDialog
        open={confirmArchive}
        title="Archive this app?"
        description="It will be hidden from the gallery and its triggers paused. This can't be undone from here."
        confirmLabel="Archive"
        pendingLabel="Archiving…"
        confirmIcon={<Trash2 className="h-3.5 w-3.5" />}
        pending={archive.isPending}
        onCancel={() => setConfirmArchive(false)}
        onConfirm={() =>
          archive.mutate(spec.app_id, {
            onSuccess: () => {
              setConfirmArchive(false);
              onBack();
            },
          })
        }
      />
    </header>
  );
}

function PauseResumeControl({ appId, status }: { appId: string; status: AppSpec["status"] }) {
  const pause = usePauseApp();
  const resume = useResumeApp();
  if (status === "paused") {
    return (
      <Button
        variant="neutral"
        size="sm"
        onClick={() => resume.mutate(appId)}
        disabled={resume.isPending}
        leadingIcon={<Play className="h-3.5 w-3.5" />}
      >
        Resume
      </Button>
    );
  }
  if (status === "live" || status === "broken") {
    return (
      <Button
        variant="ghost"
        size="sm"
        onClick={() => pause.mutate(appId)}
        disabled={pause.isPending}
        leadingIcon={<Pause className="h-3.5 w-3.5" />}
      >
        Pause
      </Button>
    );
  }
  return null;
}
