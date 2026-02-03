# Meeseeks CLI — UI/Terminal Guidance

Scope: this file applies to the `meeseeks-cli/` package only. It covers the terminal UI (Rich + Textual) and how CLI output is produced.

## Goals (UI)
- Keep the terminal UI simple, fast, and readable.
- Prefer built-in components from Rich/Textual over custom rendering.
- Stay DRY/KISS: build reusable UI helpers instead of ad‑hoc formatting.
- Preserve terminal scrollback (no full-screen takeovers).

## Rendering Pipeline (How we produce output)
- Entry point: `meeseeks-cli/cli_master.py` (`run_cli`).
- Rendering is **Rich**-based and printed via a single `Console` instance.
- High-level sections:
  - Ready panel with model/base URL (within the same border).
  - Action plan checklist (Panel + Text + Group).
  - Tool results as cards (Panel + Columns).
  - Response panel (Markdown in a bold border).
- Logging is gated by `--debug` and themed darker for CLI runs.

### Section styles (keep consistent)
- Action Plan: checklist in a panel titled `:clipboard: Action Plan`, border `cyan`.
- Tool Results: per-tool panels, title prefix `:wrench:`, border `magenta`.
- Response: `:speech_balloon: Response`, border `bold green`.
- Tool result cards dim older steps (only the latest stays normal).

If you change any of these, update this file.

## Dialogs / Prompts (Textual)
We use **Textual** only for interactive dialogs, not for overall output.

Location: `meeseeks-cli/cli_dialogs.py`

### DialogFactory (reusable)
- `select_one`: single-select list (OptionList)
- `select_many`: multi-select list (SelectionList)
- `prompt_text`: text input (Input)
- `confirm`: yes/no

Key behaviors:
- Runs **inline** (`run(inline=True)`) to avoid clearing scrollback.
- Auto-fallback to plain prompt when no TTY or `MEESEEKS_DISABLE_TEXTUAL=1`.
- Escape/Q cancels; Enter accepts.

### Commands currently using dialogs
- `/models`: Textual single-select model picker (TTY only).
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
- `/models`: model wizard (interactive only).
- `/automatic`: auto-approve all tool actions in this session.

## Core Files (UI-related)
- `meeseeks-cli/cli_master.py`: main loop, output sections, startup panel.
- `meeseeks-cli/cli_commands.py`: commands, model wizard, MCP listing.
- `meeseeks-cli/cli_dialogs.py`: Textual dialog factory.
- `meeseeks-cli/cli_context.py`: state shared across commands.

## Environment knobs (UI-relevant)
- `OPENAI_API_BASE` / `OPENAI_BASE_URL`: printed in the ready panel.
- `DEFAULT_MODEL` / `ACTION_PLAN_MODEL`: used when `--model` is not set.
- `MEESEEKS_DISABLE_TEXTUAL=1`: disable Textual dialogs (force fallback).
- `MEESEEKS_CLI=1`: set at startup to tag CLI logging.
- `MEESEEKS_CLI_LOG_STYLE=dark`: default log styling for the CLI.
- `MESEEKS_MCP_CONFIG`: MCP server config used for discovery.
- `MESEEKS_TOOL_MANIFEST`: optional override for tool registry.

## KISS / DRY rules for UI work
- Reuse existing render helpers and dialogs; add small helpers if needed.
- Avoid bespoke widgets or heavy layouting unless strictly required.
- Prefer Rich/ Textual defaults; override only when UX needs it.
- Keep new UI logic near existing UI code (`cli_master.py`, `cli_dialogs.py`).

## Keep this file updated
Whenever you change:
- Section layouts, styles, or titles
- Dialog behaviors or new dialog types
- UI-related env vars or dependencies
…update this document to reflect the new behavior.

Doc hygiene:
- Keep this file concise and actionable; link to code instead of duplicating it.
- This is a nested file for the CLI package; it should override root guidance only when CLI-specific.
