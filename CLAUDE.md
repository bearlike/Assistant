# Agents Guide — Mewbo

## MANDATORY: Hydrate before touching files

At the start of every conversation and every non-trivial task, ask a configured code-wiki MCP tool a question about `bearlike/Assistant` — try the primary configured one first, falling back to a secondary if one is configured. Use `read_wiki_structure` → `read_wiki_contents` for deeper exploration. Only read local files after hydration. Include this directive in subagent prompts.

> Wikis lag the repo (miss `mewbo_graph`, cite removed `mewbo_chat`). Use them for intuition; verify structure against local source.

## What Mewbo is

An AI assistant modelled as a conversation state machine with a hierarchical agent hypervisor. Core engine: a single async `ToolUseLoop` bound to an LLM via native `bind_tools`. Sub-agents admitted via `spawn_agent`, tracked by `AgentHypervisor`, resolved into one of four terminal states (`completed`, `failed`, `cancelled`, `rejected`). Interfaces: CLI, web console, REST API, Home Assistant, Nextcloud Talk — all share the core engine. For architecture details, query the wiki.

## Monorepo layering — read before adding a module

Dependencies flow **strictly down** this DAG; never up, not even a lazy `try/except ImportError`:

```
mewbo_core (lean SDK) → mewbo_tools · mewbo_graph · mewbo_iam → apps
```

- **`mewbo_core`** — lean orchestration SDK: generic, dependency-light primitives only. No product/graph code; heavy optional deps go behind a `mewbo-core[...]` extra.
- **`mewbo_tools`** — subprocess/remote integrations (MCP, LSP, file edit). Deps core only.
- **`mewbo_graph`** — optional capability library (graph, memory, embedding, SCG). Heavy deps behind extras + import-guards. Deps core only; never an app.
- **`mewbo_iam`** — optional identity kernel (principals, authenticators, roles, teams, grants, audit). Base deps are core + pydantic and NOTHING else; the network/crypto provider legs sit behind the `oidc`/`ldap`/`saml` extras. Deps core only; never an app.
- **apps** — thin product surfaces: HTTP routes, wire contracts, transport, glue. Compose libraries; never host a reusable engine.

**Placement rule.** Reusable engine → a library. Orchestration primitive → core. Subprocess/integration → tools. HTTP route/transport → an app. **(1) A reusable engine must never live inside an app. (2) Two apps must never import each other — extract the shared part into a library.**

**"Optional" means both layers:** PEP 621 extras AND a graceful `try/except ImportError` at every import site. Plugins/AgentDefs ship with the library whose substrate they wrap — `mewbo_graph.plugins.{wiki,scg}` are canonical. A library pushes its plugin root via `mewbo_core.plugins.register_builtin_root`; core never imports up to discover it.

## Engineering principles

**KISS & DRY.** Bias toward less code. Search for an existing utility before writing anything custom.

Proven libraries: LiteLLM (LLM+embeddings), Pydantic (validation), Flask-RESTX (API), Rich/Textual (terminal), Langfuse (tracing), Jinja2 (prompts), Tiktoken (tokens), Loguru (logging), langchain-mcp-adapters (MCP), PyMongo (Mongo).

