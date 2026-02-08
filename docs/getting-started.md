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

## Configuration setup
1. If configs are missing, run `/config init`, `/mcp init`, or `/init` from the CLI to scaffold examples.
2. Use only JSON configs under `configs/`:
   - `configs/app.json` (runtime + LLM + integrations)
   - `configs/mcp.json` (MCP servers)
   - `configs/*.example.json` are templates for new installs
3. Update `configs/app.json` with your runtime settings:
   - `llm.api_key` and `llm.api_base` (required)
   - `llm.default_model` and/or `llm.action_plan_model`
   - Optional: `llm.tool_model` for tool execution (falls back to `action_plan_model`, then `default_model`)
   - `runtime.session_dir` (optional, for transcript storage)
4. If you use an OpenAI-compatible base URL and your model name has no provider
   prefix, Meeseeks will call `openai/<model>` automatically.

## MCP setup (auto-discovery)
MCP tools are auto-discovered from `configs/mcp.json`.
1. Set each MCP server `url` and any `headers` needed for auth.
2. Start any interface once; a tool manifest is auto-generated and cached under `~/.meeseeks/`.

## Optional components
- Langfuse: set `langfuse.enabled` + keys in `configs/app.json`.
- Home Assistant: set `home_assistant.enabled` + credentials in `configs/app.json`.

## CLI approval UI
- Default approval prompts render as a Rich panel with padded, dotted borders.
- Use `/automatic` or `--auto-approve` to bypass prompts when appropriate.

## Run interfaces (local)
- CLI: `uv run meeseeks`
- API: `uv run meeseeks-api` (or `uv run python -m meeseeks_api.backend`)
- Chat UI: `uv run meeseeks-chat`
- Home Assistant integration: install `meeseeks_ha_conversation/` as a custom component and point it at the API.

API notes (polling is API-only; CLI uses the runtime in-process):
- Session runtime endpoints support async runs with polling:
  - `POST /api/sessions` create session
  - `POST /api/sessions/{session_id}/query` enqueue or core commands
  - `GET /api/sessions/{session_id}/events?after=...` poll events
  - `GET /api/sessions` list sessions (defaults to non-archived, non-empty)
  - `GET /api/sessions?include_archived=1` include archived sessions
  - `POST /api/sessions/{session_id}/archive` archive a session
  - `DELETE /api/sessions/{session_id}/archive` unarchive a session
  - `POST /api/query` legacy synchronous endpoint

## Aider edit blocks (local tool)
The edit-block tool expects strict SEARCH/REPLACE blocks and returns format guidance on mismatches.

````
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

## Docker (optional)
- Build images using `docker/Dockerfile.api` and `docker/Dockerfile.chat`.
- Mount `configs/app.json` (and `configs/mcp.json` if you use MCP).
- Persist `data/sessions` if you want transcripts across restarts.

## Docs (optional)
If you want to build the docs locally:
```bash
uv sync --all-extras --group docs
uv run mkdocs serve
```
