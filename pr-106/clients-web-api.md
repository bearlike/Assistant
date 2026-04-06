# Console + API

<div style="display: flex; flex-wrap: wrap; gap: 12px; justify-content: center;">
  <img src="../meeseeks-console-01-front.jpg" alt="Meeseeks Console landing page" style="width: 100%; max-width: 520px; height: auto;" />
  <img src="../meeseeks-console-02-tasks.jpg" alt="Meeseeks Console tasks page" style="width: 100%; max-width: 520px; height: auto;" />
</div>

The REST API lives in `apps/meeseeks_api/` and the web console lives in `apps/meeseeks_console/`. The console is built for asynchronous delegation: requests flow through the API and the console polls events for status and output. The API runs tools through the core orchestration loop while the console provides session management, event timelines, execution traces, and tool output visualization.

## Setup (uv)
```bash
uv sync --extra api
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

Both the API and console are available as pre-built container images:

```bash
# From the repo root
cp docker.example.env docker.env
# Edit docker.env — set MASTER_API_TOKEN, VITE_API_KEY, HOST_UID/GID

docker compose pull && docker compose up -d
```

The console's nginx proxies `/api/` to the API at `127.0.0.1:5125` (host networking), so no separate CORS or URL config is needed in the default setup. Runtime environment variables (`VITE_API_BASE_URL`, `VITE_API_KEY`, etc.) are injected at container startup via `runtime-config.js` — no image rebuild required.

See [Installation](getting-started.md#docker-compose-deployment) for the full environment variable reference and production reverse proxy setup.
