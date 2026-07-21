/**
 * useSessionSpec — the binding fetch must surface the projection, the
 * fail-closed `editable` map, and the durable-vs-reconstructed `source`, and
 * must not fire without a session id (the `enabled` gate).
 */
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionSpec } from "@/hooks/useSessionSpec";
import type { SessionSpecResponse } from "@/types";

vi.mock("@/api/client", () => ({ getSessionSpec: vi.fn() }));
import * as apiClient from "@/api/client";
const getSessionSpec = vi.mocked(apiClient.getSessionSpec);

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

const RESPONSE: SessionSpecResponse = {
  session_id: "s1",
  spec: {
    origin: "wiki",
    surface: "console",
    purpose_bound: true,
    project: "Assistant",
    slug: "acme/beacon",
    cwd: "/srv/projects/assistant",
    model: "openai/claude-sonnet-5",
    fallback_models: ["openai/gpt-5.4", "anthropic/claude-opus"],
    allowed_tools: ["mcp__wiki__ask", "mcp__scg__map"],
    strict_tool_scope: true,
    capabilities: ["wiki"],
    skill_instructions_present: true,
    session_step_budget: 50,
    mode: "act",
  },
  editable: {
    model: true,
    fallback_models: true,
    mode: true,
    project: false,
    slug: false,
    cwd: false,
    allowed_tools: false,
    strict_tool_scope: false,
    skill_instructions: false,
    session_step_budget: false,
  },
  source: "spec",
};

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

describe("useSessionSpec", () => {
  it("returns the projection, the editable map and the source", async () => {
    getSessionSpec.mockResolvedValue(RESPONSE);
    const { result } = renderHook(() => useSessionSpec("s1"), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.spec).not.toBeNull());

    expect(getSessionSpec).toHaveBeenCalledWith("s1");
    expect(result.current.spec?.model).toBe("openai/claude-sonnet-5");
    // The fail-closed map: an always-overridable field is true, a bound one false.
    expect(result.current.editable.model).toBe(true);
    expect(result.current.editable.allowed_tools).toBe(false);
    expect(result.current.source).toBe("spec");
  });

  it("stays disabled and fetches nothing without a session id", () => {
    const { result } = renderHook(() => useSessionSpec(undefined), { wrapper: makeWrapper() });
    expect(getSessionSpec).not.toHaveBeenCalled();
    expect(result.current.spec).toBeNull();
    expect(result.current.editable).toEqual({});
    expect(result.current.source).toBeNull();
  });
});
