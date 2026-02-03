# Agents Guide — Personal Assistant (Meeseeks)

## What this codebase is
Meeseeks is a multi‑agent LLM personal assistant that decomposes user requests into atomic actions, runs them through tools, and returns a summarized response. It ships three interfaces (Streamlit chat UI, Flask REST API, Home Assistant integration) that all call the same core engine.

## Core entry points
- `core/task_master.py`: action planning + task execution loop
- `core/classes.py`: `ActionStep`, `TaskQueue`, `AbstractTool` contracts
- `tools/`: tool implementations and integration glue
- `meeseeks-chat/chat_master.py`: Streamlit UI
- `meeseeks-api/backend.py`: Flask API
- `meeseeks_ha_conversation/`: Home Assistant integration

## How to get context fast
1. Use the DeepWiki MCP tool on `bearlike/Personal-Assistant` for a high‑level map of architecture, flows, and interfaces.
2. Read `README.md` and component READMEs for configuration and runtime details.
3. Use `rg` to locate specific behavior (`ActionPlanner`, `TaskMaster`, `tool_dict`, API routes, HA service calls).
4. Open the exact files you need; keep context small and focused.

## MCP tools (use first for external research)
When you need external context (other repos, CI failures, specs, APIs), prefer MCP tools instead of guessing.
- DeepWiki: fast repo architecture/flow Q&A without loading large files.
- GitHub Repos: precise file/commit/issue/PR lookup, and Actions runs/logs for CI debugging.
- Internet Search (SearXNG): broad web lookup for up‑to‑date facts and references.
- Web URL Read: fetch exact page content or headings for accurate summaries.
- Context7 Docs: official library/framework docs and code examples.
- Notifications: send status updates to the human owner when needed.

## Engineering principles (project‑specific)
- KISS and DRY: prefer small, obvious changes; remove redundancy instead of adding layers.
- KRY: keep requirements and acceptance criteria in view; do not drift.
- Keep tool contracts stable (`AbstractTool`, `ActionStep`, `TaskQueue`).
- Favor composition and reuse across interfaces; avoid duplicating core logic.
- Add or improve tests for non‑trivial behavior; expand coverage when touching core logic or tools.
 - Use Gitmoji + Conventional Commit format (e.g., `✨ feat: add session summary pass-through`).
 - Do not push unless explicitly requested.

## Testing & running (common paths)
- Tests live under `tests/` (use `pytest`).
- Local dev uses Poetry and `.env` based on `.env.example`.
- Docker images exist for base, chat, and API; Compose is supported when needed.

## Linting & formatting
- Primary linting uses `ruff` (root + subpackages). Auto-fix with `poetry run ruff check --fix .`.
- Type checking uses `mypy`. Run from repo root for core/tools/HA, and run inside `meeseeks-api/` or `meeseeks-chat/` for those components.
- `flake8`, `pylint`, and `autopep8` are still available as dev tools (legacy or ad‑hoc use).
- Helper targets: `make lint`, `make lint-fix`, and `make typecheck`.
- Pre-commit hooks are defined in `.pre-commit-config.yaml` (install with `make precommit-install`).

## Expectations for agents
- Start with DeepWiki for overview, then verify details in code.
- Keep changes minimal, readable, and well‑scoped.
- Document assumptions in PRs/notes when behavior is inferred.
