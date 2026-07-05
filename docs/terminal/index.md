# Get Started

Run Mewbo agent sessions in a full-screen terminal app, right where your code already lives.

The Mewbo terminal client is a full-screen terminal application for running agent sessions. You launch it from your shell. You type a request. The agent works, and you watch the whole run happen live on one screen.

Reach for the terminal when you work in a shell and want the agent in the same place. It lives where your code lives, so its tools act on your real files and your real shell. It is keyboard-first, so you drive the whole run without leaving the keyboard. It is local-first, so the engine runs in-process on your machine and the core loop makes no API round-trip. The [web console](../web/index.md) and the [Android app](../android/index.md) fit better when you want a graphical surface or a session on your phone. The terminal fits better when you want speed and locality at your desk.

The interface is one unified [Textual](https://textual.textualize.io/) application. It streams the agent's reply as the tokens arrive. It renders tool calls as cards. It tracks sub-agents in a sidebar. It asks for your approval before any write or shell command. The launcher lives in [`cli_master.py`](repo:apps/mewbo_cli/src/mewbo_cli/cli_master.py), and the app itself lives in [`app.py`](repo:apps/mewbo_cli/src/mewbo_cli/tui/app.py).

<video controls preload="metadata" style="width: 100%; max-width: 960px; height: auto; display: block; margin: 2rem auto 0;">
  <source src="../assets/videos/mewbo-cli-01-video.mp4" type="video/mp4" />
  Your browser does not support the video tag.
</video>

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.10+ | [uv](https://docs.astral.sh/uv/) manages the virtualenv |

The terminal client ships with the base install. You do not need an extra for it.

## Install and first run

Run these numbered steps from the repository root to reach a first session.

1. Install the dependencies with `uv sync`. This builds a managed virtualenv.
2. Launch the app with `uv run mewbo`. It opens full screen.
3. On a fresh machine, type `/init` and press Enter. This scaffolds the config files Mewbo needs.
4. Type a request in plain language and press Enter. For example, ask the agent to explain a file or make a small edit.
5. Watch the reply stream into the transcript. Tool calls appear below it as cards.
6. Approve or deny when the agent asks. Mewbo prompts you before any write or shell command.
7. Type `/help` to list the commands, and `/exit` when you are done.

To install the command globally instead, run `uv tool install .` from the repository root. This puts `mewbo` on your PATH, so you can start a session from any directory.

The [interface guide](interface.md) explains each surface you see during a run.

## Configuration

Mewbo loads config from the first directory that contains it, checked in this order.

1. `CWD/configs/`: project-local config, highest priority.
2. `$MEWBO_HOME/`: a custom home directory, if you set one.
3. `~/.mewbo/`: the user-home fallback.

Type `/init` from inside the app to scaffold the config files from scratch. To point the app at a specific file, pass `--config`.

For LLM provider keys and model choice, see [LLM Setup](../llm-setup.md). For every config key on the site, see the [Configuration Reference](../configuration.md). For the terminal flags and the config chain in detail, see the [terminal configuration page](configuration.md).

## Next steps

- [The Interface](interface.md): the live transcript, tool cards, the plan dock, and approval prompts.
- [Agent Fleet](agent-fleet.md): sub-agent orchestration and the fleet sidebar.
- [Remote Sync](remote-sync.md): the local-first default and the opt-in remote seam.
- [Configuration](configuration.md): flags, the config chain, and session recovery.
