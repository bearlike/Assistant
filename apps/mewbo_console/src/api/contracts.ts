import {
  AnswerDelivery,
  AttachmentPayload,
  AttachmentRecord,
  CommandResult,
  CommandSpec,
  CreateWorktreeInput,
  NotificationItem,
  ProjectBranches,
  QuestionAnswerItemPayload,
  QueryMode,
  SessionContext,
  SessionExport,
  SessionSpecResponse,
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

/**
 * Where an MCP tool's registration originates. "builtin" = core Mewbo tools,
 * "system" = MCP servers configured at the system/config level, "project" =
 * MCP servers scoped to the active project's `.mcp.json`, "plugin" = tools
 * contributed by an installed plugin. Optional/nullable-safe: older or
 * cached ``ToolSummary`` payloads may predate this field.
 */
export type ToolScope = "builtin" | "project" | "system" | "plugin";

export type ToolSummary = {
  tool_id: string;
  name: string;
  kind: string;
  enabled: boolean;
  description?: string;
  disabled_reason?: string;
  server?: string;
  scope?: ToolScope;
  // Capability id (e.g. "wiki", "scg") the session must advertise for this
  // tool to bind; `null`/absent for ungated tools. Optional/nullable-safe
  // for the same reason `scope` is: older or cached payloads predate it.
  requires_capability?: string | null;
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
  // Canonical git identity, filled by the backend only for checkouts that have
  // a remote. `repo.name` is the REPOSITORY name — distinct from `name` above,
  // which is the project's display name. Keys are absent, not null, when the
  // path has no remotes.
  repo?: { host: string; owner: string; name: string };
  aliases?: string[];
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

/**
 * 201 response from ``POST /api/sessions/<id>/fork`` — the new session's id
 * plus provenance (which session it branched from and, when ``from_ts`` was
 * given, the timestamp it branched at; ``null`` when the whole transcript
 * was copied).
 */
export type ForkResponse = {
  session_id: string;
  forked_from: string;
  forked_at: string | null;
};

/**
 * Outcome of `answerQuestion`. `ok` on a 200. On failure `kind` classifies the
 * HTTP status so the card settles silently when the question was resolved
 * elsewhere (`superseded` = 404 no-longer-pending / 409 answered-first,
 * `terminated` = 410) versus surfacing a correctable message (`invalid` = 422
 * answers don't fit, `forbidden` = 403 bad token, `error` = anything else).
 */
export type AnswerQuestionResult =
  | {
      ok: true;
      /** Where the answer landed, so the card can confirm honestly: `run` = the
       *  waiting agent received it, `message` = the run had already moved on so
       *  it was sent as a new message. Absent if the server omitted it. */
      delivery?: AnswerDelivery;
    }
  | {
      ok: false;
      kind: "superseded" | "invalid" | "forbidden" | "terminated" | "error";
      message: string;
    };

/**
 * Server-side narrowing for `GET /api/sessions`, mirroring
 * `mewbo_core.session.session_query.SessionQuery`. `project` matches a session
 * that has worked in ANY of the named projects (an auto-select session that
 * switched mid-task matches every one it touched) — repeated as multiple query
 * params, never comma-joined, since a project identity is an opaque string the
 * route must not re-split.
 */
export type SessionListFilter = {
  project?: string[];
  pinned?: boolean;
};

export type ApiClient = {
  listSessions: (
    includeArchived?: boolean,
    filter?: SessionListFilter
  ) => Promise<SessionSummary[]>;
  createSession: (context?: SessionContext) => Promise<string>;
  postQuery: (
    sessionId: string,
    query: string,
    context?: SessionContext,
    mode?: QueryMode,
    attachments?: AttachmentPayload[]
  ) => Promise<void>;
  fetchUsage: (sessionId: string) => Promise<SessionUsage>;
  getSessionSpec: (sessionId: string) => Promise<SessionSpecResponse>;
  /**
   * The one sanctioned path to change a purpose-bound session's project — the
   * per-turn override is refused server-side (`SessionSpec.field_editable`),
   * so this is a durable PUT, not a query-context key. `project` is a
   * required, non-empty string: the route only ever BINDS (an empty/absent
   * name would null the session's `cwd`, sending the next turn into an empty
   * temp dir — the defect this route exists to fix), so there is no unbind
   * verb. Returns the fresh spec projection (same shape as `getSessionSpec`)
   * so a caller can replace the cache directly instead of refetching.
   */
  rebindSessionProject: (sessionId: string, project: string) => Promise<SessionSpecResponse>;
  archiveSession: (sessionId: string) => Promise<void>;
  unarchiveSession: (sessionId: string) => Promise<void>;
  /**
   * Pin/unpin, mirroring archive's POST-then-DELETE-on-one-path shape exactly.
   * Pinning is an ORDERING signal only — the server sorts pinned rows first,
   * then newest-first, and every active filter still applies to a pinned
   * session. Returns the resulting `pinned`/`pinned_at` pair so a caller can
   * patch the cache without a refetch.
   */
  pinSession: (sessionId: string) => Promise<{ session_id: string; pinned: boolean; pinned_at: string | null }>;
  unpinSession: (sessionId: string) => Promise<{ session_id: string; pinned: boolean; pinned_at: string | null }>;
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
  /** `notes` is the optional group-level free-text box. Omit the key entirely
   *  when the user left it blank — an empty string is not a note. */
  answerQuestion: (
    sessionId: string,
    callId: string,
    body: { call_token: string; answers: QuestionAnswerItemPayload[]; notes?: string },
  ) => Promise<AnswerQuestionResult>;
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
  ) => Promise<ForkResponse>;
  fetchPlanMarkdown: (sessionId: string) => Promise<string>;
  listTools: (project?: string) => Promise<ToolSummary[]>;
  listSkills: (project?: string) => Promise<SkillSummary[]>;
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
  // Always set: the server resolves `manifest.display_name or manifest.name`,
  // so a manifest with no display_name still returns a non-null string here.
  display_name: string;
  description: string;
  version: string;
  marketplace: string;
  scope: string;
  enabled: boolean;
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
 * Server-reported writability of the config store, attached to `GET
 * /api/config`. `writable: true` in a healthy deployment, with `code`/
 * `reason` both null; `writable: false` (e.g. a read-only mounted config
 * directory) carries a stable machine `code` plus a `reason` string that is
 * already human-readable prose written for display — render it directly,
 * never re-map `code` to a hand-written string (that duplicates the server's
 * copy and drifts).
 */
export type ConfigStorageStatus = {
  writable: boolean;
  code: string | null;
  reason: string | null;
};

/**
 * Shape of GET/PATCH /api/config — the (secret-stripped) config tree plus a
 * `secrets` map of dot-path → "has a stored value" flag. `storage` is only
 * ever populated on the GET response (a PATCH echoes the same `config`/
 * `secrets` pair but carries no storage status of its own).
 */
export type ConfigState = {
  config: Record<string, unknown>;
  secrets: Record<string, boolean>;
  storage?: ConfigStorageStatus;
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
