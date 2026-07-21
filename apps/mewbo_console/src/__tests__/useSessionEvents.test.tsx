/**
 * useSessionEvents — status threading + keepalive polling.
 *
 * The `/events` poll carries authoritative `running`/`status`/`done_reason`/
 * `recoverable` every tick (backend.py's `SessionEventsResponse`), but the
 * hook used to keep only `{events, running}` and stop polling outright once
 * a run finished. That left the header stuck on a stale snapshot until
 * `resume()` happened to be called again — which two call sites (plan
 * approval, question answer) never did, and an off-tab start (CLI/Aura/
 * trigger) never can. Pinned here: the poll payload's status fields land in
 * state, and the interval keeps ticking (at a slower keepalive cadence)
 * instead of going to `false`.
 */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionEvents } from "@/hooks/useSessionEvents";

vi.mock("@/api/client", () => ({ fetchEvents: vi.fn() }));
import * as apiClient from "@/api/client";
const fetchEvents = vi.mocked(apiClient.fetchEvents);

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return wrapper;
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("useSessionEvents — status threading", () => {
  it("carries status/done_reason/terminated/recoverable from the poll payload into state", async () => {
    fetchEvents.mockResolvedValue({
      events: [],
      running: true,
      status: "running",
      done_reason: "",
      terminated: false,
      recoverable: false,
    });
    const { result } = renderHook(() => useSessionEvents("s1"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.status).toBe("running"));
    expect(result.current.doneReason).toBe("");
    expect(result.current.terminated).toBe(false);
    expect(result.current.recoverable).toBe(false);
  });
});

describe("useSessionEvents — keepalive polling", () => {
  it("keeps refetching once the session stops running instead of going idle", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchEvents.mockResolvedValue({
      events: [],
      running: false,
      status: "completed",
      done_reason: "completed",
      terminated: false,
      recoverable: false,
    });
    renderHook(() => useSessionEvents("s1"), { wrapper: makeWrapper() });

    await waitFor(() => expect(fetchEvents).toHaveBeenCalledTimes(1));

    // Advance well past the keepalive interval. A `refetchInterval` of
    // `false` would leave the call count pinned at 1 forever.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });

    await waitFor(() => expect(fetchEvents.mock.calls.length).toBeGreaterThan(1));
  });
});

describe("useSessionEvents — terminated short-circuits polling", () => {
  // Termination is an absorbing state (no un-terminate primitive) — once the
  // poll itself reports `terminated: true`, every open tab polling that
  // session forever at the keepalive cadence is pure waste, so the interval
  // must go to `false`. The other two branches pin the guard doesn't
  // over-fire: an ABSENT `terminated` (poll hasn't resolved a real value
  // yet) must still fall through to keepalive, and a live run keeps the fast
  // interval regardless of `terminated: false`.
  it("stops polling once the poll reports terminated: true", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchEvents.mockResolvedValue({
      events: [],
      running: false,
      status: "terminated",
      done_reason: "",
      terminated: true,
      recoverable: false,
    });
    renderHook(() => useSessionEvents("s1"), { wrapper: makeWrapper() });

    await waitFor(() => expect(fetchEvents).toHaveBeenCalledTimes(1));

    // Advance well past the keepalive interval — a live `refetchInterval`
    // guard bug (e.g. checking truthiness instead of `=== true`, or missing
    // the branch entirely) would show a second call here.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60000);
    });

    expect(fetchEvents).toHaveBeenCalledTimes(1);
  });

  it("falls through to the keepalive tick when terminated is undefined (not yet an explicit value)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchEvents.mockResolvedValue({
      events: [],
      running: false,
      status: "idle",
      done_reason: "",
      // `terminated` omitted on purpose — must never be treated as `true`.
      recoverable: true,
    });
    renderHook(() => useSessionEvents("s1"), { wrapper: makeWrapper() });

    await waitFor(() => expect(fetchEvents).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });

    await waitFor(() => expect(fetchEvents.mock.calls.length).toBeGreaterThan(1));
  });

  it("uses the fast interval while running, even with terminated: false", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchEvents.mockResolvedValue({
      events: [],
      running: true,
      status: "running",
      done_reason: "",
      terminated: false,
      recoverable: false,
    });
    renderHook(() => useSessionEvents("s1"), { wrapper: makeWrapper() });

    await waitFor(() => expect(fetchEvents).toHaveBeenCalledTimes(1));

    // Just past the FAST interval (1000ms), well short of the 15s keepalive
    // — a second call here can only mean the fast interval fired.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });

    await waitFor(() => expect(fetchEvents.mock.calls.length).toBeGreaterThan(1));
  });
});
