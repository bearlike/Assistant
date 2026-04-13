import { useCallback, useState } from 'react';
import {
  ArrowLeft,
  Archive,
  RotateCcw,
  Share,
  Bell,
  Moon,
  Sun,
  Github,
  EllipsisVertical,
  Settings,
  Download,
  Puzzle,
  ExternalLink,
  BookOpen,
  Clock,
  Square,
  FolderOpen } from
'lucide-react';
import { NotificationItem, SessionSummary, SessionUsage } from '../types';
import { StatusBadge } from './StatusBadge';
import { formatSessionTime } from '../utils/time';
import { ModelLabel } from './ModelLabel';
import { ContextWindowBar } from './ContextWindowBar';
import { NotificationPanel } from './NotificationPanel';
import { useIsMobile } from '../hooks/useIsMobile';
import { cn } from '../utils/cn';
import { LangfuseIcon } from './LangfuseIcon';
import { EditableTitle } from './EditableTitle';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';
import { Button } from './ui/button';
import { useIdeStatus } from '../hooks/useIdeStatus';
import { useWebIdeEnabled } from '../hooks/useWebIdeEnabled';
import { extendIde, stopIde, IdeApiError } from '../api/ide';

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

export interface NavBarProps {
  mode: 'home' | 'detail';
  session?: SessionSummary;
  onBack?: () => void;
  theme?: 'dark' | 'light';
  onToggleTheme?: () => void;
  notifications?: NotificationItem[];
  onDismissNotification?: (id: string) => void;
  onClearNotifications?: () => void;
  onArchiveSession?: (sessionId: string) => void;
  onUnarchiveSession?: (sessionId: string) => void;
  onUpdateSessionTitle?: (sessionId: string, title: string) => Promise<void>;
  onRegenerateTitle?: (sessionId: string) => Promise<string>;
  onShareSession?: (sessionId: string) => void;
  onExportSession?: (sessionId: string) => void;
  onSettingsClick?: () => void;
  onPluginsClick?: () => void;
  onProjectsClick?: () => void;
  langfuseUrl?: string | null;
  sessionTokenTotals?: SessionUsage | null;
}
const GITHUB_URL = 'https://github.com/bearlike/Assistant';

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

