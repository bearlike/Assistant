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
  uploadAttachments: (
    sessionId: string,
    files: File[]
  ) => Promise<AttachmentRecord[]>;
  createShare: (sessionId: string) => Promise<ShareRecord>;
  exportSession: (sessionId: string) => Promise<SessionExport>;
  resolveShare: (token: string) => Promise<SessionExport>;
  sendMessage: (sessionId: string, text: string) => Promise<void>;
  interruptStep: (sessionId: string) => Promise<void>;
  listTools: () => Promise<ToolSummary[]>;
  listSkills: () => Promise<SkillSummary[]>;
  listProjects: () => Promise<ProjectSummary[]>;
  listNotifications: () => Promise<NotificationItem[]>;
  dismissNotification: (ids: string[]) => Promise<void>;
  clearNotifications: (clearAll?: boolean) => Promise<void>;
};

export type ApiConfig = {
  baseUrl?: string;
  apiKey?: string;
};

export type ApiMode = "auto" | "mock" | "live";
