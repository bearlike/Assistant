# Get Started

Meeseeks runs as a local CLI, a REST API + web console, or a containerised stack. Pick the path that fits your setup.

## Prerequisites {#prerequisites}

| Requirement | Notes |
|-------------|-------|
| Python 3.10+ | [uv](https://docs.astral.sh/uv/) manages the virtualenv |
| Node.js 18+ | Console frontend only |
| Docker | Optional, for container deployment |

## Installation {#installation}

### CLI {#cli-setup}

The CLI is the fastest way to start a local session.

```bash
uv sync --extra cli
uv run meeseeks
```

For a global install so `meeseeks` works from anywhere:
```bash
uv tool install .
```

See [CLI client](clients-cli.md) for usage details, slash commands, and approval modes.

### API + Console {#api-setup}

The REST API and web console run as two separate processes.

```bash
# Install Python dependencies
uv sync --extra api

# Install console frontend dependencies (once)
cd apps/meeseeks_console && npm install && cd -

# Start the API
uv run meeseeks-api

# Start the console (separate terminal)
cd apps/meeseeks_console && npm run dev
```

The console proxies `/api/` requests to the API at `127.0.0.1:5125` by default.

See [Console + API](clients-web-api.md) for configuration, auth, and session management.

### Home Assistant {#ha-setup}

Adds the Home Assistant tool for smart-home control via the CLI or API.

```bash
uv sync --extra ha
```

Then set `home_assistant.enabled` and credentials in `configs/app.json`. See [Home Assistant](clients-home-assistant.md).

### All extras (developer) {#dev-setup}

Installs every optional component plus test/docs tooling.

```bash
uv sync --all-extras --all-groups
```

| Extra | What it adds |
|-------|-------------|
| `cli` | Terminal UI, Rich approval dialogs |
| `api` | Flask REST API, Flask-RESTX, Gunicorn |
| `ha` | Home Assistant integration |
| `tools` | Full MCP tools bundle |
| `--all-extras` | All of the above |
| `--all-groups` | Dev + test + docs groups |

## Configuration {#configuration}

Meeseeks loads config files from the first directory that contains them, checked in order:

1. `CWD/configs/`: project-local config (highest priority)
2. `$MEESEEKS_HOME/`: custom home dir if set
3. `~/.meeseeks/`: user home fallback

Bootstrap from the example template:

```bash
cp configs/app.example.json configs/app.json
```

The two files you'll edit most:

| File | Purpose |
|------|---------|
| `configs/app.json` | Runtime settings, LLM keys, integrations |
| `configs/mcp.json` | MCP server definitions |

To scaffold both from scratch, run `/init` from the CLI after a bare `uv sync`.

- For LLM provider keys and model selection: [LLM Setup](llm-setup.md)
- For every config key: [Configuration Reference](configuration.md)

## First run {#first-run}

After copying and editing `configs/app.json`:

```bash
uv run meeseeks
```

On first run, type `/init` to scaffold any missing config files. A minimal working `configs/app.json`:

```json
{
  "llm": {
    "api_key": "sk-ant-xxxxxxxx",
    "default_model": "anthropic/claude-sonnet-4-6"
  }
}
```

All other keys have sensible defaults. The CLI will prompt for approval before any write or shell operation.

## Docker quick-start {#docker-quickstart}

Pre-built images are published to GHCR. This is the fastest path to a production-ready stack.

```bash
# 1. Create your environment file and edit the three required vars
cp docker.example.env docker.env

# 2. Pull and start
docker compose pull && docker compose up -d
```

Required variables in `docker.env`:

| Variable | Purpose |
|----------|---------|
| `MASTER_API_TOKEN` | API authentication token |
| `VITE_API_KEY` | Frontend key (must match `MASTER_API_TOKEN`) |
| `HOST_UID` / `HOST_GID` | Host user/group IDs (`id` to find yours) |

See [Docker Compose](deployment-docker.md) for the full reference: volume mounts, project directories, init scripts, reverse proxy, and runtime config.

## Project instructions {#project-instructions}

Meeseeks discovers `CLAUDE.md`, `AGENTS.md`, and `.claude/rules/*.md` files
automatically. The discovery is compatible with the Claude Code and AGENTS.md conventions.

Place a `CLAUDE.md` at your project root and it will be loaded at session start.
Nested packages are handled via an on-demand index: sub-directory instruction files
are listed in the system prompt as paths; their content is fetched via `read_file`
tool calls only when work reaches those directories.

See [Project Configuration](project-configuration.md) for the full loading strategy:
four priority levels, the upward/downward pass mechanics, the noload marker, and
how project-level `.mcp.json` files are merged.

## Git hooks {#git-hooks}

Use the repo-managed hook set to enforce commit message format and block pushes that fail linting or tests.

```bash
git config core.hooksPath scripts/githooks
```

Optional: install `pre-commit` hooks for local enforcement:
```bash
make precommit-install
```

Commit message format:
```text
<emoji> <verb>(<scope>): <message>
```

Pre-push checks run `scripts/ci/check.sh`. This covers ruff format/check, mypy, and pytest.

## Next steps {#next-steps}

- [Configure your LLM](llm-setup.md): set provider keys and pick a model
- [CLI client](clients-cli.md): slash commands, approval modes, global install
- [Console + API](clients-web-api.md): web UI, REST API, session management
- [Configuration Reference](configuration.md): every config key with defaults
- [Features overview](features-builtin-tools.md): built-in tools, sub-agents, skills, plugins
