import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation } from "wouter";
import { Menu } from "lucide-react";
import type { AppendMessage } from "@assistant-ui/react";
import { NavRail } from "./nav-rail/NavRail";
import { RailControlsProvider, type RailControls } from "./nav-rail/railControls";
import type { ActiveProduct } from "./nav-rail/products";
import { MewboRuntimeProvider } from "./assistant-ui/MewboRuntimeProvider";
import { Button } from "./ui/button";
import { useIsMobile } from "../hooks/useIsMobile";
import type { NotificationItem, SessionSummary } from "../types";

// One persisted rail state (desktop expanded/collapsed). Migrates the legacy
// task-sidebar key once, then writes only the new key.
const RAIL_KEY = "mewbo:rail";
const LEGACY_SIDEBAR_KEY = "mewbo:sidebar-open";

function readInitialCollapsed(): boolean {
  try {
    const rail = localStorage.getItem(RAIL_KEY);
    if (rail === "collapsed") return true;
    if (rail === "expanded") return false;
    // Migrate: the old sidebar stored "0" when closed. Seed the new key once.
    const collapsed = localStorage.getItem(LEGACY_SIDEBAR_KEY) === "0";
    localStorage.setItem(RAIL_KEY, collapsed ? "collapsed" : "expanded");
    return collapsed;
  } catch {
    return false;
  }
}

interface AppLayoutProps {
  children: React.ReactNode;
  /** Which product the rail marks current (drives switcher + section). */
  activeProduct: ActiveProduct;
  theme: "dark" | "light";
  onToggleTheme?: () => void;
  notifications: NotificationItem[];
  onDismissNotification?: (id: string) => void;
  onClearNotifications?: () => void;
  // assistant-ui runtime wiring. The provider is mounted HERE (not per-view) so
  // both the landing and session-detail subtrees sit under one runtime — the
  // composer and thread-list WPs consume it without prop-drilling.
  sessions: SessionSummary[];
  archivedSessions: SessionSummary[];
  activeSessionId: string | null;
  isRunning: boolean;
  onComposerNew: (message: AppendMessage) => Promise<void>;
  onSwitchToThread: (threadId: string) => void | Promise<void>;
  onSwitchToNewThread: () => void | Promise<void>;
  onRenameThread?: (threadId: string, newTitle: string) => void | Promise<void>;
  onArchiveThread?: (threadId: string) => void | Promise<void>;
}

export function AppLayout({
  children,
  activeProduct,
  theme,
  onToggleTheme,
  notifications,
  onDismissNotification,
  onClearNotifications,
  sessions,
  archivedSessions,
  activeSessionId,
  isRunning,
  onComposerNew,
  onSwitchToThread,
  onSwitchToNewThread,
  onRenameThread,
  onArchiveThread,
}: AppLayoutProps) {
  const isMobile = useIsMobile();
  const [location] = useLocation();
  const [collapsed, setCollapsed] = useState<boolean>(readInitialCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(RAIL_KEY, next ? "collapsed" : "expanded");
      } catch {
        /* storage unavailable — state still toggles for the session */
      }
      return next;
    });
  }, []);

  // Auto-close the mobile drawer on every navigation (it should never cover
  // the content after the user picks a destination).
  useEffect(() => {
    setMobileOpen(false);
  }, [location]);

  const railControls = useMemo<RailControls>(
    () => ({ openMobileRail: () => setMobileOpen(true) }),
    [],
  );

  // Session detail owns its own header (with a hamburger); every other route on
  // mobile gets the floating one below.
  const isSessionRoute = /^\/s\//.test(location);

  // Expose the rail's live width so viewport-fixed satellites (TurnScroller)
  // can offset past it. 0 on mobile (the rail is an off-canvas Sheet).
  const railWidth = isMobile ? "0px" : collapsed ? "48px" : "272px";

  return (
    <MewboRuntimeProvider
      sessions={sessions}
      archivedSessions={archivedSessions}
      activeSessionId={activeSessionId}
      isRunning={isRunning}
      onNew={onComposerNew}
      onSwitchToThread={onSwitchToThread}
      onSwitchToNewThread={onSwitchToNewThread}
      onRename={onRenameThread}
      onArchive={onArchiveThread}
    >
      <RailControlsProvider value={railControls}>
        <div
          className="flex h-dvh overflow-hidden bg-[hsl(var(--rail-bg))] font-sans text-[hsl(var(--foreground))]"
          style={{ ["--rail-w" as string]: railWidth }}
        >
          <NavRail
            activeProduct={activeProduct}
            collapsed={collapsed}
            onToggleCollapsed={toggleCollapsed}
            mobileOpen={mobileOpen}
            onMobileOpenChange={setMobileOpen}
            theme={theme}
            onToggleTheme={onToggleTheme}
            notifications={notifications}
            onDismissNotification={onDismissNotification}
            onClearNotifications={onClearNotifications}
          />
          {/* Inset content sheet — one rounded card floating on the rail canvas.
              A VISUAL wrapper only: each view keeps its own scroll/fixed
              behaviour inside. `md:pl-0` lets the card sit flush to the rail. */}
          <div className="min-w-0 flex-1 p-2 md:pl-0">
            <main className="flex h-full min-h-0 flex-col overflow-hidden rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))]">
              {children}
            </main>
          </div>
          {isMobile && !isSessionRoute && (
            <Button
              variant="neutral"
              size="sm"
              iconOnly
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation"
              title="Open navigation"
              className="fixed left-3 top-3 z-20 size-8 shadow-sm"
            >
              <Menu className="size-4" />
            </Button>
          )}
        </div>
      </RailControlsProvider>
    </MewboRuntimeProvider>
  );
}
