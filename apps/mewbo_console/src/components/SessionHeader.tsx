import { useCallback, useState } from "react";
import { useLocation } from "wouter";
import {
  AppWindow,
  Archive,
  ArrowLeft,
  Ban,
  BookOpen,
  Clock,
  Download,
  ExternalLink,
  MoreHorizontal,
  Menu,
  RotateCcw,
  Share,
  Square,
} from "lucide-react";

import { SessionSummary, SessionUsage } from "../types";
import { StatusBadge } from "./StatusBadge";
import { formatSessionTime } from "../utils/time";
import { ModelSummary } from "./ModelSummary";
import { ContextWindowBar } from "./ContextWindowBar";
import { useIsMobile } from "../hooks/useIsMobile";
import { cn } from "../utils/cn";
import { LangfuseIcon } from "./LangfuseIcon";
import { EditableTitle } from "./EditableTitle";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { Button } from "./ui/button";
import { useIdeStatus } from "../hooks/useIdeStatus";
import { useWebIdeEnabled } from "../hooks/useWebIdeEnabled";
import { extendIde, stopIde, IdeApiError, IdeInstance } from "../api/ide";
import { TerminateSessionDialog } from "./triggers/TerminateSessionDialog";
import { useRailControls } from "./nav-rail/railControls";
import { buildHref } from "./apps/router";
import { useWikiProjects, useWikiSessionLink } from "./wiki/api/hooks";
import { buildHref as buildWikiHref } from "./wiki/router";

/** Coder brand mark, inlined from simple-icons (slug: coder, MIT/CC0) so we
 * avoid pulling the full @icons-pack/react-simple-icons dependency for one
 * glyph used in the IDE capsule. */
function SiCoder({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <path d="M14.862 6.67H24v10.663h-9.138zM6.945 15.304c-1.934 0-3.366-1.264-3.366-3.305s1.432-3.323 3.366-3.365c1.411-.03 2.787.99 2.878 2.543l3.472-.106c-.076-2.802-2.33-4.706-6.35-4.706S0 8.558 0 12c0 3.426 3.046 5.635 6.945 5.635 3.898 0 6.29-1.935 6.38-4.782l-3.472-.077c-.152 1.553-1.497 2.528-2.908 2.528Z" />
    </svg>
  );
}

function formatRemaining(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  if (seconds <= 0) return "0m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours > 0) {
    return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  return `${Math.max(1, minutes)}m`;
}

// --- Subcomponents ----------------------------------------------------------
// Hoisted to MODULE scope (not declared inside `SessionHeader`'s body). A
// component declared inside a render function gets a brand-new function identity
// every render, so React unmounts+remounts the subtree on every unrelated
// re-render (e.g. `useWebIdeEnabled`'s `getConfig()` query settling) — silently
// discarding any Radix open-state (dropdown/popover) living inside. Stable
// module-scope identity lets React diff and update instead.

// --- Artifact jump ----------------------------------------------------------
// Jumps from a builder/maintainer session back to the artifact it exists to
// build. Two kinds:
//   - App builder/maintainer session (`session.context.app_id`, stamped by
//     `AppLifecycle._agent_session_context` — see `types.ts`) → AppJumpButton.
//   - Wiki-origin session (indexing or Q&A, `session.origin === "wiki"`) →
//     WikiJumpButton. Wiki sessions carry no project slug on `session.context`
//     (their context only ever advertises the `wiki` capability), so the slug
//     is resolved server-side via `GET /v1/wiki/sessions/<id>`
//     (`useWikiSessionLink`), a reverse lookup over the same job/answer→session
//     mappings the wiki session-end hooks already maintain. The destination
//     deep-links to the project's `landingPageId` when the cached
//     `useWikiProjects()` record has one — same routing the nav rail's own
//     wiki rows use (`nav-rail/sections.tsx:WikiSection`) — else falls back
//     to the gallery; never guesses a page id.
// Both render ONLY when resolvable: absent for a plain/unresolved session,
// never disabled-with-tooltip.
interface AppJumpButtonProps {
  appId: string;
}

function AppJumpButton({ appId }: AppJumpButtonProps) {
  const [, setLocation] = useLocation();
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => setLocation(buildHref({ kind: "detail", appId }))}
      aria-label="Open app"
      title="Open the app this session builds">
      <AppWindow className="size-4" />
      <span className="hidden lg:inline">Open app</span>
    </Button>
  );
}

interface WikiJumpButtonProps {
  sessionId: string;
}

