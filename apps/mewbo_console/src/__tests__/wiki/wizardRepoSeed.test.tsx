/**
 * `useWizardMachine`'s registered-repository seed (`initialRepo` off the
 * configure route's `?repo=`). Drives the hook directly via `renderHook`,
 * mirroring `useQaConversation.test.tsx` — this pins the SEED contract:
 * url/platform prefill from the registry record, `?url=`/typed-url wins
 * outright, the platform stays editable (auto-detect resumes once the URL
 * diverges from the seed), a stale repeat fetch never re-seeds over an
 * edit, and an unresolved slug degrades silently to the cold wizard.
 */
import type { ReactNode } from "react";

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

import type { RepositoryDTO } from "@/api/repositories";
import { useWizardMachine } from "@/components/wiki/configure-wizard/wizardState";
import * as wikiClient from "@/components/wiki/api/client";
import type { Platform } from "@/components/wiki/api/types";

vi.mock("@/components/wiki/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/wiki/api/client")>();
  return {
    ...actual,
    listPlatforms: vi.fn(),
    listLanguages: vi.fn(),
    getWikiDefaults: vi.fn(),
    listBranches: vi.fn(),
    submitWizard: vi.fn(),
  };
});

// Keep /api/models and /api/config inert — neither is under test here, and
// both would otherwise fire real fetches (same rationale as the sibling
// useQaConversation test).
vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({
    models: [],
    defaultModel: "",
    capabilities: {},
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));
vi.mock("@/hooks/useConfig", () => ({
  useConfig: () => ({
    schema: null,
    config: { runtime: { developer_mode: false } },
    secrets: {},
    loading: false,
    saving: false,
    error: null,
    savePatch: vi.fn(),
    refresh: vi.fn(),
  }),
}));

const useRepositoryMock = vi.fn();
vi.mock("@/hooks/useRepositories", () => ({
  useRepository: (...args: unknown[]) => useRepositoryMock(...args),
}));

const listPlatforms = vi.mocked(wikiClient.listPlatforms);
const listLanguages = vi.mocked(wikiClient.listLanguages);
const getWikiDefaults = vi.mocked(wikiClient.getWikiDefaults);
const submitWizard = vi.mocked(wikiClient.submitWizard);
const listBranches = vi.mocked(wikiClient.listBranches);

const PLATFORMS: Platform[] = [
  {
    id: "github",
    name: "GitHub",
    mono: "",
    color: "#000",
    short: "GH",
    hosts: ["github.com"],
    tokenLabel: "Personal access token",
    tokenScope: "repo",
    tokenUrl: null,
    tokenSteps: [],
  },
  {
    id: "gitlab",
    name: "GitLab",
    mono: "",
    color: "#000",
    short: "GL",
    hosts: ["gitlab.com"],
    tokenLabel: "Access token",
    tokenScope: "api",
    tokenUrl: null,
    tokenSteps: [],
  },
  {
    id: "gitea",
    name: "Gitea",
    mono: "",
    color: "#000",
    short: "GT",
    hosts: ["gitea.com"],
    tokenLabel: "Token",
    tokenScope: "repo",
    tokenUrl: null,
    tokenSteps: [],
  },
];

function makeRepo(overrides: Partial<RepositoryDTO> = {}): RepositoryDTO {
  return {
    slug: "github.com/acme/beacon",
    host: "github.com",
    owner: "acme",
    repo: "beacon",
    repoUrl: "https://github.com/acme/beacon",
    platform: "github",
    defaultBranch: "main",
    name: null,
    description: null,
    origin: "manual",
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    usage: { wiki: null, tasks: null, credential: null },
    ...overrides,
  };
}

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const loc = memoryLocation({ path: "/wiki/configure", record: true });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <Router hook={loc.hook}>{children}</Router>
    </QueryClientProvider>
  );
  return wrapper;
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  listPlatforms.mockResolvedValue(PLATFORMS);
  listLanguages.mockResolvedValue([{ id: "en", label: "English" }]);
  getWikiDefaults.mockResolvedValue({});
  submitWizard.mockResolvedValue({ jobId: "job-1" } as never);
  listBranches.mockResolvedValue({ branches: [], defaultBranch: null });
  useRepositoryMock.mockReturnValue({ repository: undefined, loading: false, error: null });
});

