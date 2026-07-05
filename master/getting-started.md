# Get Started

Mewbo is an open, model-agnostic stack for agentic work. A hypervisor splits a goal into parallel sub-agents, and each one carries only the tools it needs. You watch the agent tree grow live, approve destructive steps, and steer any branch mid-run. Three products build on that core. Automation isolates every change in its own Git worktree. An Agentic Wiki turns a codebase into a graph you can question. Agentic Search ranks results across every tool you connect.

Every layer runs on any model behind LiteLLM, and all of it is open source. Getting started takes two moves. First, run the server. Then pick the client that fits where your team already works. Per-client setup lives on each client page, so this page routes you there.

## Run the server {#run-the-server}

A running Mewbo backend powers the web console, the Android client, the REST API, and every integration. Pick one path.

### Docker (recommended) {#docker-quickstart}

Pre-built images are published to GHCR. This is the fastest path to a production-ready stack.

```bash
# 1. Create your environment file and edit the three required vars
cp docker.example.env docker.env

# 2. Pull and start
docker compose pull && docker compose up -d
```

Required variables in [`docker.env`](repo:docker.env):

| Variable | Purpose |
|----------|---------|
| `MASTER_API_TOKEN` | API authentication token |
| `VITE_API_KEY` | Frontend key (must match `MASTER_API_TOKEN`) |
| `HOST_UID` / `HOST_GID` | Host user and group IDs (run `id` to find yours) |

The full reference covers volume mounts, project directories, the reverse proxy, and runtime config. See [Docker Compose](deployment-docker.md).

### From source with uv {#api-setup}

Install [uv](https://docs.astral.sh/uv/), which manages Python 3.10+ for you. Then sync the API extra and start the server.

```bash
uv sync --extra api
uv run mewbo-api
```

The API listens on `127.0.0.1:5124` by default. The Docker stack publishes it on host port 5125 instead. Swap `--extra api` for `--all-extras --all-groups` to pull in the wiki, MCP, and Home Assistant components plus the dev tooling.

Set a provider key before your first run. A minimal [`configs/app.json`](repo:configs/app.example.json):

```json
{
  "llm": {
    "api_key": "sk-ant-xxxxxxxx",
    "default_model": "anthropic/claude-sonnet-4-6"
  }
}
```

All other keys have sensible defaults. For provider keys and model selection, see [LLM Setup](llm-setup.md). For every config key, see [Configuration Reference](configuration.md).

## Pick your client {#pick-your-client}

One engine, the same behaviour and tools everywhere. Only the mode of access changes.

<div class="ms-grid ms-grid--4">

<a class="ms-card" href="../terminal/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:terminal" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Terminal</span>
  <span class="ms-card__body">A local CLI session for developers. It ships with the base install and runs the engine locally, so it needs no separate server.</span>
</a>

<a class="ms-card" href="../web/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:app-window" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Web console</span>
  <span class="ms-card__body">A browser console over the REST API. Review sessions side by side and drive scheduled runs.</span>
</a>

<a class="ms-card" href="../android/">
  <span class="ms-card__icon">
    <iconify-icon icon="simple-icons:android" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">Android</span>
  <span class="ms-card__body">Aura, the native Android client. Start and steer sessions from your phone.</span>
</a>

<a class="ms-card" href="../api/">
  <span class="ms-card__icon">
    <iconify-icon icon="lucide:braces" width="20" height="20" aria-hidden="true"></iconify-icon>
  </span>
  <span class="ms-card__title">API</span>
  <span class="ms-card__body">The REST API behind every surface. Drive sessions from CI, cron, or your own app.</span>
</a>

</div>

## More ways to connect {#integrations}

Mewbo also meets your team where it already talks.

- [Home Assistant](clients-home-assistant.md): voice control across every exposed sensor and device.
- [Email](clients-email.md): send a normal email and get a styled reply minutes later.
- [Nextcloud Talk](clients-nextcloud-talk.md): a chat adapter for the channels your team already uses.
- [MCP server](clients-mcp.md): expose Mewbo to Claude Code, Codex, Cursor, or another agent fleet over MCP.

## Project instructions {#project-instructions}

Mewbo discovers `CLAUDE.md`, `AGENTS.md`, and `.claude/rules/*.md` files automatically. The discovery is compatible with the Claude Code and AGENTS.md conventions. Place a `CLAUDE.md` at your project root and it loads at session start. Nested packages are indexed on demand. Sub-directory instruction files are listed in the system prompt as paths, and their content is fetched only when work reaches those directories.

See [Project Configuration](project-configuration.md) for the full loading strategy.
</content>
</invoke>
