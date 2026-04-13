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

export type VirtualProject = {
  project_id: string;
  name: string;
  description: string;
  path: string;
  path_source: string;
  folder_created: boolean;
  created_at: string;
  updated_at: string;
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
export type TurnTokenUsage = {
  // Peak input_tokens seen on any root LLM call in this turn — the real
  // context-pressure signal. (Summing across calls double-counts the
  // baseline prompt; the backend's build_usage_numbers doc explains why.)
  inputTokens: number;
  // Summed output tokens across the turn's root calls — output is
  // additive, summing is correct.
  outputTokens: number;
  // Sub-agents (depth>0). Each sub-agent runs in its own isolated context,
  // so we sum per-sub-agent peaks to show combined parallel pressure —
  // NOT the sum of every sub-agent call.
  subInputTokens: number;
  subOutputTokens: number;
  subAgentCount: number;
  // Per-turn cache + reasoning rollup (root + sub combined). Used by the
  // turn footer to surface cache savings ("Xk served from cache, billed
  // at 0.1×") and reasoning overhead from extended-thinking models.
  cacheCreationTokens: number;
  cacheReadTokens: number;
  reasoningTokens: number;
  // Cumulative billable input across the turn (root sum + sub sum). This
  // is the cost-side companion to ``inputTokens`` (the peak / context
  // pressure number).
  billedInputTokens: number;
};

// Raw session-level usage numbers returned by GET /api/sessions/:id/usage.
// Field names mirror the backend dict (build_usage_numbers).
//
// Two semantics for input tokens:
//   - ``_peak_`` / ``_last_``: context-pressure signal (max across calls).
//   - ``_billed``: cumulative billable cost (sum across calls).
// Output tokens are always cumulative (additive).
export type SessionUsage = {
  root_model: string;
  root_max_input_tokens: number;
  root_last_input_tokens: number;
  root_utilization: number;
  tokens_until_compact: number;
  compact_threshold: number;
  // Context-pressure (peak).
  root_peak_input_tokens: number;
  sub_peak_input_tokens: number;
  // Billable (sum). Note: input_tokens_billed INCLUDES cached portions —
  // pair with cache_read_tokens to apply the discount client-side
  // (Anthropic cache reads bill at 0.1× input, OpenAI at 0.5×).
  root_input_tokens_billed: number;
  sub_input_tokens_billed: number;
  total_input_tokens_billed: number;
  // Output (sum — always additive).
  root_output_tokens: number;
  sub_output_tokens: number;
  total_output_tokens: number;
  // Cache + reasoning subtotals — zero on transcripts captured before the
  // cache-capture commit on llm_call_end. Cache reads served from prompt
  // cache; cache creation tokens written to cache; reasoning tokens are
  // the hidden output of extended-thinking / o1-class models.
  root_cache_creation_tokens: number;
  root_cache_read_tokens: number;
  root_reasoning_tokens: number;
  sub_cache_creation_tokens: number;
  sub_cache_read_tokens: number;
  sub_reasoning_tokens: number;
  total_cache_creation_tokens: number;
  total_cache_read_tokens: number;
  total_reasoning_tokens: number;
  root_llm_calls: number;
  sub_llm_calls: number;
  sub_agent_count: number;
  compaction_count: number;
  compaction_tokens_saved: number;
};
export type TurnMeta = {
  id: string;
  events: EventRecord[];
  duration?: string;
  files: DiffFile[];
  model?: string;
  tokenUsage?: TurnTokenUsage;
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
  /** Timestamp of the underlying event. For user entries this is the user's
   * `ts`; for assistant entries it mirrors `turn.events[0].ts`. Used by
   * edit-and-regenerate / retry / fork to truncate history from this point. */
  ts?: string;
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
  type: "shell" | "diff" | "file_read" | "system" | "plan" | "permission" | "agent" | "agent_result" | "completion" | "agent_message" | "user_steer" | "compact";
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
  inputTokens?: number;
  outputTokens?: number;
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
  // File read fields (parsed from kind="file" results)
  fileReadPath?: string;
  fileReadText?: string;
  fileReadTotalLines?: number;
  // Compact fields
  compactSummary?: string;
  tokensBefore?: number;
  tokensSaved?: number;
  tokensAfter?: number;
  eventsSummarized?: number;
  compactMode?: string;
};

export type PlanStep = {
  title: string;
  description?: string;
  diffType?: "added" | "updated" | "removed";
};
