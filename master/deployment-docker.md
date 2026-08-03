# Docker Compose Deployment

The recommended way to run Mewbo in a persistent environment. Pre-built images for the API, console, and base layer are published to GHCR. A single `docker compose up` starts the full stack. It includes the API, the MCP server, the console, MongoDB, the Web IDE broker, and the nginx proxy for the Web IDE feature.

## Quick Start

**Step 1. Copy the environment template:**

```bash
cp .env.example .env
```

**Step 2. Edit `.env`.** At minimum, set these three values:

```dotenv
MEWBO_MASTER_API_TOKEN=your-strong-random-token
MEWBO_VITE_API_KEY=your-strong-random-token   # must match MEWBO_MASTER_API_TOKEN
MEWBO_HOST_UID=1000                            # output of `id -u`
```

**Step 3. Pull images and start the stack:**

```bash
docker compose pull && docker compose up -d
```

The console is available at `http://localhost:3001`. The API is at `http://localhost:5125`. The MCP server listens on `http://localhost:5127`.

## Environment Variables

All variables live in `.env` (copied from [`.env.example`](repo:.env.example)) — the one environment file every compose service reads via `env_file: ${MEWBO_ENV_FILE:-.env}`. Deployment-facing variables carry a `MEWBO_` prefix so they can't collide with an unrelated variable of the same short name in your shell or CI; `MONGO_INITDB_ROOT_USERNAME`/`MONGO_INITDB_ROOT_PASSWORD` keep their un-prefixed names because they're read by the official `mongo` image, not by Mewbo.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `MEWBO_MASTER_API_TOKEN` | Yes | _(none)_ | API authentication token. Set a strong random value. |
| `MEWBO_VITE_API_KEY` | Yes | _(none)_ | Frontend API key. Must match `MEWBO_MASTER_API_TOKEN`. |
| `MEWBO_HOST_UID` | Yes | `1000` | UID the API container runs as. Run `id -u` to find yours. |
| `MEWBO_HOST_GID` | Yes | `1000` | GID the API container runs as. Run `id -g` to find yours. |
| `MEWBO_API_PORT` | No | `5125` | Host port for the API. |
| `CORS_ORIGIN` | No | `*` | CORS allowed origin. Set to your domain in production. |
| `MEWBO_VITE_API_BASE_URL` | No | _(empty)_ | Override the API URL the browser sends requests to. Leave empty when using the nginx proxy (the console proxies `/api/` internally). |
| `MEWBO_DOCKER_GID` | No | `999` | GID of the `docker` group on the host, needed by the `mewbo-ide` broker (the only service that touches `/var/run/docker.sock` — see "Web IDE" below). Run `stat -c '%g' /var/run/docker.sock`. |
| `MEWBO_MONGO_PORT` | No | `27018` | Host port MongoDB is exposed on. |
| `MONGO_INITDB_ROOT_USERNAME` | No | `mewbo` | MongoDB root username. |
| `MONGO_INITDB_ROOT_PASSWORD` | No | `mewbo` | MongoDB root password. Change this in production. |
| `MEWBO_AGENTIC_SEARCH_SEED` | No | `1` | Agentic Search demo-data seeding. Set `0` to disable it, so a production install lists only workspaces and sources you actually configured. |
| `MEWBO_IDE_BROKER_TOKEN` | Yes, to use Web IDE | _(none)_ | Shared secret the API and the `mewbo-ide` broker both read from this file — distinct from `MEWBO_MASTER_API_TOKEN`. The broker refuses to boot without it. |
| `MEWBO_IDE_ALLOWED_ROOTS` | Yes, to use Web IDE | _(none)_ | Colon-separated absolute paths the broker will launch an IDE against. The broker refuses to boot without it — see "Web IDE" below for the matching mount requirement. |
| `MEWBO_IDE_BROKER_URL` | No | `http://127.0.0.1:5128` | Already set on the `api` service in [`docker-compose.yml`](repo:docker-compose.yml); override only if you moved the broker off its default port. |

The console's host port is fixed at `3001` — it runs on host networking rather than a configurable port mapping, so there is no `PORT` variable for it.

LLM provider keys, Langfuse credentials, and other runtime settings belong in `configs/app.json`, not `.env` — though a value there can also *name* a variable from this file instead of embedding a literal (see "Referencing `.env` from `configs/app.json`" below). See [Configuration](configuration.md).

