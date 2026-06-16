> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo CLI - UI/Terminal Guidance

Scope: this file applies to the `apps/mewbo_cli/` package only. It covers the terminal UI (renderer + dialog toolkit) and how CLI output is produced.

## Goals (UI)
- Keep the terminal UI simple, fast, and readable.
- Prefer built-in components from the rendering/dialog toolkits over custom rendering.
- Stay DRY/KISS: build reusable UI helpers instead of ad‑hoc formatting.
- Preserve terminal scrollback (no full-screen takeovers).

## Rendering Pipeline (How we produce output)
- Entry point: `apps/mewbo_cli/src/mewbo_cli/cli_master.py` (`run_cli`).
- Rendering is done via a single console renderer instance.
- High-level sections:
  - Startup header panel plus a ready line with session info.
- Action plan checklist (panel + text + group).
- Tool results as cards (panel + columns).
- Response panel (Markdown in a bold border).
- Logging is gated by `-v/--verbose` and themed darker for CLI runs.
- **Agent display**: During execution, a Rich Live panel shows the agent tree with status, model, elapsed time, and step count. Managed by `AgentDisplayManager` in `cli_agent_display.py`. Key features:
  - **Collapsible tree**: Ctrl+O toggles between expanded (full tree) and collapsed (summary line) during execution. Starts expanded.
  - **Integrated spinner**: Braille spinner animates inside the Live renderable at 4 fps. Root tool activity (via `pre_tool_use`/`post_tool_use` hooks) drives the spinner when no sub-agents exist.
  - **Status footer**: Shows the deepest running agent's task label + elapsed time below the tree.
  - **Elapsed time**: Each agent line shows time since start; token count renders when the core surfaces it.
  - **KeyListener** (`cli_keys.py`): stdlib-only (`tty.setcbreak` + daemon reader thread) keystroke capture during Live rendering. Pauses cbreak mode via `pause()`/`resume()` when approval prompts need `console.input()`.
  - **Lifecycle states**: Agent display shows 6 states: submitted (⏳), running (●), completed (✓), failed (✗), cancelled (⊘), rejected (⊘ red). Failed agents show inline error details truncated to 80 chars.
  - **Step budget**: `session_step_budget` is threaded from config through to the orchestrator.
  - Falls back to legacy `console.status()` spinners when output is piped or `--no-color` is set.

### Section styles (keep consistent)
- Action Plan: checklist in a panel titled `:clipboard: Action Plan`, border `cyan`.
- Tool Results: per-tool panels, title prefix `:wrench:`, border `magenta`.
- Response: `:speech_balloon: Response`, border `bold green`.
- Tool result cards dim unless they are the current focus; outputs are collapsed unless verbose and JSON renders formatted.

### LLM resilience notices (post-run replay)
The core emits `llm_retry` / `llm_fallback` / `recovery` (`halt_no_progress`)
events to the **transcript**, not via hooks, so the live agent panel never
sees them. `_print_resilience_events` (in `cli_master.py`) replays them after
each run as concise dim lines, scoped to events after the last `user` event so
multi-turn sessions don't re-print prior turns:
- `llm_retry` → `↻ Retrying {model} after {error_type} ({attempt}/{max}, {delay}s)` (dim yellow).
- `llm_fallback` → `⤳ Falling back: {from} → {to} ({reason})` (dim yellow). When the payload carries `sticky: true` (the destination model is pinned for the rest of the run), the line also appends ` [pinned for run]`.
- `recovery` halt → `⊘ Halted: repeated '{tool}' with no progress — /retry or /continue to recover` (dim red).

`_print_resilience_events` returns `bool` (whether a halt line was printed) so
the caller can suppress the generic recovery hint below when the halt line
already mentions those commands.

After the resilience notices, `_maybe_print_recovery_hint` prints a single concise
dim-cyan line when the run ended in a recoverable terminal state (`error`,
`max_steps_reached`, `halted_no_progress`, `canceled`) **and** a prior user turn
exists. Skipped on a clean `completed` run, on `halt_printed=True`, and when no
user turn is present.
- hint line → `↩ This session can be recovered — /continue to resume with context intact, or /retry to redo the last step.` (dim cyan).

