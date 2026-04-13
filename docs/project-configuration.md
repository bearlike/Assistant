# Project Setup

When you start a session, Meeseeks looks for a `CLAUDE.md` in your project, walks up the directory tree to the git root collecting any parent `CLAUDE.md` files, and injects them into the assistant's context. Deeper nested `CLAUDE.md` files (inside sub-packages) are indexed but not injected. The assistant reads them on demand with `read_file` when work reaches that directory.

The same mechanism handles MCP tool configuration, skills, and local overrides.

> [!TIP] Drop-in compatible with common agent conventions
> Meeseeks reads both the [Claude Code](https://docs.claude.com/en/docs/claude-code/memory) `CLAUDE.md` format and the open [`AGENTS.md`](https://agents.md) convention used by Codex, Aider, and other agent frameworks. MCP servers follow the [Model Context Protocol](https://modelcontextprotocol.io) and accept both `servers` (Meeseeks) and `mcpServers` (Claude Code / VS Code) keys. Skills follow the [Agent Skills](https://docs.claude.com/en/api/agent-skills) standard. If you already use any of these tools, your existing project files work in Meeseeks without modification.

---

## Instruction file loading

### Upward pass: content injected at startup

At session start, Meeseeks walks up from your current working directory to the git root (or the filesystem root if you are not in a repo) and loads every instruction file it finds along the way. The full text of each file is concatenated and injected into the system prompt before the first LLM call. Each source is separated by a heading, so the assistant can tell where a rule came from.

| Priority | Path | Scope |
|----------|------|-------|
| 10 | `~/.claude/CLAUDE.md` | User-global, applies to every project |
| 20–29 | `CLAUDE.md` / `.claude/CLAUDE.md` walking up from CWD | Project hierarchy; CWD = 20, parent = 21, and so on |
| 30 | `.claude/rules/*.md` (all files, sorted) | Project-local rule set |
| 40 | `CLAUDE.local.md` | Machine-local override (gitignore this) |

Lower priority means lower precedence. Higher-priority content wins on conflict. If both `CLAUDE.md` and `AGENTS.md` exist at the same path, `CLAUDE.md` takes precedence and `AGENTS.md` is treated as a fallback.

### Downward pass: context map for on-demand loading

Meeseeks also scans *down* from your working directory to a maximum depth of 5, looking for `CLAUDE.md`, `AGENTS.md`, and `.claude/CLAUDE.md` in subdirectories. Critically, the content of these files is **not** injected. Only the file paths are collected and listed in the system prompt, like so:

```
# Sub-package instruction files

The following instruction files exist in subdirectories.
Read them when working on the relevant package.

- packages/meeseeks_core/CLAUDE.md
- apps/meeseeks_console/CLAUDE.md
- apps/meeseeks_api/AGENTS.md
```

When the assistant begins work in one of those directories, it reads the appropriate file with `read_file` before proceeding. This keeps large monorepos manageable: only the directly applicable instructions are in the active context, and nested package instructions are fetched on demand.

**Pruned directories** (never walked): `node_modules`, `__pycache__`, `.venv`, `venv`, and all dotfile directories (`.git`, `.claude`, etc.).

### Noload marker

Add `<!-- meeseeks:noload -->` as the very first line of any instruction file to exclude it from both passes. The loader checks the first line before reading the rest of the file.

Use this for shim files that redirect to another `CLAUDE.md` (so you do not get duplicate injection):

```markdown
<!-- meeseeks:noload -->
See ../CLAUDE.md. This file exists only for tool compatibility.
```

### Git context

Meeseeks can include git branch and status in the session context, so the assistant knows what branch you are on and what has changed since the last commit. Individual integrations (the CLI, specific skills) opt in to this. It is not always injected.

---

## Project-level MCP configuration

### Config merge order

MCP server definitions come from four layers, merged together in this order. Later layers win on key conflicts, using deep-merge semantics (nested objects are merged recursively, not replaced wholesale).

| Layer | Source | Priority |
|-------|--------|----------|
| 1 | Plugin-contributed servers | Lowest |
| 2 | Global: `configs/mcp.json` or `$MEESEEKS_HOME/mcp.json` | Mid |
| 3 | Subtree `.mcp.json` files, deepest-first | Mid-high |
| 4 | CWD `.mcp.json` | Highest |

The practical rule: **your CWD `.mcp.json` wins over the global one**, and subtree `.mcp.json` files deeper in the tree are merged in too. A project `.mcp.json` that adds a single server key leaves all global server definitions intact.

Meeseeks re-runs this merge whenever the MCP pool reconnects, so edits to any `.mcp.json` in the hierarchy are picked up automatically. Unchanged servers keep their existing connections; changed or new servers reconnect; removed servers disconnect.

### Config normalization

All `.mcp.json` files are normalized before merging, so you can mix schemas freely:

| Input field | Normalized to | Notes |
|-------------|---------------|-------|
| `mcpServers` | `servers` | Claude Code / VS Code schema compatibility |
| `type` | `transport` | Both keys removed after normalization to avoid leaks |
| `http_headers` | `headers` | Direct rename |
| `transport: "http"` | `transport: "streamable_http"` | Legacy alias |
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
