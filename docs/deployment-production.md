# Production Setup

Running Mewbo in production means hardening the default Docker Compose setup with TLS termination, proper authentication tokens, CORS restriction, and observability. This page covers the production checklist and the nginx reverse proxy configuration.

## Security Checklist

1. **Rotate `MASTER_API_TOKEN`**. The example value is public. Generate a strong random token:
   ```bash
   openssl rand -hex 32
   ```
   Set it in `docker.env` as both `MASTER_API_TOKEN` and `VITE_API_KEY` (they must match).

2. **Restrict `CORS_ORIGIN`**. The default `*` allows any origin. In production, set it to your actual domain:
   ```dotenv
   CORS_ORIGIN=https://mewbo.example.com
   ```

3. **Use TLS**. Terminate TLS at a reverse proxy such as nginx, Caddy, or Traefik. Never expose the API or console ports directly on a public interface.

4. **Set `GITHUB_TOKEN` in `docker.env`**. If you mount git repositories, the `10-git-setup.sh` init script uses this token to configure `gh CLI` authentication. That lets `git fetch/push/pull` work without prompts.

5. **Change MongoDB credentials**. Update `MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD` in `docker.env` from their defaults. Then update `MEWBO_MONGODB_URI` to match.

## TLS with nginx

The repository includes a ready-to-use nginx reverse proxy config. Install it on the host machine (outside Docker):

```bash
sudo ln -s /path/to/mewbo/docker/nginx-reverse-proxy.conf \
           /etc/nginx/sites-enabled/mewbo
sudo nginx -t && sudo systemctl reload nginx
```

Edit the file to set your `server_name`, `ssl_certificate`, and `ssl_certificate_key` before enabling it.

**Full nginx server block (from `docker/nginx-reverse-proxy.conf`):**

```nginx
server {
    listen 443 ssl http2;
    server_name mewbo.example.com;

    ssl_certificate     /path/to/your/server.crt;
    ssl_certificate_key /path/to/your/server.key;

    # Web IDE: WebSocket upgrade + long timeout
    location /ide/ {
        proxy_pass http://127.0.0.1:5126;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }

    # Console (frontend SPA)
    location / {
        proxy_pass http://127.0.0.1:3001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # API: SSE streaming endpoints need no buffering
    location /api/sessions/ {
        proxy_pass http://127.0.0.1:5125;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
        proxy_read_timeout 300s;
    }

    # API: all other endpoints
    location /api/ {
        proxy_pass http://127.0.0.1:5125/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Wiki SSE: indexing + QA event streams need no buffering
    location ~ ^/v1/wiki/(index|qa)/[^/]+/stream$ {
        proxy_pass http://127.0.0.1:5125;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection '';
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
        proxy_read_timeout 300s;
    }

    # Wiki API: all other wiki endpoints
    location /v1/wiki/ {
        proxy_pass http://127.0.0.1:5125/v1/wiki/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# HTTP → HTTPS redirect
server {
    listen 80;
    server_name mewbo.example.com;
    return 301 https://$host$request_uri;
}
```

### Key nginx settings for Mewbo

| Setting | Why |
|---------|-----|
| `proxy_buffering off` on `/api/sessions/` | Server-Sent Events (SSE) must stream to the browser in real time; buffering breaks the stream. |
| `proxy_read_timeout 300s` on `/api/sessions/` | Sessions can run for minutes; the default 60s timeout would kill long-running agents. |
| Same SSE settings on the wiki `/stream` regex block | Wiki indexing and Q&A push progress over SSE too; without this block those streams stall behind nginx buffering. |
| `proxy_http_version 1.1` + `Connection ''` on SSE | Required for HTTP/1.1 keepalive on SSE endpoints. |
| `proxy_read_timeout 3600s` + WebSocket headers on `/ide/` | code-server uses WebSockets; upgrade headers and a long timeout are required. |
| `proxy_buffering off` on `/ide/` | Prevents nginx from interfering with the WebSocket connection. |

Since both the API (`5125`) and console (`3001`) use host networking, `127.0.0.1` is reachable from the host-level nginx.

## Observability with Langfuse

