Meeseeks Console Investigation Notes

Scope
- Track current component structure, stateful concerns, and the data hooks needed for a real API-backed UI.
- Keep this in sync as we refactor the frontend and align with backend session/event APIs.

Stop Control (v1)
- Show Stop icon in chat area when a session is running.
- Stop triggers /terminate slash command (backend cancels run).
- UI disables send button while running unless stop is pressed.

Component Inventory (current)
- App.tsx: view switch (home/detail), selectedTask state.
- TopBar: mode + selected task context.
- HomeView: task list, search dialog, input bar (home).
- TaskDetailView: conversation timeline, workspace panel, input bar (detail).
- ConversationTimeline: message bubbles, summary block, file list, logs/diff triggers.
- WorkspacePanel: tabs (diff/logs), DiffView, LogsView.
- InputBar: composer UI, MCP toggles, attachments, repo/branch selectors (mock).
- Supporting UI: TaskItem, StatusBadge, DiffStats, SummaryBlock, FileList, MessageBubble, ShellBlock, DiffView, LogsView.

Component → Data Requirements (mapping)
- TopBar: session title, repo, branch, date, diff stats, status.
- HomeView/TaskItem: list of sessions (title, repo, branch, date, status, diff stats/commit count).
- TaskDetailView/ConversationTimeline: ordered timeline events (user/system/assistant), summary, files list, duration.
- WorkspacePanel/LogsView: structured log entries (shell/system), plus optional summary/testing block.
- WorkspacePanel/DiffView: file + unified diff content (per file).
- InputBar: active session, MCP toggles, attachments, repo/branch selection (optional in v1).
- ConversationTimeline: hide Version toggles (explicit non-requirement).

Current Data Model (mockData.ts)
- Task: id, title, repo, branch, date, status, diff stats.
- Message: role, content, meta, summary, files.
- LogEntry: type, content, timestamp.
- diffContent: unified diff string for DiffView.

Stateful Concerns (to centralize)
- Active session selection (current: selectedTask).
- Search dialog state + query.
- Workspace panel open/close and active tab.
- MCP toggle list and attachment state.
- Streaming/async status (running, done, error).

Planned Data Hooks (frontend)
- useSessions(): list sessions for HomeView.
- useCreateSession(): create session (optional tag).
- useSendQuery(sessionId, text): enqueue query.
- useSessionEvents(sessionId): poll/stream transcript events.
- useSessionSummary(sessionId): derived from events/summary endpoint.
- useSessionStatus(sessionId): running/completed/error.
- useWorkspaceTab(): diff/logs open state.
- useSessionMetadata(sessionId): title, created_at, optional repo/branch.
- useSessionControl(sessionId): terminate/cancel, mark inactive, handle slash commands.
- useSessionRunning(sessionId): boolean for Stop icon and disabled inputs.

Event → UI Mapping (proposed v1)
- user event → MessageBubble(role=user, content=text).
- assistant event → MessageBubble(role=system, content=text).
- action_plan event → new PlanBlock (list of steps), or SummaryBlock placeholder.
- tool_result event → LogsView entries (shell/system) and optional inline ToolResultBlock in timeline.
- permission event → ignored in UI (auto-approve v1), still logged.
- step_reflection event → timeline “system” note (optional).
- completion event → session status badge + final summary.
- step_reflection event → optional inline note (debug).

Permissions (backend behavior)
- Default policy: get=allow, set=ask.
- Web v1: force auto-approve via `permissions.approval_mode=allow` or explicit `auto_approve`.
- Permission events still emitted when ASK is triggered.

Missing/Derived Fields (current backend)
- Title: derive from first user message (truncate).
- Date: derive from first event timestamp.
- Status: derive from completion event (done + done_reason).
- Repo/branch/diff stats: not available in backend today.
  - v1: keep as “unknown” or hide until metadata exists.
  - v2: add session metadata fields or tool-provided artifacts.

Execution Trace (UI)
- When user expands “Trace” above assistant response, render:
  - action_plan steps
  - tool_result entries (tool + action + summary)
  - completion state
- Do not show raw chain-of-thought.

New Minimal UI Components (to add)
- SessionList + SessionListItem (replaces Task list).
- TimelineEventRenderer (switch on event.type).
- PlanBlock (render action_plan steps).
- ToolResultBlock (render tool output summary).
- SessionStatusBadge (running/completed/blocked/failed).

Backend Data Gaps (to address)
- Session list endpoint (id + derived title/status/created_at).
- Events endpoint (full transcript JSON).
- Async run endpoint (enqueue, return 202).
- Optional SSE/stream endpoint (later if needed).

API Assumptions (v1)
- POST /api/sessions -> { session_id }
- POST /api/sessions/{id}/query -> 202 Accepted
- GET /api/sessions/{id}/events -> array of events (or JSONL parsed)
- Optional GET /api/sessions/{id}/stream -> SSE (if needed)
- Permissions prompt bypassed in web v1 (auto approve).
- Polling cadence: 1s minimum (client-side throttled).

Slash Commands (v1 requirements)
- Frontend parses input starting with "/" and routes to control API.
- Proposed commands: /quit, /terminate, /cancel, /compact, /status.
- Backend must support canceling running orchestration and closing thread safely.

Execution Trace vs “Thoughts”
- Do not expose raw chain-of-thought.
- Provide an “Execution Trace” panel: action_plan steps, tool_result, logs, errors.
- Optional short “Reasoning Summary” generated from events, not raw model thoughts.

Open Questions
- Do we keep the HomeView task list as real sessions or a curated “recent runs” list?
- Should logs/diff be derived from events or separate endpoints?
- Which fields in events map to UI blocks (summary, file list, tool result)?

Next Investigation Steps
- Map each UI block to transcript event types (action_plan, tool_result, assistant, completion).
- Define the minimal view model to render MessageBubble + SummaryBlock from events.
- Decide on polling interval vs SSE for v1.
