<!-- AUTO-GENERATED from configs/app.schema.json. Do not edit manually. -->
# Configuration Reference

Mewbo is configured via `configs/app.json`. This reference is auto-generated
from the JSON Schema at [`configs/app.schema.json`](repo:configs/app.schema.json).

Copy [`configs/app.example.json`](repo:configs/app.example.json) to
`configs/app.json` to get started.
See [Get Started](getting-started.md) for the full setup walkthrough.

## Runtime

Top-level key: `runtime`

Runtime environment settings.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `envmode` | string | `dev` | Free-text label for this deployment (e.g. dev, staging, prod). Its only effect is being stamped onto every Langfuse trace as the `release` tag, so you can filter and compare traces across environments. |
| `log_level` | string | `DEBUG` | Logging verbosity. One of DEBUG, INFO, WARNING, ERROR, CRITICAL. |
| `log_style` | string | `""` | Legacy override for the CLI's terminal log format. Only the literal value 'dark' has any effect (dims the log line style for a dark background); anything else uses the plain format. Takes priority over cli_log_style when set; leave empty to use that instead. |
| `cli_log_style` | string | `dark` | CLI terminal log format: only the literal value 'dark' has any effect (dims the log line style for a dark background); any other value uses the plain format. Overridden by runtime.log_style when that's set. |
| `preflight_enabled` | boolean | `false` | Run connectivity checks for LLM, Langfuse, and Home Assistant on startup. |
| `developer_mode` | boolean | `false` | Enable developer mode. Unlocks graph-only (no-LLM) repository onboarding so contributors can inspect AST graph construction without documentation generation. Read by the API, console, and CLI. |
| `cache_dir` | string | `""` | Directory for tool caches. Defaults to $MEWBO_HOME/cache. ⚠️ |
| `session_dir` | string | `""` | Directory for session transcripts. Defaults to $MEWBO_HOME/sessions. ⚠️ |
| `config_dir` | string | `""` | Root configuration directory. Defaults to $MEWBO_HOME. ⚠️ |
| `result_export_dir` | string | `""` | Directory for large tool result exports. Empty to disable. |
| `projects_home` | string | `""` | Directory for virtual project folders. Defaults to $MEWBO_HOME/projects. ⚠️ |

## Storage

Top-level key: `storage`

Session storage backend configuration.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `driver` | string | `json` | Storage driver: 'json' (filesystem) or 'mongodb'. |
| `mongodb` | MongoDB |  | MongoDB connection settings (used when driver is 'mongodb'). |
| `mongodb.uri` | string | `mongodb://localhost:27017` | MongoDB connection URI (includes host, port, credentials). |
| `mongodb.database` | string | `mewbo` | MongoDB database name for session storage. |

## Language Model

Top-level key: `llm`

LLM provider connection and model selection.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `api_base` | string | `""` | Optional base URL override. Leave empty for direct provider access (LiteLLM routes automatically from the model prefix). Set only when using a proxy (e.g. LiteLLM, Bifrost). |
| `api_key` | string |  | API key for the LLM provider (e.g. Anthropic, OpenAI) or proxy master key. ⚠️ |
| `default_model` | string | `gpt-5.2` | Model ID using 'provider/model' syntax. LiteLLM auto-routes to the right API endpoint. When using a proxy, adjust the prefix to match its routing. |
| `action_plan_model` | string | `""` | Model ID the orchestrator uses to generate a session's initial plan and, when no explicit model is passed in, as that session's default model. Falls back to default_model when empty. |
| `tool_model` | string | `""` | Model ID used by individual tools. Falls back to default_model when empty. |
| `title_model` | string | `""` | Model ID for session-title generation. Falls back to default_model when empty. |
| `compact_models` | list[string] |  | Priority-ordered list of models for context compaction. On failure, the next model in the list is tried. The keyword "default" resolves to the running agent's model. Example: ["anthropic/claude-haiku-4-5-20251001", "default"] |
| `fallback_models` | list[string] |  | Legacy ordered list of fallback model IDs. Prefer 'fallback' (typed, explicit opt-in). A non-empty value here is still honored for backward compatibility (treated as fallback enabled). |
| `fallback` | Fallback |  | Opt-in cross-model fallback policy (see FallbackConfig). |
| `fallback.enabled` | boolean | `false` | Enable automatic fallback to other models when the primary is exhausted or hits a hopeless-here error. Off by default. |
| `fallback.models` | list[string] |  | Ordered fallback model IDs, tried after the primary is exhausted. |
| `proxy_model_prefix` | string | `openai` | LiteLLM provider prefix prepended to model names when api_base is set. LiteLLM strips this prefix before forwarding the model name to the proxy, so the proxy receives the model ID it advertises in /v1/models. Leave as 'openai' for LiteLLM proxy, Bifrost, and OpenRouter. Only relevant when api_base is configured. |
| `reasoning_effort` | string | `""` | Reasoning effort hint for supported models. One of low, medium, high, none, or empty. |
| `reasoning_effort_models` | list[string] |  | Additional model IDs (or 'prefix*' patterns) that should receive the reasoning_effort parameter, on top of the built-in match for gpt-5, o3, Claude, and Gemini models. Use this to opt in a model the built-in detection doesn't recognize yet. |
| `structured_patch_models` | list[string] |  | Model IDs (or glob prefixes ending in '*') that prefer the structured_patch edit tool over search_replace_block. Runtime override layer ON TOP of the controllable model→tool-variant map (mewbo_core/prompts/model_variants.yaml), which now holds the built-in defaults (GPT-5/o3/o4/Codex/GPT-4); only set this to override or extend without editing that file. |

