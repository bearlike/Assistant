# CLI Client

<video controls preload="metadata" poster="../meeseeks-console-01-front.jpg" style="width: 100%; max-width: 960px; height: auto; display: block; margin: 0 auto;">
  <source src="../meeseeks-cli-01-video.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

The CLI client lives in `apps/meeseeks_cli/`. It is a terminal-native client for developers working in the local environment, running the core runtime in-process and executing tools directly against local files and shell commands. The key distinction from the Meeseeks Console (web client) is execution context and autonomy: the CLI operates directly on the local machine, while the web client delegates through the API. Both clients share the same core runtime and can run long-lived tasks.

## Setup (uv)
```bash
uv sync --extra cli
```

Before running, complete [Installation](getting-started.md) and [LLM setup](llm-setup.md).

## Run
```bash
uv run meeseeks
```

## CLI flags
| Flag | Purpose |
| --- | --- |
| `--query "..."` | Run a single query and exit. |
| `--model MODEL_NAME` | Override the configured model for this run. |
| `--max-iters N` | Maximum orchestration iterations (default: 3). |
| `--show-plan` | Show the action plan (default). |
| `--no-plan` | Hide the action plan. |
| `-v`, `--verbose` | Increase log verbosity (`-v` = debug, `-vv` = trace). |
| `--debug` | Hidden debug flag for CLI logging. |
| `--session SESSION_ID` | Resume a session by id. |
| `--tag TAG` | Resume or create a tagged session. |
| `--fork SESSION_OR_TAG` | Fork from another session. |
| `--session-dir PATH` | Override transcript storage path. |
| `--history-file PATH` | Override CLI history file path. |
| `--no-color` | Disable ANSI color output. |
| `--auto-approve` | Auto-approve tool permissions for the session. |

## Slash commands
| Command | Description | Notes |
| --- | --- | --- |
| `/help` | Show help. |  |
| `/exit` | Exit the CLI. |  |
| `/quit` | Exit the CLI. | Alias for `/exit`. |
| `/new` | Start a new session. |  |
| `/session` | Show current session id. |  |
| `/summary` | Show current session summary. |  |
| `/summarize` | Summarize and compact this session. | Uses `/compact` under the hood. |
| `/compact` | Compact session transcript. | Alias for `/summarize`. |
| `/status` | Show current session status. |  |
| `/terminate` | Cancel the active session run. |  |
| `/tag NAME` | Tag this session. |  |
| `/fork [TAG]` | Fork the current session. | Optional tag for the forked session. |
| `/plan on\|off` | Toggle plan display. |  |
| `/mode act\|plan` | Set orchestration mode. |  |
| `/mcp` | List MCP tools and servers. | Use `/mcp select` or `/mcp init`. |
| `/config` | Manage config files. | Use `/config init`. |
| `/init` | Scaffold app + MCP example configs. |  |
| `/models` | Open the model selection wizard. | Interactive mode only. |
| `/automatic [on\|off]` | Auto-approve tool actions. | Use `--yes` to confirm in non-interactive mode. |
| `/tokens` | Show token usage and remaining context. |  |
| `/budget` | Show token usage and remaining context. | Alias for `/tokens`. |