function WikiJumpButton({ sessionId }: WikiJumpButtonProps) {
  const [, setLocation] = useLocation();
  const { data: link } = useWikiSessionLink(sessionId);
  // Cache read, not a new fetch — the nav rail's WikiSection already keeps
  // this query warm (`["wiki","projects"]`). Reused here so the deep link
  // matches EXACTLY the rail row's own routing (`sections.tsx:WikiSection`):
  // a project's `landingPageId` wins, else fall back to the gallery — never
  // guess a page id beyond what the project record explicitly carries.
  const { data: projects = [] } = useWikiProjects();
  if (!link) return null;
  const project = projects.find((p) => p.slug === link.slug);
  const href = project?.landingPageId
    ? buildWikiHref({
        kind: "page",
        pageId: project.landingPageId,
        slug: project.slug,
        platform: project.source,
      })
    : buildWikiHref({ kind: "landing" });
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => setLocation(href)}
      aria-label="Open wiki"
      title="Open the wiki project this session belongs to">
      <BookOpen className="size-4" />
      <span className="hidden lg:inline">Open wiki</span>
    </Button>
  );
}

// --- IDE capsule ----------------------------------------------------------
// Single compound control. Two states:
//   * off   → single neutral <Button> "Open in Coder" with the Coder icon
//   * ready → 3-cell capsule: [status+Open | Extend | Stop]
interface IdeCapsuleProps {
  projectLabel: string;
  ideInstance: IdeInstance | null;
  ideBusy: boolean;
  onOpen: () => void;
  onExtend: () => void;
  onStop: () => void;
}

function IdeCapsule({ projectLabel, ideInstance, ideBusy, onOpen, onExtend, onStop }: IdeCapsuleProps) {
  if (ideInstance?.status !== "ready") {
    return (
      <Button
        variant="neutral"
        size="sm"
        onClick={onOpen}
        title={`Open ${projectLabel} in Coder`}
        leadingIcon={<SiCoder className="w-3.5 h-3.5" />}>
        <span className="hidden lg:inline">Open in Coder</span>
        <span className="lg:hidden">Coder</span>
      </Button>
    );
  }

  const remaining = formatRemaining(ideInstance.remaining_seconds);
  // Custom cell styling — cells share the parent's pill and border, so they
  // can't use the Button primitive directly. Left-border hairlines divide cells.
  const cellBase =
    "inline-flex items-center gap-1.5 px-2 h-full text-xs text-[hsl(var(--foreground))] transition-colors disabled:opacity-50 [&:not(:first-child)]:border-l [&:not(:first-child)]:border-[hsl(var(--border))]";

  return (
    <div
      className="inline-flex items-center h-7 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/60 shadow-sm overflow-hidden"
      title={`Coder is running for ${projectLabel}`}>
      <button
        type="button"
        onClick={onOpen}
        title={`Open ${projectLabel} in Coder`}
        className={cn(cellBase, "hover:bg-[hsl(var(--accent))]")}>
        <span className="relative inline-flex items-center justify-center">
          <SiCoder className="w-3.5 h-3.5 text-[hsl(var(--success))]" />
          <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-[hsl(var(--success))] ring-1 ring-[hsl(var(--muted))] animate-pulse" />
        </span>
        <span className="font-medium tabular-nums">
          <span className="hidden lg:inline">Coder · </span>
          {remaining}
          <span className="hidden md:inline"> left</span>
        </span>
      </button>
      <button
        type="button"
        onClick={onExtend}
        disabled={ideBusy}
        title="Extend lifetime by 1 hour"
        className={cn(cellBase, "hover:bg-[hsl(var(--accent))]")}>
        <Clock className="size-3.5" />
        <span className="hidden md:inline">+1h</span>
      </button>
      <button
        type="button"
        onClick={onStop}
        disabled={ideBusy}
        title="Stop and remove the IDE container"
        className={cn(cellBase, "hover:bg-[hsl(var(--destructive))]/10 hover:text-[hsl(var(--destructive-text))]")}>
        <Square className="size-3.5" />
      </button>
    </div>
  );
}

// --- Overflow menu --------------------------------------------------------
// Hosts the secondary session actions, split into two groups by a hairline:
//   session actions: Archive / Copy share link / Download export
//   external links:  Langfuse (optional) / Terminate (optional)
interface OverflowMenuProps {
  sessionId: string | undefined;
  isArchived: boolean;
  isTerminated: boolean;
  langfuseUrl?: string | null;
  onArchive?: (sessionId: string) => void;
  onUnarchive?: (sessionId: string) => void;
  onShare?: (sessionId: string) => void;
  onExport?: (sessionId: string) => void;
  onTerminateClick: () => void;
}

