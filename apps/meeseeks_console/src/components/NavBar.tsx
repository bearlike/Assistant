import { useState, useRef, useEffect } from 'react';
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
  Download } from
'lucide-react';
import { NotificationItem, SessionSummary } from '../types';
import { StatusBadge } from './StatusBadge';
import { formatSessionTime } from '../utils/time';
import { formatModelName } from '../utils/model';
import { NotificationPanel } from './NotificationPanel';
import { useIsMobile } from '../hooks/useIsMobile';
import { cn } from '../utils/cn';
import { LangfuseIcon } from './LangfuseIcon';
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
  onShareSession?: (sessionId: string) => void;
  onExportSession?: (sessionId: string) => void;
  onSettingsClick?: () => void;
  langfuseUrl?: string | null;
}
const GITHUB_URL = 'https://github.com/bearlike/Assistant';
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
  onShareSession,
  onExportSession,
  onSettingsClick,
  langfuseUrl
}: NavBarProps) {
  const [isNotifOpen, setIsNotifOpen] = useState(false);
  const [isShareOpen, setIsShareOpen] = useState(false);
  const [isOverflowOpen, setIsOverflowOpen] = useState(false);
  const isMobile = useIsMobile();
  const unreadCount = notifications.length;
  const isArchived = Boolean(session?.archived);
  const shareRef = useRef<HTMLDivElement>(null);
  const overflowRef = useRef<HTMLDivElement>(null);
  const ThemeToggle = () =>
  <button
    onClick={onToggleTheme}
    aria-label={
    theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
    }
    className="p-1.5 rounded-lg text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] transition-colors">

      {theme === 'dark' ?
    <Sun className="w-4 h-4" /> :

    <Moon className="w-4 h-4" />
    }
    </button>;

  const GithubLink = () =>
  <a
    href={GITHUB_URL}
    target="_blank"
    rel="noopener noreferrer"
    aria-label="View on GitHub"
    className="p-1.5 rounded-lg text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] transition-colors">

      <Github className="w-4 h-4" />
    </a>;

  const LangfuseLink = () =>
  langfuseUrl ?
  <a
    href={langfuseUrl}
    target="_blank"
    rel="noopener noreferrer"
    aria-label="View in Langfuse"
    title="View in Langfuse"
    className="p-1.5 rounded-lg text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] transition-colors">
      <LangfuseIcon className="w-4 h-4" />
    </a> : null;

  const NotificationButton = () =>
  <div className="relative">
      <button
      onClick={() => setIsNotifOpen(!isNotifOpen)}
      aria-label="Notifications"
      className={`p-1.5 rounded-lg transition-colors ${isNotifOpen ? 'text-[hsl(var(--foreground))] bg-[hsl(var(--accent))]' : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]'}`}>

        <Bell className="w-4 h-4" />
        {unreadCount > 0 &&
      <span className="absolute -top-0.5 -right-0.5 w-4 h-4 rounded-full bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] text-[9px] font-bold flex items-center justify-center leading-none">
            {unreadCount > 9 ? '9+' : unreadCount}
          </span>
      }
      </button>
      {isNotifOpen &&
    <NotificationPanel
      notifications={notifications}
      onDismiss={(id) => onDismissNotification?.(id)}
      onClearAll={() => onClearNotifications?.()}
      onClose={() => setIsNotifOpen(false)} />

    }
    </div>;

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      const target = event.target as Node;
      if (shareRef.current && !shareRef.current.contains(target)) {
        setIsShareOpen(false);
      }
      if (overflowRef.current && !overflowRef.current.contains(target)) {
        setIsOverflowOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const menuItemClass = "w-full text-left px-3 py-2 text-xs text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] transition-colors flex items-center gap-2";
  const menuDropdownClass = "absolute top-full right-0 mt-2 w-44 bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-lg shadow-2xl shadow-black/40 ring-1 ring-white/[0.03] overflow-hidden z-50";

  const ShareMenu = () =>
  <div className={menuDropdownClass}>
      <button
      onClick={() => {
        if (!session?.session_id) return;
        onShareSession?.(session.session_id);
        setIsShareOpen(false);
      }}
      className={menuItemClass}>
        Copy share link
      </button>
      <button
      onClick={() => {
        if (!session?.session_id) return;
        onExportSession?.(session.session_id);
        setIsShareOpen(false);
      }}
      className={menuItemClass}>
        Download export
      </button>
    </div>;

  const OverflowMenu = () =>
  <div className={menuDropdownClass}>
      <button
      onClick={() => {
        if (!session?.session_id) return;
        if (isArchived) { onUnarchiveSession?.(session.session_id); }
        else { onArchiveSession?.(session.session_id); }
        setIsOverflowOpen(false);
      }}
      className={menuItemClass}>
        {isArchived ? <RotateCcw className="w-3.5 h-3.5" /> : <Archive className="w-3.5 h-3.5" />}
        {isArchived ? 'Restore' : 'Archive'}
      </button>
      <button
      onClick={() => {
        if (!session?.session_id) return;
        onShareSession?.(session.session_id);
        setIsOverflowOpen(false);
      }}
      className={menuItemClass}>
        <Share className="w-3.5 h-3.5" />
        Copy share link
      </button>
      <button
      onClick={() => {
        if (!session?.session_id) return;
        onExportSession?.(session.session_id);
        setIsOverflowOpen(false);
      }}
      className={menuItemClass}>
        <Download className="w-3.5 h-3.5" />
        Download export
      </button>
      {langfuseUrl &&
      <a href={langfuseUrl} target="_blank" rel="noopener noreferrer"
        className={menuItemClass}
        onClick={() => setIsOverflowOpen(false)}>
          <LangfuseIcon className="w-3.5 h-3.5" />
          Langfuse
        </a>
      }
      <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer"
        className={menuItemClass}
        onClick={() => setIsOverflowOpen(false)}>
        <Github className="w-3.5 h-3.5" />
        GitHub
      </a>
      <button
      onClick={() => { onToggleTheme?.(); setIsOverflowOpen(false); }}
      className={menuItemClass}>
        {theme === 'dark' ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
        {theme === 'dark' ? 'Light mode' : 'Dark mode'}
      </button>
      <button
      onClick={() => { onSettingsClick?.(); setIsOverflowOpen(false); }}
      className={menuItemClass}>
        <Settings className="w-3.5 h-3.5" />
        Settings
      </button>
    </div>;

  const VersionBadge = () =>
  <span className="ml-1.5 px-1.5 py-0.5 rounded text-[9px] font-mono font-semibold leading-none bg-[hsl(var(--primary))]/15 text-[hsl(var(--primary))] border border-[hsl(var(--primary))]/20">
      v0.0.9
    </span>;

  return (
    <header className="sticky top-0 z-50 w-full h-12 border-b border-[hsl(var(--border-strong))] bg-[hsl(var(--background))]/95 backdrop-blur flex items-center justify-between px-4">
      {mode === 'home' ?
      <>
          <div className="flex items-center gap-2">
            <div className="w-5 h-5 rounded-full border border-[hsl(var(--border))] flex items-center justify-center">
              <div className="w-2.5 h-2.5 rounded-full bg-[hsl(var(--foreground))]" />
            </div>
            <span className="font-semibold text-sm text-[hsl(var(--foreground))]">
              Meeseeks
            </span>
            <VersionBadge />
          </div>

          <div className="flex items-center gap-1.5">
            <button
              onClick={onSettingsClick}
              className="px-2 py-1.5 text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors rounded-lg hover:bg-[hsl(var(--accent))]">
              Settings
            </button>
            <a
              href="https://kanth.tech/assistant"
              target="_blank"
              rel="noopener noreferrer"
              className="px-2 py-1.5 text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors rounded-lg hover:bg-[hsl(var(--accent))]">

              Docs
            </a>
            <div className="w-px h-4 bg-[hsl(var(--border))] mx-0.5" />
            <GithubLink />
            <ThemeToggle />
            <NotificationButton />
            <div className="w-px h-4 bg-[hsl(var(--border))] mx-0.5" />
            <div className="w-6 h-6 rounded-full bg-indigo-600 flex items-center justify-center text-[10px] font-bold text-white ml-0.5">
              JP
            </div>
          </div>
        </> :

      <>
          {/* Left group: back + title + metadata — takes all available space */}
          <div className="flex items-center gap-2 md:gap-4 overflow-hidden min-w-0 flex-1">
            <button
            onClick={onBack}
            className="p-1 hover:bg-[hsl(var(--accent))] rounded transition-colors text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] shrink-0">
              <ArrowLeft className="w-4 h-4" />
            </button>

            <div className="flex flex-col min-w-0">
              <h2 className="text-sm font-medium text-[hsl(var(--foreground))] truncate">
                {session?.title}
              </h2>
              <span className="text-xs text-[hsl(var(--muted-foreground))] hidden md:block">
                {formatSessionTime(session?.created_at)}
              </span>
            </div>

            <StatusBadge
            status={session?.status || 'idle'}
            doneReason={session?.done_reason}
            compact={isMobile} />

            {session?.context?.model &&
            <span className="px-1.5 py-0.5 rounded text-[10px] font-mono leading-none bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] border border-[hsl(var(--border))] shrink-0 hidden lg:inline-flex">
                {formatModelName(session.context.model)}
              </span>
            }
          </div>

          {/* Right group: actions — shrink-0, responsive visibility */}
          <div className="flex items-center gap-1.5 shrink-0">
            {/* Desktop/tablet actions: visible at md+ */}
            <div className="hidden md:flex items-center gap-1.5">
              <button
              onClick={() => {
                if (!session?.session_id) return;
                if (isArchived) { onUnarchiveSession?.(session.session_id); }
                else { onArchiveSession?.(session.session_id); }
              }}
              className="flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-lg transition-colors">
                {isArchived ? <RotateCcw className="w-3.5 h-3.5" /> : <Archive className="w-3.5 h-3.5" />}
                <span className="hidden lg:inline">{isArchived ? 'Restore' : 'Archive'}</span>
              </button>
              <div className="relative" ref={shareRef}>
                <button
                onClick={() => setIsShareOpen(!isShareOpen)}
                className="flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] rounded-lg transition-colors">
                  <Share className="w-3.5 h-3.5" />
                  <span className="hidden lg:inline">Share</span>
                </button>
                {isShareOpen && <ShareMenu />}
              </div>
              <LangfuseLink />
              <div className="w-px h-4 bg-[hsl(var(--border))] mx-0.5" />
              <GithubLink />
              <ThemeToggle />
            </div>

            <NotificationButton />

            {/* Overflow menu: visible below md only */}
            <div className="relative md:hidden" ref={overflowRef}>
              <button
              onClick={() => setIsOverflowOpen(!isOverflowOpen)}
              aria-label="More actions"
              className={cn(
                "p-1.5 rounded-lg transition-colors",
                isOverflowOpen
                  ? "text-[hsl(var(--foreground))] bg-[hsl(var(--accent))]"
                  : "text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]"
              )}>
                <EllipsisVertical className="w-4 h-4" />
              </button>
              {isOverflowOpen && <OverflowMenu />}
            </div>
          </div>
        </>
      }
    </header>);

}
