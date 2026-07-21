import { useState } from "react";
import { useLocation } from "wouter";
import { AlertCircle, Info, Loader2, Plus, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { cardSurface } from "@/components/ui/card-surface";
import { ComposerShell, composerInputCls } from "@/components/ui/composer-shell";
import { FOCUS_RING } from "@/components/ui/focus-ring";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { ProductHero } from "../ProductHero";
import { useApps, useCreateApp } from "../../hooks/useApps";
import type { AppSummary } from "../../types/apps";
import type { CreateAppInput } from "../../api/apps";
import { APP_CARD_MIN_H, AppCard } from "./AppCard";
import { CreateAppDialog } from "./CreateAppDialog";
import { buildHref } from "./router";

/** Archived apps drop out of the gallery — they're recoverable from Settings,
 *  not a browse target here (mirrors the sessions/archive split). */
function isGalleryApp(app: AppSummary): boolean {
  return app.status !== "archived";
}

/** The gallery's track sizing, shared by the live grid and the skeleton so the
 *  loading state occupies exactly the cells the loaded state will. */
const GALLERY_GRID = { gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" } as const;

/**
 * Apps landing — the shared `ProductHero` (peer of Tasks/Wiki/Search), a hero
 * intent composer that opens the creation dialog, then the gallery of app
 * cards. On create the flow POSTs the draft and navigates to the detail route
 * carrying the builder session id, where live build progress takes over.
 */
export function AppsLanding() {
  const [, setLocation] = useLocation();
  const appsQuery = useApps();
  const createApp = useCreateApp();

  const [intent, setIntent] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);

  const apps = (appsQuery.data ?? []).filter(isGalleryApp);

  const openCreate = () => setDialogOpen(true);
  /** One-click path from an empty-state example: prefill the intent, then open
   *  the dialog straight to a ready-to-submit draft. */
  const startWithIntent = (example: string) => {
    setIntent(example);
    setDialogOpen(true);
  };

  const handleSubmit = (input: CreateAppInput) => {
    createApp.mutate(input, {
      onSuccess: (res) => {
        setDialogOpen(false);
        setIntent("");
        // Carry the builder session id so the detail view tails its SSE for
        // live build progress until `app_ready` (the durable fallback is the
        // manifest's `owner_session_id`).
        setLocation(
          buildHref({ kind: "detail", appId: res.app_id, sessionId: res.session_id }),
        );
      },
      onError: (error) => {
        toast.error("Couldn't start the build", {
          description: error.message || "unknown error",
        });
      },
    });
  };

  return (
    // A plain <div>, not <main>: the app shell (AppLayout) already renders the
    // one <main> landmark on every route. A nested <main> is invalid landmark
    // structure and gives "skip to main content" an ambiguous target. The
    // flex/overflow behaviour is entirely in the classes, so the tag carries
    // no layout weight.
    <div className="flex-1 overflow-y-auto">
      <ProductHero
        title="Agentic Apps"
        subtitle="Describe an app in plain language. Mewbo builds the data model, the frontend, and the pipelines that keep it fresh, then keeps it running."
      >
        <HeroComposer
          value={intent}
          onChange={setIntent}
          onSubmit={openCreate}
          submitting={createApp.isPending}
        />
      </ProductHero>

      <div className="mx-auto w-full max-w-[1080px] px-4 pb-20 sm:px-6">
        <div className="mb-3.5 flex items-center justify-between gap-3 border-b border-[hsl(var(--border))] pb-2.5">
          <div className="flex items-center gap-1">
            <h2 className="text-sm font-medium">Your apps</h2>
            <HowAppsWorkPopover />
          </div>
          <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs" onClick={openCreate}>
            <Plus className="h-3.5 w-3.5" />
            New app
          </Button>
        </div>

        {appsQuery.isPending ? (
          <GallerySkeleton />
        ) : appsQuery.isError ? (
          <div className="flex items-start gap-2 py-8 text-sm text-[hsl(var(--destructive-text))]">
            <AlertCircle className="mt-px h-4 w-4 flex-none" />
            <span>Couldn't load your apps. Check that the Mewbo API server is running.</span>
          </div>
        ) : apps.length === 0 ? (
          <EmptyState onCreate={openCreate} onExample={startWithIntent} />
        ) : (
          <div className="grid gap-2.5" style={GALLERY_GRID}>
            {apps.map((app) => (
              <AppCard
                key={app.app_id}
                app={app}
                onOpen={(appId) => setLocation(buildHref({ kind: "detail", appId }))}
              />
            ))}
            {/* The dashed tile is the same grid cell as a card: same radius,
                same floor height, same hover border tint — only the fill and
                the border style say "this one is empty". */}
            <button
              type="button"
              aria-label="Create a new app"
              onClick={openCreate}
              className={cn(
                APP_CARD_MIN_H,
                "flex flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed border-[hsl(var(--border-strong))] p-3.5 text-center",
                "text-[hsl(var(--muted-foreground))] transition-colors",
                "hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)] hover:text-[hsl(var(--foreground))]",
                FOCUS_RING,
              )}
            >
              <Plus className="h-5 w-5" />
              <span className="text-sm font-medium">New app</span>
              <span className="text-2xs">Describe it in plain language</span>
            </button>
          </div>
        )}
      </div>

      <CreateAppDialog
        open={dialogOpen}
        initialIntent={intent}
        onClose={() => setDialogOpen(false)}
        onSubmit={handleSubmit}
        submitting={createApp.isPending}
      />
    </div>
  );
}

/**
 * The hero's intent entry — a calm composer that, on submit, opens the creation
 * dialog with the typed intent prefilled (the dialog owns the workspace choice).
 * Enter submits, Shift+Enter inserts a newline.
 */
function HeroComposer({
  value,
  onChange,
  onSubmit,
  submitting,
}: {
  value: string;
  onChange: (next: string) => void;
  onSubmit: () => void;
  submitting: boolean;
}) {
  return (
    <ComposerShell
      className="w-full max-w-[640px]"
      surface={{ elevation: "elev-2", halo: "strong" }}
      bodyClassName="flex flex-col gap-2 p-3"
      top={
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSubmit();
            }
          }}
          rows={2}
          placeholder="Describe an app you want, e.g. a dashboard of my open PRs grouped by repo…"
          className={cn(
            // The type size comes from the composer kit, not from here — it is
            // the iOS 16px zoom floor and it is owned in one place.
            composerInputCls(),
            "w-full resize-none bg-transparent px-1 leading-relaxed placeholder:text-[hsl(var(--muted-foreground))] focus-visible:outline-none",
          )}
        />
      }
      toolbarRight={
        // The landing's primary action, so it wears the primary variant — it
        // had been rendering in the default `neutral`, reading weaker than the
        // secondary CTA in the empty state below it. `size="md"` (h-9) is the
        // same height the shared `ComposerSendButton` uses on a hero surface,
        // and `leadingIcon` keeps the icon gap on the primitive (a hand-rolled
        // `mr-1.5` stacks on the button's own `gap-1.5` and doubles it).
        <Button
          variant="primary"
          size="md"
          onClick={onSubmit}
          disabled={submitting}
          leadingIcon={
            submitting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Sparkles className="h-3.5 w-3.5" />
            )
          }
        >
          Build app
        </Button>
      }
    />
  );
}

