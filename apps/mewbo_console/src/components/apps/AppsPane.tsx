/**
 * AppsPane — the Apps management surface, hosted in the Apps settings facet
 * (`../settings/panes.ts`). The Settings-side counterpart to the product
 * gallery: the gallery is for browsing/opening/creating, this pane is for
 * managing (pause/resume, archive, jump in). It sits in the Apps facet next to
 * where global app settings would live, the same adjacency the Triggers and
 * Plugins panes follow.
 *
 * A registered pane, so it takes ZERO props and fetches its own data via
 * `useApps` (TanStack cache, shared by queryKey). Chrome-agnostic: bare
 * `<SettingsCard>`, no page width/padding — the shell supplies both.
 */
import { useState } from "react";
import { useLocation } from "wouter";
import { AlertCircle, AppWindow, ExternalLink, Pause, Play, Trash2 } from "lucide-react";

import { Button } from "../ui/button";
import { ConfirmDialog } from "../ui/confirm-dialog";
import { SettingsCard } from "../settings/SettingsCard";
import { cn } from "@/lib/utils";
import { useApps, useArchiveApp, usePauseApp, useResumeApp } from "../../hooks/useApps";
import type { AppSummary } from "../../types/apps";
import { AppStatusBadge } from "./AppStatusBadge";
import { buildHref } from "./router";

const APPS_HELP =
  "Apps are small tools Mewbo builds for you from a plain-language description, then keeps " +
  "running on a schedule: a dashboard, a tracker, an inbox digest.\n\n" +
  "This is where you manage the ones you have. Pause an app to hold its scheduled refreshes " +
  "without deleting anything, resume it when you want it running again, or archive one you no " +
  "longer need to hide it from the gallery. Open an app to see it, roll back a version, or ask " +
  "Mewbo to change it. You create new apps from the Apps page, not here.";

export function AppsPane() {
  const [, setLocation] = useLocation();
  const appsQuery = useApps();
  const pause = usePauseApp();
  const resume = useResumeApp();
  const archive = useArchiveApp();
  const [archiveTarget, setArchiveTarget] = useState<AppSummary | null>(null);

  const apps = appsQuery.data ?? [];

  return (
    <div className="space-y-4">
      <SettingsCard
        id="settings-apps"
        title="Your apps"
        description={APPS_HELP}
        actions={
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setLocation(buildHref({ kind: "landing" }))}
            leadingIcon={<AppWindow className="h-3.5 w-3.5" />}
          >
            Open Apps
          </Button>
        }
      >
        {appsQuery.isPending ? (
          <div className="py-12 text-center text-sm text-[hsl(var(--muted-foreground))]">
            Loading apps…
          </div>
        ) : appsQuery.isError ? (
          <div className="flex items-center justify-center gap-2 py-12 text-sm text-[hsl(var(--destructive-text))]">
            <AlertCircle className="h-4 w-4 flex-none" />
            Couldn't load your apps.
          </div>
        ) : apps.length === 0 ? (
          <div className="py-12 text-center">
            {/* Empty-state illustration, not a chrome icon — sized above the
                size-3.5/4/5 ramp on purpose. */}
            <AppWindow className="mx-auto h-8 w-8 text-[hsl(var(--muted-foreground))]" />
            <p className="mt-3 text-sm font-medium">No apps yet</p>
            <p className="mx-auto mt-1 max-w-sm text-xs text-[hsl(var(--muted-foreground))]">
              Head to the Apps page and describe the first tool you want Mewbo to build.
            </p>
          </div>
        ) : (
          <div className="divide-y divide-[hsl(var(--border))] border-y border-[hsl(var(--border))]">
            {apps.map((app) => (
              <AppRow
                key={app.app_id}
                app={app}
                onOpen={() => setLocation(buildHref({ kind: "detail", appId: app.app_id }))}
                onPause={() => pause.mutate(app.app_id)}
                onResume={() => resume.mutate(app.app_id)}
                onArchive={() => setArchiveTarget(app)}
                loading={
                  (pause.isPending && pause.variables === app.app_id) ||
                  (resume.isPending && resume.variables === app.app_id)
                }
              />
            ))}
          </div>
        )}
      </SettingsCard>

      <ConfirmDialog
        open={archiveTarget !== null}
        title="Archive this app?"
        description="It will be hidden from the gallery and its triggers paused. This can't be undone from here."
        confirmLabel="Archive"
        pendingLabel="Archiving…"
        confirmIcon={<Trash2 className="h-3.5 w-3.5" />}
        pending={archive.isPending}
        onCancel={() => setArchiveTarget(null)}
        onConfirm={() => {
          if (archiveTarget) {
            archive.mutate(archiveTarget.app_id, { onSuccess: () => setArchiveTarget(null) });
          }
        }}
      />
    </div>
  );
}

function AppRow({
  app,
  onOpen,
  onPause,
  onResume,
  onArchive,
  loading,
}: {
  app: AppSummary;
  onOpen: () => void;
  onPause: () => void;
  onResume: () => void;
  onArchive: () => void;
  loading: boolean;
}) {
  const archived = app.status === "archived";
  const canPause = app.status === "live" || app.status === "broken";
  const canResume = app.status === "paused";
  return (
    <div className={cn("flex items-center gap-3 py-2.5", archived && "opacity-60")}>
      {/* `text-lg` is the app glyph's ONE size across every surface — gallery
          card, detail header, this row. */}
      <span aria-hidden className="flex-none text-lg leading-none">
        {app.icon || "✨"}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium" title={app.title}>
            {app.title}
          </span>
          <AppStatusBadge status={app.status} className="flex-none" />
        </div>
        <p className="truncate text-xs text-[hsl(var(--muted-foreground))]">{app.summary}</p>
      </div>
      <div className="flex flex-none items-center gap-1">
        {canPause && (
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            aria-label={`Pause ${app.title}`}
            title="Pause"
            disabled={loading}
            onClick={onPause}
          >
            <Pause className="h-3.5 w-3.5" />
          </Button>
        )}
        {canResume && (
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            aria-label={`Resume ${app.title}`}
            title="Resume"
            disabled={loading}
            onClick={onResume}
          >
            <Play className="h-3.5 w-3.5" />
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          aria-label={`Open ${app.title}`}
          title="Open"
          onClick={onOpen}
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </Button>
        {!archived && (
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            aria-label={`Archive ${app.title}`}
            title="Archive"
            onClick={onArchive}
            className="text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--destructive-text))]"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
    </div>
  );
}
