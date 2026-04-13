<!-- AUTO-GENERATED from configs/app.schema.json. Do not edit manually. -->
# Configuration Reference

Meeseeks is configured via `configs/app.json`. This reference is auto-generated
from the JSON Schema at [`configs/app.schema.json`](https://github.com/bearlike/Assistant/blob/main/configs/app.schema.json).

Copy `configs/app.example.json` to `configs/app.json` to get started.
See [Get Started](getting-started.md) for the full setup walkthrough.

## RuntimeConfig

Top-level key: `runtime`

Runtime environment settings.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `envmode` | string | `dev` | Environment mode (e.g. dev, prod). |
| `log_level` | string | `DEBUG` | Logging verbosity. One of DEBUG, INFO, WARNING, ERROR, CRITICAL. |
| `log_style` | string | `""` | Log output style for the core engine (empty for default). |
| `cli_log_style` | string | `dark` | Rich console log theme for the CLI (dark or light). |
| `preflight_enabled` | boolean | `false` | Run connectivity checks for LLM, Langfuse, and Home Assistant on startup. |
| `cache_dir` | string | `""` | Directory for tool caches. Defaults to $MEESEEKS_HOME/cache. ⚠️ |
| `session_dir` | string | `""` | Directory for session transcripts. Defaults to $MEESEEKS_HOME/sessions. ⚠️ |
| `config_dir` | string | `""` | Root configuration directory. Defaults to $MEESEEKS_HOME. ⚠️ |
| `result_export_dir` | string | `""` | Directory for large tool result exports. Empty to disable. |
| `projects_home` | string | `""` | Directory for virtual project folders. Defaults to $MEESEEKS_HOME/projects. ⚠️ |

## StorageConfig

Top-level key: `storage`

Session storage backend configuration.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `driver` | string | `json` | Storage driver: 'json' (filesystem) or 'mongodb'. |
| `mongodb` | MongoDBConfig |  | MongoDB connection settings (used when driver is 'mongodb'). |

## LLMConfig

Top-level key: `llm`

LLM provider connection and model selection.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `api_base` | string | `""` | Optional base URL override. Leave empty for direct provider access (LiteLLM routes automatically from the model prefix). Set only when using a proxy (e.g. LiteLLM, Bifrost). |
| `api_key` | string | `""` | API key for the LLM provider (e.g. Anthropic, OpenAI) or proxy master key. ⚠️ |
| `default_model` | string | `gpt-5.2` | Model ID using 'provider/model' syntax. LiteLLM auto-routes to the right API endpoint. When using a proxy, adjust the prefix to match its routing. |
| `action_plan_model` | string | `""` | Model ID for action-plan generation. Falls back to default_model when empty. |
| `tool_model` | string | `""` | Model ID used by individual tools. Falls back to default_model when empty. |
| `title_model` | string | `""` | Model ID for session-title generation. Falls back to default_model when empty. |
| `compact_models` | list[string] |  | Priority-ordered list of models for context compaction. On failure, the next model in the list is tried. The keyword "default" resolves to the running agent's model. Example: ["anthropic/claude-haiku-4-5-20251001", "default"] |
| `fallback_models` | list[string] |  | Ordered list of fallback model IDs. On retryable LLM failure, the system tries each in order after exhausting retries on the primary model. Empty = no fallback. |
| `proxy_model_prefix` | string | `openai` | LiteLLM provider prefix prepended to model names when api_base is set. LiteLLM strips this prefix before forwarding the model name to the proxy, so the proxy receives the model ID it advertises in /v1/models. Leave as 'openai' for LiteLLM proxy, Bifrost, and OpenRouter. Only relevant when api_base is configured. |
| `reasoning_effort` | string | `""` | Reasoning effort hint for supported models. One of low, medium, high, none, or empty. |
| `reasoning_effort_models` | list[string] |  | Model name patterns that support the reasoning_effort parameter. |
| `structured_patch_models` | list[string] |  | Model IDs (or glob prefixes ending in '*') that prefer the structured_patch edit tool over search_replace_block. Built-in defaults cover GPT-5/o3/o4/Codex/GPT-4; only set this to override or extend. |

## ContextConfig

Top-level key: `context`

Context window selection and event filtering.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `recent_event_limit` | integer | `8` | Maximum number of recent events injected into the context window. |
| `selection_threshold` | number | `0.8` | Relevance score threshold (0.0-1.0) for the context selector to keep an event. |
| `selection_enabled` | boolean | `true` | Enable LLM-based context event selection. When false, all recent events are used. |
| `context_selector_model` | string | `""` | Model ID for context selection. Falls back to llm.default_model when empty. |

## TokenBudgetConfig

Top-level key: `token_budget`

Token budget and auto-compaction thresholds.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `default_context_window` | integer | `128000` | Default context window size in tokens used when the model is not listed in model_context_windows. |
| `auto_compact_threshold` | number | `0.8` | Fraction of the context window (0.0-1.0) that triggers automatic conversation compaction. |
| `model_context_windows` | dict[str, integer] |  | Override only: per-model context window in tokens. Keys are model names (with or without provider prefix). The authoritative source is LiteLLM's model catalogue; populate this only to cap below the model's real max, or for models LiteLLM doesn't know yet. |

## CompactionConfig

Top-level key: `compaction`

Summarization prompt selection for conversation compaction.

``caveman_mode`` enables a rule-augmented prompt (inspired by the
``JuliusBrussee/caveman`` Claude Code skill) that instructs the
summarizer LLM to drop articles, filler, pleasantries, and hedging
while preserving code, paths, URLs, and error strings verbatim.
Reduces output tokens in the compaction summary without changing the
``<analysis>/<summary>`` response structure downstream parsers expect.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `caveman_mode` | boolean | `false` | Enable caveman-style terse summarization prompt. Drops articles, filler, pleasantries, and hedging in the compacted summary while preserving code, file paths, URLs, and error strings verbatim. Reduces compaction output tokens without changing the response structure downstream parsers expect. |

## ReflectionConfig

Top-level key: `reflection`

Post-execution reflection pass settings.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `true` | Enable a reflection LLM pass after tool execution to verify results. |
| `model` | string | `""` | Model ID for the reflection pass. Falls back to llm.default_model when empty. |

## LangfuseConfig

Top-level key: `langfuse`

Langfuse LLM observability integration.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `false` | Enable Langfuse tracing for all LLM calls. |
| `host` | string | `""` | Langfuse server URL. |
| `project_id` | string | `""` | Langfuse project ID for constructing dashboard URLs. |
| `public_key` | string | `""` | Langfuse project public key. ⚠️ |
| `secret_key` | string | `""` | Langfuse project secret key. ⚠️ |

## HomeAssistantConfig

Top-level key: `home_assistant`

Home Assistant smart-home integration.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `false` | Enable the Home Assistant tool for smart-home control. |
| `url` | string | `""` | Home Assistant API base URL. |
| `token` | string | `""` | Long-lived access token for Home Assistant authentication. ⚠️ |

## PermissionsConfig

Top-level key: `permissions`

Tool execution permission policy.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `policy_path` | string | `""` | Path to a JSON or TOML permission policy file. Empty uses built-in defaults. |
| `approval_mode` | string | `ask` | Default approval mode: 'ask' prompts the user, 'allow' auto-approves, 'deny' blocks. |

## CLIConfig

Top-level key: `cli`

Terminal CLI display and interaction settings.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `disable_textual` | boolean | `false` | Disable the Textual TUI and fall back to plain Rich output. |
| `approval_style` | string | `inline` | Tool-approval UI style: 'inline' (plain prompt), 'textual' (TUI dialog), or 'aider' (diff-style). |

## ChatConfig

Top-level key: `chat`

Legacy config section kept for backward compatibility with app.json files.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `port` | integer | `8501` | TCP port for the legacy chat interface. |
| `address` | string | `127.0.0.1` | Bind address for the legacy chat interface. |

## APIConfig

Top-level key: `api`

REST API authentication.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `master_token` | string | `msk-strong-password` | Bearer token required for all REST API requests. Change from the default before deploying. ⚠️ |

## AgentConfig

Top-level key: `agent`

Sub-agent hypervisor settings.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `true` | Enable the sub-agent spawning system. |
| `max_depth` | integer | `5` | Maximum nesting depth for sub-agent delegation (1 = no sub-agents). |
| `max_concurrent` | integer | `20` | Maximum number of sub-agents allowed to run concurrently. |
| `default_sub_model` | string | `""` | Default LLM model for sub-agents. Falls back to the root agent's model when empty. |
| `allowed_models` | list[string] |  | Allowlist of model names sub-agents may use. Empty means all models are allowed. |
| `llm_call_timeout` | number | `60.0` | Ceiling in seconds for a single model.ainvoke() call. Covers extended-thinking models. On timeout, the call is retried up to llm_call_retries times before cascading to fallback models. |
| `llm_call_retries` | integer | `2` | Maximum retry attempts for the primary model before cascading to fallback_models. Each fallback model gets one attempt. |
| `default_denied_tools` | list[string] |  | Tool IDs denied to all sub-agents by default (e.g. spawn_agent). |
| `edit_tool` | string | `""` | File editing mechanism override: 'search_replace_block' (Aider-style SEARCH/REPLACE blocks) or 'structured_patch' (per-file exact string replacement). Leave empty (default) to auto-select based on the active model via llm.structured_patch_models. |
| `plan_mode_shell_allowlist` | list[string] |  | Shell command prefixes allowed during plan mode. Each entry matches a command at a word boundary (e.g. 'git log' matches 'git log --oneline' but not 'git logger'). Commands containing pipes, redirects, variable expansion, command substitution, or chaining (&#124;, >, <, &, ;, $, backtick) are always rejected. Set to an empty list to disable shell in plan mode entirely. |
| `plan_mode_allow_mcp` | boolean | `true` | Allow ALL user-enabled MCP tools (tools with kind='mcp') during plan mode. Matches Claude Code's permissive default and trusts the user's mcp.json configuration. Set to false to block MCP tools in plan mode regardless of their read-only status. |
| `web_ide` | WebIdeConfig | `null` | Optional 'Open in Web IDE' feature config (code-server containers). |
| `lsp` | LSPConfig |  |  |

??? note "Deprecated fields"

    | Key | Type | Default | Description |
    | --- | ---- | ------- | ----------- |
    | `max_iters` | integer | `30` | Deprecated. The tool-use loop now runs until natural completion (model returns text without tool calls). This field is retained for API backward compatibility but is not enforced. |
    | `sub_agent_max_steps` | integer | `10` | Deprecated. Sub-agents now run until natural completion. This field is retained for API backward compatibility but is not enforced. Safety is provided by session_step_budget, stall detection, and LLM timeouts. |

## HooksConfig

Top-level key: `hooks`

External shell hooks fired during the session lifecycle.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `pre_tool_use` | list[HookEntry] |  | Hooks executed before each tool invocation. |
| `post_tool_use` | list[HookEntry] |  | Hooks executed after each tool invocation. |
| `on_session_start` | list[HookEntry] |  | Hooks executed when a new session begins. |
| `on_session_end` | list[HookEntry] |  | Hooks executed when a session ends. |

## PluginsConfig

Top-level key: `plugins`

Plugin system configuration.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `true` | Enable the plugin system. |
| `enabled_plugins` | list[string] |  | Plugin names to enable. Empty = all installed plugins. Format: 'plugin-name' or 'plugin-name@marketplace'. |
| `marketplaces` | list[string] |  | GitHub repos containing marketplace.json plugin indexes. |
| `install_path` | string | `""` | Override install path for Meeseeks-managed plugins. Defaults to $MEESEEKS_HOME/plugins/ (via resolve_meeseeks_home). |

## Channels

Top-level key: `channels`

Chat platform channel adapters (nextcloud-talk, slack, etc.).

_Structure varies by entry. See the schema source for details._

## Projects

Top-level key: `projects`

Named project directories exposed to the REST API for session scoping.

_Structure varies by entry. See the schema source for details._