- **Strict Pydantic contracts at every trust boundary.** Anything crossing one — LLM tool arguments, HTTP request/response bodies, config files, persisted documents — is a Pydantic model with `ConfigDict(extra="forbid")`, validated AT DEFINITION (`field_validator` / `model_validator`). `extra="forbid"` is load-bearing, not hygiene: it is what turns a client smuggling a `token`, a `slug` rename, or a server-owned field into a clean 400 instead of a silent no-op (`wiki/settings.py:ProjectSettingsPatch`, `git_credentials_routes.py:CredentialUpsert`).
- **Behavior intrinsic to the data lives ON the model.** A family of variants is a **discriminated union** whose members own their own validators + strategy methods — NEVER a service-side `if kind ==` switch, which drifts out of sync the moment a variant gains a field. `mewbo_core/triggers/spec.py` is canonical: five `TriggerSpec` kinds each owning its `next_fire_at`/`matches`/`verify`, one `Field(discriminator="kind")` parse seam, zero dispatch anywhere. **Models never import I/O** — the clock, a webhook's headers/body, a normalized CI/PR payload arrive as method ARGS (also why a test injects a fixed `NOW` instead of patching a clock).
- **One atomic class per feature: state + behavior together, collaborators by DI.** Clients, stores, policies, clocks and auth guards are injected as FIELDS (injecting the clock is what makes a watcher testable without sleeps). **No root-level module logic functions** — a helper belongs on the class that owns the state it reads. Module-level CONSTANTS and thin factory aliases (`create_*_store`, `parse_trigger = TriggerSpec.parse`) are fine: data and ecosystem convention, not logic.
- **The Pydantic rule stops at the process boundary.** Hot in-process runtime state (`RunHandle`, `AgentHandle`, live loop bookkeeping) stays a plain dataclass/atomic class — it crosses no trust boundary, so validating every mutation buys nothing but hot-path cost.
- **Import-cycle cure = an EXISTING atomic home, never a new bare-function module.** When two modules need a shared constant/helper and either import direction cycles, add it as a member of the atomic class both already import. The terminated-session 410 envelope lives on `ApiResponseKit` (`apps/mewbo_api/responses.py`) as `TERMINATED_ERROR_BODY` + `terminated_response()` for exactly this reason — an `errors.py` of loose functions would have re-opened the `backend.py` ↔ `triggers/routes.py` cycle it was meant to break.
- Smallest diff that solves the problem. No speculative abstractions.
- Tool contracts stable: `AbstractTool`, `ActionStep`, `TaskQueue`, `tool_id`/`operation`/`tool_input`.
- Tests prefer real code paths; stub only I/O boundaries.
- Cross-model tool-calling differences are **normalization concerns** — fix at the LiteLLM/adapter seam, never by detecting text format in the orchestration loop. See `packages/mewbo_core/CLAUDE.md` → "LLM client".
- **Every git subprocess goes through the hardened executor** (`mewbo_graph.plugins.wiki.clone` — `run_git_with_chain` + the shared argv/env builders), and every credential resolves through the ONE chain in `mewbo_graph.wiki.credentials`. A hand-rolled `git` call re-opens the credential-helper trap that masks real auth errors. See `packages/mewbo_graph/CLAUDE.md` → "Git auth".
- Gitmoji + Conventional Commits (`✨ feat: ...`). See `.github/git-commit-instructions.md`.
- Never push unless explicitly asked. Treat LLMs as non-deterministic black-box APIs.

## Project instructions loading

`discover_all_instructions()` loads four levels (low→high): user `~/.claude/CLAUDE.md`, project `CLAUDE.md` / `.claude/CLAUDE.md` walking up to git root, rules `.claude/rules/*.md`, local `CLAUDE.local.md`. Subtree discovery walks DOWN from CWD (max depth 5) and indexes nested files for on-demand reading. Add `<!-- mewbo:noload -->` on line 1 to skip auto-loading a heavy file.

**`CLAUDE.local.md` (untracked, gitignored).** Holds machine- and environment-specific instructions that are not part of this repository. **If it exists at the repo root, read it before starting work** — it is the highest-precedence layer and overrides this file. Being gitignored it never arrives via clone or a new worktree, so a fresh checkout simply won't have one; nothing in it is required to build, test, or run Mewbo. `.grove/config.json` seeds it into new Grove workspaces for the same reason.

## CLAUDE.md tree

Read the deepest file that applies before editing. Every child carries `> ↑ parent · root` at the top.

