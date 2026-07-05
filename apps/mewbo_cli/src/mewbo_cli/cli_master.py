#!/usr/bin/env python3
"""Terminal CLI for Mewbo."""
# ruff: noqa: E402

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import ThreadedCompleter
from prompt_toolkit.history import FileHistory
from rich import box
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.text import Text


def _verbosity_to_level(verbosity: int) -> str:
    if verbosity <= 0:
        return "WARNING"
    if verbosity == 1:
        return "DEBUG"
    return "TRACE"


def _parse_verbosity(argv: list[str]) -> int | None:
    count = 0
    for arg in argv[1:]:
        if arg in {"-v", "--verbose"}:
            count += 1
            continue
        if arg == "--debug":
            count = max(count, 1)
            continue
        if arg.startswith("--verbose="):
            raw = arg.split("=", 1)[1]
            try:
                count = max(count, int(raw))
            except ValueError:
                continue
            continue
        if arg.startswith("-v") and arg != "-v":
            tail = arg[1:]
            if tail and all(ch == "v" for ch in tail):
                count += len(tail)
    return count if count > 0 else None


def _bootstrap_cli_logging_env(argv: list[str]) -> None:
    """Configure logging overrides for the CLI before core imports."""
    from mewbo_core.config import set_config_override

    verbosity = _parse_verbosity(argv)
    if verbosity is not None:
        set_config_override({"runtime": {"log_level": _verbosity_to_level(verbosity)}})
        return
    set_config_override({"runtime": {"log_level": "WARNING"}})


_bootstrap_cli_logging_env(sys.argv)

from mewbo_core.classes import ActionStep, Plan, PlanStep, TaskQueue
from mewbo_core.common import MockSpeaker, format_tool_input, get_logger
from mewbo_core.components import resolve_langfuse_status
from mewbo_core.config import (
    AppConfig,
    get_app_config_path,
    get_config,
    get_config_value,
    get_mcp_config_path,
    get_version,
    set_app_config_path,
    start_preflight,
)
from mewbo_core.hooks import HookManager
from mewbo_core.permissions import auto_approve
from mewbo_core.session_event_bus import get_session_event_bus
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStoreBase, create_session_store
from mewbo_core.task_master import generate_action_plan
from mewbo_core.tool_registry import ToolRegistry, load_registry
from mewbo_tools.integration.mcp import (
    _load_mcp_config,
    mark_tool_auto_approved,
    save_mcp_config,
    tool_auto_approved,
)
from mewbo_tools.integration.reference_expansion import expand_references

from mewbo_cli.aider_ui import (
    render_diff,
    render_dir_payload,
    render_file_payload,
    render_markdown,
    render_shell_payload,
)
from mewbo_cli.cli_commands import get_registry
from mewbo_cli.cli_completer import MewboCompleter
from mewbo_cli.cli_context import CliState, CommandContext
from mewbo_cli.cli_dialogs import _confirm_rich_panel, _textual_enabled
from mewbo_cli.cli_notices import (
    maybe_print_recovery_hint,
    print_resilience_events,
    print_usage_footer,
)
from mewbo_cli.cli_remote import (
    RemoteTranscriptSync,
    expand_env,
    mewbo_mcp_server_config,
    sink_facet,
    sink_label,
)
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.agent_transcript_hub import AgentTranscriptHub
from mewbo_cli.tui.app import MewboApp
from mewbo_cli.tui.fleet_bridge import make_fleet_hook_factory
from mewbo_cli.tui.permission_service import (
    PermissionRuleStore,
    install_permission_service,
)
from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
)
from mewbo_cli.tui.transcript_render import register_transcript_renderers
from mewbo_cli.tui.turn_engine import TurnEngine
from mewbo_cli.tui.widgets.header import HeaderContext, HeaderView
from mewbo_cli.tui.widgets.orchestration_cards import register_orchestration_cards
from mewbo_cli.tui.widgets.permission_modal import PermissionModal
from mewbo_cli.tui.widgets.plan_modal import PlanApprovalModal

logging = get_logger(name="mewbo.cli")


def _resolve_session_id(
    runtime: SessionRuntime,
    session_id: str | None,
    session_tag: str | None,
    fork_from: str | None,
) -> str:
    return runtime.resolve_session(
        session_id=session_id,
        session_tag=session_tag,
        fork_from=fork_from,
    )


