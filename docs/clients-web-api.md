# Console + API

<div style="display: flex; flex-wrap: wrap; gap: 12px; justify-content: center;">
  <img src="../meeseeks-console-01-front.jpg" alt="Meeseeks Console landing page" style="width: 100%; max-width: 520px; height: auto;" />
  <img src="../meeseeks-console-02-tasks.jpg" alt="Meeseeks Console tasks page" style="width: 100%; max-width: 520px; height: auto;" />
</div>

The REST API is the programmable surface for Meeseeks — you send it queries, start and resume sessions, and poll events for progress. The web console is a browser-based client that sits on top of that API. It is built for asynchronous delegation: you submit a task, the console streams the event timeline, and you follow execution traces, tool outputs, and sub-agent activity as the session runs. The API handles orchestration; the console gives you session management, an event timeline, and rich visualization of tool output.

See [Get Started](getting-started.md#api-setup) for installation and [Docker Compose](deployment-docker.md) for container deployment.

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
- `POST /api/webhooks/<platform>` inbound webhook for chat platforms (HMAC auth, not API key). See [Nextcloud Talk](clients-nextcloud-talk.md) and [Email](clients-email.md) for setup. Slash commands: `/help`, `/usage`, `/new`, `/switch-project`.
- `GET /api/plugins` list installed plugins and their components
- `GET /api/plugins/marketplace` list available plugins from configured marketplaces
- `POST /api/plugins/marketplace` install a plugin from a marketplace
- `DELETE /api/plugins/<name>` uninstall a plugin
- `POST /api/sessions/{session_id}/ide` launch a Web IDE (code-server) container for a session
- `DELETE /api/sessions/{session_id}/ide` stop the Web IDE container
- `POST /api/sessions/{session_id}/ide/extend` extend the IDE session TTL

## Run the Console
```bash
cd apps/meeseeks_console
npm install
npm run dev
```

Console notes:
- Configure `VITE_API_BASE_URL` to point to the API server (default: `http://127.0.0.1:5124`).
- Set `VITE_API_KEY` to match `api.master_token` in `configs/app.json`.
- Set `VITE_API_MODE` to `live` for direct API access or `auto` (default) for fallback to mock data.
- During development, the Vite dev server proxies `/api/` requests to the API backend.

## Docker Compose deployment

For container-based deployment, including the full environment variable reference and production reverse proxy setup, see [Docker Compose](deployment-docker.md).

## Session management

### Fork from message / edit and regenerate

Any message in a session can be used as a branch point. In the console, hover a message
and click "Fork from here" to create a new session with history up to that point.
The API equivalent:

```bash
POST /api/sessions
{
  "fork_from": "<session_id>",
  "fork_at_ts": <timestamp>
}
```

### Per-message model override

In the console, each message input has a model selector. Submit with a different model
to use it for that turn only. The session's default model is unchanged.

## Sharing and export

```
POST /api/sessions/{id}/share     → returns { token }
GET  /api/share/{token}           → fetch shared session data (read-only)
POST /api/sessions/{id}/export    → download full session payload
```

Shared sessions are read-only and accessible without authentication.

## Attachments

Upload files to inject their content into the LLM context:

```
POST /api/sessions/{id}/attachments   (multipart/form-data)
```

Uploaded text files are read from disk and injected into the system prompt for that session.

## Mid-session steering

While a session is running, you can send messages or interrupt:

```
POST /api/sessions/{id}/message     { "text": "..." }    → queued as HumanMessage
POST /api/sessions/{id}/interrupt   → signals the current step to pause
```

In the console, the InputBar shows a steering mode UI while a run is in progress.

## Multi-project support

The console supports multiple projects that appear as virtual workspaces shared across sessions. Each project has its own working directory, its own `.mcp.json`, and its own `.claude/skills/` directory, so tools and skills scope cleanly to the project you are in.

<div style="display: flex; justify-content: center;">
  <img src="../meeseeks-console-06-projects.jpg" alt="The Projects page in the Meeseeks console showing two virtual workspaces with their paths" style="width: 100%; max-width: 720px; height: auto;" />
</div>

Create and switch projects from the **Projects** page or the project selector in the ConfigMenu. In the REST API, projects are identified by the working directory path you pass in the session context.

## Notifications

```
GET  /api/notifications          → list pending notifications
POST /api/notifications/dismiss  → dismiss by ID
POST /api/notifications/clear    → clear all
```

Notifications appear in the console bell icon for events like session errors.