Langfuse provides LLM-level tracing for every session. Each multi-turn session appears as one trace group, making it straightforward to see which tools were called, what the model reasoned, and where errors occurred.

Add Langfuse config to `configs/app.json`:

```json
{
  "langfuse": {
    "enabled": true,
    "public_key": "pk-lf-...",
    "secret_key": "sk-lf-...",
    "host": "https://cloud.langfuse.com"
  }
}
```

For a self-hosted Langfuse instance, set `host` to your deployment URL. The `configs/app.json` file is mounted read-only into the API container; changes take effect on the next `docker compose up -d` (no rebuild needed).

### Filtering traces by provenance

Every trace is tagged at run start with provenance facets, so you can slice the Langfuse dashboard by what produced the traffic. Use them to answer operator questions directly: which product is burning tokens, which repo's wiki indexing failed, which client surface sent a bad request.

Facets appear as `key:value` trace tags. The available keys:

| Facet | Example values |
|-------|----------------|
| `origin` | `user`, `wiki`, `search`, `channel`, `structured`, `draft` |
| `product` | `agent`, `wiki`, `search`, `channel`, `structured`, `draft`, `vcs` |
| `session_type` | `chat`, `wiki_index`, `wiki_qa`, `search_run`, `scg_map`, `channel_msg`, `structured_run`, `structured_fast`, `draft_stream`, `vcs_pickup` |
| `surface` | `api`, plus whatever clients stamp (CLI, console, channel platforms) |
| `project` | The named project the session ran against. |
| `repo` | Repository, e.g. `owner/repo`. |
| `branch` | Git branch. |
| `workspace` | Structured-response or search workspace. |
| `model` | Model id the session was created with. |

Trace metadata carries a superset of the tags. It adds high-cardinality fields that would bloat the tag list: `worktree` (for sessions running in an ephemeral managed worktree), per-product ids such as `wiki_id` and `search_id`, channel and thread ids, and the session's `capabilities`. Filter on metadata when you need a specific id.

API clients can stamp their surface by sending an optional `X-Mewbo-Surface` header on requests. The API defaults it to `api` when absent. The header is already in the CORS allow-list, so browser clients can send it cross-origin. A path that never stamps a surface shows up as `surface:unknown` rather than untagged, which keeps un-instrumented clients findable.

See [Troubleshooting](troubleshooting.md) for the recommended sequence: MongoDB transcript → Langfuse traces → config → Docker env.

## Health Monitoring

The API does not expose a dedicated health endpoint. Use one of these approaches to verify liveness:

```bash
# Check if the API responds
curl -sk http://localhost:5125/api/tools -H "X-API-Key: your-token" | jq length

# Stream container logs
docker compose logs -f api

# Check container status
docker compose ps
```

For external monitoring (uptime checks, alerting), probe `GET /api/tools` with your API key. It returns a non-empty list when the API is healthy.

## API Token Rotation

To rotate `MASTER_API_TOKEN`:

1. Update `docker.env`:
   ```dotenv
   MASTER_API_TOKEN=new-strong-random-token
   VITE_API_KEY=new-strong-random-token
   ```
2. Apply without rebuilding:
   ```bash
   docker compose up -d
   ```

The console reads `VITE_API_KEY` from the injected `runtime-config.js` at startup. No image rebuild is needed. Existing browser sessions will get a 401 and prompt for the new key on the next request.

## Resource Limits

Every service in `docker-compose.yml` ships with memory and CPU limits, so a runaway process cannot take down the host:

| Service | Memory limit | CPU limit |
|---------|-------------|-----------|
| `api` | `4G` | `4.0` |
| `mongo` | `1G` | `1.5` |
| `mewbo-mcp` | `1G` | `1.0` |
| `console` | `256M` | `0.5` |
| `ide-proxy` | `128M` | `0.5` |

The API gets the largest envelope because it does the heavy lifting: LLM orchestration, wiki indexing, sub-agent fan-out, and Web IDE management. Raise its memory limit if you index very large repositories. Adjust any limit in `docker-compose.override.yml`:

```yaml
services:
  api:
    deploy:
      resources:
        limits:
          memory: 8G
          cpus: '6.0'
```
