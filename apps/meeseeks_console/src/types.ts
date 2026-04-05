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
  model?: string;
  mode?: QueryMode;
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
export type PlanStatus = "pending" | "approved" | "rejected";
export type PlanMeta = {
  revision: number;
  status: PlanStatus;
  planPath?: string;
  planContent: string;
  planSummary?: string;
  timestamp?: string;
};
export type TimelineEntry = {
  id: string;
  role: "user" | "assistant" | "plan";
  content: string;
  turnId: string;
  turn?: TurnMeta;
  plan?: PlanMeta;
};
export type ParsedDiffFile = {
  name: string;
  path: string;
  additions: number;
  deletions: number;
  isNewFile: boolean;
  isDeleted: boolean;
  hunks: ParsedHunk[];
};

export type ParsedHunk = {
  header: string;
  lines: ParsedLine[];
};

export type ParsedLine = {
  type: "context" | "insert" | "delete";
  oldNumber?: number;
  newNumber?: number;
  content: string;
};

export type LogEntry = {
  id: string;
  type: "shell" | "diff" | "system" | "plan" | "permission" | "agent" | "agent_result" | "completion" | "agent_message";
  content: string;
  title?: string;
  timestamp?: string;
  steps?: PlanStep[];
  version?: number;
  label?: string;
  planMode?: "full" | "diff";
  // Permission fields
  decision?: string;
  toolId?: string;
  operation?: string;
  toolInput?: string;
  // Agent lifecycle fields
  agentId?: string;
  parentId?: string;
  model?: string;
  depth?: number;
  agentAction?: string;
  agentStatus?: string;
  stepsCompleted?: number;
  detail?: string;
  // Agent result fields
  agentResultStatus?: string;
  stepsUsed?: number;
  summary?: string;
  artifacts?: string[];
  warnings?: string[];
  // Completion fields
  doneReason?: string;
  error?: string;
  // Diff fields (parsed from kind="diff" results)
  diffTitle?: string;
  diffText?: string;
  diffSuccess?: boolean;
  // Shell separated fields
  shellInput?: string;
  shellOutput?: string;
  // Structured shell fields (parsed from JSON result)
  shellCommand?: string;
  shellCwd?: string;
  shellExitCode?: number;
  shellStdout?: string;
  shellStderr?: string;
  shellDurationMs?: number;
};

export type PlanStep = {
  title: string;
  description?: string;
  diffType?: "added" | "updated" | "removed";
};
