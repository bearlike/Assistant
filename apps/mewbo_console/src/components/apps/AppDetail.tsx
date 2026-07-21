import { useLocation } from "wouter";
import { AlertTriangle, ArrowLeft, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";
import { API_BASE } from "../../api/client";
import { useApp, useAppRenderToken, useAppSystem } from "../../hooks/useApps";
import { AppDetailHeader } from "./railSections/AppDetailHeader";
import { AppFrame } from "./AppFrame";
import { BuildProgress } from "./BuildProgress";
import { InstrumentRail } from "./railSections/InstrumentRail";
import { buildHref } from "./router";

/** Same-origin serving (spec §2.7): the served app's SDK hits the console
 *  origin, which proxies `/api`. Use the configured absolute base when set. */
const APP_API_BASE = API_BASE || (typeof window !== "undefined" ? window.location.origin : "");

const RENDERABLE = new Set(["live", "paused", "broken"]);
const BUILDING = new Set(["draft", "building"]);

export function AppDetail({ appId, sessionId }: { appId: string; sessionId?: string }) {
  const [, setLocation] = useLocation();
  const detailQuery = useApp(appId);
  const spec = detailQuery.data?.spec ?? null;
  const versions = detailQuery.data?.versions ?? [];

  const status = spec?.status;
  const renderable = status != null && RENDERABLE.has(status);
  const building = status != null && BUILDING.has(status);

  const systemQuery = useAppSystem(appId, renderable);
  // A user-writable pipeline (a frontend form) is what makes the app need a
  // write-scoped render token so `app.pipelines.submit` can POST form params.
  const writable = spec?.pipelines?.some((p) => p.user_writable === true) ?? false;
  const tokenQuery = useAppRenderToken(appId, spec?.version ?? 0, renderable, writable);

  const backToGallery = () => setLocation(buildHref({ kind: "landing" }));

  if (detailQuery.isPending) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  // The two dead-end states share one shape: a `text-sm font-medium` line, an
  // optional `text-xs` muted explanation, and the same way back. The error one
  // leads with a glyph so failure is never carried by colour alone.
  if (detailQuery.isError || !spec) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
        <div className="flex items-center gap-2 text-sm font-medium text-[hsl(var(--destructive-text))]">
          <AlertTriangle className="h-4 w-4 flex-none" />
          Couldn't load this app.
        </div>
        <Button
          variant="neutral"
          size="sm"
          onClick={backToGallery}
          leadingIcon={<ArrowLeft className="h-3.5 w-3.5" />}
        >
          All apps
        </Button>
      </div>
    );
  }

  if (status === "archived") {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
        <div className="flex max-w-[360px] items-center gap-2 text-sm font-medium">
          <span aria-hidden className="flex-none text-lg leading-none">
            {spec.icon || "✨"}
          </span>
          <span className="min-w-0 break-words">{spec.title} is archived</span>
        </div>
        <p className="max-w-[360px] text-xs text-[hsl(var(--muted-foreground))]">
          Archived apps are hidden from the gallery and their triggers are paused.
        </p>
        <Button
          variant="neutral"
          size="sm"
          onClick={backToGallery}
          leadingIcon={<ArrowLeft className="h-3.5 w-3.5" />}
        >
          All apps
        </Button>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <AppDetailHeader spec={spec} onBack={backToGallery} />

      {building ? (
        <BuildProgress
          sessionId={sessionId ?? spec.owner_session_id}
          title={spec.title}
          onReady={() => detailQuery.refetch()}
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto lg:grid lg:grid-cols-[1fr_auto] lg:overflow-hidden">
          {/* Rendered app — the primary content. Keyed on the token so a
              re-mint / rollback remounts the kernel (options are init-only). */}
          <div className="min-h-[60vh] p-3 lg:min-h-0 lg:p-4">
            {status === "broken" && (
              <div className="mb-3 flex items-start gap-2 rounded-lg border border-[hsl(var(--destructive)/0.4)] bg-[hsl(var(--destructive)/0.08)] p-3 text-xs text-[hsl(var(--destructive-text))]">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />
                <span>
                  A pipeline failed and this app is marked broken. It's showing the last good
                  version. Roll back below, or ask Mewbo to repair it.
                </span>
              </div>
            )}
            {/* The frame's own chrome is a panel card, so the token/loading
                states inside it sit on a card fill rather than the bare page. */}
            <div className={cn(cardSurface({ radius: "panel" }), "h-full overflow-hidden")}>
              {tokenQuery.isPending ? (
                <div className="flex h-full items-center justify-center text-xs text-[hsl(var(--muted-foreground))]">
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Opening app…
                </div>
              ) : tokenQuery.isError || !tokenQuery.data ? (
                <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-center text-xs text-[hsl(var(--muted-foreground))]">
                  <AlertTriangle className="h-4 w-4 text-[hsl(var(--destructive-text))]" />
                  Couldn't open the app. The render token could not be minted.
                  <Button variant="neutral" size="sm" onClick={() => tokenQuery.refetch()}>
                    Try again
                  </Button>
                </div>
              ) : (
                // No Suspense/lazy wrapper any more: the app renders in a
                // frame, so @stlite and its WASM glue load inside that frame
                // and never enter the console's own bundle graph on this route.
                <AppFrame
                  key={tokenQuery.data.token_id}
                  frontend={spec.frontend}
                  appContext={{
                    token: tokenQuery.data.token_id,
                    api_base: APP_API_BASE,
                    app_id: spec.app_id,
                    // The token's scope tells the injected SDK whether
                    // `app.pipelines.submit` (write-back) is allowed.
                    scope: tokenQuery.data.scope,
                  }}
                  title={spec.title}
                  className="h-full"
                />
              )}
            </div>
          </div>

          {/* Instrument rail — status-first cluster of Collapsible sections
              (Health, Recent runs, Pipelines, Schedule, Versions). The whole
              rail collapses to an icon strip at lg; below lg it stacks under
              the app and never collapses. */}
          <InstrumentRail
            spec={spec}
            versions={versions}
            system={systemQuery.data}
            loading={systemQuery.isPending}
          />
        </div>
      )}
    </div>
  );
}
