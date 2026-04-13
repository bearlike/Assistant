import {
  AttachmentPayload,
  AttachmentRecord,
  EventRecord,
  NotificationItem,
  QueryMode,
  SessionContext,
  SessionExport,
  SessionSummary,
  SessionUsage,
  ShareRecord,
  VirtualProject
} from "../types";

export type { VirtualProject };

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
};

export type ModelInfo = {
  models: string[];
  default: string;
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
    files: File[]
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
  ) => Promise<void>;
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
  getConfig: () => Promise<Record<string, unknown>>;
  patchConfig: (patch: Record<string, unknown>) => Promise<Record<string, unknown>>;
  listPlugins: () => Promise<PluginSummary[]>;
  listMarketplacePlugins: () => Promise<MarketplacePlugin[]>;
  installPlugin: (name: string, marketplace: string) => Promise<void>;
  uninstallPlugin: (name: string) => Promise<void>;
  createVirtualProject: (name: string, description: string, path?: string) => Promise<VirtualProject>;
  updateVirtualProject: (id: string, data: Partial<Pick<VirtualProject, "name" | "description">>) => Promise<VirtualProject>;
  deleteVirtualProject: (id: string) => Promise<void>;
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

export type ApiConfig = {
  baseUrl?: string;
  apiKey?: string;
};

export type ApiMode = "auto" | "mock" | "live";
