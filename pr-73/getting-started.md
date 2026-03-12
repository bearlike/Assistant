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
- API + Web: `uv run meeseeks-api` and `uv run meeseeks-chat` (details in [Web + API](clients-web-api.md))
- Home Assistant: see [Home Assistant voice](clients-home-assistant.md)

## Aider edit blocks (local tool)
The edit-block tool expects strict SEARCH/REPLACE blocks and returns format guidance on mismatches.

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
