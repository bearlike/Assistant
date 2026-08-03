# Web IDE

<video controls preload="metadata" poster="../../assets/img/mewbo-console-01-front.png" style="width: 100%; max-width: 960px; height: auto; display: block; margin: 0 auto;">
  <source src="../../assets/videos/mewbo-console-coder-demo-1.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

Mewbo can launch a full IDE in your browser, tied to a session's working directory. It runs [code-server](https://github.com/coder/code-server), which is VS Code in the browser. This gives you a real editor alongside the agent. You review diffs, edit files, and run terminals while Mewbo works in the same directory. One container runs per session. It starts on demand and stops itself after a configurable time-to-live.

## Enabling

Set `agent.web_ide.enabled` to `true` in `configs/app.json`.

```json
{
  "agent": {
    "web_ide": {
      "enabled": true
    }
  }
}
```

The Web IDE needs MongoDB to persist container state across API restarts. See [Storage Backends](../deployment-storage.md) for how to enable the MongoDB driver. In the Docker Compose stack the rest of the plumbing is wired for you. See [Docker Compose](../deployment-docker.md).

## Launch an IDE

### From the console

When the feature is enabled, an **Open in Web IDE** button appears on session cards. The console checks whether the feature is available and shows the button only then. Click it to start the container and open the IDE in a new tab. While the container boots, the loader shows a floral background animation. The launch is handled by [`IdeLoader`](repo:apps/mewbo_console/src/components/IdeLoader.tsx).

### From the API

The console button calls the same endpoints you can drive directly.

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/api/sessions/{session_id}/ide` | Create or reconnect to the IDE container |
| `GET` | `/api/sessions/{session_id}/ide` | Poll current container status |
| `DELETE` | `/api/sessions/{session_id}/ide` | Stop and remove the container |
| `POST` | `/api/sessions/{session_id}/ide/extend` | Extend the session lifetime |

The `POST` response includes a one-time `password` field. The `GET` response omits it. The IDE is reachable at `/ide/{session_id}/` behind the built-in nginx proxy.

Create an IDE session:

```bash
curl -sk -X POST https://mewbo.example.com/api/sessions/abc123.../ide \
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

Extend the deadline:

```bash
curl -sk -X POST https://mewbo.example.com/api/sessions/abc123.../ide/extend \
  -H "X-API-Key: your-token" \
  -H "Content-Type: application/json" \
  -d '{"hours": 2}'
```

The extend endpoint accepts either `hours`, an integer from 1 to 168, or an absolute `expires_at` ISO timestamp. Exactly one field is required. A request that would push the deadline past `max_lifetime_hours` is rejected with HTTP 409.

## Session lifetime

Every IDE session carries an expiry. When the wall clock passes `expires_at`, the container shuts itself down. You can extend a running session, stop it early, or reconnect to it at any time.

| Action | What happens |
|--------|--------------|
| Running | The container stays alive until `expires_at`. |
| Extend | Pushing the deadline out takes effect within about 15 seconds. |
| Stop | The container tears down immediately. |
| Reconnect | Reconnecting returns the current URL and password. If the container exited unexpectedly, Mewbo respawns it. |

## Configuration

All keys nest under `agent.web_ide` in `configs/app.json`.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable the feature. Requires MongoDB. |
| `image` | `codercom/code-server:latest` | Docker image to run. |
| `default_lifetime_hours` | `1` | Initial lifetime in hours (1 to 24). |
| `max_lifetime_hours` | `8` | Hard ceiling on total lifetime per session (1 to 168). |
| `cpus` | `1.0` | CPU quota per container (0.1 to 16.0). |
| `memory` | `1g` | Memory limit, for example `512m` or `2g`. |
| `pids_limit` | `512` | PID limit per container (64 to 4096). |
| `network` | `mewbo-ide` | Docker network the containers join. Must be the network `ide-proxy` is attached to, or the proxy cannot reach the container. |
| `proxy_url` | `http://127.0.0.1:5126` | Base URL the API uses to reach `ide-proxy` for its readiness probe. |
| `state_dir` | `/tmp/mewbo-ide` | Host directory for bookkeeping files. |

Restrict resources and pin the image:

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

The session's project directory is mounted into the container. Edits you make in the IDE show up immediately to the running agent, and the agent's edits show up in the IDE.

## Next steps

- [Sessions](sessions.md): the session list and the composer that starts an IDE-eligible session.
- [Docker Compose](../deployment-docker.md): the stack that wires the IDE plumbing.
- [REST API Reference](../rest-api.md): the full IDE endpoint contract.
