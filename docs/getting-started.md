# Getting Started

This guide walks through local setup, environment configuration, MCP setup, and how to run each interface.

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
- Chat UI: `uv sync --extra chat`
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
```
<emoji> <verb>(<scope>): <message>
```

Pre-push runs:
- `scripts/ci/check.sh` (ruff format/check, mypy, pytest)

## Environment setup
1. Copy `.env.example` to `.env`.
2. Set at least:
   - `OPENAI_API_KEY` (for your OpenAI-compatible endpoint)
   - `OPENAI_API_BASE` (LiteLLM proxy or other OpenAI-compatible base URL)
   - `DEFAULT_MODEL` (or `ACTION_PLAN_MODEL`)
3. If you use an OpenAI-compatible base URL and your model name has no provider
   prefix, Meeseeks will call `openai/<model>` automatically.
4. Optional runtime paths:
   - `MESEEKS_SESSION_DIR` for session transcript storage
   - `MESEEKS_TOOL_MANIFEST` if you want a custom tool list (disables MCP auto-discovery)

## MCP setup (auto-discovery)
MCP tools are auto-discovered from a server config file.
1. Copy `configs/mcp.example.json` to `configs/mcp.json`.
2. Set the MCP server `url` and any `headers` needed for auth.
3. Set `MESEEKS_MCP_CONFIG=./configs/mcp.json` in `.env`.
4. Start any interface once; a tool manifest is auto-generated and cached under `~/.meeseeks/`.

Notes:
If you override the manifest, keep at least one tool enabled for tasks that need external actions.
- MCP tool names must match the server's advertised tool list.

## Optional components
- Langfuse: set `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` (or disable with `LANGFUSE_ENABLED=0`).
- Home Assistant: set `HA_URL` + `HA_TOKEN` (or disable with `MESEEKS_HOME_ASSISTANT_ENABLED=0`).

## Run interfaces (local)
- CLI: `uv run meeseeks`
- API: `uv run meeseeks-api` (or `uv run python -m meeseeks_api.backend`)
- Chat UI: `uv run meeseeks-chat`
- Home Assistant integration: install `meeseeks_ha_conversation/` as a custom component and point it at the API.

## Docker (optional)
- Build images using `docker/Dockerfile.api` and `docker/Dockerfile.chat`.
- Provide the same `.env` values as local.
- Persist `MESEEKS_SESSION_DIR` if you want transcripts across restarts.

## Docs (optional)
If you want to build the docs locally:
```bash
uv sync --all-extras --group docs
uv run mkdocs serve
```
