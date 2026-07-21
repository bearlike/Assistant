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
  // Populated on the FULL record `POST`/`PATCH /api/v_projects/<id>` return
  // (`backend.py::_vproject_to_dict`). `GET /api/projects` — what
  // `useVirtualProjects()` reads — never includes these for managed entries
  // (`backend.py::Projects.get` builds a narrower dict), so they're optional
  // rather than fabricated; a consumer must check for `undefined` before
  // rendering one, e.g. before formatting `created_at` as a date.
  path_source?: string;
  folder_created?: boolean;
  created_at?: string;
  updated_at?: string;
  // Worktree extension (null/undefined for regular managed projects).
  parent_project_id?: string | null;
  branch?: string | null;
  is_worktree?: boolean;
};

export type WorktreeSummary = {
  /** Worktree-as-VirtualProject id when the API owns it; ``null`` for
   * user-created worktrees the app hasn't adopted. */
  project_id: string | null;
  name: string;
  branch: string;
  path: string;
  /** ``true`` for worktrees registered with the project store, ``false``
   * for entries discovered via ``git worktree list``. The console can
   * select managed worktrees as session contexts; user-created ones are
   * informational until adopted. */
  managed: boolean;
  is_worktree: true;
  parent_project_id?: string | null;
  parent_path?: string;
  /** Result of the most recent cleanliness check from the API. */
  clean?: boolean;
  head?: string | null;
};

export type ProjectBranches = {
  branches: string[];
  current_branch?: string | null;
  /**
   * Branches that ``git worktree add`` will refuse — already checked out
   * by the parent repo or another worktree. The UI uses this to disable
   * "reuse existing branch" entries that would 409.
   */
  branches_in_use?: string[];
  git_repo: boolean;
  reason?: string;
};

/**
 * Payload for ``createWorktree``. When ``base`` is set the backend creates
 * a fresh branch from ``base`` (``git worktree add -b``); otherwise
 * ``branch`` must already exist.
 */
export type CreateWorktreeInput = {
  branch: string;
  base?: string | null;
};

