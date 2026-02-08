# Meeseeks API Server
<p align="center">
    <a href="https://github.com/bearlike/Assistant/pkgs/container/meeseeks-chat"><img src="https://img.shields.io/badge/ghcr.io-bearlike/meeseeks&#x2212;api:latest-blue?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Image"></a>
    <a href="https://github.com/bearlike/Assistant/releases"><img src="https://img.shields.io/github/v/release/bearlike/Assistant?style=for-the-badge&" alt="GitHub Release"></a>
</p>

- REST API Engine wrapped around the meeseeks-core.
- No components are explicitly tested for safety or security. Use with caution in a production environment.
- For full setup and configuration, see `docs/getting-started.md`.

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
- `POST /api/query` legacy synchronous endpoint

[Link to GitHub Repository](https://github.com/bearlike/Assistant)
