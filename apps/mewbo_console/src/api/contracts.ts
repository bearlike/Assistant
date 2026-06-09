import {
  AttachmentPayload,
  AttachmentRecord,
  CommandResult,
  CommandSpec,
  CreateWorktreeInput,
  EventRecord,
  NotificationItem,
  ProjectBranches,
  QueryMode,
  SessionContext,
  SessionExport,
  SessionSummary,
  SessionUsage,
  ShareRecord,
  VirtualProject,
  WorktreeSummary
} from "../types";

export type {
  CreateWorktreeInput,
  ProjectBranches,
  VirtualProject,
  WorktreeSummary,
};

export type ProjectSource = "config" | "managed";

export type ToolSummary = {
  tool_id: string;
  name: string;
  kind: string;
  enabled: boolean;
  description?: string;
  disabled_reason?: string;
  server?: string;
  scope?: string;
};

export type SkillSummary = {
  name: string;
  description: string;
  allowed_tools: string[] | null;
  user_invocable: boolean;
  disable_model_invocation: boolean;
  context: string | null;
  source: string;
};

export type ProjectSummary = {
  name: string;
  path: string;
  description?: string;
  source?: ProjectSource;
  project_id?: string;  // only for managed projects
  // Worktree fields appear only on managed projects; backend leaves them
  // undefined for config-defined entries.
  is_worktree?: boolean;
  parent_project_id?: string | null;
  branch?: string | null;
};

export type ModelCapabilities = {
  supports_vision: boolean;
};

export type ModelInfo = {
  models: string[];
  default: string;
  /** Per-model capability map keyed by model name. Optional for back-compat. */
  capabilities?: Record<string, ModelCapabilities>;
};

export type AgentSummary = {
  agent_id: string;
  parent_id: string | null;
  depth: number;
  model: string;
  action: "start" | "stop";
  status: string;
  steps_completed: number;
  input_tokens?: number;
  output_tokens?: number;
  detail: string;
  ts: string;
};

/**
 * 202 response from ``POST /api/sessions/<id>/recover``. The server chooses
 * one of two shapes; the client detects by field presence:
 *   - generic: carries ``run_id`` — monitor via the session's event stream.
 *   - wiki-indexing dispatch: carries ``job_id`` (+ ``slug`` + ``status``), no
 *     ``run_id`` — navigate to the wiki indexing screen for ``job_id``. The
 *     ``slug`` is required so the indexing screen shows the real repo name and
 *     can navigate to its wiki on completion (without it, it falls back to a
 *     hardcoded placeholder).
 */
export type RecoverResponse = {
  session_id: string;
  action: "retry" | "continue";
  accepted: true;
  run_id?: string;
  job_id?: string;
  slug?: string;
  status?: string;
};

