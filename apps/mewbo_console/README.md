# Mewbo Console

Web-based task orchestration frontend for the Mewbo assistant. The console connects to the REST API, which drives the core orchestration loop.

## Features

- Session management (create, list, archive)
- Real-time event polling with conversation timeline
- Execution trace and log viewer
- Unified diff viewer for file changes
- MCP tool selection per query
- Mid-session message injection and step interruption

## Architecture

```
Console (React + Vite)  →  REST API (Flask)  →  Core Orchestration (ToolUseLoop)
```

The console is a static single-page application. All orchestration runs server-side through the API.

## Setup

```bash
npm install
npm run dev
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | *(empty — uses Vite proxy)* | API server URL |
| `VITE_API_KEY` | *(empty)* | Value for `X-API-Key` header |
| `VITE_API_MODE` | `auto` | `auto`, `live`, or `mock` |
| `VITE_API_USE_PROXY` | *(empty)* | Set to `1` to proxy `/api/` via Vite dev server |

## Docker

Build and run with the API using Docker Compose:

```bash
cd apps/mewbo_api
docker compose up --build
```

This starts both the API server (port 5124) and the console (port 3000).

## Development

| Command | Description |
|---------|-------------|
| `make install` | Install dependencies |
| `make dev` | Start dev server |
| `make build` | Production build |
| `make lint` | Run ESLint |
| `make test` | Run unit tests |
| `make clean` | Remove build artifacts |