function OverflowMenu({
  sessionId,
  isArchived,
  isTerminated,
  langfuseUrl,
  onArchive,
  onUnarchive,
  onShare,
  onExport,
  onTerminateClick,
}: OverflowMenuProps) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" iconOnly aria-label="More actions">
          <MoreHorizontal className="w-3.5 h-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem
          onSelect={() => {
            if (!sessionId) return;
            if (isArchived) { onUnarchive?.(sessionId); }
            else { onArchive?.(sessionId); }
          }}>
          {isArchived ? <RotateCcw className="w-3.5 h-3.5 mr-2" /> : <Archive className="w-3.5 h-3.5 mr-2" />}
          {isArchived ? "Restore" : "Archive"}
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            if (!sessionId) return;
            onShare?.(sessionId);
          }}>
          <Share className="w-3.5 h-3.5 mr-2" />
          Copy share link
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            if (!sessionId) return;
            onExport?.(sessionId);
          }}>
          <Download className="w-3.5 h-3.5 mr-2" />
          Download export
        </DropdownMenuItem>
        {langfuseUrl &&
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild>
            <a href={langfuseUrl} target="_blank" rel="noopener noreferrer">
              <LangfuseIcon className="w-3.5 h-3.5 mr-2" />
              Open in Langfuse
              <ExternalLink className="size-3.5 ml-auto text-[hsl(var(--muted-foreground))]" />
            </a>
          </DropdownMenuItem>
        </>
        }
        {sessionId && !isTerminated &&
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onSelect={onTerminateClick}
            className="text-[hsl(var(--destructive-text))] focus:text-[hsl(var(--destructive-text))] focus:bg-[hsl(var(--destructive))]/10">
            <Ban className="w-3.5 h-3.5 mr-2" />
            Terminate session
          </DropdownMenuItem>
        </>
        }
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export interface SessionHeaderProps {
  session: SessionSummary;
  /** Live usage snapshot for the subtitle (model summary + context bar). */
  usage: SessionUsage | null;
  /** Three-signal terminated flag from `SessionDetailView` (hides Terminate). */
  isTerminated: boolean;
  /** Authoritative status/done_reason from the live events poll — a fetch-time
   * snapshot (`session.status`/`session.done_reason`) can go stale between
   * polls (e.g. a plan-approval or off-tab start), so these win whenever the
   * poll has returned a value; `undefined` (poll not yet returned) falls back
   * to the snapshot. */
  liveStatus?: string;
  liveDoneReason?: string;
  onBack: () => void;
  onRenameTitle?: (sessionId: string, title: string) => Promise<void>;
  onRegenerateTitle?: (sessionId: string) => Promise<string>;
  onArchive?: (sessionId: string) => void;
  onUnarchive?: (sessionId: string) => void;
  onShare?: (sessionId: string) => void;
  onExport?: (sessionId: string) => void;
  langfuseUrl?: string | null;
}

/**
 * In-pane session header — the sticky top of the session detail view. Re-homes
 * every session obligation that used to live on the detail NavBar: back, the
 * editable title + regenerate, the timestamp/model/context subtitle, the global
 * StatusBadge, the IDE capsule, the overflow menu (archive/share/export/langfuse
 * /terminate) and the terminate dialog. On mobile a hamburger opens the rail.
 */
