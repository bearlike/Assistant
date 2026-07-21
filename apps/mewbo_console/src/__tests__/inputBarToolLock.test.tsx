/**
 * InputBar — the purpose-bound tool lock.
 *
 * The server drops any `mcp_tools` override a purpose-bound session sends
 * (`SessionSpec.merge_request_overrides`). These tests pin the composer's
 * matching honesty: a purpose-bound session with a non-editable `allowed_tools`
 * shows the toolset LOCKED and OMITS `mcp_tools` from the per-send payload
 * (never an empty array); an explicit unlock resumes sending it; and — the
 * load-bearing non-regression guard — an open session and the home composer are
 * byte-unchanged (editable picker, `mcp_tools` sent).
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InputBar } from "@/components/InputBar";
import type { SessionSpecBinding, SessionSpecEditable } from "@/types";

// ── mock every server hook InputBar owns; children are prop-driven ───────────
vi.mock("@/hooks/useSessionSpec", () => ({ useSessionSpec: vi.fn() }));
vi.mock("@/hooks/useMcpTools", () => ({ useMcpTools: vi.fn() }));
vi.mock("@/hooks/useSkills", () => ({ useSkills: vi.fn() }));
vi.mock("@/hooks/useProjects", () => ({ useProjects: vi.fn() }));
vi.mock("@/hooks/useModels", () => ({ useModels: vi.fn() }));
vi.mock("@/hooks/useCommands", () => ({ useCommands: vi.fn() }));
vi.mock("@/hooks/useProjectFiles", () => ({ useProjectFiles: vi.fn() }));
vi.mock("@/hooks/useProjectGit", () => ({ useProjectGit: vi.fn() }));
vi.mock("@/hooks/useContainerCompact", () => ({ useContainerCompact: vi.fn() }));

import { useSessionSpec } from "@/hooks/useSessionSpec";
import { useMcpTools } from "@/hooks/useMcpTools";
import { useSkills } from "@/hooks/useSkills";
import { useProjects } from "@/hooks/useProjects";
import { useModels } from "@/hooks/useModels";
import { useCommands } from "@/hooks/useCommands";
import { useProjectFiles } from "@/hooks/useProjectFiles";
import { useProjectGit } from "@/hooks/useProjectGit";
import { useContainerCompact } from "@/hooks/useContainerCompact";

const TOOL_ID = "mcp__test__tool";

// One enabled, non-capability-gated MCP tool → the rebuild effect activates it
// by default, so an unlocked/open send carries `mcp_tools: [TOOL_ID]`.
const ONE_TOOL = {
  tool_id: TOOL_ID,
  name: "Test Tool",
  kind: "mcp",
  enabled: true,
};

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

const BOUND_CEILING: SessionSpecBinding = {
  ...OPEN_CHAT,
  origin: "wiki",
  purpose_bound: true,
  allowed_tools: ["mcp__wiki__ask", "mcp__scg__map"],
  strict_tool_scope: true,
};

// Purpose-bound sessions report `allowed_tools` non-editable; the lock keys on
// THIS map, not on `purpose_bound` alone.
const LOCKED_EDITABLE: SessionSpecEditable = { allowed_tools: false, model: true };

function primeCommonHooks() {
  vi.mocked(useMcpTools).mockReturnValue({
    tools: [ONE_TOOL],
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useMcpTools>);
  vi.mocked(useSkills).mockReturnValue({
    skills: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useSkills>);
  vi.mocked(useProjects).mockReturnValue({
    projects: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useProjects>);
  vi.mocked(useModels).mockReturnValue({
    models: [],
    defaultModel: "",
    capabilities: {},
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useModels>);
  vi.mocked(useCommands).mockReturnValue({ commands: [], loading: false });
  vi.mocked(useProjectFiles).mockReturnValue({
    files: [],
    attachments: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
  } as unknown as ReturnType<typeof useProjectFiles>);
  vi.mocked(useProjectGit).mockReturnValue({
    projectKey: null,
    gitRepo: false,
    branches: [],
    currentBranch: null,
    branchesInUse: [],
    worktrees: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
    createWorktreeFor: vi.fn(),
    deleteWorktreeFor: vi.fn(),
    mutating: false,
  } as unknown as ReturnType<typeof useProjectGit>);
  vi.mocked(useContainerCompact).mockReturnValue(false);
}

function setSpec(
  spec: SessionSpecBinding | null,
  editable: SessionSpecEditable = {},
) {
  vi.mocked(useSessionSpec).mockReturnValue({
    spec,
    editable,
    source: spec ? "spec" : null,
    isLoading: false,
    error: null,
  });
}

type RenderOpts = {
  mode: "home" | "detail";
  sessionId?: string;
};

function renderBar(opts: RenderOpts) {
  const onSubmit = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <InputBar mode={opts.mode} sessionId={opts.sessionId} onSubmit={onSubmit} />
    </QueryClientProvider>,
  );
  return onSubmit;
}

/** Type a line and press Enter; returns the `context` the composer assembled. */
async function submit(
  user: ReturnType<typeof userEvent.setup>,
  ariaLabel: string,
  onSubmit: ReturnType<typeof vi.fn>,
) {
  const textarea = screen.getByLabelText(ariaLabel);
  await user.click(textarea);
  await user.keyboard("hello");
  await user.keyboard("{Enter}");
  expect(onSubmit).toHaveBeenCalledTimes(1);
  return onSubmit.mock.calls[0][1];
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  primeCommonHooks();
});

