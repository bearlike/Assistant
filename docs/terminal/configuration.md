# Configuration

The terminal client reads its behavior from three places. Command-line flags set what one launch does. Config files set the defaults. Slash commands manage the session while it runs. This page covers all three.

## Command-line flags

Pass these flags to `mewbo` at launch. The parser is defined in [`cli_master.py`](repo:apps/mewbo_cli/src/mewbo_cli/cli_master.py).

| Flag | Purpose |
|------|---------|
| `--query "..."` | Run a single query and exit. This is the non-interactive path. |
| `--model MODEL` | Override the configured model for this run. |
| `--fallback-models LIST` | Comma-separated fallback model ids for this run. `--fallback-model` is a singular alias. |
| `--no-fallback` | Disable model fallback for this run. It overrides `--fallback-models`. |
| `--max-iters N` | Maximum orchestration iterations. Default is 3. |
| `--show-plan` | Show the action plan. This is the default. |
| `--no-plan` | Hide the action plan. |
| `-v`, `--verbose` | Increase log verbosity. `-v` is debug, `-vv` is trace. |
| `--session ID` | Resume an existing session by id. |
| `--tag TAG` | Resume or create a tagged session. |
| `--fork SOURCE` | Fork from another session id or tag. |
| `--session-dir PATH` | Override the session storage directory. |
| `--history-file PATH` | Override the CLI history file path. |
| `--no-color` | Disable ANSI color output. |
| `--auto-approve` | Approve every permission prompt automatically for the session. |
| `--log-file PATH` | Stream all logs to this file and keep the terminal output clean. |
| `--log-overwrite` | Truncate the log file at startup instead of appending. `--overwrite` is an alias. |
| `--log-console` | With a log file set, also keep logs on stderr. |
| `--config PATH` | Path to the app config file. The default is auto-discover. |

## Model fallback

When a model fails mid-run, the client retries it, then escalates down a ladder of alternate models. The ladder comes from the `llm.fallback` config by default. Two flags override it per run.

```bash
mewbo --fallback-models gpt-5.4,claude-sonnet-5
mewbo --no-fallback
```

The first line sets the ladder for this launch only. The second disables fallback entirely. `--no-fallback` wins when you pass both.

After each turn, the client replays the run's resilience events as short notices. You see a retry, a fallback, or a halt without opening a log. When a fallback pins a new model for the rest of the run, the notice says so.

## The config chain

Mewbo loads config from the first directory that contains it, checked in this order.

1. `CWD/configs/`: project-local config, highest priority.
2. `$MEWBO_HOME/`: a custom home directory, if you set one.
3. `~/.mewbo/`: the user-home fallback.

To point the app at one specific file and skip discovery, pass `--config`. To scaffold the config files from scratch, run `/init` from inside the app. It creates both the app config and an MCP example config.

The two files you edit most are the app config, [`configs/app.example.json`](repo:configs/app.example.json), which holds runtime settings and LLM keys, and the MCP config, [`configs/mcp.json`](repo:configs/mcp.json), which defines your MCP servers. For every key in the app config, see the site's [Configuration Reference](../configuration.md).

## Session recovery

A session is durable. You can leave one and come back to it.

Resume an existing session at launch with a flag. Pass `--session` with its id, or `--tag` with its tag, or `--fork` to branch a new session from another one. Inside the app, press `ctrl+s` or type `/resume` to open the session switcher and pick one.

Recover a run that ended badly with a command. A killed or incomplete run is recoverable, and the client offers two ways back.

- `/continue` resumes the same session. Its memory and transcript stay intact. The agent picks up where it left off without redoing finished work.
- `/retry` restarts the last turn from a clean slate. It re-runs the last step.

Both paths run through the same engine, so a recovered run inherits the model fallback ladder like any other run.

## Next steps

- [The Interface](interface.md): the live transcript, the plan dock, and approval prompts.
- [Agent Fleet](agent-fleet.md): sub-agent orchestration and the fleet sidebar.
- [Remote Sync](remote-sync.md): the local-first default and the opt-in remote seam.
- [Configuration Reference](../configuration.md): every config key on the site, with defaults.
