/**
 * SessionBindingPanel — the read-only binding view.
 *
 * Pins the three behaviours that make the panel honest: every bound field
 * renders, sections carrying nothing hide, and the tool ceiling reads its
 * locked-vs-editable state from the server's fail-closed `editable` map (not a
 * guess) — a purpose-bound session shows a LOCKED toolset, an open one an
 * editable ceiling / open state.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionBindingPanel } from "@/components/SessionBindingPanel";
import type { SessionSpecBinding, SessionSpecEditable } from "@/types";

vi.mock("@/hooks/useSessionSpec", () => ({ useSessionSpec: vi.fn() }));
import { useSessionSpec } from "@/hooks/useSessionSpec";
const mockHook = vi.mocked(useSessionSpec);

function mount(
  spec: SessionSpecBinding | null,
  editable: SessionSpecEditable = {},
  source: "spec" | "legacy_context" | null = "spec",
) {
  mockHook.mockReturnValue({ spec, editable, source, isLoading: false, error: null });
  return render(<SessionBindingPanel sessionId="s1" />);
}

const BOUND_WIKI: SessionSpecBinding = {
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
};

// A plain console chat: purpose-open, no placement/model/limits/capabilities.
const OPEN_CHAT: SessionSpecBinding = {
  origin: "user",
  surface: "console",
  purpose_bound: false,
  project: null,
  slug: null,
  cwd: null,
  model: null,
  fallback_models: null,
  allowed_tools: null,
  strict_tool_scope: false,
  capabilities: null,
  skill_instructions_present: false,
  session_step_budget: null,
  mode: null,
};

const BOUND_EDITABLE: SessionSpecEditable = {
  model: true,
  fallback_models: true,
  mode: true,
  allowed_tools: false,
  strict_tool_scope: false,
  project: false,
  slug: false,
  cwd: false,
  skill_instructions: false,
  session_step_budget: false,
};

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

describe("SessionBindingPanel", () => {
  it("renders each bound field", () => {
    mount(BOUND_WIKI, BOUND_EDITABLE);

    // Purpose + placement + model + limits + capabilities all surface.
    expect(screen.getByText("Purpose-bound")).toBeInTheDocument();
    expect(screen.getByText("Wiki")).toBeInTheDocument(); // origin chip label
    expect(screen.getByText("Assistant")).toBeInTheDocument();
    expect(screen.getByText("acme/beacon")).toBeInTheDocument();
    expect(screen.getByText("/srv/projects/assistant")).toBeInTheDocument();
    expect(screen.getByText("openai/claude-sonnet-5")).toBeInTheDocument();
    // Fallback ladder entries, in order.
    expect(screen.getByText("openai/gpt-5.4")).toBeInTheDocument();
    expect(screen.getByText("anthropic/claude-opus")).toBeInTheDocument();
    // Capabilities + playbook presence flag (never the body).
    expect(screen.getByText("wiki")).toBeInTheDocument();
    expect(screen.getByText(/skill playbook is bound/i)).toBeInTheDocument();
    // Limits.
    expect(screen.getByText("50")).toBeInTheDocument();
    expect(screen.getByText("act")).toBeInTheDocument();
  });

  it("hides sections that carry nothing", () => {
    mount(OPEN_CHAT);

    // Empty sections collapse away entirely.
    expect(screen.queryByText("Placement")).not.toBeInTheDocument();
    expect(screen.queryByText("Model")).not.toBeInTheDocument();
    expect(screen.queryByText("Capabilities")).not.toBeInTheDocument();
    expect(screen.queryByText("Limits")).not.toBeInTheDocument();
    // Purpose (origin) and Tools always speak — an open chat is still a binding.
    expect(screen.getByText("Purpose")).toBeInTheDocument();
    expect(screen.getByText("Open chat")).toBeInTheDocument();
    expect(screen.getByText("Tools")).toBeInTheDocument();
    expect(screen.getByText("Open")).toBeInTheDocument();
  });

  it("shows a LOCKED toolset when the editable map refuses tool overrides", () => {
    mount(BOUND_WIKI, BOUND_EDITABLE);

    expect(screen.getByText("Locked")).toBeInTheDocument();
    expect(screen.getByText("mcp__wiki__ask")).toBeInTheDocument();
    expect(screen.getByText("mcp__scg__map")).toBeInTheDocument();
    expect(screen.getByText(/Strict scope/i)).toBeInTheDocument();
  });

  it("shows an EDITABLE ceiling (not locked) when the editable map allows it", () => {
    // Same tool list, but the server says allowed_tools is editable → not a lock.
    const unbound: SessionSpecBinding = {
      ...OPEN_CHAT,
      allowed_tools: ["mcp__wiki__ask"],
    };
    mount(unbound, { allowed_tools: true, model: true });

    expect(screen.queryByText("Locked")).not.toBeInTheDocument();
    expect(screen.getByText("Ceiling")).toBeInTheDocument();
    expect(screen.getByText("mcp__wiki__ask")).toBeInTheDocument();
  });

  it("renders a calm state, not error residue, when the binding is unavailable", () => {
    mockHook.mockReturnValue({
      spec: null,
      editable: {},
      source: null,
      isLoading: false,
      error: new Error("boom"),
    });
    render(<SessionBindingPanel sessionId="s1" />);
    expect(screen.getByText("Binding unavailable.")).toBeInTheDocument();
  });
});
