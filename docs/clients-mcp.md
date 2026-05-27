# MCP Server

Mewbo can run as an **MCP server** — so any MCP-compatible AI agent or IDE can put your Mewbo deployment to work. Claude Code, Codex, Cursor, Windsurf, or even another Mewbo can spin up a coding session on a fresh worktree, follow it up and steer it, read back exactly what happened at the level of detail it needs, and ask grounded questions of your Agentic Wiki. One assistant becomes a tool the rest of your agent fleet can call.

> [!INFO] One key, issued and revoked by you
> Any MCP client you hand a key to can create and drive sessions, read their history, and query the wiki. Keys are minted from the console and revocable at any time. The MCP surface is curated, but an issued key is full-power — read [Authentication](#authentication-required) before you hand one out.

## What is MCP?

The [Model Context Protocol](https://modelcontextprotocol.io) (MCP) is an open standard that lets AI applications connect to tools and data through one uniform interface — think of it as a USB-C port for AI apps. Mewbo speaks MCP in **both directions**:

- **As a client** — Mewbo connects out to other MCP servers to gain tools. See [External Tools (MCP)](features-mcp.md).
- **As a server** — covered on this page — other agents connect *in* and drive Mewbo.

## The Mewbo MCP server

Mewbo is self-hosted, so the MCP server runs as part of your own deployment (the `mewbo-mcp` service). Point your client at your deployment's MCP endpoint:

**Endpoint:** `https://<your-mewbo-host>/mcp` — Streamable HTTP.

The default Docker Compose deployment serves the MCP server on port **5127**; for local development, `uv run mewbo-mcp` serves `http://127.0.0.1:5127/mcp`.

### Authentication required

Every connection needs an API key that you issue from Mewbo:

1. Open the console and go to **Settings → API Keys**.
2. Create a key and label it (for example, the agent that will use it). **The key is shown once** — copy it then.
3. Add it to your MCP client config as a `Bearer` token (see [Connect your client](#connect-your-client)).

The same key authenticates both the REST API and the MCP server — one identity, two surfaces — and stays valid until you revoke it from the same panel.

| Token | Prefix | Use |
|---|---|---|
| **Issued API key** | `mk_` | Per-agent credential created in **Settings → API Keys**. Valid on the REST API and the MCP server; revocable. **Recommended for agents.** |
| **Master token** | operator-set string (default `msk-strong-password`) | Break-glass admin credential (it also mints and revokes keys). No prefix is enforced — it is whatever the operator configures. Never hand it to an external agent. |

> [!WARNING] Issue keys only to agents you trust
> Mewbo's MCP surface is a curated set of tools, but an issued key also authorizes the **full REST API**, which can run shell commands, edit files, and spawn agents on your machine. Treat a key like a credential to the host: give each agent its own labelled key, control who holds it, and revoke it the moment it is no longer needed.

## Available tools

### Sessions — create & control

| Tool | Description |
|---|---|
| **`create_session`** | Start a Mewbo session from a prompt. By default it provisions a fresh git worktree and branch off the target repo's base, so the work is isolated; pass an explicit `branch`/`worktree` to target an existing one. Optionally enable specific integrations (tools) and set a title or tags. |
| **`send_followup`** | Send a follow-up or steering message into a running or finished session. |
| **`interrupt_session`** | Interrupt the session's current step. |

### Sessions — read, at the detail you need

| Tool | Description |
|---|---|
| **`get_session_history`** | Read a session at one of four tiers, so you spend only the context you need: `overview` (title, status, counts, tokens), `turns` (one row per exchange), `steps` (per-step tool → result previews for a turn), or `full` (complete step logs plus the sub-agent tree). |
| **`list_sessions`** | List and filter sessions by project, status, or recency. |
| **`get_agent_tree`** | Inspect a session's sub-agent hierarchy and lifecycle state. |

### Wiki — query, ask & teach

| Tool | Description |
|---|---|
| **`list_wiki_projects`** | List the repositories indexed in the [Agentic Wiki](features-wiki.md). |
| **`read_wiki_structure`** | Get a project's knowledge-graph structure. |
| **`read_wiki_page`** | Fetch a single wiki page. |
| **`ask_wiki`** | Ask a natural-language question about an indexed project and get a cited answer. |
| **`submit_insight`** | Teach the wiki a durable fact about the codebase. The server condenses it into one or more atomic notes, anchors each to the code it's about, de-duplicates against what's already stored, and safely merges — so the [code memory graph](features-wiki.md#grounded-by-a-code-memory-graph) compounds as your agents work. |

### Discovery

| Tool | Description |
|---|---|
| **`list_integrations`** | List the tools and plugins available, so a client knows what it can switch on when it creates a session. |

## Wire protocol

The Mewbo MCP server speaks **Streamable HTTP** at `/mcp`, and works with any HTTP-compatible MCP client.

> [!NOTE] It's a network service
> Unlike a local stdio MCP server, the Mewbo MCP server is reached over the network — so put it behind the same TLS / reverse proxy as the rest of your deployment.

## Connect your client

### Claude Code

```bash
claude mcp add -s user -t http mewbo https://<your-mewbo-host>/mcp -H "Authorization: Bearer <API_KEY>"
```

### Codex, Cursor, Windsurf, and other clients

Add an entry to your client's MCP server config:

```json
{
  "mcpServers": {
    "mewbo": {
      "serverUrl": "https://<your-mewbo-host>/mcp",
      "headers": {
        "Authorization": "Bearer <API_KEY>"
      }
    }
  }
}
```

> [!TIP] Fill in your own values
> Replace `<your-mewbo-host>/mcp` with your deployment's endpoint (for example `https://mewbo.example.com/mcp`, or `http://localhost:5127/mcp` for local development) and `<API_KEY>` with the key from **Settings → API Keys**.

## What you can do with it

- **Drive Mewbo from your IDE agent.** "Have Mewbo run the migration on a fresh branch and ping me when the tests pass" — `create_session` isolates the work on a new worktree; `get_session_history` reads back the result.
- **Schedule and automate.** A Claude Code or Codex routine kicks off a nightly Mewbo task and reads the answer the next morning.
- **Ground other agents in your code.** Another agent calls `ask_wiki` for a cited answer from your codebase before it writes a line.
- **Let your fleet teach the wiki.** An agent that just learned something durable about the code calls `submit_insight` — the next `ask_wiki`, from any client, is built on it.
- **Orchestrate a fleet.** One agent fans work out to several Mewbo sessions and polls `get_session_history` at the `overview` tier to track them all.

## Two directions of MCP

Mewbo sits at both ends of the protocol — don't confuse the two:

| | [External Tools (MCP)](features-mcp.md) | MCP Server (this page) |
|---|---|---|
| **Direction** | Mewbo is the **client**, calling out to other servers | Mewbo is the **server**; other agents call in |
| **You configure** | `mcp.json` — the servers Mewbo connects to | An **API key** other agents authenticate with |
| **Result** | Mewbo gains more tools | Other agents gain Mewbo as a tool |

## Related resources

- [External Tools (MCP)](features-mcp.md) — Mewbo consuming MCP servers (the inverse of this page).
- [Web Console + API](clients-web-api.md) — the REST surface the MCP server wraps.
- [Agentic Wiki](features-wiki.md) — what `ask_wiki` and `read_wiki_*` query.
- [Model Context Protocol](https://modelcontextprotocol.io) — the open standard.
- [Connecting remote MCP servers to Claude](https://support.anthropic.com/en/articles/11175166-about-custom-integrations-using-remote-mcp) · [OpenAI's guide to remote MCP](https://platform.openai.com/docs/guides/tools-remote-mcp).