## Referencing `.env` from `configs/app.json`

A `configs/app.json` value can name an environment variable instead of embedding a literal, by writing exactly `${VARIABLE_NAME}` as the whole value — a value that merely contains `${` elsewhere is read as a literal string, no escaping needed. This is how a secret like the master token reaches `configs/app.json` without ever being written into it:

```json
{ "api": { "master_token": "${MEWBO_MASTER_API_TOKEN}" } }
```

A referenced variable that isn't set is refused at startup rather than silently read as empty; a variable that's set to the empty string resolves to empty, since it *is* set.

## Services

The [Compose file](repo:docker-compose.yml) defines six services:

| Service | Image | Default port | Purpose |
|---------|-------|-------------|---------|
| `api` | `ghcr.io/bearlike/mewbo-api` | `5125` | Gunicorn + Flask REST API. Runs as `MEWBO_HOST_UID:MEWBO_HOST_GID`. |
| `mewbo-mcp` | `ghcr.io/bearlike/mewbo-mcp` | `5127` | MCP server for external agents. A thin shim that proxies tool calls to the API. |
| `console` | `ghcr.io/bearlike/mewbo-console` | `3001` | nginx serving the React SPA. Proxies `/api/` to the API. |
| `mongo` | `mongo:7` | `27018` (host) | MongoDB for session storage and Web IDE state. |
| `mewbo-ide` | `ghcr.io/bearlike/mewbo-ide` | `127.0.0.1:5128` | Web IDE broker — the only service holding `/var/run/docker.sock`; launches and reaps the per-session code-server containers on the API's behalf. See "Web IDE" below. |
| `ide-proxy` | `nginx:1.27-alpine` | `127.0.0.1:5126` | nginx reverse proxy for per-session code-server containers. |

The `api`, `mewbo-mcp`, and `console` services use **host networking** (`network_mode: host`), so they share `127.0.0.1` with the host — this is how `api` reaches `mewbo-ide` at `http://127.0.0.1:5128` with no network change. `mewbo-ide` publishes its port to loopback the same way; it talks to the daemon over the mounted socket, not over the `mewbo-ide` bridge network, so it does not need to join that network itself. `ide-proxy` is on the `mewbo-ide` bridge network (also bound to loopback by default) so it can reach the per-session code-server containers the broker attaches to that same network.

The `mewbo-mcp` service reads the same `.env` as the API, so its `MEWBO_MASTER_API_TOKEN` always matches. It also mounts the same `api-data` volume and so shares the API's key store. API keys issued via [`POST /api/keys`](endpoint:POST /api/keys) are therefore valid on the MCP server too.

## Named Volumes

| Volume | Mounted at | Contains |
|--------|-----------|---------|
| `api-data` | `/app/data` | Session transcripts and summaries. Shared with `mewbo-mcp` for the key store. |
| `mongo-data` | `/data/db` (MongoDB) | MongoDB data files. |
| `plans-data` | `/tmp/mewbo/plans` | Plan-mode scratch files (survive restarts). |
| `wiki-clones` | `/tmp/mewbo/wiki/clones` | Git clones made by wiki indexing (survive restarts). |

All named volumes survive `docker compose down`. They are cleared only by `docker compose down -v` or `docker volume rm`.

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

Scripts in [`docker/init.d/`](repo:docker/init.d) are run inside the `api` container before Gunicorn starts (sorted lexicographically by filename). Scripts are **sourced** (not executed), so they can export environment variables into the API process environment. A failing script logs a warning and allows startup to continue.

The included scripts:

| Script | Purpose |
|--------|---------|
| [`00-toolbox.sh`](repo:docker/init.d/00-toolbox.sh) | Defines the shared download and install helpers the later scripts use. It provisions nothing itself, and runs first so every script sourced after it inherits the helpers. |
| [`10-git-setup.sh`](repo:docker/init.d/10-git-setup.sh) | If `GITHUB_TOKEN` is set and `gh` is installed, configures `git credential.helper` for non-interactive auth. Also sets `git config --global safe.directory '*'` so volume-mounted repos are trusted regardless of file ownership. |
| [`12-agent-clis.sh`](repo:docker/init.d/12-agent-clis.sh) | Installs the forge CLIs `gh` and `tea` into the runtime toolbox. Both are static release binaries, so neither needs apt or root. Pin a version from `.env` when you need one. |
| [`13-models-cli.sh`](repo:docker/init.d/13-models-cli.sh) | Installs the `models` CLI, which browses model pricing, benchmarks, and provider status. It carries no credentials. |
| [`15-tea-setup.sh`](repo:docker/init.d/15-tea-setup.sh) | Derives `tea` logins from the tokens already available in the container. Runs after `12-` because that script installs the binary this one authenticates. A host that is not a Gitea instance is skipped with a warning rather than failing startup. |

