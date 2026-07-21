/**
 * useForkSession — the fork mutation's onSuccess must actually fire.
 *
 * A future refactor that drops `vars.onSuccess?.(res)` would still create the
 * forked session (the POST succeeds, 201) but never navigate to it — a
 * silent no-op to the user, the same failure class as the pre-fix
 * `handleForkFrom` that swallowed errors behind a comment claiming a
 * notification would surface (it never did; see
 * apps/mewbo_console/CLAUDE.md, "Retry / branch / fork"). Pinned at the hook
 * seam, mirroring `useRecoverSession`'s test harness
 * (`src/__tests__/wiki/recoveryFixes.test.tsx`).
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useForkSession } from "@/hooks/useForkSession";

vi.mock("@/api/client", () => ({ forkSession: vi.fn() }));
import * as apiClient from "@/api/client";
const forkSession = vi.mocked(apiClient.forkSession);

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { wrapper, invalidateSpy };
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

describe("useForkSession — onSuccess must navigate + invalidate", () => {
  it("invokes the per-call onSuccess with the fork response AND invalidates ['sessions']", async () => {
    const response = { session_id: "fork-1", forked_from: "s1", forked_at: null };
    forkSession.mockResolvedValue(response);
    const { wrapper, invalidateSpy } = makeWrapper();
    const { result } = renderHook(() => useForkSession(), { wrapper });
    const onSuccess = vi.fn();

    await act(async () => {
      await result.current.mutateAsync({ sessionId: "s1", onSuccess });
    });

    expect(onSuccess).toHaveBeenCalledWith(response);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["sessions"] });
  });
});
