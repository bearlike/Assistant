# Installation

This guide covers local installation and environment setup for bearlike/Assistant.

## Prerequisites
- Python 3.10+
- uv
- Docker (optional, for container runs)

## Install dependencies

### User installation (core only)
```bash
uv sync
```

### Optional components (from project root)
- CLI: `uv sync --extra cli`
- API: `uv sync --extra api`
- Console: `cd apps/meeseeks_console && npm install`
- Home Assistant integration: `uv sync --extra ha`
- Tools bundle: `uv sync --extra tools`
- Everything optional: `uv sync --all-extras`

### Developer installation (all components + dev/test/docs)
```bash
uv sync --all-extras --all-groups
```

## Git hooks (recommended)
Use the repo hook set to enforce commit message format and block pushes that fail linting/tests.

Install the repo-managed hooks:
```bash
git config core.hooksPath scripts/githooks
```

Optional: enable pre-commit hooks if you use `pre-commit` locally:
```bash
make precommit-install
```

Commit message format:
```text
<emoji> <verb>(<scope>): <message>
```

Pre-push runs:
- `scripts/ci/check.sh` (ruff format/check, mypy, pytest)

## Configuration setup
1. If configs are missing, run `/config init`, `/mcp init`, or `/init` from the CLI to scaffold examples.
2. Use only JSON configs under `configs/`:
   - `configs/app.json` (runtime + LLM + integrations)
   - `configs/mcp.json` (MCP servers)
   - `configs/*.example.json` are templates for new installs
3. Update `configs/app.json` with your runtime settings.
4. For required LLM keys and a walkthrough, see [LLM setup](llm-setup.md).

## MCP setup
See [LLM setup](llm-setup.md) for MCP configuration and auto-discovery details.

## Optional components
- Langfuse: set `langfuse.enabled` + keys in `configs/app.json`.
- Home Assistant: set `home_assistant.enabled` + credentials in `configs/app.json`.

## CLI approval UI
- Default approval prompts render as a Rich panel with padded, dotted borders.
- Use `/automatic` or `--auto-approve` to bypass prompts when appropriate.

## Run interfaces (local)
- CLI: `uv run meeseeks` (details in [CLI client](clients-cli.md))
- CLI (global): `uv tool install .` then `meeseeks` from anywhere (see [CLI client](clients-cli.md))
- API: `uv run meeseeks-api` (details in [Console + API](clients-web-api.md))
- Console: `cd apps/meeseeks_console && npm run dev` (details in [Console + API](clients-web-api.md))
- Home Assistant: see [Home Assistant voice](clients-home-assistant.md)

## File edit tool

Meeseeks ships two file-editing mechanisms. Set `agent.edit_tool` in `configs/app.json` to choose which one is active:

| Value | Mechanism | Best for |
|-------|-----------|----------|
| `"search_replace_block"` (default) | Aider-style SEARCH/REPLACE blocks | Models trained on diff/patch formats; multi-file edits in one call |
| `"structured_patch"` | Per-file `file_path` + `old_string` / `new_string` | Models that perform better with explicit per-file parameters (e.g., smaller or instruction-tuned models) |

```json
{ "agent": { "edit_tool": "structured_patch" } }
```

Research shows that edit format significantly impacts LLM coding accuracy — the same model can vary by 20+ percentage points depending on the edit schema it's given ([Aider benchmarks](https://aider.chat/docs/more/edit-formats.html), [EDIT-Bench](https://arxiv.org/abs/2511.04486)). For example, a model struggling with SEARCH/REPLACE block syntax (misplacing the filename line, botching the fence markers) may succeed immediately when given the simpler `old_string` / `new_string` interface. This setting lets you match the edit format to whatever works best for your chosen model.

Both mechanisms produce identical output (`kind: "diff"` with a unified diff), so the web console, CLI, and API all render edits the same way regardless of which mechanism is active.

### SEARCH/REPLACE blocks (`search_replace_block`)

