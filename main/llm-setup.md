# LLM Setup

This page covers the minimum LLM configuration required to run bearlike/Assistant.

## Minimum configuration
Set these keys in `configs/app.json`:

```json
{
  "llm": {
    "api_key": "sk-ant-xxxxxxxx",
    "default_model": "anthropic/claude-sonnet-4-6"
  }
}
```

That's it. LiteLLM auto-routes `anthropic/claude-sonnet-4-6` to the Anthropic API using the key you provide. No `api_base` URL is needed for direct provider access.

See the optional configuration table below.

## Optional LLM configuration
| Key | Purpose | Notes |
| --- | --- | --- |
| `llm.api_base` | Base URL override. | Only needed when using a proxy (LiteLLM, Bifrost). Leave empty for direct provider access. |
| `llm.action_plan_model` | Model for plan generation. | Falls back to `llm.default_model` if unset. |
| `llm.tool_model` | Model for tool execution. | Falls back to `llm.action_plan_model`, then `llm.default_model`. |
| `llm.reasoning_effort` | Default reasoning effort level. | Values: `low`, `medium`, `high`, `none`. |
| `llm.reasoning_effort_models` | Allowlist for reasoning effort. | Supports exact matches and `*` suffix wildcards. |
| `llm.proxy_model_prefix` | Prefix prepended to model names when routing through a proxy. | Default: `"openai"`. Set to match your proxy's expected provider prefix. Falls back to `"openai"` when empty. |

## Model fallback

When the primary model fails with a retryable error, Truss walks an ordered fallback
list before giving up. Useful for rate-limit tolerance and provider outages.

Config (in `configs/app.json`):

```json
{
  "llm": {
    "default_model": "anthropic/claude-sonnet-4-6",
    "fallback_models": [
      "openai/gpt-4o",
      "anthropic/claude-haiku-4-5"
    ]
  }
}
```

Each fallback gets one attempt. Error classification:

- **Transient errors** (rate limits, timeouts): retry primary, then cascade
- **Context overflow**: skip to next model
- **Auth errors**: abort immediately (no point trying same provider)

Set `agent.llm_call_retries` for how many times to retry the primary model before cascading (default: 2).

## Short walkthrough
1. Copy the example config:

```bash
cp configs/app.example.json configs/app.json
```

2. Set `llm.api_key` and `llm.default_model` (and `llm.api_base` only if using a proxy).
3. Start a client (CLI, API, or chat). See the client pages for run commands.

## MCP setup
MCP servers are optional. When enabled, they add external tools to the registry.

1. Create `configs/mcp.json` (or run `/mcp init` in the CLI).
2. Add MCP server URLs and headers.
3. Start a client once to auto-discover tools and cache the manifest under `~/.truss/`.

For more details, see [Installation](getting-started.md).

## LiteLLM provider support
The LLM layer is backed by LiteLLM via `langchain-litellm`.

- Use `provider/model` syntax for model IDs (e.g. `anthropic/claude-sonnet-4-6`, `openai/gpt-4o`, `mistral/mistral-small`). LiteLLM routes to the correct API automatically.
- `llm.api_base` is only needed when routing through a proxy. When set with no provider prefix on the model name, the system prepends `llm.proxy_model_prefix` (default `"openai"`) to match OpenAI-compatible endpoints. Override `proxy_model_prefix` if your proxy expects a different prefix.
