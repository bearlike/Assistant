> ↑ [root /CLAUDE.md](../../CLAUDE.md)

# Mewbo CLI - UI/Terminal Guidance

Scope: this file applies to the `apps/mewbo_cli/` package only. It covers the terminal UI (renderer + dialog toolkit) and how CLI output is produced.

## Goals (UI)
- Keep the terminal UI simple, fast, and readable.
- Prefer built-in components from the rendering/dialog toolkits over custom rendering.
- Stay DRY/KISS: build reusable UI helpers instead of ad‑hoc formatting.
- Preserve terminal scrollback (no full-screen takeovers).

## North Star — full-Textual TUI (epic #149)

Direction (decided 2026-06-20): migrate this package from **four coexisting I/O stacks** (Rich `Live` + Textual + prompt_toolkit + raw `termios`) to **ONE `MewboApp(textual.App)`**. The "do not run Rich Live and Textual concurrently" rule below is exactly why the permission prompt is bad — one Textual loop removes the constraint and makes every interaction a first-class widget. Trade-off: full-screen alternate buffer, no native scrollback. Children #150–#157; sections below describe the **current Rich-based** implementation until each child lands.

Durable design rules carried into the rewrite (don't re-research — full notes in memory `project_cli_tui_overhaul`):
- **Semantic `Palette` (≈12 roles) → `build_theme()` factory → injected** Textual CSS vars (`$primary`…); never a style global. One `ICONS` module.
- **Streaming = stable-prefix cache + per-(width,theme) memoized markdown** (re-render only the trailing partial). The flicker fix.
- **Permission = layered decision chain** (skip → allowlist `tool`/`tool:action` → session-grant → modal); modal embeds the diff/command; single-key `a`/`s`/`d`; **esc=deny**; "allow always" writes a glob RULE; **shift+tab** cycles modes.
- **ONE `DiffView`** reused at approval AND transcript; difflib + Rich `Syntax`; split/unified by width; hash-cached.
- **Fleet**: hypervisor tree (our 4 terminal states) PLUS todo/queue pills; context-% (>80% warn) + cost gauge in the sidebar; identity + branch + session tokens in the footer status line (no facet shown in both).
- **Every UX surface is a file/stdin contract** (slash cmds = `.md`+frontmatter+`$ARGUMENTS`, statusLine = script+JSON-stdin, themes/keybindings = JSON) — extends the engine prompt-registry philosophy. Lean on Textual built-ins (CommandPalette, BINDINGS/Footer, modal Screens) before custom code.

### Transcript design system (the authoritative rendering contract)

The transcript rendering follows ONE coherent design system (distilled from the
patterns common to leading terminal coding agents; re-derived, never copied). Hold
to it when touching `transcript_render.py` / `transcript.py` / `fleet_panel.py`:

- **Color = identity + state · muted text = hierarchy · spinner/shimmer = liveness.**
  Status is a glyph+color, never a background alone; secondary info recedes via
  `palette.muted`/`$text-muted`; "working" is a moving spinner, not new chrome.
  Always go through the injected `Palette`/`ICONS` — never a raw hex or glyph.
- **Role identity via a colored left-rail.** USER turns carry a thick `$primary`
  rail (`.t-user`) + the message in `palette.user`; ASSISTANT turns are plain
  default-fg markdown (no rail). One blank line between every block; an extra top
  margin isolates each user turn. Never double-space.
- **Tools = inline-vs-block + collapse-by-default.** READS are a COMPACT one-liner
  (`<glyph> <label> <path> · N lines`, no body — the agent's highest-frequency
  tool must not bloat the viewport). SHELL execs are an unmistakable terminal
  block: a `$` prompt (success color) + bold command + legible output, on a
  distinct `$success` rail (`.t-bash`); detect shell tools via
  `is_shell_tool()` (name-hint based, so vendored ids like `aider_shell_tool`
  match). EDIT/WRITE render the colored `DiffView` (add=green / del=red, sign +
  tint). GENERIC/MCP unwrap the result envelope (shell→stdout, file→text,
  dir→entries) or pretty-print JSON, truncated `… +N lines`.
- **Plan/todo is dual-surfaced**: the plan inline in the transcript AND a pinned
  sidebar dock with an `N/M` count, tri-state per item (✓ done · → in-progress
  (exactly one) · • pending), so progress reads at a glance while the transcript
  flows.
- **Approval modal shows the REAL payload** (DiffView for edits / `$ command` for
  exec) under a risk badge; `esc` = deny (safe default). Risk tier via
  `RiskTier.classify` (shell ⇒ EXEC by name pattern).
- **A proposed plan is ONE decision surface, never raw JSON (#159).** A plan-mode
  run that halts awaiting approval renders `plan.md` through the `plan_proposal`
  renderer — a bordered "📋 Proposed plan" card (`$accent` `.t-plan_proposal`
  rail, full `render_markdown` fidelity, long plans capped to a bounded preview) —
  and the raw `spawn_agent`/`check_agents` planner steps are suppressed. The
  decision is the `PlanApprovalModal` (`tui/widgets/plan_modal.py`), bridged from
  the approval worker exactly like the permission modal
  (`call_from_thread(push_screen_wait)` via the `TurnEngine` `plan_approval_resolver`):
  `a`=approve & execute (→ `runtime.approve_plan` + `state.mode` plan→act + an
  act-mode follow-up run), `k`/`esc`=keep planning (refine via your next message),
  `r`=reject (`runtime.reject_plan`). There are **no plan slash commands**; the
  no-TTY / `--query` plain fallback approves via `/continue` + `cli_master`'s
  inline prompt.
- **Never leak raw logs or a usage footer into the transcript** — vitals live in
  the footer status line + sidebar gauge.

### Theme system (#151) & DiffView (#153) — landed components

These are standalone modules for the rewrite; nothing wires them into the live
Rich loop yet (the foundation child does that). Import them; never reach for raw
colors or bespoke diff rendering.

- **`cli_theme.py`** — semantic `Palette` (frozen dataclass, the binding 15-role
  contract in `ROLE_NAMES`: `primary, secondary, accent, fg_base, bg_base, muted,
  border, error, warning, success, user, assistant, diff_add, diff_del, diff_eq`).
  `build_theme(palette, *, name, dark)` → `textual.theme.Theme`: native semantic
  roles map to Theme fields (Textual generates shades); the rest — plus `fg_base`/
  `bg_base` — go in `variables` so **all 15 roles surface verbatim as CSS vars**
  (`$diff_add`, `$fg_base`, …). Built-ins: `DEFAULT_PALETTE` (dark), `HYPER_PALETTE`,
  `LIGHT_PALETTE`. `ThemeManager` (atomic class, DI — never a global): JSON loader
  from `~/.mewbo/themes/` (bad files skipped, never crashes), `register_all(app)`,
  `list_themes`, `switch`, `apply_command` (the `/theme` unit — caller wires routing).
  `detect_terminal_is_dark()`/`auto_palette()` for auto light/dark.
- **`cli_icons.py`** — `ICONS` (read-only): `check`, `cross`, `error`, `tool_pending`,
  `spinner` (braille frames), and `agent_state` mapping for the 6 lifecycle states.
- **`cli_gradient.py`** — `gradient_text(text, colors)` → Rich `Text`, per-char
  interpolation. Splash + dialog titles only.
- **`cli_diffview.py`** — ONE reusable `DiffView(Widget)` for BOTH the approval modal
  (#154) and the transcript (#152). Constructor:
  `DiffView(old_text, new_text, *, palette, file_path=None, line_numbers=True,
  layout="auto", split_min_width=120, context_lines=3, syntax_theme="ansi_dark", …)`.
  Colors via the injected `Palette` (no hardcoded hex). difflib hunks → per-side Rich
  `Syntax` highlight + add/del tint; split >`split_min_width` cols else unified, `t`
  toggles; highlight hash-cached by `(content, syntax_theme, palette)`; honest
  truncation (`e`/`enter` expands, `←/→` h-scroll). API: `set_palette()`, `expand_all()`,
  `collapse_all()`; props `effective_layout`, `expanded`, `diff_lines`, `visible_rows`.

## Foundation landed (#150) — `tui/` package

The keystone of the rewrite lives in `apps/mewbo_cli/src/mewbo_cli/tui/`:
- `app.py` — `MewboApp(textual.App)` with a `ScreenState` enum
  (onboarding|landing|chat) driving the layout
  `Vertical(Header, Horizontal(Transcript, Sidebar), Input, footerbar(StatusLine, Footer))`;
  the sidebar is hidden by CSS below a width breakpoint (compact mode). The turn
  loop runs on a thread worker via an injected engine factory. **The composer
  (`#input`) is NOT docked** — it flows above the bottom bar with a margin for a
  clear boundary; the IDE-style `StatusLine` + the keybinding `Footer` share ONE
  bottom-docked `#footerbar` `Vertical` so they stack flush instead of both
  fighting for the bottom edge (the prior double-`dock: bottom` overlapped them).
- `turn_engine.py` — `TurnEngine`: the ported `run_cli` dispatch
  (command vs skill vs query) + `_run_query`, UI-agnostic (emits content via
  injected callbacks), with `SessionRuntime` injected (core is never modified).
- `seams.py` — the FOUR stable extension seams Wave-2 children mount into
  **without editing `app.py`**, exposed as `app.messages` / `app.sidebar_slots`
  / `app.permission` / `app.input`:
  - `MessageRendererRegistry` — keyed message/tool renderers + generic fallback (transcript #152)
  - `SidebarSlotRegistry` — ordered sidebar slot factories (agent panel/status #156)
  - `PermissionGateway` — the `approval_callback` threaded into `run_sync` (modal #154)
  - `InputGateway` — input completion source (input/completion #155)
- `widgets/` — placeholders mounted at the seams: `HeaderView`/`HeaderWidget`
  (one responsive renderer that replaced the three width-variant header free
  functions), `TranscriptView` (RichLog), `SidebarView`, `InputArea`.
- `cli_notices.py` — resilience/recovery/token-usage replay shared by the App
  and the plain fallback.

**Removed in #150:** the Rich-`Live` agent-display path inside `_run_query`, the
`KeyListener` cbreak bridge + its hard-coded 0.2s race sleep (`cli_keys.py`
deleted), and the three width-variant header functions. A plain non-interactive
fallback (`_run_plain_repl`; also the `--query` one-shot) bypasses the App for
no-TTY / `MEWBO_DISABLE_TEXTUAL=1` (CI/pipes). The Rich sections documented below
still apply to the plain fallback.

## Wave-2 landed (#155 input · #156 fleet/status · #157 session)

The input, sidebar and session children are wired at the foundation seams via a
single **post-mount `installers` hook**: `MewboApp(..., installers=[...])` runs
each `AppInstaller` (`Callable[[MewboApp], None]`) at the tail of `on_mount`
(each in a try/except → a failing installer degrades to a dim notice, never
breaks the App). `cli_master._build_installers` assembles them so `app.py` stays
closed. This is the extension path — add an installer, never edit `app.py`.

- **#155 input** (`tui/input/` + `tui/widgets/input_area.py`) — ONE sigil-dispatched
  field: `@`→files (caret-anchored OptionList overlay, tiered name-priority via
  `FileCatalog`: exact-stem > basename-prefix > path-segment > substring;
  shift-arrow = insert-without-dismiss), `/`→commands+skills+custom+MCP-prompts
  (fuzzy, match-highlighted, arg-hints), `!`→bash. `CompletionEngine`
  (I/O-free ranking), `CustomCommandLoader` (`.claude/commands/*.md` +
  `~/.mewbo/commands/*.md`, frontmatter `description`/`argument-hint`/
  `allowed-tools`, `$ARGUMENTS`, subdir namespacing `frontend:component`,
  project-overrides-user), `PromptHistory` (`~/.mewbo/cli_history`, `ctrl+r`
  reverse search), Textual `CommandPalette` via `MewboCommandProvider` (default
  `ctrl+p`, instance-level `COMMANDS` — never the class global). Queued-while-busy:
  `Enter` during a run queues, `esc` pulls the latest back; the App `_run_turn`
  flips `set_busy()` and drains `drain_next()` after the turn.
- **#161 fleet/sidebar (Phases 2+4) — supersedes #156's `AgentPanel`.** The
  combined read-only `AgentPanel` (deleted) split into a **selectable**
  `FleetPanel` (`tui/widgets/fleet_panel.py`) + an extracted `TodoPanel`
  (`tui/widgets/todo_panel.py`, home of `TodoState`/`TodoItem`) + a faceted
  sidebar. **The hub is the single fleet source:** `AgentTranscriptHub` gained
  `fleet_rows()` (per-agent `FleetRow` rollups — model/tool-count/tokens/status/
  timestamps; root tokens come from `llm_call_end` cumulatives, elapsed from
  `started_at`/`stopped_at` stamped in-hub) and `items_for(agent_id)` (ordered
  `TranscriptItem`s for drill-in). `FleetPanel` is an `OptionList` whose rows are
  one-glance summaries — `<glyph> <label> · <model> · <N tools> · <elapsed> ·
  <in→out>`, glyph `●`/`✓`/`✗`, **NO tool-name dump** (count only) — and
  selecting a row calls the injected `on_select` → `FleetDrillController.open`,
  which **swaps `#transcript` for a `FleetDrillView` in place** (breadcrumb +
  per-agent footer + the agent's transcript via the SAME renderers, reusing
  `.t-settled`); `esc` restores the live transcript. The fleet panel is the
  persistent navigator so root↔child↔child navigation all re-target in place.
  **Faceting (Phase 4):** `make_sidebar_installer` registers three ordered
  `SidebarSection`s (`tui/widgets/sidebar.py`) — **Fleet** (10) · **Plan** (20) ·
  **Context** (30). `FleetBridge` no longer projects `AgentHandle` rows; it just
  marshals a UI refresh (fleet/plan/drill) on each run event — the hub (a bus
  observer) holds the rollups, and `FleetPanel` self-ticks while agents run.
- **#156 fleet/status (status half — still current).** **Status split (no facet
  shown twice between sidebar & footer):** `StatusBar` (sidebar slot) is now ONLY
  the context/cost gauge —
  `󰓅 ctx %` (>80% warn, `~` estimate) · cost; `model`/tokens on its `StatusState`
  feed the meter but are NOT rendered. The IDE-style `StatusLine`
  (`tui/widgets/status_line.py`, docked in `#footerbar`) owns identity + volume:
  `󰀄 user@host · model · 󰉋 cwd · 󰘬 branch 󰏗 stash · ↑in ↓out` (session tokens,
  input/output faceted — never a subscription quota). Both are driven live off
  ONE refresh tick (`make_statusline_installer`, offloaded via `asyncio.to_thread`)
  reading git branch/stash + session token totals. Status-line glyphs live in
  `cli_icons.py` (`ICONS.branch/stash/gauge/user/folder`, Nerd Font md) — never
  inline a glyph. `StatusLineRunner` runs a
  user script with session-state JSON on stdin (offloaded via `asyncio.to_thread`,
  interval refresh) — stable schema in `tui/status/statusline.py`. Terminal title
  via OSC (`terminal_title.py`). `FleetBridge` (`tui/fleet_bridge.py`) is the
  per-run `HookManager` (`on_agent_start/stop` + `pre/post_tool_use`) that, on
  each run event, marshals a sidebar refresh onto the UI thread; wired as the
  `TurnEngine` hook_factory. (The todo dock's source moved to the authoritative
  `todos` event in #173 — see "Faithful live progress" above; the old
  `TodoTracker` is gone.) The Rich `cli_agent_display.AgentDisplayManager` now
  serves the **plain fallback only**.
- **#157 session** (`tui/screens/`, `tui/session/`, `tui/keybindings.py`) —
  `ResumeScreen` (`ctrl+s`/`/resume`) over `list_sessions()`; `TranscriptScreen`
  (`ctrl+o`, reuses `DiffView`); `RewindCheckpointer`/`/rewind` reverts code +
  conversation together (pre-restore safety snapshot of tracked **and** untracked
  → `git restore` + `git clean`, fully recoverable; no-op outside git);
  `AutoTitler` (idempotent, first-message, reuses `title_generator`); DialogFactory
  pickers mirrored as `ModalScreen`s (`screens/dialogs.py`; `DialogFactory` kept
  for the plain fallback). Declarative `KeybindingConfig` (`~/.mewbo/keybindings.json`
  overrides, bad file skipped) → bindings attached on the App so the `Footer`
  shows them; globals owned: `ctrl+o`/`ctrl+s`/`ctrl+l`. **`shift+tab` (permission
  modes), `ctrl+p` (palette), `ctrl+r` (history) are owned elsewhere — do not bind.**

## Transcript (#152) & Permissions (#154) — landed at the seams

Both mount into the foundation seams (`tui/seams.py`) — the App is not rewired
beyond the registration done in `cli_master._run_app`.

- **Transcript (#152)** — `tui/transcript_render.py` + `tui/widgets/transcript.py`.
  `register_transcript_renderers(registry, *, palette)` registers the `user` /
  `assistant` / `tool` / `error` renderers on the `MessageRendererRegistry`; the
  controller calls it **before `app.run()`** so they win the App's `has`-guarded
  foundation defaults (the widget also self-registers idempotently via the same
  guard). `TranscriptView` is now a `VerticalScroll` of child widgets (so it can
  mount `DiffView`/`Collapsible`), preserving `write_item`/`write_renderable`.
  THE flicker fix = `StreamingMarkdown`: a stable-prefix cache that re-renders
  only the trailing partial up to the last complete markdown block boundary,
  memoized per `(prefix, width, theme_version)`; `append_stream` composes the
  cached `stable` + fresh `partial` into the live `Static` (test seam:
  `_full_render_count`). Markdown goes through the ONE renderer
  (`aider_ui.render_markdown`); Edit/Write tool items mount `DiffView`; thinking
  is a 3-mode `ThinkingCollapser` (collapsed / tail-window / expanded); tool
  output truncates via `ToolOutputCollapser` with a per-edit diffstat. Sub-agent
  deltas stream too (label-prefixed), not just the root.
- **Orchestration cards (#161-C)** — `tui/widgets/orchestration_cards.py`. The
  hypervisor tools render as dedicated cards instead of raw JSON:
  `spawn_agent`/`spawn_agents` → an agent-launch card (task truncated + model +
  agent_type, sourced from `result` + `args_summary`), `check_agents` → a fleet
  table (agent · status · tokens-or-steps · last tool), `tool_search` → a
  discovered-tools line list (name — one-line description).
  `register_orchestration_cards(registry, *, palette)` installs ONE *wrapping*
  `"tool"` renderer (called AFTER `register_transcript_renderers` in
  `cli_master._run_app`) that draws the card for those ids and delegates every
  other tool to the canonical base renderer via a private staging registry —
  additive, never edits the render dispatch.
- **Permissions (#154)** — `tui/permission_service.py` + `tui/widgets/permission_modal.py`.
  `PermissionService.decide(step)` is the layered chain (never raises → deny on
  error): skip-predicate → mode auto-accept-edits (READ/WRITE tiers) → persisted
  **deny** rule → persisted **allow** rule → in-memory session-grant → modal.
  Rules persist as globs (`tool`/`action`, `fnmatch`) in
  `~/.mewbo/permissions.json` (or `$MEWBO_HOME`) via `PermissionRuleStore`
  ("allow always" appends a rule; session grants stay in-memory). `RiskTier`
  classifies READ / WRITE / EXEC (EXEC for bash/`execute`; `_WRITE_TOOL_PATTERNS`
  elevates destructive-named tools to WRITE even on `get`) and drives badge
  colour + default focus from the `Palette`. The `PermissionModal(ModalScreen[str])`
  embeds `DiffView` for edits / full command for bash; single-key `a` allow-once ·
  `s` allow-session · `d`/**esc** deny (esc = safe default); LLM-sourced fields
  are `rich.markup.escape`'d. `install_permission_service(gateway, …)` wires
  `gateway.set_decision(service.decide)`. The **modal resolver** bridges the
  approval worker thread to the App loop:
  `app.call_from_thread(app.push_screen_wait, modal)` blocks the worker until the
  user resolves — the blocking-channel pattern that replaces the old termios race.
  **Permission modes** (`PermissionMode`: normal → auto-accept-edits → plan)
  cycle on **shift+tab** (`MewboApp.action_cycle_permission_mode`), shown in the
  `Footer` via `sub_title`.

## Live streaming transcript (#161) — the order-preserving keystone

The App path renders the turn **live, in core emission order** — not a post-run
batch dump bucketed by type. `tui/agent_transcript_hub.py:AgentTranscriptHub` is
the single source of truth: it subscribes ONCE to the shared
`SessionEventBus` (`register_observer`, wired in `cli_master._run_app`) — every
agent's events flow through it (`agent_id`/`depth`/`parent_id` on each payload) —
and demuxes by `agent_id` into `dict[agent_id -> AgentTranscript]`, each a
**strictly-ordered append log** (text · tool · spawn IN ARRIVAL ORDER, never
grouped). Rules: `agent_message_delta` coalesces into one streamed `TextSpan` per
step (closed by the step's `agent_message`, or any tool/spawn); a tool is ONE
mutable `ToolCall` keyed by `call_key` (`tool_id`+input) — created `running` on
`pre_tool_use` (the FleetBridge hook seam, the only tool-start signal; the bus has
none) and mutated in place to `done`/`error` + elapsed when its `tool_result`
event arrives; `sub_agent` appends a `Spawn` marker to the PARENT's log. The root
(`depth == 0`) drives the live `TranscriptView` via an injected `RootSink`
(`cli_master._TranscriptHubSink`, marshalling to the UI thread with
`call_from_thread`); the hub is pure state with no sink (the ordering regression
test in `tests/test_tui_agent_transcript_hub.py`). `TurnEngine(live=True)`
SUPPRESSES the legacy batch tool+assistant dump (it would duplicate + reorder
what streamed); the plain-REPL / `--query` / no-TTY path keeps `live=False` and
the batch dump. Tool cards carry THREE weights (#161): `running` = accent glyph +
full weight; `done`/`error` = settled, muted (`.t-settled` lowers `text-opacity`)
with `✓`/`✗` + elapsed (`✓ ran bash · 1.2s`). The foot spinner shows the live
step label (hub `set_status`) and on turn-settle COLLAPSES into a muted
`✓ done · {N}s` summary (`TranscriptView.finish_activity`, total elapsed from
`MewboApp._run_turn`). `build_tool_payload` (in `turn_engine.py`) is the ONE tool
payload extractor shared by the batch path and the hub.

**Deferred-sink law (never invoke the sink under `_lock`).** The sink marshals to
the UI thread (`call_from_thread`), and that UI callback re-enters the hub's read
API (`root_activity_label` takes `_lock`) — so calling the sink while holding
`_lock` cross-thread self-deadlocks (`_lock` is an `RLock`; per-thread reentrancy
does not save you). Ingest therefore **collects** sink actions into a FIFO under
`_lock` and **flushes** them only after the lock is released (`_enqueue_sink` /
`_flush_sink`); a single-drainer guard keeps delivery in strict enqueue order even
under concurrent `observe`. The `_sink_*` helpers snapshot their render payload at
enqueue time (a `ToolCall` mutates running→settled in place). Guards:
`tests/test_tui_agent_transcript_hub.py` — the foreign-thread lock probe + the
gated two-thread ordering test.

## Local-first + opt-in remote (#171) — `cli_remote.py`

The CLI is a **strictly-local engine** (core+tools only, never imports
`mewbo_api`): it runs `SessionRuntime`→`ToolUseLoop` in-process and the local
JSONL transcript is **authoritative**. Locality is transparent + user-controlled
via ONE CLI-only config block `cli.remote = {base_url, token}` (Pydantic
`extra="forbid"`, `token` is `x-secret`; `enabled` ⇔ non-empty `base_url`). All
three remote behaviours reuse existing seams — no bespoke launcher, no engine
relocation:

- **Transcript sync** — `RemoteTranscriptSync` (atomic) registers ONCE on the
  shared `SessionEventBus` as an observer (the CLI's `on_event` choke-point; the
  API bridges the same bus to `HookManager.run_on_event`, the CLI registers
  sinks on it directly — identical `(session_id, event)` contract). It filters to
  the live active session (a `session_id_provider` reading `state.session_id`, so
  it follows switches), enqueues non-blocking on the append hot path, and a daemon
  worker fire-and-forget POSTs batched events to `<base_url>/api/sessions/<id>/events`
  with `X-API-Key`. A failed POST is logged + dropped — **local JSONL stays
  truth.** Tests drive `flush()` with `autostart_worker=False` + an injected poster.
- **Product tools** — `mewbo_mcp_server_config(base,token)` synthesizes the Mewbo
  MCP entry (`transport: streamable_http`, `url=<base>/mcp`, `Authorization:
  Bearer`) and feeds it to `load_registry(extra_mcp_servers={"mewbo": …})` — the
  EXISTING registry seam plugins use. The CLI passes its registry explicitly to
  `run_sync`, so it bypasses the orchestrator's rebuild (root sees the tools).
  `ask_wiki`/`search`/`structured_query`/wiki-graph reads then execute remotely
  (the MCP server forwards `Bearer`→`X-API-Key` to REST). Only pass the kwarg when
  a server exists (stubs call `load_registry()` no-arg).
- **Provenance** — `surface=cli` is already threaded per-run via
  `run_sync(source_platform="cli")` (both App + plain paths). The **local-vs-synced
  facet** rides the EXISTING `TraceProvenance` context seam: a synced session
  writes ONE `{"transcript_sink": "synced"}` context event (via
  `runtime.append_context_event`) → `derive` promotes it to a `transcript_sink:`
  chip. Local-only sessions carry no facet (absence == local). Do NOT write a
  `source_platform` context event — `SessionOrigin.classify` reads it as CHANNEL.
- **Honest indicator** — `HeaderContext.transcript_sink` (`local-only` /
  `remote: <base_url>`) renders one `sink` row in the wide/normal header.
## Faithful live progress (#173) — authoritative todos + throughput meter

Two truth-in-progress upgrades, both riding the `AgentTranscriptHub` (the single
bus observer) — **zero core change on the CLI side**:

- **Authoritative todos.** The heuristic `fleet_bridge.TodoTracker` (re-projected
  every raw tool call) is **retired**. The Plan dock now renders core's
  authoritative `todos` event (an agent calls `update_todos` — see
  `mewbo_core/CLAUDE.md`). The hub ingests `todos` in `observe` → `_on_todos`
  (maps the event's `completed`→ the panel's `done`), stores the latest list, and
  exposes `root_todos()` — wired as the `TodoPanel` `todo_provider` in
  `cli_master`. Re-emit-each-update means the latest event fully replaces the
  dock. `update_todos` calls are SUPPRESSED as transcript cards
  (`_SUPPRESSED_CARD_TOOLS`) — their surface is the dock, not tool-card spam.
  `FleetBridge` tool hooks are now pure sidebar refreshes (no tracker).
- **`ThroughputMeter`** (`tui/status/throughput_meter.py`, mirrors `ContextMeter`)
  — one atomic class per agent: a `Phase` enum (idle/uploading/**reasoning**/
  streaming/running_tool/**stalled**), EWMA output tok/s (live `len/4`, reconciled
  to authoritative `llm_call_end` output), TTFT, and a monotonic last-event stall
  timer. **Fed from the hub's existing ingest points** (`_on_llm_start`/`_on_delta`/
  `tool_started`/`_on_tool_result`/`_on_llm_end`), the hub stamping arrival ts from
  its own clock (so `ZERO core change`; deterministic under a fake clock). The
  `reasoning` phase (a reasoning model legitimately emits no deltas while thinking)
  is inferred via core's `model_supports_reasoning_effort` (down-only import) and
  is exempt from the stall alarm. Rendered two ways: the ROOT meter drives the foot
  activity label via `TranscriptView.set_activity_label_provider` — pulled on the
  existing **0.1s (10 Hz) spinner tick** so a hung agent visibly flips to `stalled`
  with no new event (`streaming ↓82 tok/s` · `uploading…` · `running bash… (12s)` ·
  `⠿ stalled 30s`); and per-agent as a terse `FleetRow.throughput` facet in the
  fleet panel (chiefly the stall — the sub-agent-hang case).

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
  - **KeyListener** (`cli_keys.py`): REMOVED in #150 (the cbreak bridge + 0.2s race sleep are gone with the single Textual event loop). This bullet describes the historical Rich-`Live` path only.
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

> **Fixed for the App path (#154 landed).** The Textual `MewboApp` now routes
> tool approval through `tui/permission_service.py` + the `tui/widgets/permission_modal.py`
> modal (see "Permissions landed" below). `_confirm_rich_panel` survives **only** as
> the non-interactive plain-REPL fallback (`--query` / no-TTY / `MEWBO_DISABLE_TEXTUAL=1`);
> do not extend the `console.input` path — add to `PermissionService` instead.

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
**Textual App (#155):** `tui/input/completion.py:CompletionEngine` drives the
caret-anchored overlay — `@` tiered file ranking via `FileCatalog`, `/` fuzzy over
commands + skills + markdown custom commands + MCP prompts (match-highlighted,
arg-hints), `!` bash. Injected post-mount by `make_input_installer`. See
"Wave-2 landed" above. The completion never raises (degrades to no suggestions).

**Plain fallback only:** `cli_completer.py:MewboCompleter` drives live
`PromptSession` completion (`complete_while_typing=True`): `@<partial>` project
files (shared `FileCatalog`, git-index first, cached) and `/<partial>` CLI
commands **and** user-invocable skills. Never raises out of `get_completions`.

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
- `/context`: token-attribution context breakdown (#156).
- `/resume`: open the session switcher (also `ctrl+s`) (#157).
- `/rewind [N]`: revert workspace + conversation to a checkpoint (#157).
- `/keybindings`: show the effective key bindings + override-file path (#157).
- `/retry`, `/continue`: recover a halted/incomplete run (retry the last step / resume with context). In the no-TTY / `--query` plain fallback, when a plan proposal is pending `/continue` instead **approves** it (the interactive App approves via the plan-approval modal — see below).
- `/edit`: edit the last user message.
- `/mode [plan|act]`: set the run mode (plan-approval vs act).
- `/tokens`, `/budget`: report token usage / step budget for the session.

### Keybindings (Textual App, #157)
Global: `ctrl+o` transcript view · `ctrl+s` session switcher · `ctrl+l` clear+redraw
(corruption escape hatch) · `ctrl+p` command palette (#155) · `ctrl+r` reverse
history search (#155) · `shift+tab` permission-mode cycle (owned by #154). Bindings
are declarative (carry help text → `Footer`) and user-overridable via
`~/.mewbo/keybindings.json` (`KeybindingConfig`, bad file skipped).

## Console logging vs the alt-screen (#160)
The Textual `MewboApp` owns the alternate buffer, so **no loguru sink may write
to the terminal while it runs** — a live stderr sink interleaves with Textual's
rendering and corrupts the composer/footer (acute under `-vv`). `_run_app` calls
`_redirect_app_logs_to_file(args)` first: when `--log-file` was not passed it
routes all log detail to `~/.mewbo/cli.log` (overwrite-per-launch) via
`set_cli_log_file(..., quiet_console=True)`, dropping the console sink. `-vv`
therefore raises *captured* detail off-screen, never onto the screen. The
plain-REPL / `--query` / no-TTY paths never reach `_run_app`, so they keep
console logging; per-run detail is also always captured per-session by the
core's `session_log_context` file sink. No-op when `--log-file` is explicit
(`run_cli` already redirected + quieted the console up front).

## Core Files (UI-related)
- `apps/mewbo_cli/src/mewbo_cli/cli_master.py`: main loop, output sections, startup panel, Rich Live agent display.
- `apps/mewbo_cli/src/mewbo_cli/cli_agent_display.py`: `AgentDisplayManager` — thread-safe bridge between agent lifecycle hooks and Rich Live rendering. Handles collapsed/expanded tree, spinner, footer, elapsed time.
- `apps/mewbo_cli/src/mewbo_cli/tui/`: the Textual App foundation (#150) — `app.py` (`MewboApp`), `turn_engine.py`, `seams.py` (4 extension seams), `widgets/`. See "Foundation landed (#150)" above.
- `apps/mewbo_cli/src/mewbo_cli/cli_notices.py`: resilience/recovery/usage replay shared by the App and the plain fallback.
- `apps/mewbo_cli/src/mewbo_cli/cli_keys.py`: REMOVED in #150 (the `KeyListener` cbreak bridge is gone; the single Textual loop makes every keystroke a first-class widget event).
- `apps/mewbo_cli/src/mewbo_cli/cli_commands.py`: commands, model wizard, MCP listing.
- `apps/mewbo_cli/src/mewbo_cli/cli_dialogs.py`: dialog factory.
- `apps/mewbo_cli/src/mewbo_cli/cli_context.py`: state shared across commands.
- `packages/mewbo_core/src/mewbo_core/session_runtime.py`: shared runtime with `enqueue_message` (user steering) and `interrupt_step` (step interruption).

## Config knobs (UI-relevant)
- `llm.api_base`: printed in the ready panel.
- `llm.default_model` / `llm.action_plan_model`: used when `--model` is not set.
- `cli.remote.base_url` / `cli.remote.token`: opt-in remote seam (#171) — enables
  transcript sync + the Mewbo MCP product tools. CLI-only, `extra="forbid"`; empty
  ⇒ fully local. See "Local-first + opt-in remote" above.
- `cli.disable_textual`: disable dialogs (force fallback).
- `runtime.cli_log_style`: default log styling for the CLI.
- `configs/mcp.json`: MCP server config used for discovery.
- `cli.statusline.script`: path to a user statusLine script (JSON session-state on
  stdin → stdout rendered as the status line); unset ⇒ disabled (#156).
- `cli.statusline.interval_seconds`: statusLine refresh cadence (default 5.0, floor 1.0).

## Interaction design — converge on the proven affordance, don't invent (#159)

Before adding a **command, mode, or widget** for any user-facing interaction
(approval, confirm, pick-one, diff, progress), do BOTH of these — in this order —
*before writing the spec, not after*:

1. **Reuse the existing in-repo affordance.** Grep for a surface that already
   handles this class of interaction and reuse it: `PermissionModal` /
   `DialogFactory.select_one` / `ModalScreen` for decisions, `DiffView` for
   edits, the plan/todo dock for progress. A new surface that duplicates one of
   these is a DRY violation, full stop.
2. **Match the convergent reference UX.** When the feature has a well-known
   equivalent in leading terminal coding agents (plan approval, permission
   prompts, diffs, todos), check how they actually solve it (query a code-wiki
   Q&A tool against `anthropics/claude-code`, observed behaviour) and converge.
   Re-derive the pattern, never copy names — but **don't diverge from a solved
   interaction.**

**Decisions are selectable prompts/modals, NEVER new slash commands.** Approve /
reject / choose belongs in the one inline selectable dialog the reference tools
all use (and that we already have as a modal) — not a bespoke `/approve`+`/reject`
pair. Novelty in an already-solved interaction is a regression, not a feature.

> Caught in #159: the first spec invented `/approve` + `/reject` slash commands
> for plan approval. Both checks above would have rejected it up front — the
> `PermissionModal` already exists (check 1) and Claude Code/Codex use a single
> yes/no selectable dialog with no plan commands (check 2). It was reverted to
> the existing modal + a bordered plan card.

## KISS / DRY rules for UI work
- **Converge, don't invent**: reuse the existing affordance + match the reference
  UX before adding any new command/mode/widget (see the section above).
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
