# Agents Guide - Personal Assistant (Meeseeks)

## What this codebase is
Meeseeks is a multi-agent LLM personal assistant that decomposes user requests into atomic actions, runs them through tools, and returns a synthesized response. It ships multiple interfaces (CLI, chat UI, REST API, Home Assistant) that share the same core engine.

## Core entry points
- `packages/meeseeks_core/src/meeseeks_core/task_master.py`: action planning + task execution loop
- `packages/meeseeks_core/src/meeseeks_core/classes.py`: `ActionStep`, `TaskQueue`, `AbstractTool` contracts
- `packages/meeseeks_tools/src/meeseeks_tools/`: tool implementations and integration glue
- `apps/meeseeks_chat/src/meeseeks_chat/chat_master.py`: Streamlit UI
- `apps/meeseeks_api/src/meeseeks_api/backend.py`: Flask API
- `apps/meeseeks_cli/src/meeseeks_cli/cli_master.py`: terminal CLI
- `meeseeks_ha_conversation/`: Home Assistant integration

## How to get context fast
1. Use the DeepWiki MCP tool on `bearlike/Assistant` for a fast architecture map.
2. Read `README.md` and component READMEs for configuration/runtime details.
3. Use `rg` to locate specific behavior and follow the exact file path.
4. For CI issues, use GitHub Actions logs (GH CLI or MCP GitHub tools).

## MCP tools (use first for external research)
When you need external context (other repos, CI failures, specs, APIs), prefer MCP tools instead of guessing.
- DeepWiki: fast repo architecture/flow Q&A without loading large files.
- GitHub Repos: precise file/commit/issue/PR lookup, and Actions runs/logs for CI debugging.
- Internet Search (SearXNG): broad web lookup for up‑to‑date facts and references.
- Web URL Read: fetch exact page content or headings for accurate summaries.
- Context7 Docs: official library/framework docs and code examples.
- Notifications: send status updates to the human owner when needed.

## Engineering principles (project-specific)
- KISS and DRY: prefer small, obvious changes; remove redundancy instead of adding layers.
- KRY: keep requirements and acceptance criteria in view; do not drift.
- Keep tool contracts stable (`AbstractTool`, `ActionStep`, `TaskQueue`).
- Favor composition and reuse across interfaces; avoid duplicating core logic.
- Add or improve tests for non-trivial behavior; expand coverage when touching core logic or tools.
- Use Gitmoji + Conventional Commit format (e.g., `✨ feat: add session summary pass-through`).
- Do not push unless explicitly requested.
- Use `.github/git-commit-instructions.md` for commit + PR titles and bodies.
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language and describe changes objectively (e.g., “updated prompts/instructions”).
- Keep type hints precise; avoid loosening to `Any` unless no accurate alternative exists.

## Orchestration insights (transferable)
- Separate tool execution from user-facing response: synthesize after tool results, don't dump raw tool output.
- Keep the loop explicit: plan -> act -> observe -> decide; re-plan only when needed.
- Make tool inputs schema-aware; prefer structured arguments for MCP tools.
- Surface tool activity clearly (permissions, tool IDs, arguments) to reduce user confusion.

## Testing patterns (what worked)
- Mock as little as possible; prefer real code paths with stubbed I/O boundaries.
- Cover the full orchestration loop with fake tools and fake LLM outputs.
- Ensure tests fail when tool args are malformed (schema + coercion paths).
- Avoid hidden defaults in tests that mask production behavior.

## Testing & running (common paths)
- Tests live under `tests/` (use `pytest`).
- Local dev uses `uv` and `.env` based on `.env.example`.
- Core-only install: `uv sync`.
- Full dev install: `uv sync --all-extras --all-groups`.
- Run interfaces from repo root with `uv run meeseeks`, `uv run meeseeks-api`, or `uv run meeseeks-chat`.
- Dockerfiles live under `docker/` for base, chat, and API; Compose is supported when needed.

## Linting & formatting
- Primary linting uses `ruff` (root + subpackages). Auto-fix with `.venv/bin/ruff check --fix .`.
- Type checking uses `mypy`. Run from repo root after installing with `uv`.
- `flake8`, `pylint`, and `autopep8` are still available as dev tools (legacy or ad‑hoc use).
- Helper targets: `make lint`, `make lint-fix`, and `make typecheck`.
- Pre-commit hooks are defined in `.pre-commit-config.yaml` (install with `make precommit-install`).

## Expectations for agents
- Start with DeepWiki for overview, then verify details in code.
- Keep changes minimal, readable, and well‑scoped.
- Document assumptions in PRs/notes when behavior is inferred.