describe("InputBar purpose-bound tool lock", () => {
  it("locks the toolset and OMITS mcp_tools for a purpose-bound session", async () => {
    const user = userEvent.setup();
    setSpec(BOUND_CEILING, LOCKED_EDITABLE);
    const onSubmit = renderBar({ mode: "detail", sessionId: "s1" });

    // The locked pill stands in for the editable picker and shows the ceiling.
    expect(screen.getByLabelText("Tools bound to session")).toBeInTheDocument();
    expect(screen.getByText("2 tools")).toBeInTheDocument();

    const ctx = await submit(user, "Session query", onSubmit);
    // The key is OMITTED entirely — not an empty array (which is itself an
    // override meaning "grant no tools").
    expect("mcp_tools" in ctx).toBe(false);
  });

  it("resumes sending mcp_tools after an explicit unlock", async () => {
    const user = userEvent.setup();
    setSpec(BOUND_CEILING, LOCKED_EDITABLE);
    const onSubmit = renderBar({ mode: "detail", sessionId: "s1" });

    await user.click(screen.getByLabelText("Tools bound to session"));
    // The read-only ceiling detail surfaces the bound ids, then the one unlock.
    expect(screen.getByText("mcp__wiki__ask")).toBeInTheDocument();
    expect(screen.getByText("mcp__scg__map")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /unlock tool selection/i }));

    // Pill is gone; the override is sanctioned and now travels again.
    expect(screen.queryByLabelText("Tools bound to session")).not.toBeInTheDocument();
    const ctx = await submit(user, "Session query", onSubmit);
    expect(ctx.mcp_tools).toEqual([TOOL_ID]);
  });

  it("is byte-unchanged for an OPEN session — editable picker, mcp_tools sent", async () => {
    const user = userEvent.setup();
    setSpec(OPEN_CHAT, { allowed_tools: true });
    const onSubmit = renderBar({ mode: "detail", sessionId: "s2" });

    // No lock surface at all.
    expect(screen.queryByLabelText("Tools bound to session")).not.toBeInTheDocument();

    const ctx = await submit(user, "Session query", onSubmit);
    // Exactly today's minimal payload — nothing added, nothing dropped.
    expect(ctx).toEqual({ mcp_tools: [TOOL_ID] });
  });

  it("does NOT lock when the server reports allowed_tools editable", async () => {
    const user = userEvent.setup();
    // purpose_bound is true, but the server allows the override — so the
    // composer must key on `editable`, not on `purpose_bound` alone.
    setSpec(BOUND_CEILING, { allowed_tools: true });
    const onSubmit = renderBar({ mode: "detail", sessionId: "s3" });

    expect(screen.queryByLabelText("Tools bound to session")).not.toBeInTheDocument();
    const ctx = await submit(user, "Session query", onSubmit);
    expect(ctx.mcp_tools).toEqual([TOOL_ID]);
  });

  it("leaves the HOME composer unchanged — no lock, mcp_tools sent", async () => {
    const user = userEvent.setup();
    // No session yet: useSessionSpec is disabled and returns a null binding.
    setSpec(null);
    const onSubmit = renderBar({ mode: "home" });

    expect(screen.queryByLabelText("Tools bound to session")).not.toBeInTheDocument();
    const ctx = await submit(user, "Task description", onSubmit);
    expect(ctx).toEqual({ mcp_tools: [TOOL_ID] });
  });
});
