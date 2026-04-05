import {
  AttachmentPayload,
  AttachmentRecord,
  EventRecord,
  NotificationItem,
  QueryMode,
  SessionContext,
  SessionExport,
  SessionSummary,
  ShareRecord
} from "../types";

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
    fromTs?: string
  ) => Promise<void>;
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
  }>;
  getConfigSchema: () => Promise<Record<string, unknown>>;
  getConfig: () => Promise<Record<string, unknown>>;
  patchConfig: (patch: Record<string, unknown>) => Promise<Record<string, unknown>>;
};

export type ApiConfig = {
  baseUrl?: string;
  apiKey?: string;
};

export type ApiMode = "auto" | "mock" | "live";