export type SessionContext = {
  repo?: string;
  branch?: string;
  mcp_tools?: string[];
  skill?: string;
  project?: string;
  model?: string;
  mode?: QueryMode;
  /** Channel platform that opened the session (e.g. "nextcloud-talk", "email"). */
  source_platform?: string;
  attachments?: AttachmentPayload[];
  /**
   * Opt-in cross-model fallback chain. When present and non-empty the run
   * tries each model in order after the primary fails (same-model retries
   * exhausted / fatal switch). Omitted or empty = no fallback (safe default).
   * Mirrors the backend ``context.fallback_models`` contract.
   */
  fallback_models?: string[];
  /**
   * The Mewbo App this session builds/maintains. Stamped by
   * `AppLifecycle._agent_session_context` onto both the builder
   * (`owner_session_id`) and maintainer (`maintainer_session_id`) sessions'
   * context event — absent on every other session. Lets a jump-to-app
   * affordance resolve straight off the already-loaded session, no extra
   * fetch.
   */
  app_id?: string;
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
/** Coarse provenance of a session — mirrors core ``SessionOrigin``. */
export type SessionOrigin =
  | 'user'
  | 'wiki'
  | 'search'
  | 'channel'
  | 'apps'
  | 'structured'
  | 'draft'
  | 'mobile';

/**
 * The durable purpose-binding a session runs under, projected read-only for the
 * console (snake_case, mirrors `GET /api/sessions/<id>/spec`'s `spec` key). What
 * once needed Mongo forensics — which surface created a session, on what model
 * ladder, under which tool ceiling — reads at a glance here.
 *
 * Three-state `allowed_tools` mirrors the backend exactly: `null` = no ceiling
 * (open), `[]` = a real ceiling granting no MCP tool, a non-empty list = exactly
 * those. Never test it for truthiness — `[]` and `null` mean opposite things.
 * `skill_instructions_present` is a PRESENCE flag, never the playbook body (which
 * no client renders).
 */
export type SessionSpecBinding = {
  origin: SessionOrigin;
  surface: string | null;
  /** True = a purpose-built session (indexer, search) whose scope is locked;
   *  false = an open console chat. Drives the locked-ceiling read. */
  purpose_bound: boolean;
  project: string | null;
  slug: string | null;
  cwd: string | null;
  model: string | null;
  fallback_models: string[] | null;
  allowed_tools: string[] | null;
  strict_tool_scope: boolean;
  capabilities: string[] | null;
  skill_instructions_present: boolean;
  session_step_budget: number | null;
  mode: string | null;
};

/**
 * Fields the server's `editable` map may key. `skill_instructions` (the field
 * name) is keyed here even though the projection exposes only its presence flag
 * `skill_instructions_present`; `origin`/`surface`/`capabilities` are never
 * overridable and so never appear.
 */
export type SessionSpecEditableField =
  | 'project'
  | 'slug'
  | 'cwd'
  | 'model'
  | 'fallback_models'
  | 'allowed_tools'
  | 'strict_tool_scope'
  | 'skill_instructions'
  | 'session_step_budget'
  | 'mode';

/**
 * Server-declared, fail-closed per-field modifiability. A field is editable
 * ONLY when its value is `=== true`; absence never means editable-by-default
 * (the wiki project-settings pattern). The server refuses a non-editable field
 * server-side, so a client renders it read-only rather than offering an edit
 * that will be silently ignored.
 */
export type SessionSpecEditable = Partial<Record<SessionSpecEditableField, boolean>>;

/** Full envelope of `GET /api/sessions/<id>/spec`. */
export type SessionSpecResponse = {
  session_id: string;
  spec: SessionSpecBinding;
  editable: SessionSpecEditable;
  /** `spec` = a durable typed binding was recorded; `legacy_context` = the
   *  binding was reconstructed from the session's loose context keys. */
  source: 'spec' | 'legacy_context';
};

export type SessionSummary = {
  session_id: string;
  title: string;
  created_at?: string | null;
  /**
   * Derived at read time by the runtime, never stored: `idle` · `running` ·
   * `completed` · `incomplete` · `canceled` · `failed` · `awaiting_approval` ·
   * `terminated` · `unmet_goal` · `blocked`. Left a bare string because it is
   * derived server-side and a closed union here would turn a new backend
   * status into a build failure instead of a rendered badge — `StatusBadge`
   * is the one place that has to know the vocabulary.
   */
  status?: string;
  done_reason?: string | null;
  running?: boolean;
  context?: SessionContext;
  /** How the session was spawned; absent on legacy summaries → treat as "user". */
  origin?: SessionOrigin;
  /**
   * Capabilities the session was scoped to (advertised set on its context).
   * Surfaced so the landing page shows e.g. that a session reasoned over the
   * SCG. Absent/empty on legacy or plain sessions.
   */
  capabilities?: string[];
  /** Search/structured workspace id the session ran against, if any. */
  workspace?: string | null;
  archived?: boolean;
  /**
   * True iff the session is NOT running, did not successfully complete, and has
   * a prior user turn (incl. a session killed mid-call with no completion).
   * Drives the Continue / Restart recovery affordances. Absent on legacy
   * summaries → treat as not recoverable.
   */
  recoverable?: boolean;
};
export type EventRecord = {
  ts: string;
  type: string;
  payload: Record<string, unknown>;
};

export interface WidgetReadyPayload {
  widget_id: string;
  session_id: string;
  files: { "app.py": string; "data.json": string };
  requirements: string[];
  summary?: string;
}

export interface WidgetReadyEntry {
  type: "widget_ready";
  ts: string;
  payload: WidgetReadyPayload;
}

// ---------------------------------------------------------------------------
// Ask-user questions (native human-in-the-loop clarification)
// ---------------------------------------------------------------------------

/** One selectable option in a user question (mirrors core `QuestionOption`). */
export interface QuestionOptionPayload {
  label: string;
  description: string | null;
}

/**
 * One question in an ask-user-question group (mirrors core `UserQuestion`).
 * Empty `options` ⇒ a free-text question; otherwise 2-4 choices rendered as
 * radios (single-select) or checkboxes (`multi_select`). A free-text answer is
 * ALWAYS accepted regardless of options (the ever-present "Other").
 */
export interface UserQuestionItem {
  header: string;
  question: string;
  options: QuestionOptionPayload[];
  multi_select: boolean;
}

/**
 * Payload of a `user_question` transcript event: a pending question group the
 * run is blocked on. `call_token` is a single-use bearer secret the answer
 * POST must echo (same threat model as `device_tool_call`).
 */
export interface UserQuestionPayload {
  call_id: string;
  call_token: string;
  questions: UserQuestionItem[];
}

/** One answer item: selected option indexes XOR free text, never both. */
export interface QuestionAnswerItemPayload {
  selected_indexes?: number[] | null;
  text?: string | null;
}

/** How a question group resolved (mirrors core `QuestionOutcome`). */
export type QuestionOutcome = "answered" | "declined" | "interrupted" | "cancelled";

/**
 * Payload of a `user_question_answered` transcript event: the resolution of a
 * question group, emitted whatever the outcome so every surface settles its
 * card. `answers` is present only when `outcome === "answered"`.
 */
export interface UserQuestionAnsweredPayload {
  call_id: string;
  outcome: QuestionOutcome;
  answered_via: string | null;
  answers: QuestionAnswerItemPayload[] | null;
}
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
  /** Distinct model IDs actually used in the session, in first-seen order. Empty [] for legacy sessions. */
  models_used: string[];
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
/** Lifecycle of a single todo/plan-step (mirrors the backend's tri-state contract). */
export type TodoItemStatus = "pending" | "in_progress" | "completed";
export type TodoItem = {
  label: string;
  status: TodoItemStatus;
};
/**
 * The authoritative live todo/plan checklist carried by the `todos` event
 * (schema: `{ items:[{label,status}], source:"plan"|"agent",
 * agent_id }`). Rendered as a `TodoCard` in the conversation timeline.
 */
export type TodoMeta = {
  items: TodoItem[];
  source?: "plan" | "agent";
  agentId?: string;
};
/** Status of a question card: `pending` while awaiting an answer, then one of
 * the four settled {@link QuestionOutcome} states. */
export type QuestionStatus = "pending" | QuestionOutcome;
/**
 * A pending/settled ask-user-question card carried by the `user_question`
 * event and settled by `user_question_answered` (folded by `call_id` in
 * `buildTimeline`). Rendered as a `QuestionCard`, mirroring the `PlanCard`
 * pending→settled idiom.
 */
export type QuestionMeta = {
  callId: string;
  callToken: string;
  questions: UserQuestionItem[];
  status: QuestionStatus;
  /** Present once `status === "answered"` — one item per question, in order. */
  answers?: QuestionAnswerItemPayload[];
  /** The surface that answered (e.g. "console"), shown muted on the card. */
  answeredVia?: string;
};
/**
 * Compact transcript marker for a reverse-invocation trigger.
 * Carried on the `trigger_armed` / `trigger_fired` SSE events. `kind` is kept a
 * plain string (a display label) so this base type doesn't have to import the
 * strict `TriggerKind` union from the api layer (which would form a cycle).
 */
export type TriggerTranscriptMeta = {
  triggerId?: string;
  kind: string;
  action: "armed" | "fired";
  summary?: string;
  /** Number of adjacent same-identity trigger events folded into this row by
   *  {@link coalesceAdjacentTriggers}. Absent/1 means ungrouped — render as
   *  today. Only ever set by that display-layer helper, never by the parser. */
  count?: number;
  /** ISO timestamp of the first event folded into this row. Only present
   *  alongside `count > 1`; the entry's own `ts` is always the latest. */
  firstTs?: string;
};
/** Classified failure kinds carried by `completion.payload.error_detail`. */
export type RunErrorKind =
  | "upstream_bad_gateway"
  | "rate_limited"
  | "timeout"
  | "auth"
  | "context_overflow"
  | "provider_unavailable"
  | "tool_failure"
  | "unknown";
/**
 * The `completion.payload.error_detail` wire shape — a classified, bounded
 * view of a failed run. Snake-case because it is the payload verbatim.
 *
 * `detail` is the (possibly truncated) raw provider text; `detail_chars` is
 * the ORIGINAL length, so `truncated` renders an honest "showing N of M".
 * Absent on every event persisted before this field shipped — see
 * {@link RunFailureMeta}, which degrades to the capped `error`/`last_error`.
 */
export type RunErrorDetail = {
  kind: RunErrorKind;
  title: string;
  provider?: string | null;
  detail: string;
  detail_chars: number;
  truncated: boolean;
  /** The retry classifier's reason token. Deliberately a bare string, not a
   *  closed union: the backend vocabulary is open, and an unrecognised token
   *  must degrade to "not model-attributable" rather than fail a build. */
  failure_reason?: string | null;
  /** Every model the run actually attempted, in order. */
  models_tried?: string[];
};
/**
 * Which run outcomes render as a failure card. `error` and `max_steps_reached`
 * are `done_reason`s read off a completion event; `interrupted` is the one
 * outcome with NO completion behind it — a turn the next prompt superseded
 * before anything ever concluded it. `unmet_goal` covers a run that ended
 * without an exception but never achieved what it was asked to do
 * (`verification_failed` / `halted_no_progress` collapse into it); `blocked`
 * is a run that hit a wall it can't get past on its own — repository access, a
 * network path, a permission, or quota — which the runtime signals with a
 * `blocked_code` even though it leaves the completion's `done_reason` at
 * `completed`. Both were laundering into a green success card before this arm
 * existed.
 */
export type RunFailureReason =
  | "error"
  | "max_steps_reached"
  | "interrupted"
  | "unmet_goal"
  | "blocked";
/**
 * A failed run, normalized once at parse time so every surface renders the
 * same card without re-branching on whether the backend classified it.
 * Carried on `role: "run_failed"` timeline entries and on completion
 * {@link LogEntry}s.
 */
export type RunFailureMeta = {
  reason: RunFailureReason;
  /** Body text: the classified detail when present, else the legacy
   *  `error`/`last_error` string. Empty when the run failed with no message. */
  text: string;
  /** Present only when the backend shipped `error_detail`. */
  detail?: RunErrorDetail;
  /** The retry classifier's reason token (`timeout`, `rate_limit`, …). Drives
   *  whether recovery proposes a different model: a transport-shaped failure
   *  is worth escalating, a deterministic one (`bad_request`,
   *  `content_policy`, `permission_denied`) is a defect a switch would hide. */
  failureReason?: string;
  /** Models the run attempted, in order — shown so a recovery model choice is
   *  informed rather than blind. */
  modelsTried?: string[];
  /** The wall a `blocked` run hit — e.g. `repo_access`, `network`,
   *  `forbidden`, `quota_exceeded`. This is the user-actionable half: it names
   *  what to fix, and its mere presence is what distinguishes a blocked run
   *  from a genuine success, since the backend leaves `done_reason` at
   *  `completed`. Absent on every other failure. */
  blockedCode?: string;
};
/**
 * A session-level recovery the user triggered on a failed run, recorded
 * BETWEEN turns. Carried on `role: "recovery"` timeline entries.
 */
export type RecoveryMeta = {
  action: "retry" | "continue";
};
/**
 * A context-compaction boundary: the runtime replaced older transcript
 * events with a summary so the next model call stays within its context
 * budget (mewbo_core/context.py's `ContextBuilder` slices the transcript
 * forward past this point). The events themselves are untouched in the
 * persisted transcript rendered above this marker — only what the model
 * receives going forward narrows. Carried on `role: "compaction"` timeline
 * entries. Field names mirror `CompactLogEntry` (logs.ts), the trace panel's
 * fuller rendering of the same `context_compacted` event.
 */
export type CompactionMeta = {
  mode: string;
  tokensSaved?: number;
};
export type TimelineEntry = {
  id: string;
  role: "user" | "assistant" | "run_failed" | "plan" | "widget" | "todos" | "question" | "trigger" | "session_terminated" | "recovery" | "compaction";
  content: string;
  turnId: string;
  /** Timestamp of the underlying event. For user entries this is the user's
   * `ts`; for assistant entries it mirrors `turn.events[0].ts`. Used by
   * edit-and-regenerate / retry / fork to truncate history from this point. */
  ts?: string;
  turn?: TurnMeta;
  plan?: PlanMeta;
  widget?: WidgetReadyPayload;
  todos?: TodoMeta;
  /** Present on `role: "question"` entries (user_question / _answered). */
  question?: QuestionMeta;
  /** Present on `role: "trigger"` entries (trigger_armed / trigger_fired). */
  trigger?: TriggerTranscriptMeta;
  /** Present on `role: "recovery"` entries (a retry/continue between turns). */
  recovery?: RecoveryMeta;
  /** Present on `role: "compaction"` entries (a context_compacted boundary). */
  compaction?: CompactionMeta;
  /** Present on `role: "run_failed"` entries (completion with a failure reason). */
  runFailure?: RunFailureMeta;
  /** Metadata-only descriptors for files uploaded alongside this user turn
   * (filename/type/size — never pixels/content). Rendered as a glanceable
   * tile row above the user bubble by {@link AttachmentCards}. */
  attachments?: AttachmentPayload[];
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

export type AgentTreeNode = {
  id: string;
  parent_id: string | null;
  depth: number;
  task: string;
  status: string;
  steps_completed: number;
  last_tool_id: string | null;
  progress_note: string | null;
  compaction_count: number;
  result: { status: string; summary: string; content: string } | null;
};

/**
 * A rendered log row, discriminated on `type`. Each variant carries ONLY the
 * fields its `buildXxxLog` constructor (`utils/logs.ts`) sets and its
 * `renderXxx` (`LogsView.tsx`) reads — replacing the former flat 85-field bag
 * where every field was optional on every row. The discriminant lets both the
 * builders and `LogsView`'s `if (log.type === …)` dispatch narrow to the exact
 * shape, so a renderer can no longer read a field its row never carries.
 *
 * Cross-cutting fields (`agentId`/`model`/`depth`/`error`) are repeated on the
 * variants that genuinely use them rather than hoisted to the base — a `plan`
 * row has no `model`, and keeping the base minimal is what makes the narrowing
 * meaningful.
 */
interface LogEntryBase {
  id: string;
  content: string;
  title?: string;
  timestamp?: string;
}

/** Regular tool result → shell/terminal card (also the generic tool fallback). */
export interface ShellLogEntry extends LogEntryBase {
  type: "shell";
  shellInput?: string;
  shellOutput?: string;
  error?: string;
  agentId?: string;
  model?: string;
  // Structured shell fields (parsed from JSON result).
  shellCommand?: string;
  shellCwd?: string;
  shellExitCode?: number;
  shellStdout?: string;
  shellStderr?: string;
  shellDurationMs?: number;
}

/** Diff result (or a synthesized diff for a failed file edit) → DiffCard. */
export interface DiffLogEntry extends LogEntryBase {
  type: "diff";
  diffTitle?: string;
  diffText?: string;
  diffSuccess?: boolean;
  agentId?: string;
  model?: string;
}

/** File-read result → FileReadCard. */
export interface FileReadLogEntry extends LogEntryBase {
  type: "file_read";
  fileReadPath?: string;
  fileReadText?: string;
  fileReadTotalLines?: number;
  agentId?: string;
  model?: string;
}

/** step_reflection → a plain reflection card (body is `content`). */
export interface SystemLogEntry extends LogEntryBase {
  type: "system";
}

/** action_plan → a plan row, diffed against the previous version. */
export interface PlanLogEntry extends LogEntryBase {
  type: "plan";
  steps?: PlanStep[];
  version?: number;
  label?: string;
  planMode?: "full" | "diff";
}

/** A deny/pending permission decision (allow decisions produce no row). */
export interface PermissionLogEntry extends LogEntryBase {
  type: "permission";
  decision?: string;
  toolId?: string;
  operation?: string;
  toolInput?: string;
}

/** sub_agent lifecycle event → agent card. */
export interface AgentLogEntry extends LogEntryBase {
  type: "agent";
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
}

/** A finished sub-agent's AgentResult (blocking spawn or imported Agent tool). */
export interface AgentResultLogEntry extends LogEntryBase {
  type: "agent_result";
  agentResultStatus?: string;
  stepsUsed?: number;
  summary?: string;
  artifacts?: string[];
  warnings?: string[];
}

/** Run completion; a failure routes to RunFailedCard via `runFailure`. */
export interface CompletionLogEntry extends LogEntryBase {
  type: "completion";
  doneReason?: string;
  error?: string;
  /** Set when `doneReason` is a failure — routes the log entry to RunFailedCard. */
  runFailure?: RunFailureMeta;
}

/** agent_message → a chat-style row keyed by agent handle. */
export interface AgentMessageLogEntry extends LogEntryBase {
  type: "agent_message";
  agentId?: string;
  depth?: number;
  detail?: string;
}

/** user_steer → a chat-style row from the user. */
export interface UserSteerLogEntry extends LogEntryBase {
  type: "user_steer";
  detail?: string;
}

/** context_compacted → compaction card with token deltas + summary. */
export interface CompactLogEntry extends LogEntryBase {
  type: "compact";
  compactSummary?: string;
  tokensBefore?: number;
  tokensSaved?: number;
  tokensAfter?: number;
  eventsSummarized?: number;
  compactMode?: string;
  model?: string;
  agentId?: string;
}

/** check_agents (kind="agent_tree") → CheckAgentsCard with the tree + raw tab. */
export interface CheckAgentsLogEntry extends LogEntryBase {
  type: "check_agents";
  agents?: AgentTreeNode[];
  rawText?: string;
  parentId?: string;
  wait?: boolean;
  durationMs?: number;
  waitedMs?: number;
}

/** steer_agent tool call → a "root → agent" chat row. */
export interface RootSteerLogEntry extends LogEntryBase {
  type: "root_steer";
  steerAction?: string;
  steerTargetPrefix?: string;
  steerTargetFullId?: string;
  steerTargetTask?: string;
  steerMessage?: string;
  steerResult?: string;
  steerIsError?: boolean;
}

/** Non-blocking spawn_agent (or imported Agent tool) → SpawnAgentCard. */
export interface SpawnSubmitLogEntry extends LogEntryBase {
  type: "spawn_submit";
  spawnCaller?: string;
  spawnChildId?: string;
  spawnTask?: string;
  spawnAgentType?: string;
  spawnModel?: string;
  spawnAllowedTools?: string[];
  spawnDeniedTools?: string[];
  spawnAcceptance?: string;
  spawnExtras?: ReadonlyArray<readonly [string, string]>;
  spawnMessage?: string;
  spawnDurationMs?: number;
}

/** Same-model LLM retry after a transient error. `model` = the retry target. */
export interface LlmRetryLogEntry extends LogEntryBase {
  type: "llm_retry";
  model?: string;
  agentId?: string;
  depth?: number;
  retryAttempt?: number;
  retryMaxAttempts?: number;
  retryErrorType?: string;
  retryDelay?: number;
  error?: string;
}

/** Cross-model fallback. `fallbackSticky` pins the destination for the run. */
export interface LlmFallbackLogEntry extends LogEntryBase {
  type: "llm_fallback";
  agentId?: string;
  depth?: number;
  fallbackFromModel?: string;
  fallbackToModel?: string;
  fallbackReason?: string;
  fallbackPreviousErrorType?: string;
  /** Destination model is pinned for the rest of the run (sticky fallback). */
  fallbackSticky?: boolean;
}

/** The doom-loop halt (`recovery` with `action="halt_no_progress"`). */
export interface RecoveryHaltLogEntry extends LogEntryBase {
  type: "recovery_halt";
  agentId?: string;
  depth?: number;
  recoveryAction?: string;
  recoveryTool?: string;
}

export type LogEntry =
  | ShellLogEntry
  | DiffLogEntry
  | FileReadLogEntry
  | SystemLogEntry
  | PlanLogEntry
  | PermissionLogEntry
  | AgentLogEntry
  | AgentResultLogEntry
  | CompletionLogEntry
  | AgentMessageLogEntry
  | UserSteerLogEntry
  | CompactLogEntry
  | CheckAgentsLogEntry
  | RootSteerLogEntry
  | SpawnSubmitLogEntry
  | LlmRetryLogEntry
  | LlmFallbackLogEntry
  | RecoveryHaltLogEntry;

export type PlanStep = {
  title: string;
  description?: string;
  diffType?: "added" | "updated" | "removed";
};


// ---------------------------------------------------------------------------
// Server-side slash commands (see mewbo_core/commands.py)
// ---------------------------------------------------------------------------

export type CommandRender = "transcript" | "dialog" | "notification";

export interface CommandSpec {
  name: string;
  description: string;
  usage: string;
  render: CommandRender;
}

export interface CommandResult {
  render: CommandRender;
  title: string;
  body: string;
  metadata: Record<string, unknown>;
}
