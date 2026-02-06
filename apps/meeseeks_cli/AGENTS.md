# Meeseeks CLI - UI/Terminal Guidance

Scope: this file applies to the `apps/meeseeks_cli/` package only. It covers the terminal UI (renderer + dialog toolkit) and how CLI output is produced.

## Goals (UI)
- Keep the terminal UI simple, fast, and readable.
- Prefer built-in components from the rendering/dialog toolkits over custom rendering.
- Stay DRY/KISS: build reusable UI helpers instead of ad‑hoc formatting.
- Preserve terminal scrollback (no full-screen takeovers).

## Rendering Pipeline (How we produce output)
- Entry point: `apps/meeseeks_cli/src/meeseeks_cli/cli_master.py` (`run_cli`).
- Rendering is done via a single console renderer instance.
- High-level sections:
  - Startup header panel plus a ready line with session info.
- Action plan checklist (panel + text + group).
- Tool results as cards (panel + columns).
- Response panel (Markdown in a bold border).
- Logging is gated by `-v/--verbose` and themed darker for CLI runs.
- Tool execution shows a lightweight spinner while a tool is running.

### Section styles (keep consistent)
- Action Plan: checklist in a panel titled `:clipboard: Action Plan`, border `cyan`.
- Tool Results: per-tool panels, title prefix `:wrench:`, border `magenta`.
- Response: `:speech_balloon: Response`, border `bold green`.
- Tool result cards dim unless they are the current focus; outputs are collapsed unless verbose and JSON renders formatted.

If you change any of these, update this file.

## Dialogs / Prompts (Interactive Toolkit)
We use Rich for the normal CLI rendering (header, plans, tool cards, responses).
We use Textual only for full-screen style prompts (dialogs), not for the main output.
Do not run Rich rendering and Textual dialogs concurrently: Textual runs a blocking app loop
and mixing it with live Rich rendering/spinners can deadlock or break terminal state.

Location: `apps/meeseeks_cli/src/meeseeks_cli/cli_dialogs.py`

### DialogFactory (reusable)
- `select_one`: single-select list (OptionList)
- `select_many`: multi-select list (SelectionList)
- `prompt_text`: text input (Input)
- `confirm`: yes/no

Key behaviors:
- Runs **inline** to avoid clearing scrollback.
- Auto-fallback to plain prompt when no TTY or `MEESEEKS_DISABLE_TEXTUAL=1`.
- Escape/Q cancels; Enter accepts.
- Interactive app runs are blocking; do not use them for long-lived UI in the REPL loop.

### Commands currently using dialogs
- `/models`: single-select model picker (TTY only).
- `/tag` (no args): Text input for tag name.
- `/fork` (no args): Text input for optional tag.
- `/mcp select`: Multi-select to filter MCP tools displayed.

If you add a new interactive flow, use `DialogFactory` instead of writing custom prompts.

## Commands overview (keep in sync)
- `/help`: show commands.
- `/exit` or `/quit`: exit the CLI.
- `/new`: start a fresh session.
- `/session`: show current session id.
- `/summary`: show current session summary.
- `/summarize` or `/compact`: summarize + compact transcript.
- `/tag NAME`: tag the current session (dialog when NAME omitted).
- `/fork [TAG]`: fork current session (dialog when TAG omitted).
- `/plan on|off`: toggle action plan display.
- `/mcp [select|init]`: list MCP tools, filter, or scaffold config.
- `/config init`: scaffold a config example file.
- `/init`: scaffold both config and MCP example files.
- `/models`: model wizard (interactive only).
- `/automatic`: auto-approve all tool actions in this session.

## Core Files (UI-related)
- `apps/meeseeks_cli/src/meeseeks_cli/cli_master.py`: main loop, output sections, startup panel.
- `apps/meeseeks_cli/src/meeseeks_cli/cli_commands.py`: commands, model wizard, MCP listing.
- `apps/meeseeks_cli/src/meeseeks_cli/cli_dialogs.py`: dialog factory.
- `apps/meeseeks_cli/src/meeseeks_cli/cli_context.py`: state shared across commands.

## Config knobs (UI-relevant)
- `llm.api_base`: printed in the ready panel.
- `llm.default_model` / `llm.action_plan_model`: used when `--model` is not set.
- `cli.disable_textual`: disable dialogs (force fallback).
- `runtime.cli_log_style`: default log styling for the CLI.
- `configs/mcp.json`: MCP server config used for discovery.

## KISS / DRY rules for UI work
- Reuse existing render helpers and dialogs; add small helpers if needed.
- Avoid bespoke widgets or heavy layouting unless strictly required.
- Prefer toolkit defaults; override only when UX needs it.
- Keep new UI logic near existing UI code (`cli_master.py`, `cli_dialogs.py`).

## Orchestration + testing guardrails (CLI-facing)
- Show tool activity clearly (plan, spinner, tool panels) before final response.
- Do not print raw tool output as the final answer; let the core synthesize.
- Tests should drive a real CLI flow with fake tools/LLM outputs; avoid over-mocking.
- Keep permission prompts deterministic in tests (auto-approve or stub).
- Treat language models as black-box APIs with non-deterministic output; avoid anthropomorphic language in docs/changes.

## Keep this file updated
Whenever you change:
- Section layouts, styles, or titles
- Dialog behaviors or new dialog types
- UI-related env vars or dependencies
…update this document to reflect the new behavior.

Doc hygiene:
- Keep this file concise and actionable; link to code instead of duplicating it.
- This is a nested file for the CLI package; it should override root guidance only when CLI-specific.
