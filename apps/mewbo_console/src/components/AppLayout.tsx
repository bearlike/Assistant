import React, { useCallback, useState } from "react";
import { NavBar, NavBarProps } from "./NavBar";
import { TaskSidebar } from "./TaskSidebar";
import { Sheet, SheetContent, SheetTitle } from "./ui/sheet";
import { useIsMobile } from "../hooks/useIsMobile";

const SIDEBAR_KEY = "mewbo:sidebar-open";

type AppLayoutProps = Omit<NavBarProps, "onToggleSidebar" | "sidebarOpen"> & {
  children: React.ReactNode;
  /** Show the persistent task sidebar on this surface (home + session detail). */
  showSidebar?: boolean;
};

export function AppLayout({ children, showSidebar = false, ...navProps }: AppLayoutProps) {
  const isMobile = useIsMobile();
  // One open/collapsed state drives both the inline aside (desktop) and the
  // off-canvas Sheet (mobile). Persisted so a deliberate collapse sticks, but
  // defaults closed on mobile so the drawer never covers content on load.
  const [open, setOpen] = useState<boolean>(() => {
    if (typeof window !== "undefined" && window.innerWidth < 768) return false;
    try {
      return localStorage.getItem(SIDEBAR_KEY) !== "0";
    } catch {
      return true;
    }
  });
  const toggle = useCallback(() => {
    setOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0");
      } catch {
        /* storage unavailable — state still toggles for the session */
      }
      return next;
    });
  }, []);

  const showInline = showSidebar && !isMobile && open;

  return (
    <div className="flex flex-col h-screen bg-background text-foreground overflow-hidden font-sans">
      <NavBar
        {...navProps}
        onToggleSidebar={showSidebar ? toggle : undefined}
        sidebarOpen={open}
      />
      <div className="flex flex-1 overflow-hidden">
        {showInline && (
          <TaskSidebar className="w-72 shrink-0 border-r border-[hsl(var(--border-strong))]" />
        )}
        <main className="flex-1 min-w-0 overflow-hidden flex flex-col">{children}</main>
      </div>
      {showSidebar && isMobile && (
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetContent side="left" className="w-[86vw] max-w-xs p-0">
            <SheetTitle className="sr-only">Recent tasks</SheetTitle>
            <TaskSidebar onAfterNavigate={() => setOpen(false)} />
          </SheetContent>
        </Sheet>
      )}
    </div>
  );
}