/** Popover explainer reachable from the gallery header, not a page, so a
 *  first-time user can learn the paradigm without leaving the gallery. */
function HowAppsWorkPopover() {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          aria-label="How apps work"
          title="How apps work"
        >
          <Info className="h-3.5 w-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        collisionPadding={16}
        // Clamped to the viewport: at 320px a fixed 320px popover overhangs the
        // gutter on both sides.
        className="w-[min(360px,calc(100vw-2rem))] text-xs"
      >
        <p className="text-[hsl(var(--foreground))]">
          An app pairs a Streamlit frontend with the data pipelines Mewbo writes to feed
          it. Pipelines that just move data run as plain code on their own schedule, at no
          LLM cost; pipelines that need judgment wake the agent when it's their turn to
          run. Once live, the app keeps itself fresh without further prompting.
        </p>
        <dl className="mt-3 space-y-2 border-t border-[hsl(var(--border))] pt-3">
          <StateRow
            tone="text-[hsl(var(--primary-text))]"
            term="Building"
            desc="The agent is designing the frontend and pipelines for the first time."
          />
          <StateRow
            tone="text-[hsl(var(--success))]"
            term="Live"
            desc="The app is running and its pipelines are wired up."
          />
          <StateRow
            tone="text-[hsl(var(--warning))]"
            term="Never refreshed"
            desc="The app is live, but no pipeline has completed a successful run yet."
          />
          <StateRow
            tone="text-[hsl(var(--warning))]"
            term="No refresh schedule"
            desc="No trigger is armed, so this data won't update on its own."
          />
          <StateRow
            tone="text-[hsl(var(--muted-foreground))]"
            term="On-demand"
            desc="This pipeline waits for you to ask, rather than running on a timer."
          />
        </dl>
      </PopoverContent>
    </Popover>
  );
}