## Context

Top-level key: `context`

Context window selection and event filtering.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `recent_event_limit` | integer | `8` | Maximum number of recent events injected into the context window. |
| `selection_threshold` | number | `0.8` | Relevance score threshold (0.0-1.0) for the context selector to keep an event. |
| `selection_enabled` | boolean | `true` | Enable LLM-based context event selection. When false, all recent events are used. |
| `context_selector_model` | string | `""` | Model ID for context selection. Falls back to llm.default_model when empty. |

## Token Budget

Top-level key: `token_budget`

Token budget and auto-compaction thresholds.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `default_context_window` | integer | `128000` | Default context window size in tokens used when the model is not listed in model_context_windows. |
| `auto_compact_threshold` | number | `0.8` | Fraction of the context window (0.0-1.0) that triggers automatic conversation compaction. |
| `model_context_windows` | dict[str, integer] |  | Override only: per-model context window in tokens. Keys are model names (with or without provider prefix). The authoritative source is LiteLLM's model catalogue; populate this only to cap below the model's real max, or for models LiteLLM doesn't know yet. |

## Compaction

Top-level key: `compaction`

Summarization prompt selection for conversation compaction.

``caveman_mode`` enables a rule-augmented "caveman" prompt that
instructs the summarizer LLM to drop articles, filler, pleasantries,
and hedging while preserving code, paths, URLs, and error strings
verbatim. Reduces output tokens in the compaction summary without
changing the
``<analysis>/<summary>`` response structure downstream parsers expect.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `caveman_mode` | boolean | `false` | Enable caveman-style terse summarization prompt. Drops articles, filler, pleasantries, and hedging in the compacted summary while preserving code, file paths, URLs, and error strings verbatim. Reduces compaction output tokens without changing the response structure downstream parsers expect. |

## Reflection

Top-level key: `reflection`

Post-execution reflection pass settings.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `true` | Enable a reflection LLM pass after tool execution to verify results. |
| `model` | string | `""` | Model ID for the reflection pass. Falls back to llm.default_model when empty. |

## Langfuse

Top-level key: `langfuse`

Langfuse LLM observability integration.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `false` | Enable Langfuse tracing for all LLM calls. |
| `host` | string | `""` | Langfuse server URL. |
| `project_id` | string | `""` | Langfuse project ID for constructing dashboard URLs. |
| `public_key` | string |  | Langfuse project public key. ⚠️ |
| `secret_key` | string |  | Langfuse project secret key. ⚠️ |

## Home Assistant

Top-level key: `home_assistant`

Home Assistant smart-home integration.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `false` | Enable the Home Assistant tool for smart-home control. |
| `url` | string | `""` | Home Assistant API base URL. |
| `token` | string |  | Long-lived access token for Home Assistant authentication. ⚠️ |

## Permissions

Top-level key: `permissions`

Tool execution permission policy.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `policy_path` | string | `""` | Path to a JSON or TOML permission policy file. Empty uses built-in defaults. |
| `approval_mode` | string | `ask` | Default approval mode: 'ask' prompts the user, 'allow' auto-approves, 'deny' blocks. |

## CLI

Top-level key: `cli`

