# Project Setup

When you start a session, Mewbo looks for a `CLAUDE.md` in your project, walks up the directory tree to the git root collecting any parent `CLAUDE.md` files, and injects them into the assistant's context. Deeper nested `CLAUDE.md` files (inside sub-packages) are indexed but not injected. The assistant reads them on demand with `read_file` when work reaches that directory.

The same mechanism handles MCP tool configuration, skills, and local overrides.

> [!TIP] Drop-in compatible with common agent conventions
> Mewbo reads both the [Claude Code](https://docs.claude.com/en/docs/claude-code/memory) `CLAUDE.md` format and the open [`AGENTS.md`](https://agents.md) convention used by Codex, Aider, and other agent frameworks. MCP servers follow the [Model Context Protocol](https://modelcontextprotocol.io) and accept both `servers` (Mewbo) and `mcpServers` (Claude Code / VS Code) keys. Skills follow the [Agent Skills](https://docs.claude.com/en/api/agent-skills) standard. If you already use any of these tools, your existing project files work in Mewbo without modification.

---

## Choosing a workspace

Every session runs somewhere on disk, and there are three ways to pick where.

| Choice | Where the session starts | Pick this when |
|---|---|---|
| Temporary directory (default) | A fresh, empty scratch directory that exists only for the session | The task does not need an existing codebase: a one-off script, a calculation, a question with no repository behind it |
| A named project | The directory you choose before the session starts | You already know which project the work belongs to |
| Auto | A temporary directory at first, then wherever the model decides | The right project is not obvious up front, or the task genuinely touches more than one project |

The first two choices are fixed for the life of the session: you name the working directory, or you leave it unset and Mewbo defaults to a scratch directory. Auto is dynamic. The session still starts in a temporary directory like the default case, but two tools are bound to it that let the model find the project the task actually needs and move into it, then change its mind later if the task grows to cover more than one.

### What the model sees in auto mode

`list_projects` enumerates every project Mewbo currently knows about: directories an operator registered by hand, projects and worktrees Mewbo manages itself, and git repositories that have been registered but not necessarily checked out anywhere. Each entry reports a key, a name, its kind, a description, whether it is currently available, and its repository slug and branch when known. A registered repository with no local checkout still appears in the list, so the model can see that it exists; it cannot be worked in until something checks it out, and `switch_project` refuses a key pointing at one until then.

`switch_project` takes a key from that list and moves the session into it. The working directory moves, the project's own instruction files are picked up the same way described below, and any sub-agent spawned after the switch inherits the new directory too. A sub-agent already running when the switch happens keeps the directory it started in; the switch moves the session going forward, not agents already in flight. The model can call `switch_project` repeatedly, and a task that legitimately spans several projects is expected to.

Two limits are worth knowing, because both are deliberate rather than gaps:

- **A switch never grants the agent more tools than it started with.** The tool registry is rebuilt for the new directory, but the set of tools bound to the agent narrows to what the run already held; a project's own `.mcp.json` servers are not admitted partway through a run. Otherwise moving between projects would be a way to acquire tools the caller never granted. Start a fresh session against that project and its servers resolve normally.
- **Skills accumulate rather than being replaced.** The registry also holds plugin-contributed and user-level skills, and a fresh scan of the new directory alone would drop them. Carrying the previous project's skills costs less than losing those.

A parent agent can also hand a sub-agent its own project at the moment it spawns it, independent of any switch. That is what lets one session run a fleet of agents in one repository while another fleet works in a second, all under the same session.

Both tools exist only for the top-level agent, and only while the session is in auto mode. A sub-agent works in whatever directory it was spawned into or later switched to by its own parent; it never re-scopes the whole session. See [Built-in Tools](features-builtin-tools.md#list_projects) for the full parameter and result reference.

---

## Instruction file loading

### Upward pass: content injected at startup

At session start, Mewbo walks up from your current working directory to the git root (or the filesystem root if you are not in a repo) and loads every instruction file it finds along the way. The full text of each file is concatenated and injected into the system prompt before the first LLM call. Each source is separated by a heading, so the assistant can tell where a rule came from.

| Priority | Path | Scope |
|----------|------|-------|
| 10 | `~/.claude/CLAUDE.md` | User-global, applies to every project |
| 20–29 | `CLAUDE.md` / `.claude/CLAUDE.md` walking up from CWD | Project hierarchy; CWD = 20, parent = 21, and so on |
| 30 | `.claude/rules/*.md` (all files, sorted) | Project-local rule set |
| 40 | `CLAUDE.local.md` | Machine-local override (gitignore this) |

Lower priority means lower precedence. Higher-priority content wins on conflict. If both `CLAUDE.md` and `AGENTS.md` exist at the same path, `CLAUDE.md` takes precedence and `AGENTS.md` is treated as a fallback.

### Downward pass: context map for on-demand loading

Mewbo also scans *down* from your working directory to a maximum depth of 5, looking for `CLAUDE.md`, `AGENTS.md`, and `.claude/CLAUDE.md` in subdirectories. Critically, the content of these files is **not** injected. Only the file paths are collected and listed in the system prompt, like so:

```
# Sub-package instruction files

The following instruction files exist in subdirectories.
Read them when working on the relevant package.

- packages/mewbo_core/CLAUDE.md
- apps/mewbo_console/CLAUDE.md
- apps/mewbo_api/AGENTS.md
```

When the assistant begins work in one of those directories, it reads the appropriate file with `read_file` before proceeding. This keeps large monorepos manageable: only the directly applicable instructions are in the active context, and nested package instructions are fetched on demand.

**Pruned directories** (never walked): `node_modules`, `__pycache__`, `.venv`, `venv`, and all dotfile directories (`.git`, `.claude`, etc.).

### Noload marker

Add `<!-- mewbo:noload -->` as the very first line of any instruction file to exclude it from both passes. The loader checks the first line before reading the rest of the file.

Use this for shim files that redirect to another `CLAUDE.md` (so you do not get duplicate injection):

```markdown
<!-- mewbo:noload -->
See ../CLAUDE.md. This file exists only for tool compatibility.
```

### Git context

Mewbo can include git branch and status in the session context, so the assistant knows what branch you are on and what has changed since the last commit. Individual integrations (the CLI, specific skills) opt in to this. It is not always injected.

---

## Project-level MCP configuration

### Config merge order

MCP server definitions come from four layers, merged together in this order. Later layers win on key conflicts, using deep-merge semantics (nested objects are merged recursively, not replaced wholesale).

| Layer | Source | Priority |
|-------|--------|----------|
| 1 | Plugin-contributed servers | Lowest |
| 2 | Global: `configs/mcp.json` or `$MEWBO_HOME/mcp.json` | Mid |
| 3 | Subtree `.mcp.json` files, deepest-first | Mid-high |
| 4 | CWD `.mcp.json` | Highest |

The practical rule: **your CWD `.mcp.json` wins over the global one**, and subtree `.mcp.json` files deeper in the tree are merged in too. A project `.mcp.json` that adds a single server key leaves all global server definitions intact.

Mewbo re-runs this merge whenever the MCP pool reconnects, so edits to any `.mcp.json` in the hierarchy are picked up automatically. Unchanged servers keep their existing connections; changed or new servers reconnect; removed servers disconnect.

### Config normalization

All `.mcp.json` files are normalized before merging, so you can mix schemas freely:

| Input field | Normalized to | Notes |
|-------------|---------------|-------|
| `mcpServers` | `servers` | Claude Code / VS Code schema compatibility |
| `type` | `transport` | Both keys removed after normalization to avoid leaks |
| `http_headers` | `headers` | Direct rename |
| `transport: "http"` | `transport: "streamable_http"` | Accepted alias |
| `command` present, no `transport` | `transport: "stdio"` | Inferred |
| `${VAR}` / `$VAR` in values | Expanded from process environment | Unresolved vars left as-is |

This means Claude Code `.mcp.json` files (using `mcpServers`) work without modification.

### Example project `.mcp.json`

```json
{
  "servers": {
    "project_db": {
      "transport": "stdio",
      "command": ["mcp-sqlite", "--db", "./dev.db"]
    }
  }
}
```

Or using the Claude Code schema (both accepted):

```json
{
  "mcpServers": {
    "project_db": {
      "command": "mcp-sqlite",
      "args": ["--db", "./dev.db"]
    }
  }
}
```

---

## Skills and project context

Skills follow the same layered discovery as instruction files. See [Skills](features-skills.md#where-skills-live) for the full path table. The key point: project-local skills (`.claude/skills/`) take precedence over personal skills (`~/.claude/skills/`), and subtree skills fill gaps without overriding either.

---

## Summary: what goes into the context at startup

| Component | When loaded | How it enters the context |
|-----------|-------------|---------------------------|
| User + project CLAUDE.md files (upward) | Session start | Full text injected into system prompt |
| Rules (`.claude/rules/*.md`) | Session start | Full text injected into system prompt |
| Local CLAUDE.local.md | Session start | Full text injected into system prompt |
| Subtree CLAUDE.md index (downward) | Session start | Path list only. Content loaded on demand via `read_file` |
| Skills catalog | Session start | Name + description index in system prompt; body loaded via `activate_skill` |
| MCP tool schemas | Session start | Tool schemas bound to the LLM call |
| Subtree `.mcp.json` | Per reconnect | Merged into active server set |

> [!NOTE] How it works internally
> See [Architecture Overview → Instruction loading](core-orchestration.md#instruction-loading) and [MCP connection pool](core-orchestration.md#mcp).
