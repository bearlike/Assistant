# Web + API Clients

<div style="display: flex; flex-wrap: wrap; gap: 12px; justify-content: center;">
  <img src="../meeseeks-console-01-front.jpg" alt="Meeseeks Console landing page" style="width: 100%; max-width: 520px; height: auto;" />
  <img src="../meeseeks-console-02-tasks.jpg" alt="Meeseeks Console tasks page" style="width: 100%; max-width: 520px; height: auto;" />
</div>

The REST API lives in `apps/meeseeks_api/` and the Streamlit UI lives in `apps/meeseeks_chat/`. The web client (Meeseeks Console) is built for asynchronous delegation: work runs through the API and the UI polls events for status and output. The distinction from the CLI is execution context and autonomy: the web client executes through the API service, while the CLI runs tools directly on the local machine. Both clients share the same core runtime and can handle long-running tasks.

## Setup (uv)
```bash
uv sync --extra api --extra chat
```

Before running, complete [Installation](getting-started.md) and [LLM setup](llm-setup.md).

## Run the REST API
```bash
uv run meeseeks-api
```

API notes:

- Protected routes require `X-API-Key` matching `api.master_token` in `configs/app.json`.
- Session runtime endpoints support async runs and event polling.

Core endpoints:

- `POST /api/sessions` create a session
- `POST /api/sessions/{session_id}/query` enqueue a query or core command
- `GET /api/sessions/{session_id}/events?after=...` poll events
- `GET /api/sessions` list sessions (defaults to non-archived, non-empty)
- `GET /api/sessions?include_archived=1` include archived sessions
- `POST /api/sessions/{session_id}/archive` archive a session
- `DELETE /api/sessions/{session_id}/archive` unarchive a session
- `POST /api/query` synchronous endpoint
- `GET /api/tools` list tool registry entries
- `GET /api/notifications` list notifications
- `POST /api/notifications/dismiss` dismiss notifications
- `POST /api/notifications/clear` clear notifications
- `POST /api/sessions/{session_id}/attachments` upload attachments
- `POST /api/sessions/{session_id}/share` create share link
- `POST /api/sessions/{session_id}/export` export session payload
- `GET /api/share/{token}` fetch shared session data

## Run the web UI (Streamlit)
```bash
uv run meeseeks-chat
```

UI notes:
- Streamlit options live under `chat.*` in `configs/app.json` (address and port).
- The chat UI runs the core engine in-process.