Terminal CLI display and interaction settings.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `disable_textual` | boolean | `false` | Disable the Textual TUI and fall back to plain Rich output. |
| `remote` | CLI Remote |  | Opt-in remote session sync + product tools (CLI-only). |
| `remote.base_url` | string | `""` | Base URL of the remote Mewbo deployment, a reverse-proxy root that serves the REST API under ``/api`` and the Mewbo MCP server under ``/mcp``. Empty (default) ⇒ the CLI is fully local. |
| `remote.token` | string |  | API token presented to the remote deployment: sent as ``X-API-Key`` to the REST API for transcript sync and as a ``Bearer`` token to the Mewbo MCP server (which forwards it to the REST API). ``${ENV_VAR}`` references are expanded by the CLI at use time. ⚠️ |
| `approval_style` | string | `inline` | Tool-approval UI style: 'inline' (plain prompt), 'textual' (TUI dialog), or 'aider' (diff-style). |

## Chat

Top-level key: `chat`

Legacy config section kept for backward compatibility with app.json files.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `port` | integer | `8501` | TCP port for the legacy chat interface. |
| `address` | string | `127.0.0.1` | Bind address for the legacy chat interface. |

## API Server

Top-level key: `api`

REST API authentication.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `master_token` | string | `msk-strong-password` | Bearer token required for all REST API requests. Change from the default before deploying. ⚠️ |
| `allow_external_cwd` | boolean | `false` | Allow callers to anchor sessions in an arbitrary host path via the `cwd` field on POST /api/sessions and POST /api/sessions/{id}/query. Off by default; enable only for trusted external workspace managers that manage their own worktrees. |
| `apps_token_secret` | string |  | Signing secret for Mewbo Apps render tokens — the short-lived, app-scoped read tokens the served app frontend presents on the read-only data/system endpoints. Set this to sign (and rotate) app tokens independently of the master token; when left empty it falls back to the master token, logging one startup warning. ⚠️ |
| `auth` | Authentication |  | Identity & access management (opt-in; off by default). Configures authenticators, roles, and sessions. Documented in `docs/authentication.md`. |
| `auth.enabled` | boolean | `false` | Master switch for identity & access management. When off (the default), every request resolves to the built-in full-power identity and the server behaves exactly as it did before IAM. Turn on only after configuring at least one authenticator. |
| `auth.authenticators` | list[Authenticator] |  | Ordered list of identity sources (local API keys, OIDC, trusted reverse-proxy headers, LDAP, SAML). Each entry is an object whose `kind` field selects the authenticator type, plus that type's own settings. Validated in full at server startup; an invalid entry stops the server from booting. Each authenticator type's own settings are documented in `docs/authentication.md`. |
| `auth.role_mappings` | object | `null` | Rules mapping identity-provider group names to Mewbo roles at login: an object with an ordered `rules` list and a `default_role` applied when no rule matches. The rule format is documented in `docs/authentication.md`. |
| `auth.team_mappings` | object | `null` | Rules mapping identity-provider group names to team slugs at login: an object with an ordered `rules` list. The rule format is documented in `docs/authentication.md`. |
| `auth.bootstrap` | object | `null` | Cold-start admin rule: grants the admin role to the first users matching an identity-provider group or an explicit subject allowlist, so an administrator exists before any role has been assigned. Documented in `docs/authentication.md`. |
| `auth.session` | Session |  | Browser session/cookie settings for federated logins. |
| `auth.session.cookie_name` | string | `mewbo_session` | Name of the browser session cookie issued after a federated login. |
| `auth.session.ttl_seconds` | integer | `28800` | Lifetime of a browser session, in seconds. |
| `auth.session.secret` | string |  | Signing secret for the browser session cookie. Required once any non-API-key authenticator is configured. Write-only: never returned by the config API. ⚠️ |
| `auth.avatars` | object | `null` | Avatar-resolution policy: whether to fall back to Gravatar for users without a profile picture, and the default image style. Documented in `docs/authentication.md`. |
| `auth.scim` | SCIM |  | SCIM 2.0 provisioning settings. |
| `auth.scim.enabled` | boolean | `false` | Whether the SCIM 2.0 provisioning endpoint is served. |
| `auth.scim.secret` | string |  | Bearer secret an identity provider presents to the SCIM endpoint. Write-only: never returned by the config API. ⚠️ |
| `auth.audit` | object | `null` | Auth audit-trail settings: an object with an `enabled` flag; on by default once IAM is enabled. The events recorded are listed in `docs/authentication.md`. |

## Agent

