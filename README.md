
<p align="center">
  <img src="docs/logos/logo-transparent.svg" alt="Meeseeks logo" width="96" />
</p>

<h1 align="center">Meeseeks</h1>
<p align="center"><em>An AI assistant modeled as a conversation state machine. A hierarchical agent hypervisor admits, scopes, and terminates parallel sub-agents under explicit lifecycle control.</em></p>

<p align="center">
    <a href="https://deepwiki.com/bearlike/Assistant"><img alt="Ask DeepWiki" src="https://deepwiki.com/badge.svg"></a>
    <a href="https://github.com/bearlike/Assistant/actions/workflows/docker-buildx.yml"><img alt="Build and Push Docker Images" src="https://github.com/bearlike/Assistant/actions/workflows/docker-buildx.yml/badge.svg"></a>
    <a href="https://github.com/bearlike/Assistant/actions/workflows/lint.yml"><img alt="Lint" src="https://github.com/bearlike/Assistant/actions/workflows/lint.yml/badge.svg"></a>
    <a href="https://github.com/bearlike/Assistant/actions/workflows/docs.yml"><img alt="Docs" src="https://github.com/bearlike/Assistant/actions/workflows/docs.yml/badge.svg"></a>
    <a href="https://codecov.io/gh/bearlike/Assistant"><img src="https://codecov.io/gh/bearlike/Assistant/graph/badge.svg?token=OJ2YUCIZ2I" alt="Codecov"></a>
    <a href="https://github.com/bearlike/Assistant/releases"><img src="https://img.shields.io/github/v/release/bearlike/Assistant" alt="GitHub Release"></a>
    <a href="https://github.com/bearlike/Assistant/pkgs/container/meeseeks-api"><img src="https://img.shields.io/badge/ghcr.io-bearlike/meeseeks--api:latest-blue?logo=docker&logoColor=white" alt="Docker Image"></a>
</p>



