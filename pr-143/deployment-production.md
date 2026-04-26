# Production Setup

Running Truss in production means hardening the default Docker Compose setup with TLS termination, proper authentication tokens, CORS restriction, and observability. This page covers the production checklist and the nginx reverse proxy configuration.

## Security Checklist

1. **Rotate `MASTER_API_TOKEN`**. The example value is public. Generate a strong random token:
   ```bash
   openssl rand -hex 32
   ```
   Set it in `docker.env` as both `MASTER_API_TOKEN` and `VITE_API_KEY` (they must match).

2. **Restrict `CORS_ORIGIN`**. The default `*` allows any origin. In production, set it to your actual domain:
   ```dotenv
   CORS_ORIGIN=https://truss.example.com
   ```

3. **Use TLS**. Terminate TLS at a reverse proxy such as nginx, Caddy, or Traefik. Never expose the API or console ports directly on a public interface.

4. **Set `GITHUB_TOKEN` in `docker.env`**. If you mount git repositories, the `10-git-setup.sh` init script uses this token to configure `gh CLI` authentication. That lets `git fetch/push/pull` work without prompts.

5. **Change MongoDB credentials**. Update `MONGO_INITDB_ROOT_USERNAME` and `MONGO_INITDB_ROOT_PASSWORD` in `docker.env` from their defaults. Then update `TRUSS_MONGODB_URI` to match.

## TLS with nginx

The repository includes a ready-to-use nginx reverse proxy config. Install it on the host machine (outside Docker):

```bash
sudo ln -s /path/to/truss/docker/nginx-reverse-proxy.conf \
           /etc/nginx/sites-enabled/truss
sudo nginx -t && sudo systemctl reload nginx
```

Edit the file to set your `server_name`, `ssl_certificate`, and `ssl_certificate_key` before enabling it.

**Full nginx server block (from `docker/nginx-reverse-proxy.conf`):**

```nginx
server {
    listen 443 ssl http2;
    server_name truss.example.com;

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
}

# HTTP → HTTPS redirect
server {
    listen 80;
    server_name truss.example.com;
    return 301 https://$host$request_uri;
}
```

### Key nginx settings for Truss

| Setting | Why |
|---------|-----|
| `proxy_buffering off` on `/api/sessions/` | Server-Sent Events (SSE) must stream to the browser in real time; buffering breaks the stream. |
| `proxy_read_timeout 300s` on `/api/sessions/` | Sessions can run for minutes; the default 60s timeout would kill long-running agents. |
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

See [Troubleshooting](troubleshooting.md) for the recommended sequence: MongoDB transcript → Langfuse traces → config → Docker env.

## Health Monitoring

The API does not expose a dedicated health endpoint. Use one of these approaches to verify liveness:

```bash
# Check if the API responds
curl -sk http://localhost:5125/api/tools -H "X-API-Key: your-token" | jq length

# Stream container logs
docker compose logs -f truss-api

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

The MongoDB service in `docker-compose.yml` is pre-configured with memory limits (`512M`) and CPU limits (`1.0` core). For high-traffic deployments, adjust these in `docker-compose.override.yml`:

```yaml
services:
  mongo:
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: '2.0'
```

The API and console services do not set explicit limits by default. Add them in your override file if running on a shared host.