Top-level key: `agent`

Sub-agent hypervisor settings.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `true` | Enable the sub-agent spawning system. |
| `max_depth` | integer | `5` | Maximum nesting depth for sub-agent delegation (1 = no sub-agents). |
| `max_concurrent` | integer | `20` | Maximum number of sub-agents allowed to run concurrently. |
| `default_sub_model` | string | `""` | Default LLM model for sub-agents. Falls back to the root agent's model when empty. |
| `allowed_models` | list[string] |  | Allowlist of model names sub-agents may use. Empty means all models are allowed. |
| `model_tiers` | dict[str, string] |  | Coarse model-cost tier -> concrete model id map (e.g. {'economy': 'anthropic/claude-haiku-4-5', 'frontier': 'anthropic/claude-opus-4-6'}), resolved by a spawned agent's DelegationContract.model_tier. An explicit spawn model arg or an agent_type's configured model always wins; a declared tier with no map entry is a silent no-op fallthrough. |
| `session_step_budget` | integer | `0` | Ceiling on total tool-execution steps across every agent in a session (root + all sub-agents combined), enforced by the hypervisor: a warning is injected as the budget nears, and the run hard-stops at exhaustion. 0 = unlimited. |
| `attestation_enabled` | boolean | `true` | Record a best-effort provenance hash chain over every spawn/terminal transition in a session's agent tree — bounded scalars + a contract snapshot + a summary FINGERPRINT only, never task text or raw summary content. Additive and never fatal: a failed or absent chain degrades to the historical no-attestation behaviour. Default ON; this is the kill switch. |
| `default_workspace_mode` | string | `full_access` | Root filesystem-containment tier every session starts at, narrowed per sub-agent by spawn_agent's workspace_mode. One of 'read_only' (reads confined to the workspace, no writes), 'workspace_write' (reads + writes confined to the workspace), or 'full_access' (no path restriction; default, historical behaviour). Only bites when workspace_enforcement is on. |
| `workspace_enforcement` | boolean | `false` | Master switch for workspace_mode filesystem containment. OFF by default (staged): while off, every path resolves through the historical tenant-union allowlist regardless of an agent's workspace_mode, so the tiers are carried + narrowed but never enforced. Flip on to collapse each contained agent's reachable paths to its own workspace root + Mewbo scratch. |
| `stall_threshold_s` | number | `120.0` | Seconds of no tool-execution progress before the watchdog flags an agent as stalled and injects an NL warning into its message queue. |
| `stall_check_interval_s` | number | `30.0` | Seconds between watchdog stall-detection sweeps. |
| `write_progress_signal_step_threshold` | integer | `25` | Consecutive non-write tool-execution steps before the write-progress signal fires telemetry for a write-capable agent. 0 disables. |
| `write_progress_signal_event_interval` | integer | `10` | Steps between repeat write-progress signal events once the threshold is crossed. |
| `write_progress_signal_max_events` | integer | `2` | Maximum write-progress signal events emitted before it goes quiet. |
| `write_progress_signal_reminder_enabled` | boolean | `false` | When the write-progress signal fires, also inject a criterion-blind objective-restatement reminder (states the task goal only — never the signal or its criteria). Default off: telemetry alone is the observe-only default. |
| `verification_enabled` | boolean | `false` | Master switch for verifier-gated completion. OFF by default (staged): while off, a spawn's verification spec is carried but never run, so every natural completion is accepted exactly as historically. Flip on to gate a write-capable agent's claimed completion behind a ground-truth command check before its text is accepted. |
| `verification_max_retries` | integer | `2` | Maximum times a failed completion verifier re-drives the agent before its text is accepted, honestly flagged verification_failed. Clamped to [0, 10] and bounded ALSO by the step/wall budget, whichever is tighter. 0 = one check, no retry. |
| `verification_timeout_s` | number | `60.0` | Ceiling in seconds on a single verifier subprocess; a spawn's per-spec timeout_s is clamped down to this at run. Clamped to [1, 600]. |
| `llm_call_timeout` | number | `120.0` | Ceiling in seconds for a single model.ainvoke() call. Covers extended-thinking models (raised from 60s because bare timeouts were the largest single failure class). On timeout, the call is retried up to llm_call_retries times before cascading to fallback models. |
| `llm_call_retries` | integer | `2` | Maximum attempts for the primary model before cascading to fallback models (default 2 = one try + one retry). Each fallback model gets retry.fallback_retries attempts. A rescue model that wins is pinned for the rest of the run. Backoff/budget/circuit-breaker live under agent.retry. |
| `retry` | Retry |  | Automatic LLM-call retry / fallback resilience knobs. |
| `retry.backoff_base` | number | `1.0` | Base seconds for full-jitter backoff: random(0, min(cap, base*2^(n-1))). |
| `retry.backoff_cap` | number | `60.0` | Maximum backoff delay in seconds. |
| `retry.retry_after_cap` | number | `60.0` | Upper bound in seconds applied to a server's Retry-After header before the loop sleeps on it; caps how long one misbehaving response can stall a run. |
| `retry.turn_deadline` | number | `240.0` | Wall-clock seconds budget for one logical LLM call across all retries and fallbacks. Checked before each attempt. 0 disables. |
| `retry.fallback_retries` | integer | `1` | Attempts per fallback model after the primary is exhausted. |
| `retry.circuit_breaker_threshold` | integer | `3` | Consecutive per-model failures before that model is cooled down and skipped (when an alternative exists). 0 disables. |
| `retry.circuit_breaker_cooldown` | number | `30.0` | Seconds a model is skipped after tripping the circuit breaker. |
| `retry.budget_capacity` | number | `24.0` | Token-bucket retry budget. Retries stop once the bucket drops to half capacity, so a sustained outage fails fast instead of storming. |
| `retry.doom_loop_threshold` | integer | `3` | Halt cleanly when the model repeats the same tool + identical input this many times in a row (no progress). 0 disables. |
| `default_denied_tools` | list[string] |  | Tool IDs denied to all sub-agents by default (e.g. spawn_agent). |
| `edit_tool` | string | `""` | File editing mechanism override: 'search_replace_block' (Aider-style SEARCH/REPLACE blocks) or 'structured_patch' (per-file exact string replacement). Leave empty (default) to auto-select based on the active model via llm.structured_patch_models. |
| `plan_mode_shell_allowlist` | list[string] |  | Shell command prefixes allowed during plan mode. Each entry matches a command at a word boundary (e.g. 'git log' matches 'git log --oneline' but not 'git logger'). Commands containing pipes, redirects, variable expansion, command substitution, or chaining (&#124;, >, <, &, ;, $, backtick) are always rejected. Set to an empty list to disable shell in plan mode entirely. |
| `web_ide` | Web IDE | `null` | Optional 'Open in Web IDE' feature config (code-server containers). |
| `web_ide.enabled` | boolean | `false` | Turn on the 'Open in Web IDE' feature (per-session code-server containers via Docker). Also requires a MongoDB-backed session store; toggling this needs an API process restart to take effect since the /api/ide routes are registered at startup. |
| `web_ide.image` | string | `codercom/code-server:latest` | Docker image used to launch each session's code-server container. |
| `web_ide.default_lifetime_hours` | integer | `1` | Hours a new Web IDE container stays up before it self-terminates, unless the session extends it first. |
| `web_ide.max_lifetime_hours` | integer | `8` | Hard ceiling on a session's total Web IDE lifetime across all extensions; a request to extend past this is rejected. |
| `web_ide.cpus` | number | `1.0` | CPU core limit for each Web IDE container, e.g. 1.0 = one core (maps to Docker's --cpus / nano_cpus). |
| `web_ide.memory` | string | `1g` | Memory limit for each Web IDE container, in Docker's --memory syntax: digits followed by m or g, e.g. '1g' or '512m'. |
| `web_ide.pids_limit` | integer | `512` | Maximum number of processes/threads allowed inside a Web IDE container; bounds a runaway process from exhausting the host. |
| `web_ide.network` | string | `mewbo-ide` | Docker network each Web IDE container joins. Must be the same network the ide-proxy is attached to, or the proxy can't reach the container. |
| `web_ide.proxy_url` | string | `http://127.0.0.1:5126` | Base URL the API uses to reach the ide-proxy for readiness probes. The default suits a host-networked API, where the proxy is published on loopback 127.0.0.1:5126. A bridge-networked API (e.g. one joined to extra Docker networks via a compose override) cannot reach that loopback: attach it to the `network` above and point this at the proxy's in-network name, e.g. http://mewbo-ide-proxy:8080. |
| `web_ide.state_dir` | string | `/tmp/mewbo-ide` | Host directory where each Web IDE container's expiry-deadline file is written; the container's internal watchdog reads it to self-terminate on schedule. |
| `lsp` | LSP |  | Language Server Protocol integration settings. |
| `lsp.enabled` | boolean | `true` | Master switch for the native LSP tool (hover/diagnostics/go-to-definition). When off, or when the pygls dependency isn't installed, the tool is never registered and the agent works from grep/read alone. |
| `lsp.servers` | dict[str, object] |  | Override or extend built-in server definitions. Set {"pyright": {"disabled": true}} to disable a built-in, or add custom servers with command/extensions/root_markers. |
| `tool_search` | Tool Search |  | Deferred tool loading via on-demand schema fetching. |
| `tool_search.mode` | string | `auto` | 'off' keeps every tool's schema in the initial bind. 'on' always defers MCP tools and any spec with metadata.deferred=True; the model loads schemas on demand via tool_search. 'auto' defers only when the number of deferrable tools exceeds auto_threshold, so lean / zero-MCP sessions keep verbatim binding and pay nothing, while many-MCP sessions are spared ~240 tokens per tool every turn. |
| `tool_search.auto_threshold` | integer | `25` | In 'auto' mode, defer tool schemas only when more than this many deferrable tools (MCP + metadata.deferred specs) are registered. Ignored when mode is 'off' or 'on'. |

??? note "Deprecated fields"

    | Key | Type | Default | Description |
    | --- | ---- | ------- | ----------- |
    | `max_iters` | integer | `30` | Deprecated. The tool-use loop now runs until natural completion (model returns text without tool calls). This field is retained for API backward compatibility but is not enforced. |
    | `sub_agent_max_steps` | integer | `10` | Deprecated. Sub-agents now run until natural completion. This field is retained for API backward compatibility but is not enforced. Safety is provided by session_step_budget, stall detection, and LLM timeouts. |

## Wiki

Top-level key: `wiki`

Operator-facing knobs for the wiki subsystem.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `default_model` | string | `""` | Model the wiki picker pre-selects for indexing (the wizard). Overrides ``llm.default_model`` for the wizard. Empty string means: fall back to ``llm.default_model``. |
| `default_qa_model` | string | `""` | Model the Q&A composer pre-selects. Typically smaller/faster than ``default_model`` because Q&A is a tight read-only loop where latency matters more than depth. Empty string means: fall back to ``default_model``, then to ``llm.default_model``. |
| `default_depth` | string | `""` | Indexing depth the wizard pre-selects. Empty string means: use the wizard's own default (``comprehensive``). |
| `default_language` | string | `""` | Language code the wizard pre-selects (e.g. ``en``, ``es``). Empty string means: use the wizard's own default. |
| `embedding` | Embedding |  | Embedding settings for the wiki indexer. |
| `embedding.enabled` | boolean | `true` | When false, ``wiki_build_graph`` skips embedding generation and retrieval falls back to BM25 + graph traversal only. |
| `embedding.model` | string | `openai/text-embedding-3-small` | Embedding model ID routed through the LLM proxy. Must support the OpenAI ``/v1/embeddings`` shape (LiteLLM normalises Gemini and others to this shape). Pin a fast model here to speed up indexing, since embedding is per-node and runs synchronously. |
| `embedding.batch_size` | integer | `64` | Number of graph nodes embedded per API call during indexing. Higher values index faster but send larger requests; lower it if you hit the embedding provider's rate or payload limits. |
| `memory` | Memory |  | Multiplex memory-layer knobs (atomic insights over the graph). |
| `memory.enabled` | boolean | `true` | Master switch for the memory layer (gates wiki_submit_insight). |
| `memory.model` | string | `""` | Chat model for condense + LLM dedup on the human/REST/MCP path. Empty → falls back to default_qa_model, then default_model. |
| `memory.max_insight_chars` | integer | `200` | Hard cap on a memory note's length. |
| `memory.max_anchors` | integer | `8` | Max code anchors per note. |
| `memory.dedup_k` | integer | `5` | kNN candidate window for fuzzy + LLM dedup tiers. |
| `memory.dedup_cosine` | number | `0.6` | Cosine floor for the LLM dedup tier. |
| `memory.fuzzy_jaccard` | number | `0.85` | Jaccard floor for the fuzzy dedup tier. |
| `memory.fusion_w_ppr` | number | `0.1` | Weight applied to a code node's score when it's surfaced only by following a memory note's anchor rather than direct text/code search. Raise it to rank memory-anchored context higher relative to direct hits; lower it toward 0 to favor direct hits. |
| `memory.hub_degree` | integer | `50` | Degree above which an anchor is hub-damped. |
| `memory.expansion_hops` | integer | `1` | Structural hops to expand from an anchor. |
| `refresh` | Refresh |  | On-demand incremental-refresh thresholds. |
| `refresh.default_mode` | string | `auto` | Default re-index strategy when none is requested. |
| `refresh.closure_max_depth` | integer | `4` | Reverse-dependency closure depth cap. |
| `refresh.drift_keep` | number | `0.9` | Cosine ≥ this keeps a memory anchor (no LLM). |
| `refresh.drift_invalidate` | number | `0.75` | Cosine < this invalidates a memory anchor. |
| `refresh.page_keep` | number | `0.05` | Doc staleness < this → keep. |
| `refresh.page_edit` | number | `0.35` | Doc staleness < this → edit. |
| `refresh.page_regen` | number | `0.7` | Doc staleness ≥ this → regenerate + review. |
| `refresh.new_page_min` | integer | `5` | Uncovered public symbols to propose a new page. |
| `refresh.require_scope_confirm` | boolean | `false` | Require a human gate on the scope preview before the act phase. |

## SCG

Top-level key: `scg`

Operator-facing knobs for the Source Capability Graph (agentic search).

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `false` | Master switch for the SCG feature (source mapping and orchestrated agentic-search runs). |
| `traversal` | Traversal |  | Traversal defaults (the per-run search tier). |
| `traversal.default_tier` | string | `auto` | Default search tier, one budget knob over decomposition depth and probe fan-out. Overridable per run. |
| `traversal.tier_models` | Tier models |  | Which LLM each search tier runs on (fast/auto/deep). |
| `traversal.tier_models.fast` | string | `openai/gpt-oss-120b` | Model for `fast` tier runs (the tier still sets the low-latency budget). |
| `traversal.tier_models.auto` | string | `openai/gpt-oss-120b` | Model for `auto` tier runs (the tier still sets the balanced budget). |
| `traversal.tier_models.deep` | string | `openai/gpt-oss-120b` | Model for `deep` tier runs (the tier still sets the exhaustive budget). |

## Hooks

Top-level key: `hooks`

External shell hooks fired during the session lifecycle.

Command hooks run unsandboxed shell commands with the API/CLI process's
own privileges (see ``HookEntry.command``'s docstring) — a caller who can
PATCH this section can execute arbitrary code on the host. ``x-protected``
puts the whole section in the same never-read-never-written-via-API tier
as the other host-level settings in this file: settable only by editing
the config file directly, never over the network regardless of
credential (see ``ConfigSchemaView`` in ``apps/mewbo_api``).

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `pre_tool_use` | list[Hook] |  | Hooks executed before each tool invocation. |
| `post_tool_use` | list[Hook] |  | Hooks executed after each tool invocation. |
| `on_session_start` | list[Hook] |  | Hooks executed when a new session begins. |
| `on_session_end` | list[Hook] |  | Hooks executed when a session ends. |
| `on_event` | list[Hook] |  | Hooks executed (fire-and-forget) for every event appended to a session transcript. The matcher fnmatches the event type. |

## Plugins

Top-level key: `plugins`

Plugin system configuration.

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `true` | Turn the whole plugin system on, including Mewbo's own built-in suites such as `widget_builder`.

While this is off, no plugin contributes anything to the agent, whether it is built in or installed from a marketplace: no agent definitions, no skills, no hooks, no MCP tools. The `enabled_plugins` and `marketplaces` settings below are ignored entirely until you turn it back on. |
| `enabled_plugins` | list[string] |  | Plugin names to enable. Empty = all installed plugins. Format: 'plugin-name' or 'plugin-name@marketplace'. |
| `marketplaces` | list[string] |  | Marketplace catalogs holding a marketplace.json plugin index, on any git host. Each entry is a full git URL (https/ssh/git, or scp-style git@host:owner/repo), a 'host/owner/repo' shorthand, or a bare 'owner/repo' (cloned from marketplace_default_host). |
| `marketplace_default_host` | string | `github.com` | Default git host for bare 'owner/repo' marketplace entries. Full URLs and 'host/owner/repo' entries ignore this. |
| `install_path` | string | `""` | Override install path for Mewbo-managed plugins. Defaults to $MEWBO_HOME/plugins/ (via resolve_mewbo_home). |

## Triggers

Top-level key: `triggers`

Reverse-invocation trigger subsystem.

The durable peer of the sub-agent hypervisor: a background watcher that
fires time / cron / CI / forge-PR / webhook triggers and re-invokes the
sessions that armed them. OFF by default (``enabled=False``), so the feature
is un-enableable until an operator turns it on and a stock deployment pays
nothing. The lower half of this section is the admission policy the
``schedule_trigger`` tool + the arm route enforce (mirrors
``mewbo_core.triggers.policy.TriggerPolicy`` field-for-field; ``to_policy``
builds one).

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `enabled` | boolean | `false` | Turn the trigger watcher on. Nothing fires until you do.

A trigger is how Mewbo starts a session later, on its own, with nobody watching: at a set time, on a repeating schedule, or when a CI run finishes, a pull request changes, or a webhook calls in. The watcher is the background loop that notices those moments and wakes the session that asked to be woken. While it is off, the trigger routes still work, so a session can arm a trigger and you can list, pause, or cancel it, but no trigger ever fires. Armed triggers simply wait until you turn the watcher on. |
| `tick_interval_seconds` | number | `5.0` | How often the watcher wakes up to look at the schedule, in seconds.

On each pass it expires the triggers whose deadline has gone by and fires the time and cron triggers that have come due. A shorter interval wakes a session closer to the moment it asked for; a longer one costs the server less. This is also the cadence at which the forge poll below gets a chance to run. |
| `poll_interval_seconds` | number | `60.0` | How often the watcher asks the forge about CI runs and pull requests, in seconds.

Time and cron triggers can be judged from the clock alone, but `ci.workflow` and `forge.pr` triggers cannot: the watcher has to call the forge's REST API to see what changed. Those calls are rate-limited and cost a round trip each, so they run on this deliberately coarser cadence rather than on every pass. Raise it if you are bumping into API limits; lower it if you want CI results picked up sooner. |
| `max_consecutive_failures` | integer | `5` | How many errors in a row one trigger may hit before it is given up on.

When a fire or a forge poll raises, the watcher records the error on the trigger and leaves it armed, so a passing outage never throws away a schedule. Once a trigger has failed this many times back to back without a single success in between, the watcher stops retrying it and moves it to `failed`. Any success resets the count to zero. |
| `max_armed_per_session` | integer | `20` | The most triggers one session may have armed at the same time.

Triggers are armed by the agent from inside a session, so this ceiling is what keeps a single session from filling the schedule with wakes. An attempt to arm one past the limit is refused, and the agent is told why. Cancelling a trigger, or letting one finish, frees the slot again. |
| `max_fires_cap` | integer | `100` | The ceiling on how many times any single trigger may fire.

A repeating trigger, a cron schedule for instance, can name its own `max_fires` limit when it is armed. This is the ceiling on that request: an attempt to arm a trigger asking for more is refused. A trigger that reaches its own limit completes and stops firing. |
| `default_expiry_days` | number | `7.0` | How long an armed trigger lives when it names no expiry of its own, in days.

Every trigger expires eventually, so that a wake nobody remembers arming cannot linger forever. When the agent arms one without setting an expiry date, this many days from the moment of arming is stamped on it. Once that moment passes, the watcher expires the trigger instead of firing it. |
| `cron_min_interval_seconds` | integer | `60` | The shortest gap allowed between two fires of a cron trigger, in seconds.

A cron expression can be written to fire far more often than a session is worth waking, so this is the floor. When a cron trigger is armed, the gap between its first two fires is measured, and a schedule tighter than this is rejected there and then rather than being throttled later. |
| `webhook_payload_max_bytes` | integer | `200000` | How much of an incoming webhook body the woken session gets to see, in bytes.

A webhook can carry a large payload, and all of it becomes context the session has to read. A body bigger than this is truncated rather than rejected: the call still fires the trigger, the session receives the first part of the body, and it is told the payload was cut short. When a signature is configured, it is checked against the whole body before any truncation happens. |

## Channels

Top-level key: `channels`

Chat platform channel adapters (nextcloud-talk, slack, etc.).

_Structure varies by entry. See the schema source for details._

## Projects

Top-level key: `projects`

Directories you've already created, registered here by hand under a short name; sessions reference them by that name (e.g. `"project": "<key>"`). Distinct from Mewbo-managed ("virtual") projects, which the API creates and owns itself and which sessions reference as `managed:<project_id>`: entries here are never created, modified, or deleted by Mewbo, only pointed at.

_Structure varies by entry. See the schema source for details._