export type ApiClient = {
  listSessions: (includeArchived?: boolean) => Promise<SessionSummary[]>;
  createSession: (context?: SessionContext) => Promise<string>;
  postQuery: (
    sessionId: string,
    query: string,
    context?: SessionContext,
    mode?: QueryMode,
    attachments?: AttachmentPayload[]
  ) => Promise<void>;
  fetchEvents: (
    sessionId: string,
    after?: string
  ) => Promise<{ events: EventRecord[]; running: boolean }>;
  fetchUsage: (sessionId: string) => Promise<SessionUsage>;
  archiveSession: (sessionId: string) => Promise<void>;
  unarchiveSession: (sessionId: string) => Promise<void>;
  updateSessionTitle: (
    sessionId: string,
    title: string
  ) => Promise<{ session_id: string; title: string }>;
  regenerateTitle: (sessionId: string) => Promise<{ session_id: string; title: string }>;
  uploadAttachments: (
    sessionId: string,
    files: File[],
    model?: string | null
  ) => Promise<AttachmentRecord[]>;
  createShare: (sessionId: string) => Promise<ShareRecord>;
  exportSession: (sessionId: string) => Promise<SessionExport>;
  resolveShare: (token: string) => Promise<SessionExport>;
  sendMessage: (sessionId: string, text: string) => Promise<void>;
  interruptStep: (sessionId: string) => Promise<void>;
  approvePlan: (sessionId: string, approved: boolean) => Promise<void>;
  recoverSession: (
    sessionId: string,
    action: "retry" | "continue",
    fromTs?: string,
    editedText?: string,
    model?: string
  ) => Promise<RecoverResponse>;
  forkSession: (
    sessionId: string,
    opts?: { fromTs?: string; model?: string; compact?: boolean; tag?: string }
  ) => Promise<{ session_id: string; forked_from: string; forked_at: string | null }>;
  fetchPlanMarkdown: (sessionId: string) => Promise<string>;
  listTools: (project?: string) => Promise<ToolSummary[]>;
  listSkills: (project?: string) => Promise<SkillSummary[]>;
  streamEvents: (
    sessionId: string,
    onEvent: (event: EventRecord) => void,
    onEnd: () => void
  ) => () => void;
  listModels: () => Promise<ModelInfo>;
  listProjects: () => Promise<ProjectSummary[]>;
  listNotifications: () => Promise<NotificationItem[]>;
  dismissNotification: (ids: string[]) => Promise<void>;
  clearNotifications: (clearAll?: boolean) => Promise<void>;
  listAgents: (sessionId: string) => Promise<{
    agents: AgentSummary[];
    running: boolean;
    total_steps: number;
    total_input_tokens: number;
    total_output_tokens: number;
  }>;
  getConfigSchema: () => Promise<Record<string, unknown>>;
  getConfig: () => Promise<ConfigState>;
  patchConfig: (patch: Record<string, unknown>) => Promise<ConfigState>;
  listPlugins: () => Promise<PluginSummary[]>;
  listMarketplacePlugins: () => Promise<MarketplacePlugin[]>;
  installPlugin: (name: string, marketplace: string) => Promise<void>;
  uninstallPlugin: (name: string) => Promise<void>;
  createVirtualProject: (name: string, description: string, path?: string) => Promise<VirtualProject>;
  updateVirtualProject: (id: string, data: Partial<Pick<VirtualProject, "name" | "description">>) => Promise<VirtualProject>;
  deleteVirtualProject: (id: string) => Promise<void>;
  listProjectBranches: (projectId: string) => Promise<ProjectBranches>;
  listWorktrees: (projectId: string) => Promise<WorktreeSummary[]>;
  createWorktree: (
    projectId: string,
    input: CreateWorktreeInput,
  ) => Promise<WorktreeSummary>;
  deleteWorktree: (projectId: string, worktreeId: string, force?: boolean) => Promise<void>;
  fetchCommands: () => Promise<CommandSpec[]>;
  executeCommand: (
    sessionId: string,
    name: string,
    args: string[]
  ) => Promise<CommandResult>;
  listApiKeys: () => Promise<ApiKeySummary[]>;
  createApiKey: (label: string) => Promise<ApiKeyCreated>;
  revokeApiKey: (id: string) => Promise<ApiKeyRevoked>;
};

export type PluginSummary = {
  name: string;
  description: string;
  version: string;
  marketplace: string;
  scope: string;
  skills: number;
  agents: number;
  commands: number;
  mcp_servers: number;
  has_hooks: boolean;
};

export type MarketplacePlugin = {
  name: string;
  description: string;
  category: string;
  marketplace: string;
  installed: boolean;
};

/**
 * Shape of GET/PATCH /api/config — the (secret-stripped) config tree plus a
 * `secrets` map of dot-path → "has a stored value" flag.
 */
export type ConfigState = {
  config: Record<string, unknown>;
  secrets: Record<string, boolean>;
};

export type ApiConfig = {
  baseUrl?: string;
  apiKey?: string;
};

export type ApiMode = "auto" | "mock" | "live";

// ---------------------------------------------------------------------------
// API Keys
// ---------------------------------------------------------------------------

/** Metadata returned by GET /api/keys (no secrets). */
export type ApiKeySummary = {
  id: string;
  label: string;
  created_at: string;
  revoked_at: string | null;
};

/** Response from POST /api/keys — plaintext key shown exactly once. */
export type ApiKeyCreated = {
  id: string;
  label: string;
  key: string;
  created_at: string;
};

/** Response from DELETE /api/keys/<id>. */
export type ApiKeyRevoked = {
  id: string;
  revoked: boolean;
};
