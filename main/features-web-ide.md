# Web IDE

Meeseeks can launch a per-session [code-server](https://github.com/coder/code-server) (VS Code in the browser) tied to a session's working directory. This gives you a full IDE alongside the AI — you can review diffs, edit files directly, and run terminals while Meeseeks works in parallel. One container is created per session, started on demand, and automatically stopped after a configurable time-to-live.

## Enabling

Set `agent.web_ide.enabled` to `true` in `configs/app.json`:

```json
{
  "agent": {
    "web_ide": {
      "enabled": true
    }
  }
}
```

The Web IDE feature requires MongoDB to persist container state across API restarts. See [Storage Backends](deployment-storage.md) for how to enable the MongoDB driver. In the Docker Compose stack the rest of the plumbing is wired automatically — see [Docker Compose Deployment](deployment-docker.md).

## Launching an IDE session

### From the console

When `agent.web_ide.enabled` is `true`, an **Open in Web IDE** button appears on session cards in the web console. Clicking it starts the container and opens the IDE in a new tab.

### From the API

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/api/sessions/{id}/ide` | Create or reconnect to the IDE container |
| `GET` | `/api/sessions/{id}/ide` | Poll current container status |
| `DELETE` | `/api/sessions/{id}/ide` | Stop and remove the container |
| `POST` | `/api/sessions/{id}/ide/extend` | Extend the session TTL |

The `POST` response includes a one-time `password` field (omitted from `GET` responses). The IDE is reachable at `/ide/{session_id}/` behind the built-in nginx proxy.

**Example — create an IDE session:**

```bash
curl -sk -X POST https://meeseeks.example.com/api/sessions/abc123.../ide \
  -H "X-API-Key: your-token" | jq .
```

```json
{
  "session_id": "abc123...",
  "status": "starting",
  "url": "/ide/abc123.../",
  "password": "...",
  "expires_at": "2026-04-18T15:00:00+00:00",
  "remaining_seconds": 3600
}
```

**Extend the deadline:**

```bash
curl -sk -X POST https://meeseeks.example.com/api/sessions/abc123.../ide/extend \
  -H "X-API-Key: your-token" \
  -H "Content-Type: application/json" \
  -d '{"hours": 2}'
```

The extend endpoint accepts either `hours` (integer, 1–168) or an absolute `expires_at` ISO timestamp. Exactly one field is required. Requests that would push the deadline past `max_lifetime_hours` are rejected with HTTP 409.

## Session lifetime

Every IDE session carries an expiry. When the wall clock passes `expires_at`, the container shuts itself down automatically. You can extend a running session, stop it early, or reconnect to it at any time.

| Action | What happens |
|-------|-----------|
| Running | The container stays alive until `expires_at`. |
| Extend | `POST .../ide/extend` pushes the deadline out; the change takes effect within about 15 seconds. |
| Stop | `DELETE .../ide` tears the container down immediately. |
| Reconnect | `POST .../ide` on an existing session returns the current URL and password. If the container exited unexpectedly, Meeseeks respawns it automatically. |

## Configuration

All keys are nested under `agent.web_ide` in `configs/app.json`.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable the feature. Requires MongoDB. |
| `image` | `codercom/code-server:latest` | Docker image to run. |
| `default_lifetime_hours` | `1` | Initial TTL in hours (1–24). |
| `max_lifetime_hours` | `8` | Hard ceiling on total lifetime per session (1–168). |
| `cpus` | `1.0` | CPU quota assigned to each container (0.1–16.0). |
| `memory` | `1g` | Memory limit (e.g. `512m`, `2g`). |
| `pids_limit` | `512` | PID limit per container (64–4096). |
| `network` | `meeseeks-ide` | Docker network the containers join. |
| `state_dir` | `/tmp/meeseeks-ide` | Host directory used for bookkeeping files. |

**Example — restrict resources and pin the image:**

```json
{
  "agent": {
    "web_ide": {
      "enabled": true,
      "image": "codercom/code-server:4.95.3",
      "default_lifetime_hours": 2,
      "max_lifetime_hours": 4,
      "cpus": 0.5,
      "memory": "512m"
    }
  }
}
```

The session's project directory is mounted into the container so edits you make in the IDE show up immediately to the running agent, and vice versa.

---

> **How it works internally:** See [Architecture Overview → Web IDE manager](core-orchestration.md#web-ide).
