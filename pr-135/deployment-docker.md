# Docker Compose Deployment

The recommended way to run Meeseeks in a persistent environment. Pre-built images for the API, console, and base layer are published to GHCR. A single `docker compose up` starts the full stack. It includes the API, console, MongoDB, and the nginx proxy for the Web IDE feature.

## Quick Start

**Step 1. Copy the environment template:**

```bash
cp docker.example.env docker.env
```

**Step 2. Edit `docker.env`.** At minimum, set these three values:

```dotenv
MASTER_API_TOKEN=your-strong-random-token
VITE_API_KEY=your-strong-random-token   # must match MASTER_API_TOKEN
HOST_UID=1000                            # output of `id -u`
```

**Step 3. Pull images and start the stack:**

```bash
docker compose pull && docker compose up -d
```

The console is available at `http://localhost:3001`. The API is at `http://localhost:5125`.

## Environment Variables

All variables live in `docker.env` (copied from `docker.example.env`).

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `MASTER_API_TOKEN` | Yes | _(none)_ | API authentication token. Set a strong random value. |
| `VITE_API_KEY` | Yes | _(none)_ | Frontend API key. Must match `MASTER_API_TOKEN`. |
| `HOST_UID` | Yes | `1000` | UID the API container runs as. Run `id -u` to find yours. |
| `HOST_GID` | Yes | `1000` | GID the API container runs as. Run `id -g` to find yours. |
| `HOST_USER` | No | `youruser` | Username (informational only). |
| `API_PORT` | No | `5125` | Host port for the API. |
| `CONSOLE_PORT` | No | `3001` | Host port for the console. |
| `CORS_ORIGIN` | No | `*` | CORS allowed origin. Set to your domain in production. |
| `VITE_API_BASE_URL` | No | _(empty)_ | Override the API URL the browser sends requests to. Leave empty when using the nginx proxy (the console proxies `/api/` internally). |
| `DOCKER_GID` | No | `999` | GID of the `docker` group on the host (needed for Web IDE). Run `stat -c '%g' /var/run/docker.sock`. |
| `MONGO_PORT` | No | `27018` | Host port MongoDB is exposed on. |
| `MONGO_INITDB_ROOT_USERNAME` | No | `meeseeks` | MongoDB root username. |
| `MONGO_INITDB_ROOT_PASSWORD` | No | `meeseeks` | MongoDB root password. Change this in production. |

LLM provider keys, Langfuse credentials, and other runtime settings belong in `configs/app.json`, not `docker.env`. See [Configuration](configuration.md).

## Services

The Compose file defines four services:

| Service | Image | Default port | Purpose |
|---------|-------|-------------|---------|
| `api` | `ghcr.io/bearlike/meeseeks-api` | `5125` | Gunicorn + Flask REST API. Runs as `HOST_UID:HOST_GID`. |
| `console` | `ghcr.io/bearlike/meeseeks-console` | `3001` | nginx serving the React SPA. Proxies `/api/` to the API. |
| `mongo` | `mongo:7` | `27018` (host) | MongoDB for session storage and Web IDE state. |
| `ide-proxy` | `nginx:1.27-alpine` | `127.0.0.1:5126` | nginx reverse proxy for per-session code-server containers. |

Both `api` and `console` use **host networking** (`network_mode: host`), so they share `127.0.0.1` with the host. The `ide-proxy` runs on the `meeseeks-ide` bridge network and is bound to loopback by default.

## Named Volumes

| Volume | Mounted at | Contains |
|--------|-----------|---------|
| `api-data` | `/app/data` | Session transcripts and summaries. |
| `mongo-data` | `/data/db` (MongoDB) | MongoDB data files. |
| `plans-data` | `/tmp/meeseeks/plans` | Plan-mode scratch files (survive restarts). |

## Mounting Project Directories

Use a `docker-compose.override.yml` (auto-loaded by Compose) to mount your project directories:

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
# Edit to add your project paths
```

> [!IMPORTANT]
> Mount each project at the **same absolute path** as on the host. `configs/app.json` stores project paths, and they must match inside and outside the container.

**Example `docker-compose.override.yml`:**

```yaml
services:
  api:
    volumes:
      - ./docker/init.d:/app/docker/init.d:ro
      - /home/you/Projects/my-project:/home/you/Projects/my-project
      - /home/you/Projects/another-repo:/home/you/Projects/another-repo
```

## Post-Init Scripts

Scripts in `docker/init.d/` are run inside the `api` container before Gunicorn starts (sorted lexicographically by filename). Scripts are **sourced** (not executed), so they can export environment variables into the API process environment. A failing script logs a warning and allows startup to continue.

The included script:

| Script | Purpose |
|--------|---------|
| `10-git-setup.sh` | If `GITHUB_TOKEN` is set and `gh` is installed, configures `git credential.helper` for non-interactive auth. Also sets `git config --global safe.directory '*'` so volume-mounted repos are trusted regardless of file ownership. |

To add your own scripts, mount the `docker/init.d/` directory in your override file (see example above) and add `.sh` files there. The API image does not need to be rebuilt.

## Runtime Config Injection

The console image generates a `runtime-config.js` file at container startup from environment variables (`VITE_API_BASE_URL`, `VITE_API_KEY`). This means you can change the API URL or key by updating `docker.env` and running `docker compose up -d`. No image rebuild required.

## Web IDE Socket Access

The API container needs access to `/var/run/docker.sock` to spawn sibling code-server containers. The Compose file mounts the socket and adds the container to the `DOCKER_GID` supplementary group. Set `DOCKER_GID` in `docker.env` to the GID of the `docker` group on your host:

```bash
stat -c '%g' /var/run/docker.sock
```

## Building from Source

To build images locally instead of pulling from GHCR:

```bash
docker compose up --build -d
```

## Images

| Image | Purpose |
|-------|---------|
| `ghcr.io/bearlike/meeseeks-api` | REST API (Gunicorn + Flask). |
| `ghcr.io/bearlike/meeseeks-console` | Web console (nginx + React SPA). |
| `ghcr.io/bearlike/meeseeks-base` | Base image with Python, Node, and core packages. Used as build arg for the API image. |

For production TLS, CORS hardening, and observability setup, see [Production Setup](deployment-production.md).