| Scope | File |
|---|---|
| Engine: tool-use loop, hypervisor, hooks, plugins | `packages/mewbo_core/CLAUDE.md` |
| Integrations: MCP pool, file edit, LSP, Aider | `packages/mewbo_tools/CLAUDE.md` |
| Graph/memory/embedding/SCG substrate | `packages/mewbo_graph/CLAUDE.md` |
| SCG plugin tools (map + search) | `packages/mewbo_graph/src/mewbo_graph/plugins/scg/CLAUDE.md` |
| Identity kernel: principals, authenticators, roles, teams, grants, audit | `packages/mewbo_iam/CLAUDE.md` |
| HTTP API server (routes, channels, Web IDE) | `apps/mewbo_api/CLAUDE.md` |
| MewboWiki — API side | `apps/mewbo_api/src/mewbo_api/wiki/CLAUDE.md` |
| Agentic Search — API side | `apps/mewbo_api/src/mewbo_api/agentic_search/CLAUDE.md` |
| Agentic Search — SCG lifecycle glue | `apps/mewbo_api/src/mewbo_api/agentic_search/scg/CLAUDE.md` |
| Mewbo Apps — API side (stores, lifecycle, routes, SDK injection) | `apps/mewbo_api/src/mewbo_api/apps/CLAUDE.md` |
| Web console (React, shadcn, TanStack Query) | `apps/mewbo_console/CLAUDE.md` *(noload — heavy)* |
| Console NavRail — the single navigation surface | `apps/mewbo_console/src/components/nav-rail/CLAUDE.md` |
| MewboWiki — Console side | `apps/mewbo_console/src/components/wiki/CLAUDE.md` |
| Agentic Search — Console side | `apps/mewbo_console/src/components/agentic_search/CLAUDE.md` |
| Mewbo Apps — Console side (gallery/detail, stlite app panel) | `apps/mewbo_console/src/components/apps/CLAUDE.md` |
| MCP server: tools exposing Mewbo to agents | `apps/mewbo_mcp/CLAUDE.md` |
| CLI (Rich/Textual display, agent panel) | `apps/mewbo_cli/CLAUDE.md` |
| Aura Android client (Compose, orb overlay, redroid dev loop) | `apps/mewbo_aura/CLAUDE.md` |
| Home Assistant conversation agent | `apps/mewbo_ha_conversation/CLAUDE.md` |
| Demo-as-code: seeded demo stack + artifact rendering | `demo/CLAUDE.md` |
| Test patterns + fixtures | `tests/CLAUDE.md` |
| Docs site: code-ref badges, Scalar, authoring | `docs/CLAUDE.md` |

## MCP tools — when to use each

- **Code-wiki MCP tools** (`ask_question`, `read_wiki_structure`, `read_wiki_contents`) — primary context source. Try the primary configured wiki tool first, fall back to a secondary if configured. Handles up to 10 repos at once.
- **Remote coding-agent session tools** (`*_session_create`, `*_session_interact`, etc.) — delegate long-running tasks, manage knowledge notes, schedule automated work. Session IDs need a provider-specific prefix when reused.
- **Langfuse** (`mcp__langfuse__*`) — observability. Path: `get_error_count(age)` → `fetch_sessions` → `fetch_traces` → `fetch_trace(id, include_observations=true)` → `fetch_observation(id)`. Filter by `sessionId` == Mewbo session_id. `mewbo-tool-use` arrives as a trace TAG (trace name is `step:N`); `mewbo-task-master`/`mewbo-context` are `user_id` values, not trace names. `age` in minutes (max 10080). Use `output_mode="full_json_file"` for large payloads.
- **SearXNG** (`searxng_web_search`) + `web_url_read` — current events, docs, errors outside codebase.
- **Context7** (`resolve_library_id` → `query_docs`) — library/framework API docs.

Fire MCP calls in parallel. The code-wiki tool = "how should it work"; Langfuse = "how did it actually work."

## Debugging sessions

See `apps/mewbo_api/CLAUDE.md` → "Debugging session errors" for the full trace methodology. Quick orientation: MongoDB `db.events.find({session_id}).sort({ts:1})` (port 27018) is authoritative; Langfuse for LLM conversation chain. Per-agent attribution lives in Mongo `llm_call_start`/`llm_call_end` events (`agent_id`/`depth`/`step`); token counts live ONLY in Langfuse — Mongo llm_call events and `/usage` currently report zeros, and Langfuse's span tree orphans parents under concurrent sub-agents, so join the two by session, not by span. Cost/cache-hit truth is the LiteLLM proxy's Postgres spend log (`litellm-postgres`, `LiteLLM_SpendLogs.metadata->usage_object` → `cache_creation/read_input_tokens`); Langfuse mis-costs at full list price for any model string lacking a Langfuse Model Definition, and its `cache_hit` column tracks the Redis response cache, not Anthropic prompt caching.

## Running, testing, linting

- Tests: `pytest` under `tests/`.
- Install: `uv sync` (core) or `uv sync --all-extras --all-groups` (dev).
- Run: `uv run mewbo` / `uv run mewbo-api` from repo root, or `npm run dev` in `apps/mewbo_console`.
- Config chain: `CWD/configs/` → `$MEWBO_HOME/` → `~/.mewbo/`. Override with `--config`. Run `/init` to scaffold.
- Lint: `ruff check .` (auto-fix: `ruff check --fix .`). Types: `mypy`. Helpers: `make lint`, `make lint-fix`, `make typecheck`, `make precommit-install`.
- **Never blind `ruff --fix`** — always re-run `ruff check .` after any autofix (strips intentional `noqa`).