export function NavBar({
  mode,
  session,
  onBack,
  theme = 'dark',
  onToggleTheme,
  notifications = [],
  onDismissNotification,
  onClearNotifications,
  onArchiveSession,
  onUnarchiveSession,
  onUpdateSessionTitle,
  onRegenerateTitle,
  onShareSession,
  onExportSession,
  onSettingsClick,
  onPluginsClick,
  onProjectsClick,
  langfuseUrl,
  sessionTokenTotals
}: NavBarProps) {
  const [isNotifOpen, setIsNotifOpen] = useState(false);
  const isMobile = useIsMobile();
  const unreadCount = notifications.length;
  const isArchived = Boolean(session?.archived);

  // IDE state lives here so the header owns the capsule control. Both hooks
  // are safe to call unconditionally — `useIdeStatus(null)` no-ops and
  // `useWebIdeEnabled` returns null until the config resolves.
  const webIdeEnabled = useWebIdeEnabled();
  const ideTrackingSessionId =
    mode === 'detail' && webIdeEnabled === true && Boolean(session?.context?.project)
      ? session?.session_id ?? null
      : null;
  const { instance: ideInstance, refresh: refreshIde, setInstance: setIdeInstance } =
    useIdeStatus(ideTrackingSessionId);
  const [ideBusy, setIdeBusy] = useState(false);

  const handleOpenIde = useCallback(() => {
    if (!session?.session_id) return;
    window.open(`/ide-loader/${session.session_id}`, "_blank", "noopener");
  }, [session?.session_id]);

  const handleExtendIde = useCallback(async () => {
    if (ideBusy || !session?.session_id) return;
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
  }, [ideBusy, session?.session_id, setIdeInstance]);

  const handleStopIde = useCallback(async () => {
    if (ideBusy || !session?.session_id) return;
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
  }, [ideBusy, session?.session_id, refreshIde, setIdeInstance]);

  const NotificationButton = () =>
  <NotificationPanel
    notifications={notifications}
    onDismiss={(id) => onDismissNotification?.(id)}
    onClearAll={() => onClearNotifications?.()}
    open={isNotifOpen}
    onOpenChange={setIsNotifOpen}
    trigger={
      <Button
        variant="ghost"
        size="sm"
        iconOnly
        aria-label="Notifications"
        className={cn(
          "relative",
          isNotifOpen && "text-[hsl(var(--foreground))] bg-[hsl(var(--accent))]"
        )}>
        <Bell className="w-3.5 h-3.5" />
        {unreadCount > 0 &&
        <span className="absolute -top-0.5 -right-0.5 w-4 h-4 rounded-full bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] text-[9px] font-bold flex items-center justify-center leading-none">
          {unreadCount > 9 ? '9+' : unreadCount}
        </span>
        }
      </Button>
    } />;

  // --- IDE capsule ----------------------------------------------------------
  // Single compound control replacing the old three-button toolbar. Two states:
  //   * off   → single neutral <Button> "Open in Coder" with VSCode icon
  //   * ready → 3-cell capsule: [status+Open | Extend | Stop]
  //
  // State A uses the shared <Button variant="neutral"> primitive so the chip
  // visually matches the running-state capsule container (same muted fill +
  // border). State B keeps custom cells because cells inside a shared pill
  // container can't individually be pills — the parent owns the shape via
  // `rounded-full overflow-hidden` and cells use left-border dividers.
  const IdeCapsule = () => {
    if (ideTrackingSessionId === null) return null;

    const projectLabel = session?.context?.project || 'project';

    if (ideInstance?.status !== 'ready') {
      return (
        <Button
          variant="neutral"
          size="sm"
          onClick={handleOpenIde}
          title={`Open ${projectLabel} in Coder`}
          leadingIcon={<SiCoder className="w-3.5 h-3.5" />}>
          <span className="hidden lg:inline">Open in Coder</span>
          <span className="lg:hidden">Coder</span>
        </Button>
      );
    }

    const remaining = formatRemaining(ideInstance.remaining_seconds);
    // Custom cell styling — cells share the parent's pill and border, so they
    // can't use the Button primitive directly. Left-border hairlines divide
    // cells; `disabled:opacity-50` and `transition-colors` match Button's base.
    const cellBase =
      "inline-flex items-center gap-1.5 px-2.5 h-full text-xs text-[hsl(var(--foreground))] transition-colors disabled:opacity-50 [&:not(:first-child)]:border-l [&:not(:first-child)]:border-[hsl(var(--border))]";

    return (
      <div
        className="inline-flex items-center h-7 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/60 shadow-sm overflow-hidden"
        title={`Coder is running for ${projectLabel}`}>
        <button
          type="button"
          onClick={handleOpenIde}
          title={`Open ${projectLabel} in Coder`}
          className={cn(cellBase, "hover:bg-[hsl(var(--accent))]")}>
          <span className="relative inline-flex items-center justify-center">
            <SiCoder className="w-3.5 h-3.5 text-emerald-500" />
            <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-emerald-500 ring-1 ring-[hsl(var(--muted))] animate-pulse" />
          </span>
          <span className="font-medium">
            <span className="hidden lg:inline">Coder · </span>
            {remaining}
            <span className="hidden md:inline"> left</span>
          </span>
        </button>
        <button
          type="button"
          onClick={handleExtendIde}
          disabled={ideBusy}
          title="Extend lifetime by 1 hour"
          className={cn(cellBase, "hover:bg-[hsl(var(--accent))]")}>
          <Clock className="w-3 h-3" />
          <span className="hidden md:inline">+1h</span>
        </button>
        <button
          type="button"
          onClick={handleStopIde}
          disabled={ideBusy}
          title="Stop and remove the IDE container"
          className={cn(cellBase, "hover:bg-red-500/10 hover:text-red-500")}>
          <Square className="w-3 h-3" />
        </button>
      </div>
    );
  };

  // --- Overflow menu --------------------------------------------------------
  // Now visible on every viewport (was mobile-only before). Hosts the five
  // secondary session actions, split into two groups by a single hairline:
  //   session actions: Archive / Copy share link / Download export
  //   external links:  Langfuse (optional) / GitHub
  const OverflowMenu = () =>
  <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          aria-label="More actions">
          <EllipsisVertical className="w-3.5 h-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem
          onSelect={() => {
            if (!session?.session_id) return;
            if (isArchived) { onUnarchiveSession?.(session.session_id); }
            else { onArchiveSession?.(session.session_id); }
          }}>
          {isArchived ? <RotateCcw className="w-3.5 h-3.5 mr-2" /> : <Archive className="w-3.5 h-3.5 mr-2" />}
          {isArchived ? 'Restore' : 'Archive'}
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            if (!session?.session_id) return;
            onShareSession?.(session.session_id);
          }}>
          <Share className="w-3.5 h-3.5 mr-2" />
          Copy share link
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            if (!session?.session_id) return;
            onExportSession?.(session.session_id);
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
              <ExternalLink className="w-3 h-3 ml-auto text-[hsl(var(--muted-foreground))]" />
            </a>
          </DropdownMenuItem>
        </>
        }
      </DropdownMenuContent>
    </DropdownMenu>;

  // --- Avatar menu ----------------------------------------------------------
  // Three rows above a single hairline (navigation + docs), then the theme
  // toggle as a standalone preference action. Grouping isolates the toggle
  // from the nav items — the ticket's core complaint about this menu.
  const AvatarButton = () =>
  <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          aria-label="User menu"
          className="w-6 h-6 rounded-full bg-indigo-600 flex items-center justify-center text-[10px] font-bold text-white ml-0.5 cursor-pointer hover:ring-2 hover:ring-[hsl(var(--primary))]/50 transition-all">
          JP
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuItem onSelect={() => onProjectsClick?.()}>
          <FolderOpen className="w-3.5 h-3.5 mr-2" />
          Projects
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onPluginsClick?.()}>
          <Puzzle className="w-3.5 h-3.5 mr-2" />
          Plugins
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => onSettingsClick?.()}>
          <Settings className="w-3.5 h-3.5 mr-2" />
          Settings
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <a href="https://kanth.tech/assistant" target="_blank" rel="noopener noreferrer">
            <BookOpen className="w-3.5 h-3.5 mr-2" />
            Documentation
            <ExternalLink className="w-3 h-3 ml-auto text-[hsl(var(--muted-foreground))]" />
          </a>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
            <Github className="w-3.5 h-3.5 mr-2" />
            View on GitHub
            <ExternalLink className="w-3 h-3 ml-auto text-[hsl(var(--muted-foreground))]" />
          </a>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onToggleTheme?.()}>
          {theme === 'dark' ? <Sun className="w-3.5 h-3.5 mr-2" /> : <Moon className="w-3.5 h-3.5 mr-2" />}
          {theme === 'dark' ? 'Switch to Light mode' : 'Switch to Dark mode'}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>;

  const VersionBadge = () =>
  <span className="ml-1.5 px-1.5 py-0.5 rounded text-[9px] font-mono font-semibold leading-none bg-[hsl(var(--primary))]/15 text-[hsl(var(--primary))] border border-[hsl(var(--primary))]/20">
      v0.0.10
    </span>;

  return (
    <header className="sticky top-0 z-50 w-full h-14 border-b border-[hsl(var(--border-strong))] bg-[hsl(var(--card))]/95 backdrop-blur flex items-center justify-between px-4">
      {mode === 'home' ?
      <>
          <button
            type="button"
            onClick={onBack}
            aria-label="Go to home"
            className="flex items-center gap-2 rounded px-1 -mx-1 py-0.5 hover:opacity-70 transition-opacity">
            <img src="/logo-transparent.svg" alt="Meeseeks" className="w-5 h-5" />
            <span className="font-semibold text-sm text-[hsl(var(--foreground))]">
              Meeseeks
            </span>
            <VersionBadge />
          </button>

          <div className="flex items-center gap-1.5">
            <NotificationButton />
            <AvatarButton />
          </div>
        </> :

      <>
          {/* Left zone: back + title/subtitle + status. Identity only. */}
          <div className="flex items-center gap-2 md:gap-3 overflow-hidden min-w-0 flex-1">
            <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={onBack}
            aria-label="Back"
            className="shrink-0">
              <ArrowLeft className="w-3.5 h-3.5" />
            </Button>

            <div className="group flex flex-col min-w-0 flex-1">
              {onUpdateSessionTitle && session?.session_id ?
                <EditableTitle
                  value={session?.title ?? ''}
                  onSave={(next) => onUpdateSessionTitle(session.session_id, next)}
                  onRegenerate={onRegenerateTitle ? () => onRegenerateTitle(session.session_id) : undefined}
                  className="text-sm font-medium text-[hsl(var(--foreground))]" /> :

                <h2 className="text-sm font-medium text-[hsl(var(--foreground))] truncate">
                  {session?.title}
                </h2>
              }
              {/* Subtitle line — timestamp · model name, both quiet muted text */}
              <div className="hidden md:flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] min-w-0">
                <span className="truncate">{formatSessionTime(session?.created_at)}</span>
                {session?.context?.model &&
                  <>
                    <span aria-hidden className="shrink-0">·</span>
                    <ModelLabel
                      modelId={session?.context?.model}
                      className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] truncate" />
                  </>
                }
                {sessionTokenTotals && sessionTokenTotals.root_max_input_tokens > 0 &&
                  <>
                    <span aria-hidden className="shrink-0">·</span>
                    {/* Visual context-window bar — replaces the old text "peak X/Y · Z out".
                        Reads `root_last_input_tokens` (size of the most recent prompt)
                        as the canonical "current fill" signal; peak / billed / cache
                        savings / compactions all live in the click-to-expand popover. */}
                    <ContextWindowBar usage={sessionTokenTotals} compact />
                  </>
                }
              </div>
            </div>

            <StatusBadge
            status={session?.status || 'idle'}
            doneReason={session?.done_reason}
            compact={isMobile} />
          </div>

          {/* Right zone: IDE capsule → bell → overflow → divider → avatar.
              ml-3 md:ml-4 guarantees breathing room between the status badge
              (right edge of the left zone) and the capsule, so they never
              touch even when the title shrinks. */}
          <div className="flex items-center gap-1.5 shrink-0 ml-3 md:ml-4">
            <IdeCapsule />

            <NotificationButton />

            <OverflowMenu />

            <div className="w-px h-4 bg-[hsl(var(--border))] mx-0.5" />
            <AvatarButton />
          </div>
        </>
      }
    </header>);

}
