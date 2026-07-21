import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { TriggerDTO } from "../api/triggers";

vi.mock("../api/triggers", () => ({
  listTriggers: vi.fn(),
  listSessionTriggers: vi.fn(),
  updateTriggerStatus: vi.fn(),
  cancelTrigger: vi.fn(),
  terminateSession: vi.fn(),
}));

import * as api from "../api/triggers";
import {
  useCancelTrigger,
  usePauseResumeTrigger,
  useTerminateSession,
} from "../hooks/useTriggers";

const updateTriggerStatus = vi.mocked(api.updateTriggerStatus);
const cancelTrigger = vi.mocked(api.cancelTrigger);
const terminateSession = vi.mocked(api.terminateSession);

function trig(overrides: Partial<TriggerDTO> = {}): TriggerDTO {
  return {
    id: "t1",
    session_id: "s1",
    kind: "time.cron",
    status: "armed",
    wake_prompt: "p",
    action: "message",
    args: {},
    fires: 0,
    created_at: "2026-07-13T10:00:00Z",
    created_by: "agent",
    ...overrides,
  };
}

function makeQc() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}
function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(cleanup);

describe("usePauseResumeTrigger", () => {
  it("optimistically flips the cached status and calls the API", async () => {
    const qc = makeQc();
    qc.setQueryData(["triggers", "session", "s1"], [trig({ status: "armed" })]);
    updateTriggerStatus.mockResolvedValue(trig({ status: "paused" }));

    const { result } = renderHook(() => usePauseResumeTrigger(), { wrapper: wrapperFor(qc) });
    await act(async () => {
      await result.current.mutateAsync({ id: "t1", status: "paused" });
    });

    expect(updateTriggerStatus).toHaveBeenCalledWith("t1", "paused");
    const data = qc.getQueryData<TriggerDTO[]>(["triggers", "session", "s1"]);
    expect(data?.[0].status).toBe("paused");
  });

  it("rolls the cache back when the mutation fails", async () => {
    const qc = makeQc();
    qc.setQueryData(["triggers", "session", "s1"], [trig({ status: "armed" })]);
    updateTriggerStatus.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => usePauseResumeTrigger(), { wrapper: wrapperFor(qc) });
    await act(async () => {
      await result.current.mutateAsync({ id: "t1", status: "paused" }).catch(() => undefined);
    });

    await waitFor(() => {
      const data = qc.getQueryData<TriggerDTO[]>(["triggers", "session", "s1"]);
      expect(data?.[0].status).toBe("armed");
    });
  });

  it("scopes rollback to the failed row, leaving a concurrent row's optimistic update intact", async () => {
    const qc = makeQc();
    const listKey = ["triggers", "list", {}] as const;
    qc.setQueryData(
      listKey,
      [trig({ id: "t1", status: "armed" }), trig({ id: "t2", status: "armed" })],
    );

    // t1's mutation will resolve; t2's will fail. Both are in flight at once.
    let resolveT1!: (v: TriggerDTO) => void;
    updateTriggerStatus.mockImplementation((id) => {
      if (id === "t1") return new Promise((resolve) => (resolveT1 = resolve));
      return Promise.reject(new Error("boom"));
    });

    const { result: hookA } = renderHook(() => usePauseResumeTrigger(), { wrapper: wrapperFor(qc) });
    const { result: hookB } = renderHook(() => usePauseResumeTrigger(), { wrapper: wrapperFor(qc) });

    let t1Promise!: Promise<unknown>;
    let t2Promise!: Promise<unknown>;
    await act(async () => {
      // t1 (A) starts first and stays pending — its onMutate snapshot is taken
      // BEFORE t2's optimistic patch lands.
      t1Promise = hookA.current.mutateAsync({ id: "t1", status: "paused" });
      await Promise.resolve();
      // t2 (B) starts, patches optimistically, then fails.
      t2Promise = hookB.current.mutateAsync({ id: "t2", status: "paused" }).catch(() => undefined);
      await t2Promise;
    });

    // t2's failure rollback must not revert t1's still-pending optimistic row.
    let data = qc.getQueryData<TriggerDTO[]>(listKey);
    expect(data?.find((t) => t.id === "t1")?.status).toBe("paused");
    expect(data?.find((t) => t.id === "t2")?.status).toBe("armed");

    await act(async () => {
      resolveT1(trig({ id: "t1", status: "paused" }));
      await t1Promise;
    });

    data = qc.getQueryData<TriggerDTO[]>(listKey);
    expect(data?.find((t) => t.id === "t1")?.status).toBe("paused");
    expect(data?.find((t) => t.id === "t2")?.status).toBe("armed");
  });
});

describe("useCancelTrigger", () => {
  it("optimistically flips the row to cancelled", async () => {
    const qc = makeQc();
    qc.setQueryData(["triggers", "list", {}], [trig({ status: "armed" })]);
    cancelTrigger.mockResolvedValue({ id: "t1", status: "cancelled" });

    const { result } = renderHook(() => useCancelTrigger(), { wrapper: wrapperFor(qc) });
    await act(async () => {
      await result.current.mutateAsync("t1");
    });

    expect(cancelTrigger).toHaveBeenCalledWith("t1");
    const data = qc.getQueryData<TriggerDTO[]>(["triggers", "list", {}]);
    expect(data?.[0].status).toBe("cancelled");
  });
});

describe("useTerminateSession", () => {
  it("calls terminateSession with the session id", async () => {
    const qc = makeQc();
    terminateSession.mockResolvedValue({
      session_id: "s1",
      status: "terminated",
      terminated_at: "2026-07-13T11:00:00Z",
      cancelled_triggers: 1,
    });
    const { result } = renderHook(() => useTerminateSession(), { wrapper: wrapperFor(qc) });
    await act(async () => {
      await result.current.mutateAsync("s1");
    });
    expect(terminateSession).toHaveBeenCalledWith("s1");
  });
});
