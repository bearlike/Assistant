/**
 * TriggersPane — render coverage for the Automation-facet pane the standalone
 * `/triggers` page migrated into (see console CLAUDE.md
 * "Triggers + terminated sessions"). No prior test rendered `TriggersView` at
 * all, so this is new coverage for: mount + row render, pause/resume firing
 * the right mutation, the cancel-confirm dialog firing `cancelTrigger`, and —
 * load-bearing — a `?session=<id>` in the URL seeding the session filter the
 * same way it did on the old `/triggers?session=<id>` route.
 *
 * `../../api/triggers` is mocked via `orig()` passthrough (agenticSearchUrlContract
 * pattern) so real helpers like `isActiveTrigger` (used by both the pane and
 * `TriggerRow`) stay live instead of needing to be hand-duplicated here.
 *
 * wouter's `useSearchParams` reads the REAL browser history (jsdom
 * `window.location`), same as `SettingsView.integration.test.tsx` — seed the
 * URL via `window.history.replaceState` before render, no Router wrapper
 * needed.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  cleanup,
  render as rtlRender,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

import { TriggersPane } from "../components/settings/panes/TriggersPane";
import * as triggersApi from "../api/triggers";
import * as client from "../api/client";
import type { TriggerDTO } from "../api/triggers";
import type { SessionSummary } from "../types";

vi.mock("../api/triggers", async (orig) => {
  const actual = await orig<typeof import("../api/triggers")>();
  return {
    ...actual,
    listTriggers: vi.fn(),
    listSessionTriggers: vi.fn(),
    updateTriggerStatus: vi.fn(),
    cancelTrigger: vi.fn(),
    terminateSession: vi.fn(),
  };
});

// useSessions() (via `refreshArchived`/`sessionMap`) reaches these; mirror the
// full mock shape from `taskSidebar.test.tsx` so every export it references is
// defined even though only `listSessions` is actually invoked here.
vi.mock("../api/client", () => ({
  listSessions: vi.fn(),
  archiveSession: vi.fn(),
  unarchiveSession: vi.fn(),
  updateSessionTitle: vi.fn(),
  regenerateTitle: vi.fn(),
  createSession: vi.fn(),
}));

const listTriggers = vi.mocked(triggersApi.listTriggers);
const updateTriggerStatus = vi.mocked(triggersApi.updateTriggerStatus);
const cancelTrigger = vi.mocked(triggersApi.cancelTrigger);
const listSessions = vi.mocked(client.listSessions);

function trig(overrides: Partial<TriggerDTO> = {}): TriggerDTO {
  return {
    id: "t1",
    session_id: "s1",
    kind: "time.cron",
    status: "armed",
    wake_prompt: "check the deploy",
    action: "message",
    args: { cron: "0 9 * * *" },
    fires: 0,
    created_at: "2026-07-13T10:00:00Z",
    created_by: "agent",
    ...overrides,
  };
}

function session(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: "s1",
    title: "Deploy watcher",
    status: "completed",
    ...overrides,
  };
}

const SESSIONS: SessionSummary[] = [
  session({ session_id: "s1", title: "Deploy watcher" }),
  session({ session_id: "s2", title: "PR reviewer" }),
];

const TRIGGERS: TriggerDTO[] = [
  trig({ id: "t1", session_id: "s1", kind: "time.cron", status: "armed", wake_prompt: "check the deploy" }),
  trig({ id: "t2", session_id: "s2", kind: "webhook", status: "paused", wake_prompt: "review the PR", args: {} }),
];

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function render(ui: ReactElement) {
  const qc = makeQc();
  return rtlRender(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  listTriggers.mockResolvedValue(TRIGGERS);
  listSessions.mockResolvedValue(SESSIONS);
  // Reset the URL between tests — `useSearchParams` reads real jsdom history.
  window.history.replaceState({}, "", "/settings?facet=automation");
});

describe("TriggersPane", () => {
  test("mounts and renders a row per trigger", async () => {
    render(<TriggersPane />);

    expect(await screen.findByText("check the deploy")).toBeInTheDocument();
    expect(screen.getByText("review the PR")).toBeInTheDocument();
    // Card title + the policy-section pointer (inline first line of the
    // description) both render.
    expect(
      screen.getByRole("heading", { name: "Reverse-invocation triggers" })
    ).toBeInTheDocument();
    // The pane is the GLOBAL trigger list — it has no "this session" in scope,
    // so the visible first line speaks of "a session", not "this session".
    expect(
      screen.getByText(/starts a session later, on its own, with nobody watching/)
    ).toBeInTheDocument();
  });

  test("pause/resume fires updateTriggerStatus for the clicked row", async () => {
    updateTriggerStatus.mockResolvedValue(trig({ id: "t1", status: "paused" }));
    render(<TriggersPane />);
    await screen.findByText("check the deploy");

    // t1 is "armed" → its action button is "Pause trigger".
    await userEvent.click(screen.getByRole("button", { name: "Pause trigger" }));

    expect(updateTriggerStatus).toHaveBeenCalledWith("t1", "paused");
  });

  test("cancel goes through the confirm dialog before calling cancelTrigger", async () => {
    cancelTrigger.mockResolvedValue({ id: "t1", status: "cancelled" });
    render(<TriggersPane />);
    await screen.findByText("check the deploy");

    const row = screen.getByText("check the deploy").closest("div.px-1") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Cancel trigger" }));

    // Dialog opens; cancelTrigger must NOT fire until the user confirms.
    const dialog = await screen.findByRole("dialog", { name: "Cancel this trigger?" });
    expect(cancelTrigger).not.toHaveBeenCalled();

    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel trigger" }));
    expect(cancelTrigger).toHaveBeenCalledWith("t1");
  });

  test("a ?session= deep link seeds the session filter to that session's triggers only", async () => {
    window.history.replaceState({}, "", "/settings?facet=automation&session=s2");
    render(<TriggersPane />);

    // s2's trigger renders; s1's is filtered out.
    expect(await screen.findByText("review the PR")).toBeInTheDocument();
    expect(screen.queryByText("check the deploy")).not.toBeInTheDocument();

    // The count readout reflects the narrowed filter against the full list.
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
  });
});