describe("useWizardMachine — registered-repository seed", () => {
  it("cold wizard: no initialRepo, no initialUrl", async () => {
    const wrapper = makeWrapper();
    const { result } = renderHook(() => useWizardMachine({}), { wrapper });

    await waitFor(() => expect(listPlatforms).toHaveBeenCalled());
    expect(result.current.state.url).toBe("");
    expect(result.current.state.platform).toBe("github");
    expect(result.current.seedDefaultBranch).toBeNull();
    expect(useRepositoryMock).toHaveBeenCalledWith(undefined);
  });

  it("seeds url + platform once the registered repo resolves, and surfaces its default branch", async () => {
    const repo = makeRepo({
      platform: "gitea",
      repoUrl: "https://gitea.example.com/acme/beacon",
      defaultBranch: "develop",
    });
    useRepositoryMock.mockReturnValue({ repository: repo, loading: false, error: null });
    const wrapper = makeWrapper();

    const { result } = renderHook(
      () => useWizardMachine({ initialRepo: "gitea.example.com/acme/beacon" }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.state.url).toBe(repo.repoUrl));
    expect(result.current.state.platform).toBe("gitea");
    expect(result.current.seedDefaultBranch).toBe("develop");
    // The seed never pins `ref` — an untouched branch picker still submits
    // no ref, so the backend always clones the git host's CURRENT default.
    expect(result.current.state.ref).toBe("");
    expect(useRepositoryMock).toHaveBeenCalledWith("gitea.example.com/acme/beacon");
  });

  it("omits an untouched embedding model and submits a selected override", async () => {
    const wrapper = makeWrapper();
    const { result } = renderHook(
      () => useWizardMachine({ initialUrl: "https://github.com/acme/beacon" }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.state.model).toBe(""));
    act(() => result.current.set({ model: "anthropic/claude-sonnet-5" }));
    act(() => result.current.onSubmit());
    await waitFor(() => expect(submitWizard).toHaveBeenCalledTimes(1));
    expect(submitWizard.mock.calls[0][0]).not.toHaveProperty("embeddingModel");

    act(() => result.current.set({ embeddingModel: "text-embedding-3-large" }));
    act(() => result.current.onSubmit());
    await waitFor(() => expect(submitWizard).toHaveBeenCalledTimes(2));
    expect(submitWizard.mock.calls[1][0]).toMatchObject({
      embeddingModel: "text-embedding-3-large",
    });
  });

  it("an explicit ?url= wins outright over the repo seed", async () => {
    useRepositoryMock.mockReturnValue({ repository: makeRepo(), loading: false, error: null });
    const wrapper = makeWrapper();

    const { result } = renderHook(
      () =>
        useWizardMachine({
          initialUrl: "https://gitlab.com/typed/repo",
          initialRepo: "github.com/acme/beacon",
        }),
      { wrapper },
    );

    await waitFor(() => expect(listPlatforms).toHaveBeenCalled());
    expect(result.current.state.url).toBe("https://gitlab.com/typed/repo");
  });

  it("seeding is a seed, not a lock: editing the URL re-enables platform auto-detect", async () => {
    const repo = makeRepo({ platform: "gitea" });
    useRepositoryMock.mockReturnValue({ repository: repo, loading: false, error: null });
    const wrapper = makeWrapper();

    const { result } = renderHook(
      () => useWizardMachine({ initialRepo: "github.com/acme/beacon" }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.state.platform).toBe("gitea"));

    act(() => result.current.set({ url: "https://gitlab.com/someone/else" }));

    await waitFor(() => expect(result.current.state.platform).toBe("gitlab"));
  });

  it("a later refetch of the registered repo never re-seeds over a user edit", async () => {
    const repo = makeRepo();
    useRepositoryMock.mockReturnValue({ repository: repo, loading: false, error: null });
    const wrapper = makeWrapper();

    const { result, rerender } = renderHook(
      () => useWizardMachine({ initialRepo: "github.com/acme/beacon" }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.state.url).toBe(repo.repoUrl));

    act(() => result.current.set({ url: "https://github.com/edited/repo" }));

    // Simulate the shared ["repositories"] cache refetching with a fresh
    // object identity (e.g. an unrelated registry mutation invalidated it).
    useRepositoryMock.mockReturnValue({ repository: { ...repo }, loading: false, error: null });
    rerender();

    expect(result.current.state.url).toBe("https://github.com/edited/repo");
  });

  it("an unknown/unreachable repo slug degrades silently to the cold wizard", async () => {
    useRepositoryMock.mockReturnValue({
      repository: undefined,
      loading: false,
      error: new Error("not found"),
    });
    const wrapper = makeWrapper();

    const { result } = renderHook(
      () => useWizardMachine({ initialRepo: "gone/gone/gone" }),
      { wrapper },
    );

    await waitFor(() => expect(listPlatforms).toHaveBeenCalled());
    expect(result.current.state.url).toBe("");
    expect(result.current.errors).toEqual({});
  });
});