def _format_steps(steps: Iterable[PlanStep]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for step in steps:
        rows.append((step.title, step.description))
    return rows


def _ensure_history_path(path: str) -> str:
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _render_resume_hint(console: Console, session_id: str, session_dir: str | None) -> None:
    command = f"uv run mewbo --session {session_id}"
    if session_dir:
        command = f"{command} --session-dir {session_dir}"
    console.print(f"Resume: {command}", style="dim")


def _parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    command = parts[0]
    return command, parts[1:]


def _resolve_display_model(model_name: str | None) -> str:
    return (
        model_name
        or get_config_value("llm", "action_plan_model")
        or get_config_value("llm", "default_model")
        or "gpt-5.2"
    )


def _resolve_query_mode(query: str, state: CliState) -> str:
    """Resolve the orchestration mode from explicit CLI state.

    Keyword heuristics were removed — users opt into plan mode via
    ``/mode plan``. The ``query`` parameter is retained for API stability
    but deliberately unused.
    """
    del query  # Intentionally unused — mode is driven only by state.mode.
    return state.mode if state.mode in {"plan", "act"} else "act"


# Header rendering (HeaderContext + the responsive HeaderView) moved to
# ``mewbo_cli.tui.widgets.header`` so the App and the plain fallback share one
# renderer; the three width-variant free functions were collapsed into
# ``HeaderView.render(width)``.


def run_cli(args: argparse.Namespace) -> int:
    """Run the CLI application loop.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code for the CLI process.
    """
    console = Console(color_system=None if args.no_color else "auto")
    if args.config:
        set_app_config_path(args.config)
    config = get_config()
    # CLI local-first remote seam (#171): the engine still runs locally; when a
    # remote base URL is opted into via ``cli.remote`` the CLI additionally
    # mirrors its transcript to the remote API AND auto-registers the Mewbo MCP
    # server so the product tools appear + execute remotely. Env-expand so the
    # token can be a ``${VAR}`` reference; ``enabled`` keys off the resolved base.
    remote_base = expand_env(config.cli.remote.base_url).strip()
    remote_token = expand_env(config.cli.remote.token).strip()
    remote_enabled = bool(remote_base)
    if remote_enabled and not remote_token:
        logging.warning(
            "cli.remote.base_url is set but cli.remote.token is empty; "
            "remote sync + product tools will fail authentication."
        )
    remote_extra_servers = (
        {"mewbo": mewbo_mcp_server_config(remote_base, remote_token)} if remote_enabled else None
    )
    if getattr(args, "log_file", None):
        from mewbo_core.common import set_cli_log_file

        log_path = set_cli_log_file(
            args.log_file,
            overwrite=getattr(args, "log_overwrite", False),
            quiet_console=not getattr(args, "log_console", False),
        )
        console.print(f"[dim]Streaming logs to {log_path}[/dim]")
    logging.info(
        "Config paths: app={} mcp={}",
        get_app_config_path(),
        get_mcp_config_path() or "(disabled)",
    )
    logging.info(
        "LLM config: default={} action_plan={} tool={} api_base={} model_override={}",
        get_config_value("llm", "default_model", default=""),
        get_config_value("llm", "action_plan_model", default=""),
        get_config_value("llm", "tool_model", default=""),
        get_config_value("llm", "api_base", default=""),
        args.model or "",
    )
    if config.runtime.preflight_enabled:
        start_preflight(
            config,
            on_complete=lambda results: _render_preflight_warnings(console, results),
        )
    log_level = str(get_config_value("runtime", "log_level", default="INFO")).upper()
    verbosity = getattr(args, "verbose", 0)
    if verbosity > 0:
        logging.info(
            "CLI logging set to {} via --verbose (count={}).",
            log_level,
            verbosity,
        )
    store = create_session_store(root_dir=args.session_dir)
    runtime = SessionRuntime(session_store=store)
    session_id = _resolve_session_id(runtime, args.session, args.tag, args.fork)
    if getattr(args, "no_fallback", False):
        fallback_models: tuple[str, ...] | None = ()
    elif args.fallback_models:
        fallback_models = tuple(m.strip() for m in args.fallback_models.split(",") if m.strip())
    else:
        fallback_models = None
    state = CliState(
        session_id=session_id,
        show_plan=args.show_plan,
        model_name=args.model,
        fallback_models=fallback_models,
        auto_approve_all=args.auto_approve,
    )
    if remote_enabled:
        # Register the transcript mirror ONCE on the shared SessionEventBus — the
        # CLI's ``on_event`` choke-point (the append hot path). It follows session
        # switches via the live ``state.session_id`` provider; local JSONL stays
        # authoritative. Every CLI surface (App / plain REPL / --query) rides this.
        get_session_event_bus().register_observer(
            RemoteTranscriptSync(
                remote_base, remote_token, session_id_provider=lambda: state.session_id
            )
        )
        # Provenance facet (#171): tag the synced session ``transcript_sink:synced``
        # via the EXISTING TraceProvenance context seam so console/wiki/search can
        # filter it apart from a purely-local CLI session (which carries no facet).
        runtime.append_context_event(session_id, {"transcript_sink": sink_facet(enabled=True)})
    tool_registry = (
        load_registry(extra_mcp_servers=remote_extra_servers)
        if remote_extra_servers
        else load_registry()
    )
    registry = get_registry()

    from mewbo_core.plugins import load_all_plugin_components
    from mewbo_core.skills import SkillRegistry

    skill_registry = SkillRegistry()
    skill_registry.load()
    # Plugin commands/*.md files and skills/ dirs ride alongside built-in
    # skills — without this the dispatcher's ``/<name>`` lookup and
    # ``/skills`` listing only show base skills (orchestrator loads the
    # full set during query execution, so the two views drifted).
    skill_registry.load_plugin_components(load_all_plugin_components())

    base_url = get_config_value("llm", "api_base") or ""
    model_name = _resolve_display_model(state.model_name)
    langfuse_status = resolve_langfuse_status()
    all_specs = tool_registry.list_specs(include_disabled=True)
    builtin_enabled = sum(1 for spec in all_specs if spec.kind == "local" and spec.enabled)
    builtin_disabled = sum(1 for spec in all_specs if spec.kind == "local" and not spec.enabled)
    external_enabled = sum(1 for spec in all_specs if spec.kind == "mcp" and spec.enabled)
    external_disabled = sum(1 for spec in all_specs if spec.kind == "mcp" and not spec.enabled)
    try:
        mcp_config = _load_mcp_config()
        configured_servers = set(mcp_config.get("servers", {}).keys())
        discovered_servers = {
            spec.metadata.get("server")
            for spec in all_specs
            if spec.kind == "mcp" and spec.metadata.get("server")
        }
        missing_servers = configured_servers - discovered_servers
        if missing_servers:
            external_disabled += len(missing_servers)
    except Exception:
        pass
    version = get_version()
    header_ctx = HeaderContext(
        title="Mewbo",
        version=version,
        status_label="Ready",
        status_color="green",
        model=model_name,
        session_id=session_id,
        base_url=base_url or "",
        langfuse_enabled=langfuse_status.enabled,
        langfuse_reason=langfuse_status.reason,
        builtin_enabled=builtin_enabled,
        builtin_disabled=builtin_disabled,
        external_enabled=external_enabled,
        external_disabled=external_disabled,
        skill_count=len(skill_registry.list_all()),
        transcript_sink=sink_label(enabled=remote_enabled, base_url=remote_base),
    )
    # Single --query runs and non-interactive contexts (no TTY or
    # MEWBO_DISABLE_TEXTUAL=1) bypass the Textual App for CI/pipes; an
    # interactive TTY gets the full ``MewboApp``.
    if args.query:
        _print_plain_header(console, header_ctx)
        _maybe_warn_missing_configs(console, tool_registry, config)
        return _run_single_query(console, store, runtime, state, tool_registry, args.query, args)

    if not _textual_enabled():
        _print_plain_header(console, header_ctx)
        _maybe_warn_missing_configs(console, tool_registry, config)
        console.print("Mewbo CLI ready")
        console.print(f"Session: {state.session_id}")
        console.print("Type /help for commands.", style="dim")
        console.print()
        return _run_plain_repl(
            console, store, runtime, state, tool_registry, registry, skill_registry, args
        )

    return _run_app(
        header_ctx=header_ctx,
        store=store,
        runtime=runtime,
        state=state,
        tool_registry=tool_registry,
        command_registry=registry,
        skill_registry=skill_registry,
        config=config,
        args=args,
    )


def _print_plain_header(console: Console, header_ctx: HeaderContext) -> None:
    """Render the header for the non-interactive (plain) surface."""
    console.print(HeaderView(header_ctx).render(console.width or 80))


def _redirect_app_logs_to_file(args: argparse.Namespace) -> str | None:
    """Silence the console log sink for the Textual App run, routing detail to a file.

    The Textual ``MewboApp`` owns the alternate screen; any loguru stderr sink
    still active during ``app.run()`` interleaves with Textual's own rendering
    and visibly corrupts the composer/footer (most acute under ``-vv``, where
    TRACE lines stream onto the raw alt-screen). Reuse the ``--log-file``
    plumbing (``set_cli_log_file``) to send all log detail to a file off-screen
    and drop the console sink — so ``-vv`` still raises *captured* detail, never
    onto the screen.

    No-op when ``--log-file`` was already passed (``run_cli`` redirected to that
    file and quieted the console up front). The plain-REPL / ``--query`` /
    no-TTY paths never reach here, so they keep their console logging.

    Returns the file path logs were routed to, or ``None`` when already redirected.
    """
    if getattr(args, "log_file", None):
        return None
    from mewbo_core.common import set_cli_log_file
    from mewbo_core.config import resolve_mewbo_home

    return set_cli_log_file(
        str(resolve_mewbo_home() / "cli.log"),
        overwrite=True,
        quiet_console=True,
    )


class _TranscriptHubSink:
    """RootSink (#161): drive the live ``TranscriptView`` from the hub.

    Resolves the mounted App lazily through ``app_ref`` (the App exists only
    post-mount, like the fleet hooks) and marshals every render onto the UI
    thread via ``call_from_thread``. The transcript is read off the App through
    the same ``getattr`` pattern the FleetBridge uses for the agent panel. A
    missing App/transcript makes every method a safe no-op (plain fallback).
    """

    def __init__(self, app_provider: Callable[[], Any]) -> None:
        """Bind the lazy App provider; track which stream slots are open."""
        self._app_provider = app_provider
        self._begun: set[str] = set()

    def _resolve(self) -> tuple[Any, Any]:
        app = self._app_provider()
        return app, (getattr(app, "_transcript", None) if app is not None else None)

    def stream_delta(self, stream_id: str, delta: str) -> None:
        """Open the slot on first delta, then append (stable-prefix streaming)."""
        app, tv = self._resolve()
        if tv is None:
            return
        if stream_id not in self._begun:
            self._begun.add(stream_id)
            app.call_from_thread(tv.begin_stream, stream_id)
        app.call_from_thread(tv.append_stream, stream_id, delta)

    def stream_end(self, stream_id: str) -> None:
        """Finalise a streaming slot."""
        app, tv = self._resolve()
        self._begun.discard(stream_id)
        if tv is not None:
            app.call_from_thread(tv.end_stream, stream_id)

    def upsert_tool(self, card_id: str, item: Any) -> None:
        """Mount-or-update the mutable tool card in place."""
        app, tv = self._resolve()
        if tv is not None:
            app.call_from_thread(tv.upsert_tool, card_id, item)

    def spawn(self, item: Any) -> None:
        """Append a sub-agent spawn marker."""
        app, tv = self._resolve()
        if tv is not None:
            app.call_from_thread(tv.write_item, item)

    def set_status(self, label: str) -> None:
        """Update the foot activity label to reflect the live step."""
        app, tv = self._resolve()
        if tv is not None:
            app.call_from_thread(tv.set_activity_label, label)


def _run_app(
    *,
    header_ctx: HeaderContext,
    store: SessionStoreBase,
    runtime: SessionRuntime,
    state: CliState,
    tool_registry: ToolRegistry,
    command_registry: Any,
    skill_registry: Any,
    config: AppConfig,
    args: argparse.Namespace,
) -> int:
    """Launch the Textual ``MewboApp`` with the four seams wired up.

    The seams are created here and injected; ``TurnEngine`` (built via the
    factory once the App can supply its thread-safe emit callbacks) drives the
    runtime. The permission seam carries the ``/automatic`` + ``--auto-approve``
    auto-approve predicate; the modal decision lands with #154.
    """
    # The App owns the alt-screen — silence the console log sink (route detail
    # to a file) for the duration of the run so log lines can't bleed onto the
    # composer/footer under -vv. Plain-REPL / --query / no-TTY never reach here.
    _redirect_app_logs_to_file(args)
    budget = int(get_config_value("agent", "session_step_budget", default=0))
    palette = DEFAULT_PALETTE
    messages = MessageRendererRegistry()
    # Register the #152 transcript renderers BEFORE the App mounts so they win
    # the App's `has`-guarded foundation defaults (and the transcript widget's
    # own idempotent self-registration).
    register_transcript_renderers(messages, palette=palette)
    # Orchestration cards (#161-C) wrap the "tool" renderer to draw dedicated
    # spawn_agent/check_agents/tool_search cards; registered AFTER so they win.
    register_orchestration_cards(messages, palette=palette)
    sidebar_slots = SidebarSlotRegistry()
    permission = PermissionGateway(
        auto_approve=lambda: bool(state.auto_approve_all or getattr(args, "auto_approve", False))
    )
    input_gateway = InputGateway()
    input_gateway.set_completion_provider(
        _build_completion_provider(command_registry, skill_registry)
    )

    # Shared holder for the mounted App, populated by the first installer. The
    # engine's per-run fleet hooks (#161 sidebar refresh) and the sidebar queue
    # pill both resolve the App lazily through it (the App exists post-mount).
    app_ref: dict[str, Any] = {}

    # #161 — the AgentTranscriptHub is the single, order-preserving transcript
    # source: it subscribes ONCE to the shared SessionEventBus (every agent's
    # events flow through it) and demuxes by agent_id, streaming the root
    # (depth 0) live into the TranscriptView via the sink. It is ALSO the source
    # of the authoritative live todos (the ``todos`` event, #173) and per-agent
    # throughput. The per-run hook factory below feeds it the pre_tool_use ts.
    transcript_hub = AgentTranscriptHub(
        sink=_TranscriptHubSink(lambda: app_ref.get("app"))
    )
    get_session_event_bus().register_observer(transcript_hub.observe)
    fleet_hook_factory = make_fleet_hook_factory(lambda: app_ref.get("app"))

    def _hook_factory() -> HookManager:
        """Per-run hooks: FleetBridge + the hub's tool-start tap + session scope.

        Built fresh per run (run_sync calls it), so it is the natural place to
        scope the hub's bus observer to the current session and append the hub's
        ``pre_tool_use`` tap onto the same HookManager the FleetBridge uses.
        """
        manager = fleet_hook_factory()
        transcript_hub.set_active_session(state.session_id)
        manager.pre_tool_use.append(transcript_hub.tool_started)
        return manager

    def _plan_approval_resolver(plan_markdown: str, revision: int) -> str:
        """Bridge a pending plan proposal to the modal (mirrors the perm modal).

        ``push_screen_wait`` blocks the approval worker until the user chooses
        Approve / Keep-planning / Reject. Degrades to ``"refine"`` (the safe
        default — nothing destructive) when the App is unavailable.
        """
        app = app_ref.get("app")
        if app is None:
            return "refine"
        try:
            return (
                app.call_from_thread(
                    app.push_screen_wait,
                    PlanApprovalModal(plan_markdown, revision, palette=palette),
                )
                or "refine"
            )
        except Exception:  # noqa: BLE001 — a modal failure must never break the turn
            return "refine"

    def _engine_factory(
        emit: Callable[[Any], None],
        emit_renderable: Callable[[Any], None],
    ) -> TurnEngine:
        return TurnEngine(
            runtime=runtime,
            store=store,
            state=state,
            tool_registry=tool_registry,
            command_registry=command_registry,
            skill_registry=skill_registry,
            permission=permission,
            hook_factory=_hook_factory,
            emit=emit,
            emit_renderable=emit_renderable,
            plan_approval_resolver=_plan_approval_resolver,
            max_iters=args.max_iters,
            session_step_budget=budget,
            no_color=bool(getattr(args, "no_color", False)),
            live=True,
        )

    onboarding, notices = _onboarding_state(tool_registry, config)

    # Post-mount installers — the Wave-2 input/sidebar/session children mount
    # their widgets/keymaps here without editing app.py. Assembled here so the
    # wiring stays in one place; each entry configures the mounted App.
    installers = _build_installers(
        app_ref=app_ref,
        store=store,
        runtime=runtime,
        state=state,
        tool_registry=tool_registry,
        command_registry=command_registry,
        skill_registry=skill_registry,
        config=config,
        args=args,
        transcript_hub=transcript_hub,
        messages=messages,
        palette=palette,
    )

    app = MewboApp(
        header_ctx=header_ctx,
        messages=messages,
        sidebar_slots=sidebar_slots,
        permission=permission,
        input_gateway=input_gateway,
        engine_factory=_engine_factory,
        onboarding=onboarding,
        onboarding_notices=notices,
        installers=installers,
    )

    # #154 — install the layered PermissionService (skip → allow/deny rules →
    # session-grant → modal). The modal resolver bridges the worker thread the
    # approval_callback runs on to the App's main loop: ``push_screen_wait``
    # blocks the worker until the user resolves the modal (esc=deny default).
    def _modal_resolver(step: ActionStep) -> str:
        return app.call_from_thread(app.push_screen_wait, PermissionModal(step, palette=palette))

    install_permission_service(
        permission,
        rule_store=PermissionRuleStore(),
        session_id=lambda: state.session_id,
        mode_getter=lambda: app.permission_mode,
        modal_resolver=_modal_resolver,
    )
    return app.run() or 0


def _build_installers(
    *,
    app_ref: dict[str, Any],
    store: SessionStoreBase,
    runtime: SessionRuntime,
    state: CliState,
    tool_registry: ToolRegistry,
    command_registry: Any,
    skill_registry: Any,
    config: AppConfig,
    args: argparse.Namespace,
    transcript_hub: AgentTranscriptHub,
    messages: MessageRendererRegistry,
    palette: Palette,
) -> list[Callable[[Any], None]]:
    """Assemble the post-mount App installers for the Wave-2 children.

    Each Wave-2 feature (#155 input, #156 sidebar/status, #157 session) appends
    its installer here. Kept in one place so ``app.py`` stays closed. The shared
    ``app_ref`` is populated by the first installer so the engine's fleet hooks
    and the sidebar queue pill can resolve the mounted App lazily. The plan dock's
    todos come from the hub's authoritative ``todos`` event (``root_todos``, #173).
    """
    from mewbo_cli.tui.input.palette import make_input_installer
    from mewbo_cli.tui.session.install import (
        cmd_keybindings,
        cmd_resume,
        cmd_rewind,
        make_session_installer,
    )
    from mewbo_cli.tui.status.install import (
        cmd_context,
        make_sidebar_installer,
        make_statusline_installer,
    )
    from mewbo_cli.tui.widgets.input_area import InputArea

    # Register the Wave-2 slash commands on the live registry (idempotent — skip
    # any name already present, e.g. the base /compact). app.py / cli_commands.py
    # stay closed; the commands surface in completion + the palette automatically.
    _register_tui_commands(
        command_registry,
        context=cmd_context,
        resume=cmd_resume,
        rewind=cmd_rewind,
        keybindings=cmd_keybindings,
    )

    # The sidebar's queue pill reads #155's queued-message count off the mounted
    # InputArea; bind it lazily (the App is only available post-mount).
    def _capture_app(app: Any) -> None:
        app_ref["app"] = app

    def _queue_count() -> int:
        app = app_ref.get("app")
        if app is None:
            return 0
        try:
            return app.query_one(InputArea).queued_count()
        except Exception:
            return 0

    installers: list[Callable[[Any], None]] = [
        _capture_app,
        # #155 — sigil dispatch, tiered @ completion, palette, custom commands.
        make_input_installer(command_registry=command_registry, skill_registry=skill_registry),
        # #157 — session UX + global keymap (ctrl+o/ctrl+s/ctrl+l) + footer.
        make_session_installer(store=store, runtime=runtime, state=state),
        # #161 — faceted sidebar (Fleet · Plan · Context) + in-place drill-in.
        # The fleet rows + drill transcript come from the hub; the plan dock's
        # tri-state checklist reads the hub's authoritative ``todos`` event (#173).
        make_sidebar_installer(
            state=state,
            hub=transcript_hub,
            registry=messages,
            palette=palette,
            config=config,
            runtime=runtime,
            queue_count_provider=_queue_count,
            todo_provider=transcript_hub.root_todos,
        ),
        # IDE-style footer status line (host · model · cwd · branch · tokens).
        # state.model_name is only set by --model; pass the resolved display
        # model so the line shows the real model when the flag is omitted.
        make_statusline_installer(
            state=state,
            runtime=runtime,
            hub=transcript_hub,
            model=_resolve_display_model(state.model_name),
        ),
    ]
    return installers


def _register_tui_commands(command_registry: Any, **handlers: Any) -> None:
    """Register the Wave-2 slash commands, skipping any name already present."""
    existing = set(command_registry.list_commands())
    specs = [
        ("/context", "Show a token-attribution context breakdown", handlers["context"]),
        ("/resume", "Open the session switcher (also ctrl+s)", handlers["resume"]),
        ("/rewind", "Revert workspace + conversation to a checkpoint", handlers["rewind"]),
        ("/keybindings", "Show the effective key bindings", handlers["keybindings"]),
    ]
    for name, help_text, handler in specs:
        if name in existing:
            continue
        command_registry.command(name, help_text)(handler)


def _build_completion_provider(
    command_registry: Any, skill_registry: Any
) -> Callable[[str], list[str]]:
    """Foundation slash completion: command + user-invocable skill names (#155 extends)."""

    def _complete(text: str) -> list[str]:
        if not text.startswith("/") or " " in text:
            return []
        partial = text[1:]
        names = {c.lstrip("/") for c in command_registry.list_commands()}
        try:
            names.update(s.name for s in skill_registry.list_user_invocable())
        except Exception:
            pass
        return sorted(f"/{name}" for name in names if name.startswith(partial))

    return _complete


def _onboarding_state(tool_registry: ToolRegistry, config: AppConfig) -> tuple[bool, list[str]]:
    """Whether to open in the onboarding state, plus the notices to show."""
    config_path = Path(get_app_config_path())
    mcp_path_str = get_mcp_config_path()
    mcp_path = Path(mcp_path_str) if mcp_path_str else None
    missing: list[str] = []
    if not config_path.exists():
        missing.append(str(config_path))
    if mcp_path and not mcp_path.exists():
        missing.append(str(mcp_path))
    if not missing:
        return False, []
    return True, [
        "Config files missing: " + ", ".join(missing),
        "Run /config init, /mcp init, or /init to scaffold examples, then start chatting.",
    ]


def _run_plain_repl(
    console: Console,
    store: SessionStoreBase,
    runtime: SessionRuntime,
    state: CliState,
    tool_registry: ToolRegistry,
    registry: Any,
    skill_registry: Any,
    args: argparse.Namespace,
) -> int:
    """Plain prompt_toolkit REPL used when the Textual App is unavailable.

    The historic interactive surface, minus the Rich ``Live`` agent display and
    its ``KeyListener`` cbreak bridge (removed with #150): a non-``Live`` query
    path renders results after each run.
    """
    history_path = _ensure_history_path(args.history_file)
    completer = ThreadedCompleter(MewboCompleter(registry.list_commands(), skill_registry))
    session: PromptSession[str] = PromptSession(
        history=FileHistory(history_path),
        completer=completer,
        complete_while_typing=True,
    )

    while True:
        try:
            user_input = session.prompt("mewbo> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye.")
            _render_resume_hint(console, state.session_id, args.session_dir)
            return 0

        if not user_input:
            continue
        if user_input.startswith("/"):
            command, cmd_args = _parse_command(user_input)
            # 1. Try registered CLI commands first.
            if command in registry.list_commands():
                context = CommandContext(
                    console=console,
                    store=store,
                    state=state,
                    tool_registry=tool_registry,
                    runtime=runtime,
                    prompt_func=session.prompt,
                )
                if not registry.execute(command, context, cmd_args):
                    _render_resume_hint(console, state.session_id, args.session_dir)
                    return 0
                continue
            # 2. Try skill invocation: /skill-name [args]
            _skill_name = command.lstrip("/")
            _skill = skill_registry.get(_skill_name)
            if _skill is not None and _skill.user_invocable:
                from mewbo_core.skills import activate_skill

                _skill_args = " ".join(cmd_args)
                _instructions, _ = activate_skill(_skill, _skill_args)
                console.print(f"Activating skill: {_skill_name}", style="dim cyan")
                _run_query(
                    console,
                    store,
                    runtime,
                    state,
                    tool_registry,
                    user_input,
                    args,
                    session.prompt,
                    skill_instructions=_instructions,
                )
                continue
            # 3. Fall through to unknown command.
            console.print(
                "Unknown command. Use /help for commands or /skills for skills.",
            )
            continue

        _run_query(
            console,
            store,
            runtime,
            state,
            tool_registry,
            user_input,
            args,
            session.prompt,
        )


def _run_single_query(
    console: Console,
    store: SessionStoreBase,
    runtime: SessionRuntime,
    state: CliState,
    tool_registry: ToolRegistry,
    query: str,
    args: argparse.Namespace,
) -> int:
    _run_query(console, store, runtime, state, tool_registry, query, args, None)
    return 0


def _run_query(
    console: Console,
    store: SessionStoreBase,
    runtime: SessionRuntime,
    state: CliState,
    tool_registry: ToolRegistry,
    query: str,
    args: argparse.Namespace,
    prompt_func: Callable[[str], str] | None,
    skill_instructions: str | None = None,
) -> None:
    # Inline @<ref> expansion (files/dirs/@diff/URLs) against the CLI's cwd,
    # pre-LLM. The CLI runs the engine in-process, so it shares the same
    # reusable expander as the API rather than POSTing for expansion.
    query = expand_references(query, os.getcwd()) or query

    initial_plan = None
    mode = _resolve_query_mode(query, state)

    if state.show_plan and mode != "plan":
        initial_plan = generate_action_plan(
            user_query=query,
            model_name=state.model_name,
            session_summary=store.load_summary(state.session_id),
            mode=mode,
        )
        _render_plan_with_registry(console, initial_plan)

    auto_approve_enabled = bool(
        state.auto_approve_all or getattr(args, "auto_approve", False) or prompt_func is None
    )
    approval_callback = _build_approval_callback(
        console,
        state,
        tool_registry,
        auto_approve_enabled=auto_approve_enabled,
    )
    budget = int(get_config_value("agent", "session_step_budget", default=0))
    hook_manager = _build_cli_hook_manager(console, tool_registry)
    task_queue = runtime.run_sync(
        user_query=query,
        model_name=state.model_name,
        fallback_models=state.fallback_models,
        max_iters=args.max_iters,
        initial_plan=initial_plan,
        session_id=state.session_id,
        tool_registry=tool_registry,
        approval_callback=approval_callback,
        hook_manager=hook_manager,
        mode=mode,
        skill_instructions=skill_instructions,
        session_step_budget=budget,
        source_platform="cli",
    )

    # Handle episodic plan approval — the run terminated because the model
    # proposed a plan and is waiting for user approval/rejection.
    _pending, _revision, _plan_path = runtime._has_pending_plan_proposal(
        state.session_id,
    )
    if _pending and mode == "plan":
        # Render the proposed plan from the plan file.
        try:
            with open(_plan_path, encoding="utf-8") as _pf:
                _plan_content = _pf.read()
        except OSError:
            _plan_content = "(could not read plan file)"
        console.print(
            Panel(
                render_markdown(_plan_content),
                title=f":clipboard: Proposed Plan (revision {_revision})",
                border_style="cyan",
            )
        )
        if prompt_func is not None:
            _choice = (prompt_func("[A]pprove  [R]eject  > ") or "r").strip().lower()
            _approved = _choice.startswith("a") or _choice in {"y", "yes"}
        else:
            # Non-interactive (--query mode): auto-reject, user cannot interact.
            _approved = False

        if _approved:
            runtime.approve_plan(state.session_id)
            console.print(":white_check_mark: Plan approved.", style="green")
            state.mode = "act"
            # Start a follow-up act-mode run to execute the approved plan.
            task_queue = runtime.run_sync(
                user_query=(
                    "[system] The user approved your plan. Proceed with "
                    "implementation using the full toolset."
                ),
                session_id=state.session_id,
                model_name=state.model_name,
                fallback_models=state.fallback_models,
                max_iters=args.max_iters,
                tool_registry=tool_registry,
                approval_callback=approval_callback,
                hook_manager=hook_manager,
                mode="act",
                skill_instructions=skill_instructions,
                session_step_budget=budget,
                source_platform="cli",
            )
        else:
            runtime.reject_plan(state.session_id)
            console.print(
                ":pencil: Plan rejected. Type your refinement guidance "
                "at the next prompt and I will revise.",
                style="yellow",
            )
            return  # Return to REPL input

    _render_results_with_registry(
        console,
        task_queue,
        tool_registry,
        highlight_latest=not bool(task_queue.task_result),
        verbose=getattr(args, "verbose", 0) > 0,
    )
    if task_queue.task_result:
        console.print(
            Panel(
                render_markdown(task_queue.task_result),
                title=":speech_balloon: Response",
                border_style="bold green",
            )
        )
        print_usage_footer(console, store, state.session_id, state.model_name)

    # Replay LLM resilience notices (retry / fallback / no-progress halt)
    # from the run just completed — the core emits these to the transcript
    # rather than through hooks, so the live display never showed them.
    _halt_printed = print_resilience_events(console, store, state.session_id)

    # Surface recovery hint when the run ended in a recoverable failure so
    # users can type ``/retry`` or ``/continue`` at the next prompt.
    # Pass halt_printed so we skip the generic hint when the halt line (which
    # already mentions both commands) was just shown.
    maybe_print_recovery_hint(console, store, state.session_id, halt_printed=_halt_printed)


def _maybe_warn_missing_configs(
    console: Console,
    tool_registry: ToolRegistry,
    config: AppConfig,
) -> None:
    config_path = Path(get_app_config_path())
    mcp_path_str = get_mcp_config_path()
    mcp_path = Path(mcp_path_str) if mcp_path_str else None
    missing: list[str] = []
    if not config_path.exists():
        missing.append(str(config_path))
    if mcp_path and not mcp_path.exists():
        missing.append(str(mcp_path))
    if missing:
        console.print(
            "Config files missing: "
            + ", ".join(missing)
            + ". Run /config init, /mcp init, or /init to scaffold examples.",
            style="yellow",
        )

    llm_api_base = get_config_value("llm", "api_base", default="")
    llm_api_key = get_config_value("llm", "api_key", default="")
    if not llm_api_base:
        console.print("LLM base URL is not set (llm.api_base).", style="yellow")
    if not llm_api_key:
        console.print("LLM API key is not set (llm.api_key).", style="yellow")

    langfuse_enabled, langfuse_reason, _ = config.langfuse.evaluate()
    if config.langfuse.enabled and not langfuse_enabled:
        console.print(f"Langfuse disabled: {langfuse_reason}", style="yellow")

    ha_enabled, ha_reason, _ = config.home_assistant.evaluate()
    if config.home_assistant.enabled and not ha_enabled:
        console.print(f"Home Assistant disabled: {ha_reason}", style="yellow")

    disabled_tools = [
        spec for spec in tool_registry.list_specs(include_disabled=True) if not spec.enabled
    ]
    for spec in disabled_tools:
        reason = spec.metadata.get("disabled_reason") or "disabled"
        console.print(f"Tool {spec.tool_id} is disabled: {reason}", style="yellow")

    try:
        from mewbo_tools.integration import mcp as mcp_module

        failures = mcp_module.get_last_discovery_failures()
        for server, reason in failures.items():
            console.print(f"MCP server {server} unreachable: {reason}", style="yellow")
    except Exception:
        pass


def _render_preflight_warnings(console: Console, results: dict[str, dict[str, object]]) -> None:
    failures = [
        (name, info) for name, info in results.items() if info.get("enabled") and not info.get("ok")
    ]
    for name, info in failures:
        reason = info.get("reason") or "unknown failure"
        console.print(f"Preflight failed for {name}: {reason}", style="yellow")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(description="Mewbo terminal CLI")
    parser.add_argument("--query", help="Run a single query and exit")
    parser.add_argument("--model", help="Override model name")
    parser.add_argument(
        "--fallback-models",
        "--fallback-model",
        dest="fallback_models",
        help="Comma-separated fallback model IDs (e.g. gpt-5.4,gemini-2.5-pro)",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable model fallback for this run (ignores --fallback-models)",
    )
    parser.add_argument("--max-iters", type=int, default=3)
    parser.add_argument("--show-plan", action="store_true", default=True)
    parser.add_argument("--no-plan", action="store_false", dest="show_plan")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v=debug, -vv=trace)",
    )
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--session", help="Existing session id")
    parser.add_argument("--tag", help="Session tag to resume or create")
    parser.add_argument("--fork", help="Session id or tag to fork from")
    parser.add_argument(
        "--session-dir",
        default=None,
        help="Override session storage directory",
    )
    parser.add_argument(
        "--history-file",
        default="~/.mewbo/cli_history",
        help="Path to CLI history file",
    )
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument(
        "--log-file",
        default=None,
        help="Stream all logs to this file and keep the terminal output clean",
    )
    parser.add_argument(
        "--log-overwrite",
        "--overwrite",
        dest="log_overwrite",
        action="store_true",
        help="Truncate --log-file at startup instead of appending",
    )
    parser.add_argument(
        "--log-console",
        dest="log_console",
        action="store_true",
        help="With --log-file, ALSO keep logs on stderr (default: file only)",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve all permission prompts",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to app config file (default: auto-discover)",
    )
    return parser