Identity is acquired at runtime rather than baked into the image, which is why the CLIs install here instead of in the Dockerfile: they are inert without the credentials that only arrive at start.

To add your own scripts, mount the [`docker/init.d/`](repo:docker/init.d) directory in your override file (see example above) and add `.sh` files there. The API image does not need to be rebuilt. A common addition is a script that trusts a private root CA: drop the PEM at `/usr/local/share/ca-certificates/<name>.crt` (the `.crt` extension is required), run `update-ca-certificates`, and export `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` so `git clone` and Python HTTPS clients verify against it instead of disabling verification.

## Runtime Config Injection

The console image generates a `runtime-config.js` file at container startup from environment variables (`MEWBO_VITE_API_BASE_URL`, `MEWBO_VITE_API_KEY`). This means you can change the API URL or key by updating `.env` and running `docker compose up -d`. No image rebuild required.

## Web IDE

The `api` container has no access to `/var/run/docker.sock` and no Docker tooling at all — it runs the agent's arbitrary shell tool, so co-locating daemon access there would let any shell command reach the whole host as root (`docker run -v /:/host`). Spawning the per-session code-server container is instead delegated over HTTP to the dedicated `mewbo-ide` broker service, which is the **only** thing in the stack that mounts the socket. The API selects this path automatically once `MEWBO_IDE_BROKER_TOKEN` is set (both services read it from `.env`) and `MEWBO_IDE_BROKER_URL` points at it (already set in [`docker-compose.yml`](repo:docker-compose.yml)); without a broker configured, the API falls back to talking to a local Docker daemon directly, for local development only.

The security property this buys: the container running an agent's shell has no path to the Docker daemon. A compromised or misbehaving agent session cannot reach the socket even in principle — it would have to compromise the separate `mewbo-ide` broker process first, and that process accepts only the narrow request shape in `mewbo_ide`'s API contract (a workspace path, a TTL, a password), never an arbitrary container spec.

To enable it:

1. Set `MEWBO_IDE_BROKER_TOKEN` in `.env` to a strong random secret.
2. Set `MEWBO_DOCKER_GID` in `.env` to the GID of the `docker` group on your host:
   ```bash
   stat -c '%g' /var/run/docker.sock
   ```
3. Set `MEWBO_IDE_ALLOWED_ROOTS` in `.env` to a colon-separated list of the absolute project paths you want to allow launching an IDE against.
4. Mount each of those same paths, **read-only**, into the `mewbo-ide` service at the identical path — see [`docker-compose.override.example.yml`](repo:docker-compose.override.example.yml). This is not optional: the broker realpath-resolves a requested workspace path in its own mount namespace to catch a symlink escape, so a path it cannot see always fails validation even if the string is allowlisted.

The broker binds to `127.0.0.1:5128` — loopback only, never published off-host, since anyone who can reach it can ask the daemon to launch an arbitrary container as root.

## Building from Source

To build images locally instead of pulling from GHCR:

```bash
docker compose up --build -d
```

## Images

| Image | Purpose |
|-------|---------|
| `ghcr.io/bearlike/mewbo-api` | REST API (Gunicorn + Flask). |
| `ghcr.io/bearlike/mewbo-mcp` | MCP server (port 5127). Exposes Mewbo to external agents. |
| `ghcr.io/bearlike/mewbo-console` | Web console (nginx + React SPA). |
| `ghcr.io/bearlike/mewbo-ide` | Web IDE broker (Node). The only image with Docker socket access. |
| `ghcr.io/bearlike/mewbo-base` | Base image with Python, Node, and core packages. Used as build arg for the API and MCP images. |

For production TLS, CORS hardening, and observability setup, see [Production Setup](deployment-production.md).
