# Meeseeks API Server
<p align="center">
    <a href="https://github.com/bearlike/Assistant/pkgs/container/meeseeks-chat"><img src="https://img.shields.io/badge/ghcr.io-bearlike/meeseeks&#x2212;api:latest-blue?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Image"></a>
    <a href="https://github.com/bearlike/Assistant/releases"><img src="https://img.shields.io/github/v/release/bearlike/Assistant?style=for-the-badge&" alt="GitHub Release"></a>
</p>

- REST API engine wrapped around meeseeks-core.
- No components are explicitly tested for safety or security. Use with caution in production.
- For setup and configuration, see `docs/getting-started.md`.

## Run
```bash
uv sync --extra api
uv run meeseeks-api
```

## Core endpoints
- `POST /api/sessions` create a session
- `GET /api/sessions` list sessions (filters empty + archived by default; use `?include_archived=1`)
- `POST /api/sessions/{session_id}/query` enqueue a run or core command
- `GET /api/sessions/{session_id}/events?after=...` poll events
- `POST /api/sessions/{session_id}/archive` archive a session
- `DELETE /api/sessions/{session_id}/archive` unarchive a session
- `POST /api/query` synchronous endpoint (simple/CLI-compatible)
- `GET /api/tools` list tool registry entries
- `GET /api/notifications` list notifications
- `POST /api/notifications/dismiss` dismiss notifications
- `POST /api/notifications/clear` clear notifications
- `POST /api/sessions/{session_id}/attachments` upload attachments
- `POST /api/sessions/{session_id}/share` create share link
- `POST /api/sessions/{session_id}/export` export session payload
- `GET /api/share/{token}` fetch shared session data

[Link to GitHub Repository](https://github.com/bearlike/Assistant)
