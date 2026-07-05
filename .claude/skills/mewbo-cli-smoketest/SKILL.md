---
name: mewbo-cli-smoketest
description: End-to-end smoke testing of the Mewbo CLI via tmux. Use this skill when asked to test the CLI, verify CLI behavior after changes, smoke-test the agent loop, check for regressions, or validate MCP/plugin/session features work correctly through the terminal interface. Also use when validating the live-streaming transcript ("glide"), the activity footer/spinner, or the fleet sidebar + per-agent drill-in (epic #161), and when debugging CLI crashes, MCP connection issues, or session lifecycle problems that need live reproduction.
---

# Mewbo CLI Smoke Test via Tmux

Automate end-to-end CLI testing by running `mewbo` inside a tmux pane, driving it with commands/queries, and analyzing both the rendered TUI frame and the verbose log for errors, warnings, and regressions.

## Why tmux

Since epic #149 the CLI is **one full-screen Textual `App`** (not the old Rich `Live` + prompt stack), and #161 layered live streaming + a per-agent fleet sidebar on top. You cannot run it via the Bash tool because it requires a PTY and renders interactive widgets on the **alternate screen**. Tmux gives you a real terminal to drive the app while capturing output programmatically.

## Two capture channels (read this first)

A Textual app paints the **alternate screen**, so the two things you want to observe come from two different places:

1. **The rendered TUI frame** — `tmux capture-pane -p` returns the *current visible frame only*. The alt-screen keeps **no scrollback**, so `-S -100` does **not** give you history the way it does for a plain shell; it just returns the visible grid. To inspect what scrolled past, drive the app to re-render (e.g. scroll the transcript) or rely on the log channel below.
2. **The verbose log** — route logs to a file so they don't corrupt the TUI, then `grep` the file. **Do not** use `--log-console` with the TUI (it paints over the widgets). Launch with:

```bash
tmux send-keys -t <session>:mewbo-test \
  "mewbo -vv --auto-approve --log-file /tmp/mewbo-test.log --log-overwrite" Enter
```

Then, after each step: `tmux capture-pane -p -t <session>:mewbo-test` for the **frame**, and `grep -nE 'WARNING|ERROR|Traceback|Failed to' /tmp/mewbo-test.log` for **diagnostics**. The two are complementary — a clean frame can still hide a logged reconnect error, and a busy log can accompany a correctly-rendered frame.

> Use the installed `mewbo` (from `uv tool install .`) or `uv run mewbo` from the repo root. The installed binary tests what the user actually runs.

## Setup

```bash
tmux list-sessions
tmux new-window -t <session>:<next> -n mewbo-test
# width matters: the sidebar/fleet only render legibly with enough columns
tmux resize-window -t <session>:mewbo-test -x 200 -y 50 2>/dev/null || true
tmux send-keys -t <session>:mewbo-test \
  "mewbo -vv --auto-approve --log-file /tmp/mewbo-test.log --log-overwrite" Enter
sleep 10 && tmux capture-pane -p -t <session>:mewbo-test
```

Startup (MCP pool connect, plugin load, skill discovery) takes 8-10s. Verify the frame shows the **header** (model, session id, tool counts), the **transcript** region, the **sidebar** on the right, the **input** box, and the **status line** at the very bottom. If widgets are missing, the window is probably too narrow — resize and re-capture.

### Screen anatomy (widget IDs / regions)

The layout (`app.py` `compose`) is the map for where to look in a captured frame:

| Region | id | Widget | What lives there |
|---|---|---|---|
| top | `#header` | `HeaderWidget` | model · session id · tool/skill counts |
| body left | `#transcript` | `TranscriptView` | the conversation + **streaming text** + tool cards + the **activity spinner** at its foot |
| body right | `#sidebar` | `SidebarView` | faceted sections: **FLEET · PLAN · CONTEXT** |
| below body | `#input` | `InputArea` | the `mewbo>` prompt |
| bottom bar | `#statusline` (in `#footerbar`) | `StatusLine` | `user@host · model · cwd · branch · stash · session tokens (↑in ↓out)` |

Global keys: `ctrl+c` quit, `shift+tab` cycle permission mode. In a drill-in view (below): `esc` / `backspace` returns to chat.

## Capture timing

Single `sleep N && tmux capture-pane` commands — never split them. Different operations need different waits:

| Operation | Wait (s) | Why |
|---|---|---|
| Startup | 8-10 | MCP pool, plugins, skills |
| Slash command (`/help`, `/status`) | 2-3 | local only |
| Interactive picker (`/mcp`, `/models`) | 2-3 | opens a modal — `Escape` to dismiss before next input |
| Simple query (no tools) | 10-15 | plan + LLM + response |
| Tool-using query | 15-25 | plan + LLM + tool + synthesis |
| MCP tool query | 25-40 | plan + LLM + network + synthesis |
| Sub-agent / parallel-fleet query | 30-60 | spawns populate the FLEET section incrementally |
| `/compact` | 10-15 | rebuilds summary via LLM (re-inits subsystems) |

For **streaming** and **fleet** behavior, a single end-of-turn capture is not enough — you must capture **mid-turn** (see below) to prove things render *incrementally*, not just at the end.

---

# Test progression

Layers run cheapest → most expensive. An early failure cascades, so stop and fix before climbing.

## Layer 1 — Slash commands (no LLM, no network)

Validate the framework, config loading, and plugin/skill discovery:

```
/help · /status · /session · /tokens · /skills · /plugins
```

In `/tmp/mewbo-test.log` look for `WARNING`/`ERROR` during plugin/skill load: `Failed to parse manifest` (stale plugin cache), `No YAML frontmatter` (agent def format), `Missing or invalid 'name'` (skill `SKILL.md`). Non-fatal but they mark integration gaps.

## Layer 2 — Interactive pickers

`/mcp` and `/models` open modals. Capture to confirm the modal renders, then **dismiss with `Escape`** before the next input or it goes to the modal, not the prompt.

## Layer 3 — The streaming transcript ("glide") — #152/#161

This is the headline #161 surface: assistant text must **glide in incrementally**, tool cards must **mutate in place**, and everything must appear in **exact emission order**. Send a tool-using query and capture **mid-turn**:

```bash
tmux send-keys -t ... "List the files in the current directory, then summarize what this project is" Enter
sleep 4  && tmux capture-pane -p -t ...   # mid-turn frame #1
sleep 6  && tmux capture-pane -p -t ...   # mid-turn frame #2
sleep 12 && tmux capture-pane -p -t ...   # settled frame
```

**What to expect, by phase:**

| Phase | In the frame | In the log |
|---|---|---|
| turn starts | activity spinner appears at transcript foot: `<braille> Working… (Ns)` (frames `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) | — |
| thinking | spinner label flips to `thinking` (`set_status("thinking")`) | assistant deltas arriving |
| text glides | partial assistant text grows token-by-token **above** the spinner; the visible tail extends between frames #1 and #2 without the whole block flickering (stable-prefix: only the trailing partial re-renders) | — |
| tool runs | a tool card appears (`🔧 <tool_id>`), spinner label flips to `running <tool_id>` | `Running <tool>...` |
| tool settles | the **same** card mutates to its done/error state and dims (`.t-settled`, ~65% opacity) — it must **not** spawn a second card | — |
| turn ends | spinner collapses into a settled summary line `done · N.Ns` (muted); the next turn mounts a fresh spinner below it | — |

**Nuance to actually catch regressions:**
- **Incrementality**: compare frame #1 vs #2 — the assistant text tail must be *longer* in #2. If text only appears in the settled frame, streaming is unwired (the #1 bug this epic fixed).
- **Order/provenance invariant**: text, tool cards, and spawn markers must be interleaved in **arrival order**, never bucketed by type (all-text-then-all-tools = regression).
- **Mutate-in-place**: a tool that runs then finishes is **one** card that changes state, not two stacked cards.
- **Settled vs running styling**: running content is full-weight; completed/failed content is dimmed/muted. A finished turn whose spinner keeps animating, or whose old cards stay full-bright, is a lifecycle bug.

## Layer 4 — Footer activity + status line

The foot-of-transcript **activity spinner** is distinct from the bottom **status line**:
- **Activity spinner** (in `#transcript`): live step label — `Working` → `thinking` → `running <tool>` → settles to `done · N.Ns`. It must always end settled (never spin forever after the response renders).
- **Status line** (`#statusline`): `user@host · model · cwd(~) · branch · stash · ↑<in> ↓<out>` session tokens. After queries, `↑/↓` token counts must be **non-zero** and grow across turns. Cross-check with `/tokens` and `/budget`.

## Layer 5 — Fleet sidebar + per-agent drill-in — #161 Phase 2+4

Trigger sub-agents so the **FLEET** section populates, then drill in. Use a query that fans out:

```bash
tmux send-keys -t ... "Spawn two subagents in parallel: one to list the python packages under packages/, one to count the test files under tests/. Then combine their findings." Enter
sleep 8  && tmux capture-pane -p -t ...   # fleet populating
sleep 25 && tmux capture-pane -p -t ...   # agents running/completing
```

**Faceted sidebar (Phase 4):** the right column shows uppercase `$accent` section headers **FLEET**, **PLAN**, **CONTEXT**, each a titled block (`SidebarSection`). PLAN is the todo dock; CONTEXT is the context/cost gauge.

**Fleet rows (Phase 2):** the FLEET section is a selectable `OptionList` with **one row per agent** in the hypervisor tree (root + every sub-agent + parallel agents), tree-ordered and indented. Each row is a **one-glance summary**, never a tool-name dump:

```
<glyph> <label/type> · <model> · <N tools> · <elapsed> · <in→out>
```

- **Glyph** (`ICONS.agent_state`): `⏳` submitted · `●` running · `✓` completed · `✗` failed · `⊘` cancelled/rejected. Root label is bold-accent.
- **`<N tools>` is a COUNT only.** The single most important assertion of this phase: **no tool *names* appear in a row** (the old `AgentPanel` dumped names; the whole point of #161 was to kill that). If you see tool names in the sidebar, that's a regression.
- Rows refresh live — `elapsed` ticks and token facets update while agents run; a finished agent's glyph flips to `✓`/`✗` and its row may bell once.

**Drill-in:** select a fleet row (move highlight with arrows, `Enter` to select):

```bash
tmux send-keys -t ... Down Enter   # select a child agent row
sleep 2 && tmux capture-pane -p -t ...
tmux send-keys -t ... Escape       # back to the live root transcript
```

Expect: the `#transcript` region **swaps in place** to that agent's transcript (live if still running, full history if done) — *not* a separate fullscreen. It shows:
- a **breadcrumb** `Fleet ▸ root / <child>` with `esc to return`,
- the agent's own transcript rendered with the same renderers/lifecycle styling as root,
- a **per-agent footer** (`.drill-footer`): `<glyph> <status> · model · N tools · elapsed · in→out` — the "how long did this agent take" answer.

**Navigation nuance (must verify all three):**
- root → child (select a child row),
- child → child (select a *different* row while drilled in — re-targets in place; an EXCLUSIVE worker serializes rapid re-targets so you never see a half-stale view),
- child → root (`esc`/`backspace` returns to the live root).

## Layer 6 — Orchestration cards — #161 Phase 3

The hypervisor tools render as **dedicated cards**, not JSON dumps (registered *after* base renderers so they win). Force each:

| Tool | Trigger idea | Card shows |
|---|---|---|
| `spawn_agent` / `spawn_agents` | "spawn an agent to …" / "spawn 3 agents to …" | header `spawn_agent · <agent_type>`, dim `model · agent <id>` meta; batch card = header counts + capped per-task rows + `… +N more` |
| `check_agents` | "check on the agents" | fleet-status card (per-agent state), not raw JSON |
| `tool_search` | a query needing a deferred tool | discovery card listing matched tools, not raw JSON |

Regression signal: any of these rendering as a raw `{...}` JSON blob means the renderer isn't registered / lost the registry race.

## Layer 7 — Stress: `/compact`, `/fork`, `/edit`

```
/fork test-fork     — "Forked session: <id>"
/edit What is 2+2?  — re-runs edited prompt, returns "4"
/compact            — produces a summary; re-inits plugins/skills/MCP/tool-registry
/budget             — non-zero token usage after queries
```

`/compact` is the **stress test** — it re-initializes the most subsystems and triggers `refresh_if_config_changed`, the most common place MCP config-merge bugs surface. Check the log carefully right after it.

---

# MCP tool query & config-merge issues

Force an MCP call (`Use a configured code-wiki MCP tool to look up the architecture of bearlike/Assistant`), wait 30-40s, and watch `/tmp/mewbo-test.log` for:

- `Connected to MCP server` / `Disconnected from MCP server` — lazy connect / churn (project servers connect on first need, not at startup).
- `Failed to reconnect MCP server` — config merge / library compat.
- `_create_streamable_http_session() got an unexpected keyword argument` — type/transport collision in normalization.
- `Configuration error: Missing 'transport' key` — plugin MCP config not normalized.

These often surface only on the **second** tool call or during `/compact`, when `refresh_if_config_changed` re-merges config. Root causes usually live in the merge pipeline:

1. **Multiple formats**: `configs/mcp.json` (native: `servers` + `transport`) vs `.mcp.json` (Claude Code: `mcpServers` + `type`) vs plugin `.mcp.json`.
2. **Deep-merge collision**: `_deep_merge` can yield a server with **both** `type` and `transport` when CWD overrides global.
3. **Plugin normalization**: plugin `.mcp.json` may use the `mcpServers` wrapper or a bare entry without `transport`.

To trace: read the configs, then `_normalize_mcp_config` in `mewbo_tools/integration/mcp.py` and `load_all_plugin_components` in `mewbo_core/plugins.py`.

# Analyzing results

Scan `/tmp/mewbo-test.log` after each step:

```
grep -nE 'WARNING|ERROR|Traceback|Failed to' /tmp/mewbo-test.log
```

`WARNING` = non-fatal (plugin/config). `ERROR`/`Traceback` = real failure. For the **visual** layers there is no log line — the assertion is the captured **frame** (does the text glide, does the card mutate, does the fleet row omit tool names, does the drill-in swap and the breadcrumb appear). Pair every visual claim with a concrete grep against the captured frame, e.g. confirm the fleet glyphs and `·`-separated facets are present and that no `🔧`/tool-name token appears inside the sidebar columns.

For deeper verification: Langfuse traces (`trace_id == session_id`) and the MongoDB transcript.

# Cleanup

```bash
tmux send-keys -t <session>:mewbo-test "/exit" Enter
sleep 2
tmux kill-window -t <session>:mewbo-test
```

# Non-obvious gotchas

- **Alt-screen has no scrollback.** `capture-pane -S -100` won't give history for the live TUI — it returns the visible grid. Use the `--log-file` channel for anything that scrolled off, and capture **mid-turn** to observe transient states (spinner label, partial text, running cards).
- **Verbose goes to the log, not the pane.** With the full-Textual app, `-vv` output is only useful via `--log-file`; never `--log-console` (it paints over the widgets).
- **Width gates the sidebar.** Too narrow a window and FLEET/PLAN/CONTEXT collapse or clip — resize to ≥ ~200 cols before asserting on the sidebar.
- **Streaming proof needs ≥ 2 mid-turn frames.** A single settled capture cannot distinguish "streamed" from "dumped at end" — the whole point of #161. Always diff two mid-turn frames.
- **Fleet rows are summaries, by design.** Seeing only counts/metadata (no tool names) is *correct*, not missing detail — the detail lives in the **drill-in**, reached by selecting the row.
- **Model-choice provenance is name-only.** Rows show the model *name*; how it was chosen (auto/tier vs override) is intentionally absent — core doesn't surface it. Don't file that as a bug.
- **Plugin warnings repeat every turn.** Plugin/skill loading runs each tool-use iteration, so identical warnings recur — focus on *unique* messages, not counts.
- **Interactive modals block the prompt.** Dismiss `/mcp`/`/models` (and any permission/plan modal) with `Escape` before sending the next input.
- **Env var expansion.** CWD `.mcp.json` may use `${VAR}`; if unset in the launching shell, tokens stay literal and MCP calls fail silently.
- **`/compact` is the integration stress test** — re-inits plugins, skills, MCP, and the tool registry in one shot; the likeliest place to surface merge/lifecycle bugs.
