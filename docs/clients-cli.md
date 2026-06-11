# CLI Client

<video controls preload="metadata" poster="../mewbo-console-01-front.png" style="width: 100%; max-width: 960px; height: auto; display: block; margin: 0 auto;">
  <source src="../mewbo-cli-01-video.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

The CLI is a terminal-native client for developers working in their local environment. It runs the core runtime in-process, so tools execute directly against your local files and shell. There is no API round-trip. The main difference from the web console is execution context and autonomy. The CLI operates on your local machine, while the web console delegates work through the API. Both clients share the same underlying runtime and can run long-lived tasks.

See [Get Started](getting-started.md#cli-setup) for installation.

## Run
```bash
uv run mewbo
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
| `--fallback-models LIST` | Comma-separated fallback model ids for this run (alias: `--fallback-model`). |
| `--no-fallback` | Disable model fallback for this run. Overrides `--fallback-models` and the config ladder. |
| `--config PATH` | Path to app config file (default: auto-discover). |

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
| `/retry` | Re-run the last user query after a failed run. |  |
| `/continue` | Resume after a failed run with a recovery prompt. |  |
| `/tag NAME` | Tag this session. |  |
| `/fork [TAG]` | Fork the current session. | Optional tag for the forked session. |
| `/plan on\|off` | Toggle plan display. |  |
| `/mode act\|plan` | Set orchestration mode. |  |
| `/mcp` | List MCP tools and servers. | Use `/mcp select` or `/mcp init`. |
| `/config` | Manage config files. | Use `/config init`. |
| `/init` | Scaffold app + MCP example configs. |  |
| `/models` | Open the model selection wizard. | Interactive mode only. |
| `/plugins [marketplace\|install\|uninstall]` | List installed plugins or manage them. | Supports marketplace browsing and install/uninstall. |
| `/automatic [on\|off]` | Auto-approve tool actions. | Use `--yes` to confirm in non-interactive mode. |
| `/tokens` | Show token usage and remaining context. |  |
| `/budget` | Show token usage and remaining context. | Alias for `/tokens`. |

## Session management

### Tags and resuming

Sessions can be tagged for easy retrieval:

```bash
mewbo --tag my-project       # create or resume a tagged session
mewbo --session <session-id> # resume by ID
```

### Forking

Fork a session to branch from any point in its history:

```bash
mewbo --fork my-project      # fork from a tagged session at its current state
```

Inside a running session, `/fork [tag]` creates a new session branching from the current conversation state. The optional tag names the fork for later retrieval.

### Forking from a message

In the console, every message has a "Fork from here" option that creates a new session with history up to that point. In the CLI, use `/fork` to branch from the current session state. The API equivalent is `POST /api/sessions` with `fork_from` and `fork_at_ts`.

## Token usage

View current token consumption at any time:

```bash
/tokens   # or /budget (alias)
```

A usage summary appears below each response showing:

- Root input/output tokens and LLM call count
- Sub-agent rollup (if any agents were spawned)
- Compaction count and tokens saved

See [Token Usage & Caching](features-token-usage.md) for details.

## Model fallback and resilience notices

When a model fails mid-run, the runtime retries it, then escalates down a fallback ladder of alternate models. The ladder comes from the `llm.fallback` config by default. Two flags control it per run:

```bash
mewbo --fallback-models gpt-5.4,gemini-2.5-pro  # override the ladder for this run
mewbo --no-fallback                             # disable fallback entirely
```

`--fallback-model` is a singular alias for `--fallback-models`. `--no-fallback` wins when both are given.

After each response, the CLI replays the run's resilience events as concise one-line notices. You see what the runtime did without digging through logs:

- Retry: `↻ Retrying <model> after <error> (attempt/max, delay)`
- Fallback: `⤳ Falling back: <from> → <to> (<reason>)`, with `[pinned for run]` when the new model is kept for the rest of the run
- Halt: `⊘ Halted: repeated '<tool>' with no progress`, pointing at `/retry` and `/continue`

Only the latest turn's notices print, so multi-turn sessions never repeat old ones. When a run ends in a recoverable state, a hint line follows: `/continue` resumes with context intact, `/retry` redoes the last step.

## Trace identity

Every CLI run tags its Langfuse trace with the `cli` client surface, so you can filter terminal work apart from console and API traffic.
