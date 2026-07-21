/**
 * NavRail — collapsed 48px rail symmetry.
 *
 * The user asked for the collapsed rail's icons to sit "perfectly laid out in
 * one line", i.e. one vertical centerline. This guards that law where it is
 * easiest to regress silently: the FOOTER controls (the notification bell and
 * the account avatar) must sit in the SAME box the product switcher glyphs use
 * (`railActionRowCls` + centered) — same `h-8 w-full` centered hit target — so
 * brand, product glyphs, bell and avatar read as one column. A future edit that
 * re-hand-rolls a footer icon at a different size breaks these assertions
 * instead of shipping an off-axis rail.
 */
import { cleanup, render as rtlRender, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import { NavRail } from "../components/nav-rail/NavRail";
import { NotificationItem } from "../types";

// The collapsed rail never renders the scoped ProductSection (`{!collapsed && …}`),
// so stub it to keep assistant-ui out of this suite's import graph.
vi.mock("../components/nav-rail/sections", () => ({
  ProductSection: () => null,
}));

// `useAuthSession` is the rail footer's only network read. An anonymous result
// is enough: the avatar falls back to its brand glyph and the buttons render.
vi.mock("../api/auth", () => ({
  fetchMe: vi.fn().mockResolvedValue({ authenticated: false, auth_enabled: false }),
  logout: vi.fn().mockResolvedValue(undefined),
}));

const NOTIFS: NotificationItem[] = [
  { id: "n1", title: "One", message: "", level: "info", created_at: "2026-06-10T12:00:00Z" },
  { id: "n2", title: "Two", message: "", level: "info", created_at: "2026-06-10T12:01:00Z" },
  { id: "n3", title: "Three", message: "", level: "info", created_at: "2026-06-10T12:02:00Z" },
];

function render(ui: ReactElement) {
  const { hook } = memoryLocation({ path: "/", record: true });
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return rtlRender(
    <QueryClientProvider client={qc}>
      <Router hook={hook}>{ui}</Router>
    </QueryClientProvider>,
  );
}

function collapsedRail() {
  return (
    <NavRail
      activeProduct="tasks"
      collapsed
      onToggleCollapsed={() => undefined}
      mobileOpen={false}
      onMobileOpenChange={() => undefined}
      theme="dark"
      notifications={NOTIFS}
    />
  );
}

// The one centered box every collapsed glyph must sit in.
const BOX = ["h-8", "w-full", "justify-center", "rounded-lg"];
const boxOf = (el: HTMLElement) => BOX.filter((c) => el.className.split(/\s+/).includes(c));

beforeEach(() => {
  vi.clearAllMocks();
});

// This suite doesn't enable Vitest globals, so RTL's auto-cleanup never runs.
afterEach(() => cleanup());

test("collapsed footer bell + avatar share the product row's centered box", async () => {
  render(collapsedRail());

  const product = await screen.findByLabelText("Wiki");
  const bell = screen.getByRole("button", { name: /^Notifications/ });
  const account = screen.getByLabelText("Account menu");

  // All three occupy the identical h-8 / w-full / centered box → one centerline.
  expect(boxOf(product)).toEqual(BOX);
  expect(boxOf(bell)).toEqual(BOX);
  expect(boxOf(account)).toEqual(BOX);
});

test("the unread badge rides the bell's corner without widening its box", () => {
  render(collapsedRail());

  const bell = screen.getByRole("button", { name: "Notifications, 3 unread" });
  // Count is shown (capped form would be "9+"; here 3), and the true count is in
  // the accessible name above — the badge never knocks the w-full bell off-center.
  expect(bell).toHaveTextContent("3");
  expect(bell.className.split(/\s+/)).toContain("w-full");
});

test("the collapsed avatar shrinks to the bell's size-4 glyph box", () => {
  render(collapsedRail());

  const account = screen.getByLabelText("Account menu");
  // Avatar glyph steps down from its default size-6 to size-4 so it matches the
  // bell rather than dominating the icon column.
  expect(account.querySelector(".size-4")).not.toBeNull();
});