def main() -> int:
    """Entry point for the CLI executable."""
    parser = build_parser()
    args = parser.parse_args()
    return run_cli(args)


def _tool_specs_by_id(tool_registry: ToolRegistry) -> dict[str, object]:
    return {spec.tool_id: spec for spec in tool_registry.list_specs()}


def _render_plan_with_registry(
    console: Console,
    plan: Plan,
) -> None:
    lines: list[Text] = []
    for index, (title, description) in enumerate(_format_steps(plan.steps), start=1):
        line = Text()
        line.append("[ ] ", style="dim")
        line.append(f"{index}. ", style="bold")
        line.append(title, style="cyan")
        if description:
            line.append(" — ", style="dim")
            line.append(description)
        lines.append(line)
    if not lines:
        lines.append(Text("No planned steps.", style="dim"))
    console.print(
        Panel(
            Group(*lines),
            title=":clipboard: Action Plan",
            border_style="cyan",
        )
    )


def _render_results_with_registry(
    console: Console,
    task_queue: TaskQueue,
    tool_registry: ToolRegistry,
    highlight_latest: bool = True,
    verbose: bool = False,
) -> None:
    specs = _tool_specs_by_id(tool_registry)
    panels: list[Panel] = []
    steps = task_queue.action_steps
    last_index = len(steps) - 1
    for index, step in enumerate(steps):
        spec = specs.get(step.tool_id)
        label = step.tool_id
        if spec is not None and getattr(spec, "kind", "") == "mcp":
            label = f"{label} (MCP)"
        label = f":wrench: {label}"
        result = None
        if step.result is not None:
            result = getattr(step.result, "content", step.result)
        is_latest = highlight_latest and index == last_index
        content_style = None if is_latest else "dim"
        border_style = "magenta" if is_latest else "dim magenta"
        renderable: RenderableType
        if result is None:
            renderable = Text("(no result)", style="dim")
        elif not verbose and not _should_force_preview(result):
            renderable = Text("(output hidden; use -v/--verbose)", style="dim")
        else:
            renderable = _format_tool_output(result, content_style)
        panels.append(
            Panel(
                renderable,
                title=label,
                border_style=border_style,
                padding=(0, 0),
                box=box.MINIMAL,
            )
        )
    if not panels:
        console.print(Text("No tool results.", style="dim"))
        return
    if len(panels) == 1:
        console.print(panels[0])
        return
    console.print(Columns(panels, expand=True))


