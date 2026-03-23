# Meeseeks Chat - Project Guidance

Scope: this file applies to the `apps/meeseeks_chat/` UI app. It captures runtime behavior, hidden dependencies, and testing notes.

## Runtime flow
- Entry point: `apps/meeseeks_chat/src/meeseeks_chat/chat_master.py` (UI app).
- Uses `generate_action_plan(...)` for preview and `orchestrate_session(...)` for execution.
- Stores session state in `st.session_state`:
  - `session_store`, `session_id`, `messages`, and `conversation_memory`.
- Action plan (title/description steps) is displayed in an expander ("thought" role).

## Hidden dependencies / assumptions
- Expects static assets packaged under `src/meeseeks_chat/static/`.
- Uses `time.sleep(...)` for spinner pacing (adds latency even if tool work is fast).
- Approvals are auto-approved via `core.permissions.auto_approve`.

## Pitfalls / gotchas
- UI session state is per-browser tab; multi-tab behavior can fork sessions unexpectedly.
- The local rolling memory buffer is separate from core session summaries.
- If core orchestration changes the shape of `TaskQueue`, update UI formatting here.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language in docs/changes.

## Testing guidance
- Keep tests light: mock `orchestrate_session` and `generate_action_plan` only.
- Avoid end-to-end UI testing unless UX regressions require it.

## Cross-project insights
- Show the plan and reflect tool outputs before final response; keep the plan display concise.
- Context summaries should be short and recent; avoid dumping long histories into the UI.
- Keep turn boundaries clear (user input -> plan -> tool -> response).