https://github.com/user-attachments/assets/78754e8f-828a-4c54-9e97-29cbeacbc3bc
> Four access surfaces — terminal, web console, REST API, Home Assistant — driven by the same session state machine. Full docs at [kanth.tech/Assistant](https://kanth.tech/Assistant/).

## Overview

Meeseeks is an AI assistant modeled as a conversation state machine. A top-level session binds an LLM to a filtered tool set via `bind_tools` and advances through a `submitted → running → terminal` lifecycle. Where parallelism is useful, the session issues `spawn_agent`; the hypervisor admits child sessions under a concurrency budget, constrains their tool scope, and resolves them into one of four terminal states — `completed`, `failed`, `cancelled`, or `rejected`. Transcripts are persisted per session, long histories are compacted, and summaries are retained across compactions. The plugin and skill layers follow the Claude-ecosystem Agent Skills + Plugin standard, and the console renders interactive [stlite (Streamlit-in-WASM) widgets](docs/features-widgets.md) inline in the conversation timeline via the bundled `widget-builder` plugin.

### Meeseeks Console

The web console provides a task orchestration frontend backed by the REST API. It supports session management, real-time event polling, tool selection, and execution trace viewing.

<table align="center">
    <tr>
        <th>Task detail page</th>
        <th>Console landing page</th>
    </tr>
    <tr>
        <td align="center"><img src="docs/meeseeks-console-02-tasks.png" alt="Meeseeks task detail page" height="360px"></td>
        <td align="center"><img src="docs/meeseeks-console-01-front.png" alt="Meeseeks console landing page" height="360px"></td>
    </tr>
</table>

### Capabilities at a glance

<table align="center">
    <tr>
        <th>Plan mode approval</th>
        <th>Live diff on every edit</th>
    </tr>
    <tr>
        <td align="center"><img src="docs/meeseeks-console-03-plan-approval.jpg" alt="Plan approval in the Meeseeks console" height="300px"></td>
        <td align="center"><img src="docs/meeseeks-console-04-file-edit.jpg" alt="File-edit diff card in the Meeseeks console" height="300px"></td>
    </tr>
    <tr>
        <th>Plugin marketplace</th>
        <th>Virtual projects</th>
    </tr>
    <tr>
        <td align="center"><img src="docs/meeseeks-console-05-plugins.jpg" alt="Plugins page with installed plugins and marketplace listings" height="300px"></td>
        <td align="center"><img src="docs/meeseeks-console-06-projects.jpg" alt="Projects page showing virtual workspaces shared across sessions" height="300px"></td>
    </tr>
    <tr>
        <th colspan="2">Widgets inline in chat</th>
    </tr>
    <tr>
        <td colspan="2" align="center"><img src="docs/meeseeks-console-07-widgets.png" alt="Stock ticker and GitHub repo card widgets rendered inline in the Meeseeks Console" width="100%"></td>
    </tr>
</table>

## Features

### Core workflow
- (✅) **Unified tool-use loop:** A single async `ToolUseLoop` where the LLM drives tool selection and execution via native `bind_tools`.
- (✅) **Sub-agent spawning:** Subtasks can be delegated to parallel sub-agents via `spawn_agent`, managed by the `AgentHypervisor` control plane.
- (✅) **Tool scoping & permissions:** Sub-agents receive scoped tool access (allowlist/denylist filtered before binding). Permission policies gate all tool execution.
- (✅) **Concurrency-aware execution:** Tools are partitioned into concurrent-safe (parallel) and exclusive (sequential) batches with per-tool timeouts.
- (✅) **Conversation fork & edit:** Fork a session from any message (`fork_at_ts`), edit and regenerate past turns, and override the model per-message.

### Memory and context management
- (✅) **Session transcripts:** Writes tool activity and responses to disk for continuity.
- (✅) **Context compaction:** Two-mode compaction (full/partial) with structured summaries, analysis scratchpad, and post-compact file restoration. Auto-compacts near the context budget using partial mode.
- (✅) **Token awareness:** Tracks context window usage and exposes budgets in the CLI.
- (✅) **Selective recall:** Builds context from recent turns plus a summary of prior events.
- (✅) **Hierarchical instructions:** Discovers CLAUDE.md from user, project, rules, and local levels with priority ordering. Injects git context (branch, status, recent commits) into the system prompt.
- (✅) **Session listing hygiene:** Filters empty sessions and supports archiving via the API.

### Tooling and integrations
- (✅) **Tool registry:** Discovers local tools and MCP tools via persistent connection pool with automatic reconnection and config change detection.
- (✅) **Skills:** Supports the [Agent Skills](https://agentskills.io) open standard. Place `SKILL.md` files in `~/.claude/skills/` or `.claude/skills/` to teach the assistant reusable workflows. Skills can be invoked via `/skill-name` slash commands or auto-activated by the LLM. `requires-capabilities` frontmatter gates a skill to sessions that advertise the matching capability bundle.
- (✅) **Configurable file editing:** Two built-in edit mechanisms — Aider-style SEARCH/REPLACE blocks and per-file structured patch (`file_path` / `old_string` / `new_string`). Select via `agent.edit_tool` in config, or let the system auto-select based on model identity. Different models perform better with different formats; the choice is transparent to the rest of the stack.
- (✅) **Plugin system:** Discover, install, and manage plugins from configured marketplaces, alongside a built-in plugin scan path for first-party bundles. Plugins can provide agent definitions, skills, hooks, MCP tool integrations, and per-agent stateful session tools via the `SessionTool` protocol. `requires-capabilities` frontmatter plus the `X-Meeseeks-Capabilities` request header gate capability bundles to compatible sessions, and `${CLAUDE_PLUGIN_ROOT}` substitution lets plugins reference their own assets by absolute path. Managed via the CLI (`/plugins`), console UI, or REST API.
- (✅) **Interactive widgets:** Inline [stlite (Streamlit-in-WASM) widgets](docs/features-widgets.md) rendered in the conversation timeline via the bundled `widget-builder` plugin. A sub-agent writes a two-file widget (`app.py` + `data.json`), calls `submit_widget`, and the console mounts the result in a sandboxed Web Worker — no server round-trip, no CORS. Ships with a component library (GitHubRepoCard, SearchResultCard, StockTickerCard) and an AST import-allowlist lint that returns line-numbered feedback to the generating agent.
- (✅) **Native LSP integration:** Opt-in code intelligence via `lsp_tool` (pygls/lsprotocol). Supports diagnostics, go-to-definition, find-references, and hover. Built-in servers: pyright (Python), typescript-language-server (TS/JS), gopls (Go), rust-analyzer (Rust) — auto-discovered on the PATH. Passive diagnostics inject automatically after file edits. Configure via `agent.lsp` in config.
- (✅) **Web IDE:** Opt-in per-session code-server containers for browser-based editing, accessible from the console via "Open in Web IDE".
- (✅) **Local file + shell tools:** Built-in tools for file reads, directory listing, and shell commands (approval-gated).
- (✅) **Chat platform adapters:** `ChannelAdapter` protocol with shared `_process_inbound()` pipeline. Adapters for [Nextcloud Talk](docs/clients-nextcloud-talk.md) (webhook-driven, HMAC-SHA256, ActivityStreams 2.0) and [Email](docs/clients-email.md) (IMAP polling with SMTP replies rendered as HTML from markdown). Session tag mapping, deduplication guard, and slash commands (`/help`, `/usage`, `/new`, `/switch-project`).
- (✅) **REST API:** Exposes the assistant over HTTP for third-party integration.
- (✅) **Web console:** Task orchestration frontend backed by the REST API.
- (✅) **Terminal CLI:** Fast interactive shell with plan visibility and tool result cards.
- (✅) **Model routing:** Supports provider-qualified model names, a configurable API base URL, and `proxy_model_prefix` for proxy routing.

### Safety and observability
- (✅) **Permission gate:** Uses approval callbacks and hooks to control tool execution.
- (✅) **Operational visibility:** Optional Langfuse tracing (session-scoped traces) stays off if unconfigured.
- (✅) **Hook system:** Error-isolated hooks with session lifecycle events, external command hook configuration, and fnmatch-based tool matcher filtering.

### Interface notes
- **CLI layout adapts to terminal width.** Headers and tool result cards adjust to small and wide shells.
- **Interactive CLI controls.** Use a model picker, MCP browser, session summary, and token budget commands.
- **Inline approvals.** Rich-based approval prompts render with padded, dotted borders and clear after input.
- **Unified experience.** Console, API, Home Assistant, Nextcloud Talk, Email, and CLI interfaces share the same core engine to reduce duplicated maintenance.
- **Shared session runtime.** The API exposes polling endpoints; the CLI runs the same runtime in-process for sync execution, cancellation, and summaries.
- **Event payloads.** `action_plan` steps are `{title, description}`; `tool_result`/`permission` use `tool_id`, `operation`, and `tool_input`.

### Home Assistant integration

<table align="center">
    <tr>
        <th>Answer questions and interpret sensor information</th>
        <th>Control devices and entities</th>
    </tr>
    <tr>
        <td align="center"><img src="docs/screenshot_ha_assist_1.png" alt="Home Assistant sensor Q&A" height="360px"></td>
        <td align="center"><img src="docs/screenshot_ha_assist_2.png" alt="Home Assistant device control" height="360px"></td>
    </tr>
</table>

### Email integration

<p align="center">
    <img src="docs/meeseeks-email-01.jpg" alt="Meeseeks email thread in Gmail" height="480px">
</p>

## Installation

<p align="center">
    <img src="docs/meeseeks-console-banner.gif" alt="Meeseeks console banner" width="100%">
</p>

User install (core only):
```bash
uv sync
```

Optional components:
```bash
uv sync --extra cli   # CLI
uv sync --extra api   # REST API
cd apps/meeseeks_console && npm install  # Web console
uv sync --extra ha    # Home Assistant integration
```

Developer install (all components + dev/test/docs):
```bash
uv sync --all-extras --all-groups
```

Global install (available system-wide as `meeseeks`):
```bash
uv tool install .
# Set up global config:
mkdir -p ~/.meeseeks
cp configs/app.json ~/.meeseeks/app.json
cp configs/mcp.json ~/.meeseeks/mcp.json
# Or run `meeseeks` and use /init to scaffold example configs
```

Config discovery priority: `CWD/configs/`, then `$MEESEEKS_HOME/`, then `~/.meeseeks/`. Use `--config /path/to/app.json` for explicit override, or set `MEESEEKS_HOME` in your shell profile (`~/.bashrc`, `~/.zshrc`, etc.) to permanently point to a custom config directory:
```bash
export MEESEEKS_HOME="/path/to/your/config"
```

### Docker Compose

Pre-built images are published to GHCR on every release:

```bash
# Copy and edit the environment file
cp docker.example.env docker.env
# Edit docker.env — set MASTER_API_TOKEN, VITE_API_KEY, HOST_UID/GID

# Pull and start (recommended)
docker compose pull && docker compose up -d
```

To build from source instead: `docker compose up --build -d`.

The stack uses host networking. The API serves on port `5125` and the console on `3001`. Nginx in the console container proxies `/api/` requests to the API. See [docs/getting-started.md](docs/getting-started.md) for full configuration details.

## Architecture

See [docs/index.md](docs/index.md) for the full architecture diagram.

## Monorepo layout

- `packages/meeseeks_core/`: orchestration loop, schemas, session storage, two-mode compaction, tool registry, hook system, hierarchical instruction discovery, plugin system, agent registry.
- `packages/meeseeks_tools/`: tool implementations and integrations (including Home Assistant and MCP).
- `apps/meeseeks_api/`: Flask REST API for programmatic access, plugin management endpoints, Web IDE lifecycle, channel adapters (Nextcloud Talk, Email).
- `apps/meeseeks_console/`: Web console for task orchestration, plugin management, and Web IDE access.
- `apps/meeseeks_cli/`: Terminal CLI frontend for interactive sessions.
- `meeseeks_ha_conversation/`: Home Assistant integration that routes voice to the API.
- `packages/meeseeks_core/src/meeseeks_core/prompts/`: planner prompts and tool instructions.

## Documentation

Full docs live at **[kanth.tech/Assistant](https://kanth.tech/Assistant/)** — including setup, every client surface, the capability reference, deployment guides, and the internals/SDK track. The source lives under [`docs/`](docs/) and is published with MkDocs.

If you are just getting started, jump to [Get Started](https://kanth.tech/Assistant/getting-started/).

## Development principles

- Keep the core engine centralized. Interfaces should remain thin to avoid duplicated maintenance.
- Organize logic into clear modules, classes, and functions. Favor readable, well-scoped blocks.
- Prefer small, composable changes that keep behavior consistent across interfaces.

## Contributing

We welcome contributions from the community to improve Meeseeks.

1. Fork the repository and clone it to your local machine.
2. Create a new branch for your contribution.
3. Make your changes, commit them, and push to your fork.
4. Open a pull request describing the change and the problem it solves.

If you encounter bugs or have ideas for features, open an issue on the [issue tracker](https://github.com/bearlike/Assistant/issues). Include reproduction steps and error messages when possible.
