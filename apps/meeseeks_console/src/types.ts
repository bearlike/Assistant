export type AttachmentMeta = {
  name: string;
  size: number;
  type: string;
};

export type AttachmentRecord = {
  id: string;
  filename: string;
  stored_name: string;
  content_type: string;
  size_bytes: number;
  uploaded_at: string;
};

export type AttachmentPayload = AttachmentMeta | AttachmentRecord;

export type QueryMode = "plan" | "act";

export type NotificationItem = {
  id: string;
  title: string;
  message: string;
  level: string;
  created_at: string;
  dismissed?: boolean;
  session_id?: string | null;
  event_type?: string | null;
  metadata?: Record<string, unknown> | null;
};

export type SessionContext = {
  repo?: string;
  branch?: string;
  mcp_tools?: string[];
  skill?: string;
  project?: string;
  attachments?: AttachmentPayload[];
};

export type ShareRecord = {
  token: string;
  session_id: string;
  created_at?: string;
};

export type SessionExport = {
  session_id: string;
  events: EventRecord[];
  summary?: string | null;
  token?: string;
  created_at?: string;
};
export type SessionSummary = {
  session_id: string;
  title: string;
  created_at?: string | null;
  status?: string;
  done_reason?: string | null;
  running?: boolean;
  context?: SessionContext;
  archived?: boolean;
};
export type EventRecord = {
  ts: string;
  type: string;
  payload: Record<string, unknown>;
};
export type DiffFile = {
  name: string;
  path: string;
  additions: number;
  deletions: number;
  diff?: string;
};
export type TurnMeta = {
  id: string;
  events: EventRecord[];
  duration?: string;
  files: DiffFile[];
};
export type TimelineEntry = {
  id: string;
  role: "user" | "assistant";
  content: string;
  turnId: string;
  turn?: TurnMeta;
};
export type LogEntry = {
  id: string;
  type: "shell" | "system" | "plan";
  content: string;
  title?: string;
  timestamp?: string;
  steps?: PlanStep[];
  version?: number;
  label?: string;
  planMode?: "full" | "diff";
};

export type PlanStep = {
  title: string;
  description?: string;
  diffType?: "added" | "updated" | "removed";
};
