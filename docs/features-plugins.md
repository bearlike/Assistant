# Plugins & Marketplace

<div style="display: flex; justify-content: center;">
  <img src="../meeseeks-console-05-plugins.jpg" alt="The Plugins page in the Meeseeks console showing four installed plugins and a marketplace listing with install buttons" style="width: 100%; max-width: 720px; height: auto;" />
</div>

Plugins extend Meeseeks with new agent definitions, skills, hooks, and MCP tool configurations. Install them from a marketplace or from a local directory. They activate automatically at session start without a restart. Plugins are first-class citizens. A plugin's skills appear in the skill catalogue, its hooks fire alongside native hooks, and its MCP servers appear in the tool list.

> [!TIP] Drop-in compatible with Claude Code plugins
> Meeseeks reads the exact same [Claude Code plugin](https://docs.claude.com/en/docs/claude-code/plugins) manifest, directory layout (`.claude-plugin/plugin.json`, `agents/`, `skills/`, `hooks/hooks.json`, `.mcp.json`), and marketplace format. The default marketplace is the [official Claude plugins marketplace](https://github.com/anthropics/claude-plugins-official). Any private marketplace that follows the Claude Code marketplace schema works too. Point `plugins.marketplaces` at the repo and it loads without translation. Plugins authored for Claude Code work in Meeseeks unchanged.

For setup and installation, see [Getting Started](getting-started.md).

---

## What a plugin can add

| Contribution type | How it works |
|---|---|
| **Agent definitions** | `.md` files in the plugin's `agents/` directory; addressable via `agent_type` in `spawn_agent` |
| **Skills** | `SKILL.md` files under `skills/<name>/SKILL.md`; added to the skill catalogue at session start |
| **Hooks** | `hooks/hooks.json`; fires on `pre_tool_use`, `post_tool_use`, and `on_session_end` events |
| **MCP tool configurations** | `.mcp.json` at the plugin root; merged into the MCP server list, making the plugin's external tools available to the session |

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
  "author": "Your Name"
}
```

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique plugin name; used as the registry key |
| `description` | No | Human-readable description |
| `version` | No | Semver string |
| `author` | No | Author name or `{"name": "..."}` object |

Inside `.mcp.json` and `hooks/hooks.json`, you can reference the plugin's own installation directory with `${CLAUDE_PLUGIN_ROOT}`. This variable is substituted when the plugin is discovered.

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
| `plugins.install_path` | string | `""` | Override for the plugin cache directory; defaults to `$MEESEEKS_HOME/plugins/` |

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

When `marketplaces` lists repos that are not yet cloned locally, Meeseeks shallow-clones them the first time you browse or install. Subsequent runs use the local cache; a fast-forward `git pull` keeps it up to date.

Plugin skills never override personal (`~/.claude/skills/`) or project-local (`.claude/skills/`) skills with the same name. Plugin MCP servers are merged additively. Later plugins do not overwrite earlier ones for the same server name.

---

## Writing a local plugin

To develop a plugin locally before publishing it to a marketplace, place the plugin directory anywhere on disk and point `plugins.registry_paths` at a custom `installed_plugins.json` that references it. Alternatively, use the CLI install flow with a `./relative-path` source in a local `marketplace.json`.

The minimum viable plugin is a directory containing only `.claude-plugin/plugin.json`. Everything else (`skills/`, `agents/`, `hooks/`, `.mcp.json`) is optional and discovered automatically.

---

> [!NOTE] How it works internally
> See [Architecture Overview → Plugin loading](core-orchestration.md#plugins).
