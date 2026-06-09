# Agents Guide — Mewbo

## MANDATORY: Hydrate before touching files

At the start of every conversation and every non-trivial task, call `ask_question` on `bearlike/Assistant`. Try DeepWiki (`mcp__deepwiki__Deepwiki-OSS-ask_question`) first; fall back to Devin Wiki (`mcp__devin__Devin-Wiki-Personal-ask_question`). Use `read_wiki_structure` → `read_wiki_contents` for deeper exploration. Only read local files after hydration. Include this directive in subagent prompts.

> Wikis lag the repo (miss `mewbo_graph`/#25, cite removed `mewbo_chat`). Use them for intuition; verify structure against local source.

## What Mewbo is

An AI assistant modelled as a conversation state machine with a hierarchical agent hypervisor. Core engine: a single async `ToolUseLoop` bound to an LLM via native `bind_tools`. Sub-agents admitted via `spawn_agent`, tracked by `AgentHypervisor`, resolved into one of four terminal states (`completed`, `failed`, `cancelled`, `rejected`). Interfaces: CLI, web console, REST API, Home Assistant, Nextcloud Talk — all share the core engine. For architecture details, query the wiki.

## Monorepo layering — read before adding a module

Dependencies flow **strictly down** this DAG; never up, not even a lazy `try/except ImportError`:

```
mewbo_core (lean SDK) → mewbo_tools · mewbo_graph → apps
```

- **`mewbo_core`** — lean orchestration SDK: generic, dependency-light primitives only. No product/graph code; heavy optional deps go behind a `mewbo-core[...]` extra.
- **`mewbo_tools`** — subprocess/remote integrations (MCP, LSP, file edit). Deps core only.
- **`mewbo_graph`** — optional capability library (graph, memory, embedding, SCG). Heavy deps behind extras + import-guards. Deps core only; never an app.
- **apps** — thin product surfaces: HTTP routes, wire contracts, transport, glue. Compose libraries; never host a reusable engine.

**Placement rule.** Reusable engine → a library. Orchestration primitive → core. Subprocess/integration → tools. HTTP route/transport → an app. **(1) A reusable engine must never live inside an app. (2) Two apps must never import each other — extract the shared part into a library.**

**"Optional" means both layers:** PEP 621 extras AND a graceful `try/except ImportError` at every import site. Plugins/AgentDefs ship with the library whose substrate they wrap — `mewbo_graph.plugins.{wiki,scg}` are canonical. A library pushes its plugin root via `mewbo_core.plugins.register_builtin_root`; core never imports up to discover it.

## Engineering principles

**KISS & DRY.** Bias toward less code. Search for an existing utility before writing anything custom.

Proven libraries: LiteLLM (LLM+embeddings), Pydantic (validation), Flask-RESTX (API), Rich/Textual (terminal), Langfuse (tracing), Jinja2 (prompts), Tiktoken (tokens), Loguru (logging), langchain-mcp-adapters (MCP), PyMongo (Mongo).

- Validate at definition: Pydantic `field_validator`, `ConfigDict(extra="forbid")`.
- One atomic class per feature: state attrs + class/static methods + DI.
- Smallest diff that solves the problem. No speculative abstractions.
- Tool contracts stable: `AbstractTool`, `ActionStep`, `TaskQueue`, `tool_id`/`operation`/`tool_input`.
- Tests prefer real code paths; stub only I/O boundaries.
- Cross-model tool-calling differences are **normalization concerns** — fix at the LiteLLM/adapter seam, never by detecting text format in the orchestration loop. See `packages/mewbo_core/CLAUDE.md` → "LLM client".
- Gitmoji + Conventional Commits (`✨ feat: ...`). See `.github/git-commit-instructions.md`.
- Never push unless explicitly asked. Treat LLMs as non-deterministic black-box APIs.

## Project instructions loading

`discover_all_instructions()` loads four levels (low→high): user `~/.claude/CLAUDE.md`, project `CLAUDE.md` / `.claude/CLAUDE.md` walking up to git root, rules `.claude/rules/*.md`, local `CLAUDE.local.md`. Subtree discovery walks DOWN from CWD (max depth 5) and indexes nested files for on-demand reading. Add `<!-- mewbo:noload -->` on line 1 to skip auto-loading a heavy file.

## CLAUDE.md tree

Read the deepest file that applies before editing. Every child carries `> ↑ parent · root` at the top.

| Scope | File |
|---|---|
| Engine: tool-use loop, hypervisor, hooks, plugins | `packages/mewbo_core/CLAUDE.md` |
| Integrations: MCP pool, file edit, LSP, Aider | `packages/mewbo_tools/CLAUDE.md` |
| Graph/memory/embedding/SCG substrate | `packages/mewbo_graph/CLAUDE.md` |
| SCG plugin tools (map + search) | `packages/mewbo_graph/src/mewbo_graph/plugins/scg/CLAUDE.md` |
| HTTP API server (routes, channels, Web IDE) | `apps/mewbo_api/CLAUDE.md` |
| MewboWiki — API side | `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` |
| Agentic Search — API side | `apps/mewbo_api/src/mewbo_api/agentic_search/CLAUDE.md` |
| Agentic Search — SCG lifecycle glue | `apps/mewbo_api/src/mewbo_api/agentic_search/scg/CLAUDE.md` |
| Web console (React, shadcn, TanStack Query) | `apps/mewbo_console/CLAUDE.md` *(noload — heavy)* |
| MewboWiki — Console side | `apps/mewbo_console/src/components/wiki/CLAUDE.md` |
| Agentic Search — Console side | `apps/mewbo_console/src/components/agentic_search/CLAUDE.md` |
| MCP server: tools exposing Mewbo to agents | `apps/mewbo_mcp/CLAUDE.md` |
| CLI (Rich/Textual display, agent panel) | `apps/mewbo_cli/CLAUDE.md` |
| Home Assistant conversation agent | `mewbo_ha_conversation/CLAUDE.md` |
| Test patterns + fixtures | `tests/CLAUDE.md` |

## MCP tools — when to use each

- **DeepWiki / Devin Wiki** (`ask_question`, `read_wiki_structure`, `read_wiki_contents`) — primary context source. DeepWiki first, Devin fallback. Handles up to 10 repos at once.
- **Devin session tools** (`devin_session_create`, `devin_session_interact`, etc.) — delegate long-running tasks, manage knowledge notes, schedule automated work. Session IDs need `devin-` prefix when reused.
- **Langfuse** (`mcp__langfuse__*`) — observability. Path: `get_error_count(age)` → `fetch_sessions` → `fetch_traces` → `fetch_trace(id, include_observations=true)` → `fetch_observation(id)`. Trace names: `mewbo-tool-use`, `mewbo-task-master`, `mewbo-context`. `age` in minutes (max 10080). Use `output_mode="full_json_file"` for large payloads.
- **SearXNG** (`searxng_web_search`) + `web_url_read` — current events, docs, errors outside codebase.
- **Context7** (`resolve_library_id` → `query_docs`) — library/framework API docs.

Fire MCP calls in parallel. DeepWiki = "how should it work"; Langfuse = "how did it actually work."

## Debugging sessions

See `apps/mewbo_api/CLAUDE.md` → "Debugging session errors" for the full trace methodology. Quick orientation: MongoDB `db.events.find({session_id}).sort({ts:1})` (port 27018) is authoritative; Langfuse for LLM conversation chain.

## Running, testing, linting

- Tests: `pytest` under `tests/`.
- Install: `uv sync` (core) or `uv sync --all-extras --all-groups` (dev).
- Run: `uv run mewbo` / `uv run mewbo-api` from repo root, or `npm run dev` in `apps/mewbo_console`.
- Config chain: `CWD/configs/` → `$MEWBO_HOME/` → `~/.mewbo/`. Override with `--config`. Run `/init` to scaffold.
- Lint: `ruff check .` (auto-fix: `ruff check --fix .`). Types: `mypy`. Helpers: `make lint`, `make lint-fix`, `make typecheck`, `make precommit-install`.
- **Never blind `ruff --fix`** — always re-run `ruff check .` after any autofix (strips intentional `noqa`).
- **Browser automation / Playwright**: use the warm Chrome on the homelab Selenium Grid — connect via Playwright `connectOverCDP`. See `/home/kk/Agents/homelab/docs/selenium-cdp-guide.md`.