Fallback is opt-in per run via flags (parsed in `run_cli`): `--fallback-models`
(comma-split) with singular alias `--fallback-model`, and `--no-fallback`
(sets the chain to `()` — an explicit empty tuple that overrides the
`llm.fallback.*` config default, distinct from `None` which uses the default).

If you change any of these, update this file.

## Dialogs / Prompts (Interactive Toolkit)
We use Rich for the normal CLI rendering (header, plans, tool cards, responses).
We use Textual only for full-screen style prompts (dialogs), not for the main output.
Do not run Rich rendering and Textual dialogs concurrently: Textual runs a blocking app loop
and mixing it with live Rich rendering/spinners can deadlock or break terminal state.

Location: `apps/mewbo_cli/src/mewbo_cli/cli_dialogs.py`

### DialogFactory (reusable)
- `select_one`: single-select list (OptionList)
- `select_many`: multi-select list (SelectionList)
- `prompt_text`: text input (Input)
- `confirm`: yes/no

Key behaviors:
- Runs **inline** to avoid clearing scrollback.
- Auto-fallback to plain prompt when no TTY or `MEWBO_DISABLE_TEXTUAL=1`.
- Escape/Q cancels; Enter accepts.
- Interactive app runs are blocking; do not use them for long-lived UI in the REPL loop.

### Commands currently using dialogs
- `/models`: single-select model picker (TTY only).
- `/tag` (no args): Text input for tag name.
- `/fork` (no args): Text input for optional tag.
- `/mcp select`: Multi-select to filter MCP tools displayed.

If you add a new interactive flow, use `DialogFactory` instead of writing custom prompts.

## Prompt completion
`cli_completer.py:MewboCompleter` drives live `PromptSession` completion (wired
in `run_cli`, `complete_while_typing=True`): `@<partial>` suggests project files
(shared `FileCatalog`, git-index first, cached per-cwd) and `/<partial>` at the
line start suggests CLI commands **and** user-invocable skills. It never raises
out of `get_completions` — a failed git call just yields nothing.

## Commands overview (keep in sync)
- `/help`: show commands.
- `/exit` or `/quit`: exit the CLI.
- `/new`: start a fresh session.
- `/session`: show current session id.
- `/summary`: show current session summary.
- `/summarize` or `/compact`: summarize + compact transcript.
- `/status`: show session status (shared runtime).
- `/terminate`: cancel the active run (shared runtime).
- `/tag NAME`: tag the current session (dialog when NAME omitted).
- `/fork [TAG]`: fork current session (dialog when TAG omitted).
- `/plan on|off`: toggle action plan display.
- `/skills [name]`: list available skills or show skill detail.
- `/plugins [marketplace|install|uninstall]`: list installed plugins or manage them.
- `/mcp [select|init]`: list MCP tools, filter, or scaffold config.
- `/config init`: scaffold a config example file.
- `/init`: scaffold both config and MCP example files.
- `/models`: model wizard (interactive only).
- `/automatic`: auto-approve all tool actions in this session.

## Core Files (UI-related)
- `apps/mewbo_cli/src/mewbo_cli/cli_master.py`: main loop, output sections, startup panel, Rich Live agent display.
- `apps/mewbo_cli/src/mewbo_cli/cli_agent_display.py`: `AgentDisplayManager` — thread-safe bridge between agent lifecycle hooks and Rich Live rendering. Handles collapsed/expanded tree, spinner, footer, elapsed time.
- `apps/mewbo_cli/src/mewbo_cli/cli_keys.py`: `KeyListener` — stdlib-only keystroke capture during Rich Live (cbreak mode + daemon thread). Reusable for future keybindings.
- `apps/mewbo_cli/src/mewbo_cli/cli_commands.py`: commands, model wizard, MCP listing.
- `apps/mewbo_cli/src/mewbo_cli/cli_dialogs.py`: dialog factory.
- `apps/mewbo_cli/src/mewbo_cli/cli_context.py`: state shared across commands.
- `packages/mewbo_core/src/mewbo_core/session_runtime.py`: shared runtime with `enqueue_message` (user steering) and `interrupt_step` (step interruption).

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