export function SessionHeader({
  session,
  usage,
  isTerminated,
  liveStatus,
  liveDoneReason,
  onBack,
  onRenameTitle,
  onRegenerateTitle,
  onArchive,
  onUnarchive,
  onShare,
  onExport,
  langfuseUrl,
}: SessionHeaderProps) {
  const isMobile = useIsMobile();
  // Poll-derived truth wins whenever the poll has returned a value; `??` only
  // falls back to the snapshot on `undefined` (poll not started/returned
  // yet), never on an intentionally empty `liveDoneReason`.
  const displayStatus = liveStatus ?? session.status ?? "idle";
  const displayDoneReason = liveDoneReason ?? session.done_reason;
  const { openMobileRail } = useRailControls();
  const [terminateOpen, setTerminateOpen] = useState(false);
  const isArchived = Boolean(session.archived);

  // Model(s) used — prefer usage data (richer: all models per turn) with a
  // fallback to the session context model for legacy/unloaded sessions.
  const models = usage?.models_used?.length
    ? usage.models_used
    : session.context?.model ? [session.context.model] : [];
  const currentModel = usage?.root_model || session.context?.model || null;

  // IDE state lives here so the header owns the capsule control. Both hooks are
  // safe to call unconditionally — `useIdeStatus(null)` no-ops and
  // `useWebIdeEnabled` returns null until the config resolves.
  const webIdeEnabled = useWebIdeEnabled();
  const ideTrackingSessionId =
    webIdeEnabled === true && Boolean(session.context?.project)
      ? session.session_id ?? null
      : null;
  const { instance: ideInstance, refresh: refreshIde, setInstance: setIdeInstance } =
    useIdeStatus(ideTrackingSessionId);
  const [ideBusy, setIdeBusy] = useState(false);

  const handleOpenIde = useCallback(() => {
    if (!session.session_id) return;
    window.open(`/ide-loader/${session.session_id}`, "_blank", "noopener");
  }, [session.session_id]);

  const handleExtendIde = useCallback(async () => {
    if (ideBusy || !session.session_id) return;
    setIdeBusy(true);
    try {
      const updated = await extendIde(session.session_id, { hours: 1 });
      setIdeInstance(updated);
    } catch (err) {
      if (err instanceof IdeApiError && err.status === 409) {
        const cap = err.body.max_deadline;
        window.alert(
          cap
            ? `Can't extend — the IDE has reached its maximum lifetime (${cap}).`
            : "Can't extend — the IDE has reached its maximum lifetime."
        );
      } else {
        const message = err instanceof Error ? err.message : String(err);
        window.alert(`Failed to extend IDE: ${message}`);
      }
    } finally {
      setIdeBusy(false);
    }
  }, [ideBusy, session.session_id, setIdeInstance]);

  const handleStopIde = useCallback(async () => {
    if (ideBusy || !session.session_id) return;
    if (!window.confirm("Stop the Web IDE container for this session?")) return;
    setIdeBusy(true);
    try {
      await stopIde(session.session_id);
      setIdeInstance(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      window.alert(`Failed to stop IDE: ${message}`);
    } finally {
      setIdeBusy(false);
      void refreshIde();
    }
  }, [ideBusy, session.session_id, refreshIde, setIdeInstance]);

  return (
    <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-2 border-b border-[hsl(var(--border))] bg-[hsl(var(--background))]/95 px-4 backdrop-blur">
      {/* Left zone: hamburger (mobile) + back + title/subtitle + status. */}
      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden md:gap-3">
        {isMobile && (
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={openMobileRail}
            aria-label="Open navigation"
            title="Open navigation"
            className="shrink-0">
            <Menu className="size-4" />
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          onClick={onBack}
          aria-label="Back"
          className="shrink-0">
          <ArrowLeft className="w-3.5 h-3.5" />
        </Button>

        <div className="group flex min-w-0 flex-1 flex-col">
          {onRenameTitle && session.session_id ?
            <EditableTitle
              value={session.title ?? ""}
              onSave={(next) => onRenameTitle(session.session_id, next)}
              onRegenerate={onRegenerateTitle ? () => onRegenerateTitle(session.session_id) : undefined}
              className="text-sm font-semibold text-[hsl(var(--foreground))]" /> :

            <h2 className="truncate text-sm font-semibold text-[hsl(var(--foreground))]">
              {session.title}
            </h2>
          }
          {/* Subtitle line — timestamp · model name · context window. */}
          <div className="hidden min-w-0 items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] md:flex">
            <span className="truncate">{formatSessionTime(session.created_at)}</span>
            {models.length > 0 &&
              <>
                <span aria-hidden className="shrink-0">·</span>
                <ModelSummary models={models} current={currentModel} />
              </>
            }
            {usage && usage.root_max_input_tokens > 0 &&
              <>
                <span aria-hidden className="shrink-0">·</span>
                <ContextWindowBar usage={usage} compact />
              </>
            }
          </div>
        </div>

        <StatusBadge
          status={displayStatus}
          doneReason={displayDoneReason}
          compact={isMobile} />
      </div>

      {/* Right zone: artifact jump → IDE capsule → overflow menu. */}
      <div className="ml-3 flex shrink-0 items-center gap-1.5 md:ml-4">
        {session.context?.app_id &&
          <AppJumpButton appId={session.context.app_id} />
        }

        {session.origin === "wiki" && session.session_id &&
          <WikiJumpButton sessionId={session.session_id} />
        }

        {ideTrackingSessionId !== null &&
          <IdeCapsule
            projectLabel={session.context?.project || "project"}
            ideInstance={ideInstance}
            ideBusy={ideBusy}
            onOpen={handleOpenIde}
            onExtend={handleExtendIde}
            onStop={handleStopIde} />
        }

        <OverflowMenu
          sessionId={session.session_id}
          isArchived={isArchived}
          isTerminated={isTerminated}
          langfuseUrl={langfuseUrl}
          onArchive={onArchive}
          onUnarchive={onUnarchive}
          onShare={onShare}
          onExport={onExport}
          onTerminateClick={() => setTerminateOpen(true)} />
      </div>

      {session.session_id &&
        <TerminateSessionDialog
          sessionId={session.session_id}
          open={terminateOpen}
          onOpenChange={setTerminateOpen} />
      }
    </header>
  );
}
