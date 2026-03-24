import React from "react";
import { NavBar, NavBarProps } from "./NavBar";

type AppLayoutProps = NavBarProps & {
  children: React.ReactNode;
};

export function AppLayout({ children, ...navProps }: AppLayoutProps) {
  return (
    <div className="flex flex-col h-screen bg-background text-foreground overflow-hidden font-sans">
      <NavBar {...navProps} />
      <main className="flex-1 overflow-hidden flex flex-col">{children}</main>
    </div>
  );
}
