/**
 * SessionItem — generic Continue / Restart recovery affordance (Part F).
 *
 * The buttons appear only when the backend marks the session ``recoverable``
 * and it is not running; clicking them POSTs the matching recover action via
 * the shared ``useRecoverSession`` hook (Continue → "continue", Restart →
 * "retry") and stops row-click propagation.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionItem } from "../SessionItem";
import { ProjectLabel } from "../../utils/projectLabel";
import type { SessionSummary } from "../../types";
import * as client from "../../api/client";

vi.mock("../../api/client", () => ({
  recoverSession: vi.fn().mockResolvedValue({
    session_id: "s1",
    action: "continue",
    accepted: true,
    run_id: "s1:r1",
  }),
}));

const recoverSession = vi.mocked(client.recoverSession);

function renderItem(session: SessionSummary, onClick = vi.fn()) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { hook } = memoryLocation();
  const ui: ReactElement = (
    <QueryClientProvider client={qc}>
      <Router hook={hook}>
        <SessionItem session={session} projectLabel={new ProjectLabel([])} onClick={onClick} />
      </Router>
    </QueryClientProvider>
  );
  return { onClick, ...render(ui) };
}

const base: SessionSummary = {
  session_id: "s1",
  title: "Crashed session",
  status: "failed",
};

afterEach(cleanup);
beforeEach(() => {
  recoverSession.mockClear();
});

describe("SessionItem — recovery affordance", () => {
  it("hides Continue / Restart when the session is not recoverable", () => {
    renderItem({ ...base, recoverable: false });
    expect(screen.queryByRole("button", { name: /continue/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /restart/i })).toBeNull();
  });

  it("hides Continue / Restart while the session is running", () => {
    renderItem({ ...base, recoverable: true, running: true });
    expect(screen.queryByRole("button", { name: /continue/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /restart/i })).toBeNull();
  });

  it("shows both buttons when recoverable and idle", () => {
    renderItem({ ...base, recoverable: true });
    expect(screen.getByRole("button", { name: /continue/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /restart/i })).toBeInTheDocument();
  });

  it("Continue dispatches action 'continue' and does not open the row", async () => {
    const user = userEvent.setup();
    const { onClick } = renderItem({ ...base, recoverable: true });
    await user.click(screen.getByRole("button", { name: /continue/i }));
    await waitFor(() =>
      expect(recoverSession).toHaveBeenCalledWith("s1", "continue", undefined, undefined, undefined),
    );
    expect(onClick).not.toHaveBeenCalled();
  });

  it("Restart dispatches action 'retry'", async () => {
    const user = userEvent.setup();
    renderItem({ ...base, recoverable: true });
    await user.click(screen.getByRole("button", { name: /restart/i }));
    await waitFor(() =>
      expect(recoverSession).toHaveBeenCalledWith("s1", "retry", undefined, undefined, undefined),
    );
  });
});
