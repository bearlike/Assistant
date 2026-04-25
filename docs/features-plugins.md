# Plugins & Marketplace

<div style="display: flex; justify-content: center;">
  <img src="../mewbo-console-05-plugins.png" alt="The Plugins page in the Mewbo console showing four installed plugins and a marketplace listing with install buttons" style="width: 100%; max-width: 720px; height: auto;" />
</div>

Plugins extend Mewbo with new agent definitions, skills, hooks, and MCP tool configurations. Install them from a marketplace or from a local directory. They activate automatically at session start without a restart. Plugins are first-class citizens. A plugin's skills appear in the skill catalogue, its hooks fire alongside native hooks, and its MCP servers appear in the tool list.

> [!TIP] Drop-in compatible with Claude Code plugins
> Mewbo reads the exact same [Claude Code plugin](https://docs.claude.com/en/docs/claude-code/plugins) manifest, directory layout (`.claude-plugin/plugin.json`, `agents/`, `skills/`, `hooks/hooks.json`, `.mcp.json`), and marketplace format. The default marketplace is the [official Claude plugins marketplace](https://github.com/anthropics/claude-plugins-official). Any private marketplace that follows the Claude Code marketplace schema works too. Point `plugins.marketplaces` at the repo and it loads without translation. Plugins authored for Claude Code work in Mewbo unchanged.

For setup and installation, see [Getting Started](getting-started.md).

---

## What a plugin can add

| Contribution type | How it works |
|---|---|
| **Agent definitions** | `.md` files in the plugin's `agents/` directory; addressable via `agent_type` in `spawn_agent` |
| **Skills** | `SKILL.md` files under `skills/<name>/SKILL.md`; added to the skill catalogue at session start |
| **Hooks** | `hooks/hooks.json`; fires on `pre_tool_use`, `post_tool_use`, and `on_session_end` events |
| **MCP tool configurations** | `.mcp.json` at the plugin root; merged into the MCP server list, making the plugin's external tools available to the session |
| **Session tools** | Python classes listed under `session_tools` in `plugin.json`; per-session stateful tools bound to the `ToolUseLoop`. See [Session tools](#session-tools). |

---

## Plugin directory layout

A plugin is a directory with the following optional structure:

```
my-plugin/
├── .claude-plugin/
│   └── plugin.json          # required manifest
├── agents/
│   └── code-reviewer.md     # agent definition
├── skills/
│   └── my-skill/
│       └── SKILL.md         # skill file
├── hooks/
│   └── hooks.json           # lifecycle hooks
└── .mcp.json                # MCP server configuration
```

### plugin.json manifest

```json
{
  "name": "my-plugin",
  "description": "A short description",
  "version": "1.0.0",
  "author": "Your Name",
  "requires-capabilities": ["stlite"],
  "session_tools": [
    {
      "tool_id": "submit_widget",
      "module": "my_plugin.submit_widget",
      "class": "SubmitWidgetTool"
    }
  ]
}
```

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique plugin name; used as the registry key |
| `description` | No | Human-readable description |
| `version` | No | Semver string |
| `author` | No | Author name or `{"name": "..."}` object |
| `requires-capabilities` | No | List of capability ids the whole plugin bundle needs; unioned into every contributed agent and skill. See [Capability gating](#capability-gating). |
| `session_tools` | No | List of `{tool_id, module, class}` entries describing Python classes that implement the [Session tools](#session-tools) protocol. |

### Path substitution

Inside any plugin-owned file — `.mcp.json`, `hooks/hooks.json`, `agents/*.md` bodies, `skills/*/SKILL.md` bodies — you can reference the plugin's own installation directory with `${CLAUDE_PLUGIN_ROOT}`. It is substituted when the plugin is discovered. In agent bodies, additional placeholders are resolved at spawn time:

| Placeholder | Resolves to |
|---|---|
| `${CLAUDE_PLUGIN_ROOT}` | Absolute path to the plugin's install directory |
| `${SESSION_ID}` | The current session's id |
| `${MEWBO_WIDGET_ROOT}` | Widget output root (widget-builder only); `:-` default syntax supported |

Substitution is a single linear `replace` pass — no template engine — so a body with no placeholders is byte-identical after substitution.

---

## Installing plugins

### Via CLI

```
/plugins                          # list installed plugins and their components
/plugins marketplace              # browse available plugins
/plugins install <name>           # install from the default marketplace
/plugins uninstall <name>         # remove an installed plugin
```

### Via the console

Open the **Plugins** view from the left-hand navigation. The view shows all installed plugins with their version, marketplace origin, and a contribution summary (skill count, hook count, MCP server count). A **Marketplace** tab lists available plugins with install / uninstall buttons. Changes take effect on the next session start.

### Via API

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/plugins` | List installed plugins and their components |
| `GET` | `/api/plugins/marketplace` | List available plugins from all configured marketplaces |
| `POST` | `/api/plugins/marketplace` | Install a plugin: body `{"name": "plugin-name", "marketplace": "Official"}` |
| `DELETE` | `/api/plugins/<name>` | Uninstall a plugin by name |

---

## Configuration

Plugin system settings live under `plugins` in [`configs/app.json`](configuration.md#pluginsconfig).

| Key | Type | Default | Description |
|---|---|---|---|
| `plugins.enabled` | boolean | `true` | Enable or disable the entire plugin system |
| `plugins.enabled_plugins` | array | `[]` | Explicit allowlist of plugin names to activate; empty = all installed plugins |
| `plugins.marketplaces` | array | `["anthropics/claude-plugins-official"]` | GitHub repos (`owner/repo`) that contain a `marketplace.json` index |
| `plugins.install_path` | string | `""` | Override for the plugin cache directory; defaults to `$MEWBO_HOME/plugins/` |

```json
{
  "plugins": {
    "enabled": true,
    "marketplaces": [
      "anthropics/claude-plugins-official",
      "my-org/internal-plugins"
    ],
    "enabled_plugins": ["code-reviewer", "doc-generator"]
  }
}
```

When `marketplaces` lists repos that are not yet cloned locally, Mewbo shallow-clones them the first time you browse or install. Subsequent runs use the local cache; a fast-forward `git pull` keeps it up to date.

Plugin skills never override personal (`~/.claude/skills/`) or project-local (`.claude/skills/`) skills with the same name. Plugin MCP servers are merged additively. Later plugins do not overwrite earlier ones for the same server name.

---

## Capability gating

A plugin can declare that its agents, skills, and session tools only make sense on sessions that advertise a specific capability — for instance, the widget-builder bundle only makes sense when the client has an stlite runtime.

Capabilities flow top-down:

1. The **client** announces its capabilities on every request. The web console sends `X-Mewbo-Capabilities: stlite` by default. Other clients (CLI, webhook adapters) send nothing unless configured to.
2. The **API** writes the advertised list onto the session's context event.
3. The **orchestrator** resolves `session_capabilities` once per session and passes the tuple to `ToolUseLoop`.
4. The **registries** apply `filter_by_capabilities` before rendering the agent and skill catalogs. An entry whose `requires-capabilities` is not a subset of the session's set is invisible — no tool schema, no catalog line, no accidental invocation.

Declare capabilities at the bundle level in `plugin.json`:

```json
{
  "name": "widget-builder",
  "requires-capabilities": ["stlite"]
}
```

Or per file on the agent / skill frontmatter:

```yaml
---
name: st-widget-builder
requires-capabilities: [stlite]
---
```

Both are combined as a union at discovery time, so a plugin-level capability overlays every contributed agent and skill without the author repeating it per file.

Empty `requires-capabilities` is the default and means "always visible".

---

## Session tools

A **session tool** is a per-agent stateful tool — a tool whose lifecycle is coupled to a specific agent instance rather than the global `ToolRegistry`. The core `exit_plan_mode` tool is a session tool; the widget-builder's `submit_widget` is a session tool.

Plugins contribute session tools through the `session_tools` array in `plugin.json`:

```json
{
  "session_tools": [
    {
      "tool_id": "submit_widget",
      "module": "mewbo_core.builtin_plugins.widget_builder.submit_widget",
      "class": "SubmitWidgetTool"
    }
  ]
}
```

The class must implement the `SessionTool` protocol:

| Member | Type | Purpose |
|---|---|---|
| `tool_id` | `str` | Tool identifier used in the LLM's function schema and for dispatch |
| `schema` | `dict` | OpenAI function schema (same shape as any other bound tool) |
| `async handle(action_step)` | coroutine | Called when the LLM invokes the tool; returns a `MockSpeaker` with the tool result |
| `should_terminate_run()` | `bool` | Returns `True` to signal the `ToolUseLoop` to exit cleanly after this step |

At session start, the core instantiates a `SessionToolRegistry`, imports each listed `module` + `class`, and registers a factory. When an agent spawns with a `session_tools`-contributed tool in its `allowed_tools`, the loop instantiates one per-agent instance and wires it alongside the built-in session tools. Dispatch, schema injection, and termination all go through the same path — no widget-specific branch exists in core.

This keeps core widget-agnostic: the full contract for a capability bundle is **(a)** a plugin manifest and **(b)** a Python class that satisfies the protocol.

---

## Built-in plugins

Some plugins ship inside the core package at `packages/mewbo_core/src/mewbo_core/builtin_plugins/`. They are discovered through the same plugin pipeline as user and marketplace plugins — byte-for-byte normal plugins, indistinguishable except for their location on the scan path. No `installed_plugins.json` entry is needed.

Currently bundled:

| Plugin | Capability | What it contributes |
|---|---|---|
| [widget-builder](features-widgets.md) | `stlite` | `st-widget-builder` agent + skill, `submit_widget` session tool, an stlite example library, and an AST-based import allowlist |

The built-in path is resolved via `importlib.resources`, so it survives editable installs, wheels, and zipapps identically.

---

## Writing a local plugin

To develop a plugin locally before publishing it to a marketplace, place the plugin directory anywhere on disk and point `plugins.registry_paths` at a custom `installed_plugins.json` that references it. Alternatively, use the CLI install flow with a `./relative-path` source in a local `marketplace.json`.

The minimum viable plugin is a directory containing only `.claude-plugin/plugin.json`. Everything else (`skills/`, `agents/`, `hooks/`, `.mcp.json`, `session_tools`) is optional and discovered automatically. The bundled [widget-builder](features-widgets.md) is a complete working example — see `packages/mewbo_core/src/mewbo_core/builtin_plugins/widget_builder/`.

---

> [!NOTE] How it works internally
> See [Architecture Overview → Plugin loading](core-orchestration.md#plugins) and [Capability overlay](core-orchestration.md#capability-overlay).
