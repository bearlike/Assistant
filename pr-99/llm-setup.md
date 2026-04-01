# LLM Setup

This page covers the minimum LLM configuration required to run bearlike/Assistant.

## Minimum configuration
Set these keys in `configs/app.json`:

```json
{
  "llm": {
    "api_base": "https://your-llm-endpoint/v1",
    "api_key": "sk-your-key",
    "default_model": "gpt-5.2"
  }
}
```

See the optional configuration table below.

## Optional LLM configuration
| Key | Purpose | Notes |
| --- | --- | --- |
| `llm.action_plan_model` | Model for plan generation. | Falls back to `llm.default_model` if unset. |
| `llm.tool_model` | Model for tool execution. | Falls back to `llm.action_plan_model`, then `llm.default_model`. |
| `llm.reasoning_effort` | Default reasoning effort level. | Values: `low`, `medium`, `high`, `none`. |
| `llm.reasoning_effort_models` | Allowlist for reasoning effort. | Supports exact matches and `*` suffix wildcards. |

## Short walkthrough
1. Copy the example config:

```bash
cp configs/app.example.json configs/app.json
```

2. Edit the `llm` block with the API base, API key, and model names.
3. Start a client (CLI, API, or chat). See the client pages for run commands.

## MCP setup
MCP servers are optional. When enabled, they add external tools to the registry.

1. Create `configs/mcp.json` (or run `/mcp init` in the CLI).
2. Add MCP server URLs and headers.
3. Start a client once to auto-discover tools and cache the manifest under `~/.meeseeks/`.

For more details, see [Installation](getting-started.md).

## LiteLLM provider support
The LLM layer is backed by LiteLLM via `langchain-litellm`.

- Model names can include provider prefixes (for example, `openai/gpt-4-turbo`, `anthropic/claude-3-sonnet`, or `mistral/mistral-small`).
- If `llm.api_base` is set and a model has no provider prefix, the system defaults to `openai/<model>` to match OpenAI-style endpoints.