def _should_force_preview(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    kind_raw = result.get("kind")
    kind = kind_raw if isinstance(kind_raw, str) else str(kind_raw or "")
    kind = kind.strip().lower()
    return kind in {"diff", "file"}


def _format_tool_output(result: object, content_style: str | None) -> RenderableType:
    style = content_style or ""
    if isinstance(result, dict):
        renderable = _render_tool_payload(result, style)
        if renderable is not None:
            return renderable
        return Syntax(
            json.dumps(result, indent=2, ensure_ascii=True),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    if isinstance(result, list):
        return Syntax(
            json.dumps(result, indent=2, ensure_ascii=True),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    if isinstance(result, str):
        stripped = result.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict | list):
                return Syntax(
                    json.dumps(parsed, indent=2, ensure_ascii=True),
                    "json",
                    theme="ansi_dark",
                    word_wrap=True,
                )
        return Text(result, style=style)
    return Text(str(result), style=style)


def _render_tool_payload(payload: dict[str, object], style: str) -> RenderableType | None:
    kind_raw = payload.get("kind")
    kind = kind_raw if isinstance(kind_raw, str) else str(kind_raw or "")
    kind = kind.strip().lower()
    if kind == "diff":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return Text("(empty diff)", style="dim")
        return render_diff(text)
    if kind == "file":
        path = payload.get("path")
        text = payload.get("text")
        if isinstance(path, str) and isinstance(text, str):
            return render_file_payload(path, text)
    if kind == "dir":
        path = payload.get("path")
        entries = payload.get("entries")
        if isinstance(path, str) and isinstance(entries, list):
            return render_dir_payload(path, [str(item) for item in entries])
    if kind == "shell":
        command = payload.get("command")
        exit_code = payload.get("exit_code")
        stdout = payload.get("stdout")
        stderr = payload.get("stderr")
        duration_ms = payload.get("duration_ms")
        cwd = payload.get("cwd")
        return render_shell_payload(
            command if isinstance(command, str) else None,
            stdout if isinstance(stdout, str) else None,
            stderr if isinstance(stderr, str) else None,
            exit_code if isinstance(exit_code, int) else None,
            duration_ms if isinstance(duration_ms, int) else None,
            cwd if isinstance(cwd, str) else None,
        )
    return None


def _build_cli_hook_manager(
    console: Console,
    tool_registry: ToolRegistry,
) -> HookManager:
    """Build the plain-path hook manager: per-tool spinner + compaction notice.

    The live agent tree / token streaming that used Rich ``Live`` is gone with
    #150 — the interactive surface is now ``MewboApp`` (#152/#156 own live
    feedback there).
    """

    def _on_compact(session_id: str, **kwargs: Any) -> None:
        summary = kwargs.get("summary", "")
        tokens_before = kwargs.get("tokens_before", 0)
        tokens_saved = kwargs.get("tokens_saved", 0)
        events_summarized = kwargs.get("events_summarized", 0)
        tokens_after = tokens_before - tokens_saved

        parts: list[RenderableType] = []
        if tokens_before and tokens_saved:
            pct = round((tokens_saved / tokens_before) * 100)
            parts.append(
                Text(f"{tokens_before:,} → {tokens_after:,} tokens ({pct}% reduction)", style="dim")
            )
        elif tokens_before:
            parts.append(Text(f"{tokens_before:,} tokens in context", style="dim"))
        if events_summarized:
            parts.append(Text(f"{events_summarized} events summarized", style="dim"))
        if summary:
            parts.append(Text(""))
            parts.append(Markdown(summary))
        elif parts:
            parts.append(Text(""))
            parts.append(
                Text("Summary unavailable — structured compaction failed.", style="dim italic")
            )

        if parts:
            console.print(
                Panel(
                    Group(*parts),
                    title="[dim blue]Context Compacted[/dim blue]",
                    border_style="dim blue",
                    padding=(0, 1),
                )
            )
        else:
            console.print("[dim blue]Context compacted[/dim blue]")

    # Per-tool console.status() spinner (the live agent tree is the App's job).
    status_holder: dict[str, Status] = {}
    specs = _tool_specs_by_id(tool_registry)

    def _start_spinner(action_step: ActionStep) -> ActionStep:
        spec = specs.get(action_step.tool_id)
        label = action_step.tool_id
        if spec is not None and getattr(spec, "kind", "") == "mcp":
            label = f"{label} (MCP)"
        status = console.status(f"Running {label}...", spinner="dots")
        status.start()
        status_holder["status"] = status
        return action_step

    def _stop_spinner(action_step: ActionStep, result: MockSpeaker) -> MockSpeaker:
        status = status_holder.pop("status", None)
        if status is not None:
            status.stop()
        return result

    return HookManager(
        pre_tool_use=[_start_spinner],
        post_tool_use=[_stop_spinner],
        on_compact=[_on_compact],
    )


def _persist_mcp_auto_approve(
    console: Console,
    server_name: str | None,
    tool_name: str | None,
) -> None:
    """Save an MCP tool as auto-approved in the MCP config file."""
    if not server_name or not tool_name:
        return
    try:
        config = _load_mcp_config()
        config = mark_tool_auto_approved(config, server_name, tool_name)
        save_mcp_config(config)
    except Exception as exc:
        console.print(f"Failed to persist auto-approve: {exc}")


def _build_approval_callback(
    console: Console,
    state: CliState,
    tool_registry: ToolRegistry,
    *,
    auto_approve_enabled: bool,
) -> Callable[[ActionStep], bool]:
    if auto_approve_enabled:
        return auto_approve
    specs_by_id = _tool_specs_by_id(tool_registry)

    def _approve(action_step: ActionStep) -> bool:
        spec = specs_by_id.get(action_step.tool_id)
        is_mcp = spec is not None and getattr(spec, "kind", "") == "mcp"
        server_name = tool_name = None
        if is_mcp:
            metadata = getattr(spec, "metadata", {}) or {}
            server_name = metadata.get("server")
            tool_name = metadata.get("tool")
            if server_name and tool_name:
                try:
                    if tool_auto_approved(_load_mcp_config(), server_name, tool_name):
                        return True
                except Exception:
                    pass

        subject = (
            f"{action_step.tool_id}:{action_step.operation} "
            f"({format_tool_input(action_step.tool_input)})"
        )
        decision = _confirm_rich_panel(
            console,
            "Approve tool use?",
            subject=subject,
            default=False,
            allow_always=bool(is_mcp),
            allow_session=True,
        )
        if decision == "always":
            _persist_mcp_auto_approve(console, server_name, tool_name)
            return True
        if decision == "session":
            state.auto_approve_all = True
            return True
        return decision == "yes"

    return _approve


if __name__ == "__main__":
    raise SystemExit(main())