/** One legend row. The popover is ONE type size (`text-xs`, inherited); the
 *  term/definition split is carried by weight and colour, which is the whole
 *  point of the weight ramp — a second size step here would say nothing the
 *  colour is not already saying. */
function StateRow({ tone, term, desc }: { tone: string; term: string; desc: string }) {
  return (
    <div className="flex gap-2">
      <dt className={cn("w-[124px] flex-none font-medium", tone)}>{term}</dt>
      <dd className="min-w-0 text-[hsl(var(--muted-foreground))]">{desc}</dd>
    </div>
  );
}

/** Examples that teach the shape: one code-simple (CSV), one broad workspace
 *  scan, one agentic digest, so the range of what an intent can describe reads
 *  immediately rather than needing to be inferred from a single sample. */
const EXAMPLE_INTENTS = [
  "Visualize all tracker files in my workspace",
  "Track my job applications from applications.csv",
  "Digest my unread email into a checklist every morning",
];

function EmptyState({
  onCreate,
  onExample,
}: {
  onCreate: () => void;
  onExample: (intent: string) => void;
}) {
  return (
    <div className="py-10 text-center">
      <div className="text-sm font-medium">No apps yet</div>
      <div className="mx-auto mt-1.5 max-w-[420px] space-y-1 text-xs text-[hsl(var(--muted-foreground))] [text-wrap:balance]">
        <p>
          An app pairs a Streamlit frontend with the data pipelines Mewbo writes to feed
          it, both built from one description.
        </p>
        <p>Pipelines that just move data run as plain code on a schedule, at no LLM cost.</p>
        <p>
          Describe what you want below. The builder works for a few minutes, then the app
          goes live and keeps itself fresh.
        </p>
      </div>

      <div className="mx-auto mt-4 flex max-w-[440px] flex-wrap items-center justify-center gap-1.5">
        {EXAMPLE_INTENTS.map((example) => (
          <Button
            key={example}
            variant="neutral"
            size="sm"
            // Layout only — `h-auto` + `min-h-7` lets a long example wrap while
            // keeping the AA hit target. The size class stays the primitive's
            // (`size="sm"` = text-xs); an 11px override made these the smallest
            // interactive text on the surface.
            className="h-auto min-h-7 whitespace-normal py-1.5 text-left font-normal leading-snug"
            onClick={() => onExample(example)}
          >
            {example}
          </Button>
        ))}
      </div>

      <Button
        variant="primary"
        size="md"
        className="mt-4"
        onClick={onCreate}
        leadingIcon={<Sparkles className="h-3.5 w-3.5" />}
      >
        Create your first app
      </Button>
    </div>
  );
}

function GallerySkeleton() {
  return (
    <div className="grid gap-2.5" style={GALLERY_GRID} aria-busy="true">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className={cn(
            cardSurface({ radius: "panel", elevation: "elev-1" }),
            APP_CARD_MIN_H,
            "flex flex-col gap-2.5 p-3.5",
          )}
        >
          <div className="flex gap-2.5">
            <div className="h-9 w-9 flex-none animate-pulse rounded-lg bg-[hsl(var(--muted))]" />
            <div className="flex-1 space-y-2">
              <div className={cn("h-4 w-2/3 animate-pulse rounded bg-[hsl(var(--muted))]")} />
              <div className="h-3 w-4/5 animate-pulse rounded bg-[hsl(var(--muted))]" />
            </div>
          </div>
          <div className="mt-auto h-3 w-24 animate-pulse rounded bg-[hsl(var(--muted))]" />
        </div>
      ))}
    </div>
  );
}