````text
<path>
```text
<<<<<<< SEARCH
<exact text to match>
=======
<replacement text>
>>>>>>> REPLACE
```
````

Rules:
- Filename line immediately before the opening fence.
- SEARCH must match exactly (including whitespace/newlines).
- Use a line with `...` in both SEARCH and REPLACE to skip unchanged sections.

### Structured patch (`structured_patch`)

The LLM calls the tool with three parameters:
- `file_path` — path to the file to edit.
- `old_string` — exact text to find (empty to create a new file).
- `new_string` — replacement text.
- `replace_all` — (optional, default false) replace all occurrences.

One file per call. The tool fails with guidance if the match is ambiguous.

## Docker Compose deployment

The API and web console ship as container images published to GHCR. A `docker-compose.yml` in the repo root orchestrates both services with host networking.

### Quick start

```bash
# 1. Create your environment file
cp docker.example.env docker.env
```

Edit `docker.env` with your values:

| Variable | Purpose | Required |
|----------|---------|----------|
| `MASTER_API_TOKEN` | API authentication token | Yes |
| `VITE_API_KEY` | Frontend API key (should match `MASTER_API_TOKEN`) | Yes |
| `HOST_UID` / `HOST_GID` | Host user/group IDs (run `id` to find yours) | Yes |
| `API_PORT` | API server port (default: `5125`) | No |
| `CONSOLE_PORT` | Console port (default: `3001`) | No |
| `CORS_ORIGIN` | Allowed CORS origin (default: `*`) | No |
| `VITE_API_BASE_URL` | Override frontend API URL (leave empty when using nginx proxy) | No |

```bash
# 2. Pull pre-built images from GHCR and start (recommended)
docker compose pull && docker compose up -d
```

To build from source instead (e.g., for local development):
```bash
docker compose up --build -d
```

### How it works

- **API** (`ghcr.io/bearlike/meeseeks-api`) — Gunicorn serving the Flask REST API on port `5125`. Single worker with 8 threads.
- **Console** (`ghcr.io/bearlike/meeseeks-console`) — Nginx serving the React SPA on port `3001`. Proxies `/api/` requests to the API at `127.0.0.1:5125`.
- Both services use **host networking** so they share `127.0.0.1`.
- The API image is built on top of `ghcr.io/bearlike/meeseeks-base` which includes Python, Node.js, and the core/tools packages.

### Project directories & overrides

Create a `docker-compose.override.yml` (auto-loaded by Compose) to mount your project directories and init scripts. A template is provided:

```bash
cp docker-compose.override.example.yml docker-compose.override.yml
```

Mount each project at **the same path** as on the host so `configs/app.json` paths work without translation:

```yaml
services:
  api:
    volumes:
      - ./docker/init.d:/app/docker/init.d:ro
      - /home/you/Projects/my-repo:/home/you/Projects/my-repo
```

### Post-init scripts

The API entrypoint runs all `*.sh` scripts in `docker/init.d/` (sorted by filename) before starting Gunicorn. Scripts are sourced so they can export env vars; a failing script logs a warning but does not block startup.

The included `10-git-setup.sh` configures git to authenticate via `gh` CLI (using `GITHUB_TOKEN`) and trusts all volume-mounted repos (`safe.directory *`). Add your own numbered scripts to the same directory — only `10-git-setup.sh` is version-controlled; the rest are gitignored.

### Runtime configuration

- Mount `configs/app.json` and `configs/mcp.json` (read-only) for runtime + MCP settings.
- The `api-data` volume persists session transcripts at `/app/data`.
- The console generates `runtime-config.js` at startup from environment variables — no rebuild needed to change API URLs or keys.

### Production reverse proxy

For TLS termination, a sample nginx config is provided at `docker/nginx-reverse-proxy.conf`. It proxies both the console and API behind a single domain with SSE-aware buffering settings for the streaming endpoints.

## Docs (optional)
If you want to build the docs locally:
```bash
uv sync --all-extras --group docs
uv run mkdocs serve
```
