"""Tool implementations for the Mewbo MCP server.

Each tool group is one **atomic class** — :class:`SessionTools`,
:class:`WikiTools`, :class:`IntegrationTools`, :class:`SearchTools`. A class
holds the injected :class:`~mewbo_mcp.rest.RestClient` (already carrying the
caller's token) plus any per-feature config as state, and exposes the feature's
behaviors as methods over that state; pure helpers are ``@staticmethod`` /
``@classmethod``. Construct one per call with an authenticated client
(dependency injection) and invoke a behavior:

    await SessionTools(client).history(session_id=sid, level="overview")

No class touches FastMCP or auth — ``server.py`` is the only place that does —
so the unit tests stub only the HTTP boundary. The three module-level
``_as_dict`` / ``_dict_list`` / ``_as_list`` helpers are cross-cutting coercion
primitives shared by every group; everything feature-specific lives on its
class.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from .rest import RestClient, RestError
from .timeline import Turn, build_timeline

# ---------------------------------------------------------------------------
# Shared coercion primitives (cross-cutting — belong to no single feature)
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """Return *value* if it is a dict, else an empty dict (defensive coercion)."""
    return value if isinstance(value, dict) else {}


def _dict_list(payload: Any, key: str) -> list[dict[str, Any]]:
    """Extract ``payload[key]`` as a list of dicts, dropping non-dict entries."""
    raw = _as_dict(payload).get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _as_list(value: Any) -> list[Any]:
    """Return *value* if it is a list, else an empty list (defensive coercion)."""
    return value if isinstance(value, list) else []


async def bounded_poll(
    fetch: Callable[[], Awaitable[Any]],
    is_terminal: Callable[[Any], bool],
    *,
    timeout_s: float,
    interval_s: float,
) -> tuple[Any, bool]:
    """Poll ``fetch`` until ``is_terminal`` or the deadline; return (last, terminal).

    The one bounded-await every long-running tool shares — search, wiki ``ask``,
    and ``structured_query`` — so a non-streaming caller gets the settled result
    or a clean ``running`` partial instead of hanging. Fetches immediately, then
    every ``interval_s`` until terminal or ``timeout_s`` elapses.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        snapshot = await fetch()
        if is_terminal(snapshot):
            return snapshot, True
        if asyncio.get_running_loop().time() >= deadline:
            return snapshot, False
        await asyncio.sleep(interval_s)


# Long-running-tool budget invariant (#41): every inline poll budget
# (``WikiTools``/``SearchTools``/``StructuredQueryTools`` ``timeout_s``) MUST be
# strictly below the transport/proxy timeout (httpx read 30s, and the shorter
# ``mcp.hurricane.home`` front-proxy ceiling). The budget is the tightest ceiling
# so :func:`poll_or_handle` ALWAYS returns the resumable handle as
# ``status:"running"`` before any layer cuts the connection — a raw
# ``httpx.ReadTimeout`` mid-poll can never strand the caller without an id.
PROXY_CEILING_S: float = 30.0


async def poll_or_handle(
    run_id: str | None,
    fetch: Callable[[], Awaitable[Any]],
    is_terminal: Callable[[Any], bool],
    shape: Callable[[Any, bool], dict[str, Any]],
    *,
    timeout_s: float,
    interval_s: float,
) -> dict[str, Any]:
    """Bounded-poll a started run; degrade to the resumable handle on timeout.

    On budget OR transport timeout, return the resumable handle as
    ``status:'running'`` instead of raising (#41). ``run_id`` is obtained before
    polling, so a slow API / short proxy can't strand the caller.

    Only a *transport-level* failure (``RestError.status_code is None`` — a
    proxy/read timeout where the run's true state is unknown) degrades to the
    running handle. A definitive server response (a 4xx/5xx with a status code,
    e.g. a 422 "model did not emit") is re-raised so the ``_enveloped`` decorator
    surfaces the real reason — masking it as ``running`` would make the caller
    poll a terminally-failed run forever.
    """
    if not run_id:
        snapshot = await fetch()
        return shape(snapshot, is_terminal(snapshot))
    try:
        snapshot, terminal = await bounded_poll(
            fetch, is_terminal, timeout_s=timeout_s, interval_s=interval_s
        )
        return shape(snapshot, terminal)
    except RestError as exc:
        if exc.status_code is not None:
            raise
        return shape({}, False)


# ---------------------------------------------------------------------------
# A + B. Sessions — create & control, discover & read (tiered)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionTools:
    """The session tool surface over the REST API — one atomic unit.

    Holds the injected REST :attr:`client` and exposes the session feature's
    behaviors — create + steer a session, and read its history at four detail
    tiers so a caller controls its own context spend — as methods over that
    state. Construct it per call with an authenticated client (dependency
    injection); it never touches FastMCP, so tests stub only the HTTP boundary.
    """

    client: RestClient

    # Truncation budget for user/assistant text in the ``turns`` tier so a long
    # conversation stays cheap for the consuming agent.
    TURN_TEXT_TRUNC: ClassVar[int] = 500
    # ``full`` tier caps (#42): page the step list and bound each inlined field so
    # a single fat turn can't blow the token cap. ``STEP_FIELD_TRUNC`` is a backstop
    # — the API ``?truncate=1`` already trims oversized fields at the source; we
    # keep the MCP cap ≥ the API cap so MCP only trims if the API didn't.
    FULL_STEPS_PAGE: ClassVar[int] = 20
    STEP_FIELD_TRUNC: ClassVar[int] = 4000
    LEVELS: ClassVar[frozenset[str]] = frozenset({"overview", "turns", "steps", "full"})
    # Mirrors core's ``tool_use_loop._NO_CONTENT_PLACEHOLDER`` (duplicated, never
    # imported — same HTTP/process boundary rule as ``SearchTools``). A pure
    # tool-call turn's content is sanitized to this upstream; we render such a
    # turn from its steps instead of surfacing the sentinel.
    NO_CONTENT_SENTINEL: ClassVar[str] = "(no content)"

    # -- create & control -------------------------------------------------

    async def create(
        self,
        *,
        prompt: str,
        project: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        worktree: str | None = None,
        integrations: list[str] | None = None,
        mode: str | None = None,
        title: str | None = None,
        tag: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a session, optionally provisioning a worktree, then run *prompt*.

        Default behavior auto-provisions a fresh worktree+branch off the base in
        the target project — ``repo``/``project`` is a registered project name OR
        its git identity (``host/owner/repo`` / ``owner/repo``; see
        ``list_projects``). When ``branch`` or ``worktree`` is supplied, the
        existing one is targeted instead. ``integrations`` maps to the session's
        ``context.mcp_tools`` allowlist.

        ``tag`` is a single optional session tag (the API stores ONE
        ``session_tag``). ``idempotency_key`` tags the session so a client retry
        is identifiable/reapable (used as the tag when no explicit ``tag`` given).

        Returns ``{session_id, status, title?}`` — the minimal shape. Worktree
        lifecycle is system-owned; the API's ``on_session_end`` hook is the sole
        authoritative reaper, so worktree ids are not surfaced here (Fix 2).

        Wires: ``POST /api/v_projects/<id>/worktrees`` (create-from-base) →
        ``POST /api/sessions`` → ``POST /api/sessions/<id>/query``.
        """
        target_project = repo or project
        project_ref: str | None = None

        if target_project:
            if worktree:
                # Caller pinned an existing managed worktree by its project id.
                project_ref = f"managed:{worktree}"
            else:
                # ``branch`` set → target it; else a fresh worktree off the base.
                base = None if branch else await self._current_branch(target_project)
                new_branch = branch or self._auto_branch_name(title or prompt)
                wt = await self._provision_worktree(target_project, new_branch, base=base)
                worktree_project_id = str(wt["project_id"])
                project_ref = f"managed:{worktree_project_id}"

        context: dict[str, Any] = {}
        if integrations:
            context["mcp_tools"] = list(integrations)

        session_body: dict[str, Any] = {}
        if project_ref:
            session_body["project"] = project_ref
        if context:
            session_body["context"] = context
        # ``POST /api/sessions`` accepts a single ``session_tag``.
        session_tag = tag or idempotency_key
        if session_tag:
            session_body["session_tag"] = session_tag

        created = await self.client.post("/api/sessions", json=session_body)
        session_id = _as_dict(created).get("session_id")
        if not session_id:
            raise RestError("Session creation did not return a session_id.")

        if title:
            # Best-effort title set (PATCH = user-provided title); never block on it.
            try:
                await self.client.request(
                    "PATCH", f"/api/sessions/{session_id}/title", json={"title": title}
                )
            except RestError:
                pass

        query_body: dict[str, Any] = {"query": prompt}
        if mode:
            query_body["mode"] = mode
        if context:
            query_body["context"] = context
        await self.client.post(f"/api/sessions/{session_id}/query", json=query_body)

        return {"session_id": session_id, "status": "running"}

    async def send_followup(self, *, session_id: str, message: str) -> dict[str, Any]:
        """Send a steering message to a session, re-engaging it if idle.

        Wires ``POST /api/sessions/<id>/message``. The API queues the message
        into a running session, or starts a fresh run when the session is idle
        or finished (only a terminated session rejects). Surfaces the new
        ``run_id`` when a run was (re)started so the caller can poll it.
        """
        result = _as_dict(
            await self.client.post(f"/api/sessions/{session_id}/message", json={"text": message})
        )
        enqueued = bool(result.get("enqueued"))
        out: dict[str, Any] = {
            "session_id": session_id,
            "status": "enqueued" if enqueued else "unknown",
        }
        if result.get("run_id"):
            out["run_id"] = result["run_id"]
        return out

    async def interrupt(self, *, session_id: str) -> dict[str, Any]:
        """Interrupt the current step of a session (graceful no-op when idle).

        Wires ``POST /api/sessions/<id>/interrupt``; an idle session is a 200
        no-op (``interrupted: false``), never an error.
        """
        result = await self.client.post(f"/api/sessions/{session_id}/interrupt")
        ok = bool(_as_dict(result).get("interrupted"))
        return {"session_id": session_id, "status": "interrupted" if ok else "no_active_run"}

    # -- discover & read (tiered) -----------------------------------------

    async def list_sessions(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List sessions (newest-first, capped), projected compact.

        Wires ``GET /api/sessions``, filters by ``project``/``status``/``since``
        locally, returns at most ``limit`` rows (default 20, newest-first) so the
        default response stays small. Each row is projected to
        ``{session_id, title, project, status, done_reason, created_at, origin?,
        archived?}`` — dropping the redundant ``running`` flag and the verbose raw
        ``context`` while SURFACING the ``project`` the filter matches on.

        ``project`` matches the underlying repo name/identity: a session matches
        when ``project`` equals ``context.repo`` OR ``context.project`` (any
        ``managed:`` prefix stripped — see :meth:`_row_project`).
        """
        sessions = _dict_list(await self.client.get("/api/sessions"), "sessions")

        def _keep(s: dict[str, Any]) -> bool:
            if status and str(s.get("status")) != status:
                return False
            if since and str(s.get("created_at") or "") < since:
                return False
            if project:
                ctx = s.get("context")
                if not isinstance(ctx, dict):
                    return False
                repo = str(ctx.get("repo") or "")
                proj = str(ctx.get("project") or "")
                proj_bare = proj[len("managed:") :] if proj.startswith("managed:") else proj
                if project not in {repo, proj, proj_bare}:
                    return False
            return True

        kept = [s for s in sessions if _keep(s)]
        kept.sort(key=lambda s: str(s.get("created_at") or ""), reverse=True)
        if isinstance(limit, int) and limit > 0:
            kept = kept[:limit]
        return {"sessions": [self._shape_session(s) for s in kept], "count": len(kept)}

    @staticmethod
    def _row_project(s: dict[str, Any]) -> str | None:
        """The session's project identity from its context (repo or bare project)."""
        ctx = s.get("context")
        if not isinstance(ctx, dict):
            return None
        repo = str(ctx.get("repo") or "")
        proj = str(ctx.get("project") or "")
        proj_bare = proj[len("managed:") :] if proj.startswith("managed:") else proj
        return repo or proj_bare or None

    @classmethod
    def _shape_session(cls, s: dict[str, Any]) -> dict[str, Any]:
        """Compact projection of one ``GET /api/sessions`` summary row."""
        out: dict[str, Any] = {
            "session_id": s.get("session_id"),
            "title": s.get("title"),
            "project": cls._row_project(s),
            "status": s.get("status"),
            "done_reason": s.get("done_reason"),
            "created_at": s.get("created_at"),
        }
        for key in ("origin", "archived"):
            if s.get(key) is not None:
                out[key] = s.get(key)
        return out

    async def history(
        self, *, session_id: str, level: str, turn: int | None = None, step_offset: int = 0
    ) -> dict[str, Any]:
        """Tiered read of a session's history.

        ``level`` is one of ``overview`` | ``turns`` | ``steps`` | ``full``. The
        ``steps`` and ``full`` tiers require ``turn`` (1-based). The consuming
        agent picks the tier so it controls its own context spend. The ``full``
        tier pages its steps (``FULL_STEPS_PAGE`` per call from ``step_offset``)
        so a fat turn can't blow the token cap (#42).
        """
        if level not in self.LEVELS:
            raise ValueError(f"Invalid level '{level}'. Use overview|turns|steps|full.")

        turns, meta = await self._load_turns(session_id)

        if level == "overview":
            return self._overview(session_id, turns, meta)
        if level == "turns":
            return {"session_id": session_id, "turns": [self._turn_summary(t) for t in turns]}

        # steps / full require a specific turn.
        if turn is None:
            raise ValueError(f"level '{level}' requires a 'turn' index (1-based).")
        selected = self._select_turn(turns, turn)
        if selected is None:
            raise ValueError(f"Turn {turn} not found (session has {len(turns)} turns).")

        if level == "steps":
            return {
                "session_id": session_id,
                "turn": selected.index,
                "steps": [self._step_summary(e) for e in selected.steps],
            }
        # full — references the sub-agent tree rather than inlining it (it is a
        # duplicate of ``get_agent_tree`` and the heaviest payload otherwise).
        # Page the (potentially huge) step list so one fat turn stays bounded.
        all_steps = selected.steps
        offset = max(step_offset, 0)
        page = all_steps[offset : offset + self.FULL_STEPS_PAGE]
        out: dict[str, Any] = {
            "session_id": session_id,
            "turn": selected.index,
            "user_text": selected.user_text,
            "assistant_text": self._clean_summary(selected),
            "done_reason": selected.done_reason,
            "steps": [self._step_full(e) for e in page],
            "step_count": len(all_steps),
            "step_offset": offset,
            "agents": "call get_agent_tree(session_id) for the sub-agent tree",
        }
        if offset + self.FULL_STEPS_PAGE < len(all_steps):
            out["next_step_offset"] = offset + self.FULL_STEPS_PAGE
        return out

    async def agent_tree(self, *, session_id: str) -> dict[str, Any]:
        """Return the sub-agent tree with lifecycle state.

        Wires ``GET /api/sessions/<id>/agents``. During the post-create spin-up
        window the API may be briefly unreachable; rather than leak a raw
        transport error we return ``{"status": "initializing"}`` so a caller can
        retry — the same guard :meth:`_safe_agent_tree` gives the ``full`` tier.
        """
        tree = await self._safe_agent_tree(session_id)
        return tree if tree is not None else {"status": "initializing"}

    # -- internals: behavior over the atomic state ------------------------

    async def _current_branch(self, project_ref: str) -> str | None:
        """Return the project's current branch to use as a worktree base, if any."""
        try:
            info = await self.client.get(f"/api/v_projects/{project_ref}/branches")
        except RestError:
            return None
        if isinstance(info, dict):
            current = info.get("current_branch")
            if isinstance(current, str) and current:
                return current
        return None

    async def _provision_worktree(
        self, project_ref: str, branch: str, *, base: str | None
    ) -> dict[str, Any]:
        """Create (or reuse) a worktree for *branch* under *project_ref*.

        ``base`` set → create a fresh branch from it (create-from-base);
        ``base`` is ``None`` → target an existing branch. Returns the worktree's
        VirtualProject dict (it carries ``project_id``).
        """
        body: dict[str, Any] = {"branch": branch}
        if base:
            body["base"] = base
        wt = await self.client.post(f"/api/v_projects/{project_ref}/worktrees", json=body)
        if not isinstance(wt, dict) or not wt.get("project_id"):
            raise RestError("Worktree creation did not return a project_id.")
        return wt

    async def _load_turns(self, session_id: str) -> tuple[list[Turn], dict[str, Any]]:
        """Fetch a session's events and reconstruct turns.

        Returns ``(turns, meta)`` where ``meta`` is the raw ``/events`` payload —
        it carries the API's authoritative ``status``/``done_reason``/``title``/
        ``running`` so the overview never has to reconstruct them from the
        timeline tail (the old source of the ``status: null`` / title bugs).

        ``?truncate=1`` opts into the API's field-capping so oversized
        ``result``/``tool_input`` fields are trimmed at the source for every tier
        (only ``full`` inlines them) — the first half of the #42 token-cap fix.
        """
        payload = _as_dict(
            await self.client.get(
                f"/api/sessions/{session_id}/events", params={"truncate": "1"}
            )
        )
        events = _dict_list(payload, "events")
        return build_timeline(events), payload

    async def _safe_agent_tree(self, session_id: str) -> dict[str, Any] | None:
        """Fetch the agent tree, returning ``None`` if the call fails."""
        try:
            result = await self.client.get(f"/api/sessions/{session_id}/agents")
        except RestError:
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _auto_branch_name(seed: str) -> str:
        """Derive a short, git-safe ``mewbo/<slug>`` branch name from *seed*."""
        import re

        slug = re.sub(r"[^a-z0-9]+", "-", seed.lower()).strip("-")[:32] or "session"
        return f"mewbo/{slug}"

    @staticmethod
    def _select_turn(turns: list[Turn], index: int) -> Turn | None:
        """Return the turn with 1-based ``index``, or ``None`` if out of range."""
        for t in turns:
            if t.index == index:
                return t
        return None

    @classmethod
    def _overview(
        cls, session_id: str, turns: list[Turn], meta: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the cheapest tier: counts + token totals, no per-turn detail."""
        first = turns[0] if turns else None
        last = turns[-1] if turns else None
        total_steps = sum(t.step_count for t in turns)
        total_input = 0
        total_output = 0
        for t in turns:
            usage = t.token_usage()
            if usage is None:
                continue
            total_input += usage.input_tokens + usage.sub_input_tokens
            total_output += usage.output_tokens + usage.sub_output_tokens
        running = bool(meta.get("running"))
        # Prefer the API's authoritative status/title/done_reason; reconstruct
        # from the timeline only when the events payload omits them.
        status = meta.get("status") or (
            "running" if running else (last.done_reason if last else None)
        )
        title = meta.get("title") or (first.user_text[:120] if first else "")
        return {
            "session_id": session_id,
            "title": title,
            "summary": cls._clean_summary(last) if last else "",
            "status": status,
            "done_reason": meta.get("done_reason") or (last.done_reason if last else None),
            "running": running,
            "turn_count": len(turns),
            "step_count": total_steps,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
        }

    @classmethod
    def _clean_summary(cls, turn: Turn) -> str:
        """Readable assistant text for *turn*, robust to tool-call-only turns.

        A pure tool-call turn has its content sanitized upstream to the
        ``(no content)`` placeholder; rather than surface that sentinel (or any
        serialized/text-leaked tool call trailing it) we render the turn from its
        tool steps. Keyed on OUR placeholder, NEVER on a model's text-leak format
        — that leak is a response-normalization concern, not a display one.
        """
        text = turn.assistant_text or ""
        if not text.startswith(cls.NO_CONTENT_SENTINEL):
            return text
        tools = [
            tid
            for tid in (cls._event_payload(e).get("tool_id") for e in turn.steps)
            if isinstance(tid, str) and tid
        ]
        return "→ called " + ", ".join(dict.fromkeys(tools)) if tools else ""

    @classmethod
    def _truncate(cls, text: str) -> str:
        """Truncate *text* to :attr:`TURN_TEXT_TRUNC`, appending an ellipsis."""
        limit = cls.TURN_TEXT_TRUNC
        return text if len(text) <= limit else text[:limit] + "…"

    @classmethod
    def _turn_summary(cls, turn: Turn) -> dict[str, Any]:
        """Per-turn summary for the ``turns`` tier (truncated text, no step detail)."""
        usage = turn.token_usage()
        return {
            "index": turn.index,
            "user_text": cls._truncate(turn.user_text),
            "assistant_text": cls._truncate(cls._clean_summary(turn)),
            "done_reason": turn.done_reason,
            "step_count": turn.step_count,
            "tokens": usage.to_dict() if usage else None,
        }

    @staticmethod
    def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
        """Return an event's ``payload`` dict, or an empty dict when absent/malformed."""
        return _as_dict(event.get("payload"))

    @classmethod
    def _step_summary(cls, event: dict[str, Any]) -> dict[str, Any]:
        """A cheap per-step preview: ``tool_id → summary`` (no full result)."""
        payload = cls._event_payload(event)
        return {
            "tool_id": payload.get("tool_id"),
            "operation": payload.get("operation"),
            "summary": payload.get("summary"),
            "success": payload.get("success"),
            "agent_id": payload.get("agent_id"),
            "ts": event.get("ts"),
        }

    @classmethod
    def _step_full(cls, event: dict[str, Any]) -> dict[str, Any]:
        """Full per-step log: inputs, result, and error for the ``full`` tier.

        ``tool_input``/``result`` are capped to :attr:`STEP_FIELD_TRUNC` as a
        backstop in case the API's ``?truncate=1`` didn't trim them (#42).
        """
        payload = cls._event_payload(event)
        return {
            "tool_id": payload.get("tool_id"),
            "operation": payload.get("operation"),
            "tool_input": cls._cap_field(payload.get("tool_input")),
            "result": cls._cap_field(payload.get("result")),
            "success": payload.get("success"),
            "summary": payload.get("summary"),
            "error": payload.get("error"),
            "agent_id": payload.get("agent_id"),
            "model": payload.get("model"),
            "ts": event.get("ts"),
        }

    @classmethod
    def _cap_field(cls, value: Any) -> Any:
        """Cap a step's ``tool_input``/``result`` to :attr:`STEP_FIELD_TRUNC`.

        A dict/list is JSON-stringified before measuring so an oversized nested
        payload is trimmed too; an over-budget value becomes a string with a
        ``…(truncated, N chars)`` marker. Small / non-string values pass through.
        """
        import json

        if value is None:
            return value
        if isinstance(value, str):
            text = value
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, default=str)
            if len(text) <= cls.STEP_FIELD_TRUNC:
                return value  # within budget — keep the structured value as-is
        else:
            return value
        if len(text) <= cls.STEP_FIELD_TRUNC:
            return text if isinstance(value, str) else value
        full_len = len(text)
        return f"{text[: cls.STEP_FIELD_TRUNC]}…(truncated, {full_len} chars)"


# ---------------------------------------------------------------------------
# C. Wiki — query & ask
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WikiTools:
    """The Agentic Wiki tool surface over the REST API — one atomic unit.

    Holds the injected REST :attr:`client` plus the :meth:`ask` poll config as
    state, and exposes the wiki behaviors — list projects, read structure/page,
    and ask a cited question — as methods. The ``ask`` await is bounded
    (kick-off-then-poll, the same shape as :meth:`SearchTools.search`).
    """

    client: RestClient
    # How long ``ask`` polls before returning a partial "still running" answer
    # rather than hanging. Kept strictly under :data:`PROXY_CEILING_S` (#41) so
    # the tool always returns the resumable ``answer_id`` before any transport/
    # proxy timeout can strand the caller.
    timeout_s: float = 25.0
    poll_interval_s: float = 1.5
    # Terminal QA snapshot statuses (the snapshot's ``status`` starts "running").
    TERMINAL_QA: ClassVar[frozenset[str]] = frozenset({"complete", "cancelled", "error"})

    # -- behaviors --------------------------------------------------------

    @staticmethod
    def _data(el: dict[str, Any]) -> dict[str, Any]:
        """Cytoscape element payload — fields live under ``data`` (API wire shape).

        Falls back to the element itself so a flat/legacy node still resolves.
        """
        inner = el.get("data")
        return inner if isinstance(inner, dict) else el

    async def list_projects(self) -> list[Any]:
        """List indexed wiki projects. Wires ``GET /v1/wiki/projects``."""
        result = await self.client.get("/v1/wiki/projects")
        return result if isinstance(result, list) else []

    async def read_structure(
        self,
        *,
        project: str,
        detail: str = "stats",
        layer: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return a wiki project's structure at a chosen detail tier.

        Wires ``GET /v1/wiki/projects/<slug>/graph``. The full multiplex graph
        can be hundreds of KB, so the DEFAULT tier is the compact ``stats`` block
        — a caller opts into more:
        - ``stats`` (default) — counts only (node/edge/kind/per-layer).
        - ``nodes`` — ``stats`` + the node list (optionally one ``layer``, capped
          by ``limit``).
        - ``full`` — ``nodes`` + the edges among the returned nodes.
        """
        if detail not in {"stats", "nodes", "full"}:
            raise ValueError(f"Invalid detail '{detail}'. Use stats|nodes|full.")
        graph = _as_dict(await self.client.get(f"/v1/wiki/projects/{project}/graph"))
        return self._project_structure(graph, detail, layer, limit)

    @classmethod
    def _project_structure(
        cls, graph: dict[str, Any], detail: str, layer: str | None, limit: int | None
    ) -> dict[str, Any]:
        """Project a raw KG into a ``stats`` / ``nodes`` / ``full`` tier."""
        nodes = [n for n in _as_list(graph.get("nodes")) if isinstance(n, dict)]
        edges = [e for e in _as_list(graph.get("edges")) if isinstance(e, dict)]
        stats = graph.get("stats")
        out: dict[str, Any] = {
            "project": graph.get("project") or graph.get("slug"),
            "stats": stats if isinstance(stats, dict) else cls._derive_stats(nodes, edges),
        }
        if detail == "stats":
            return out
        if layer:
            # Nodes are Cytoscape-shaped — ``layer`` lives under ``data`` (#63).
            nodes = [n for n in nodes if cls._data(n).get("layer") == layer]
        if isinstance(limit, int) and limit > 0:
            nodes = nodes[:limit]
        out["nodes"] = nodes
        out["node_count"] = len(nodes)
        if detail == "full":
            keep = {cls._data(n).get("id") for n in nodes}
            out["edges"] = [
                e
                for e in edges
                if cls._data(e).get("source", cls._data(e).get("from")) in keep
                or cls._data(e).get("target", cls._data(e).get("to")) in keep
            ]
            out["edge_count"] = len(out["edges"])
        return out

    @classmethod
    def _derive_stats(
        cls, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Compact counts when the graph carries no ``stats`` block."""
        per_layer: dict[str, int] = {}
        for node in nodes:
            key = str(cls._data(node).get("layer") or "unknown")
            per_layer[key] = per_layer.get(key, 0) + 1
        return {"nodeCount": len(nodes), "edgeCount": len(edges), "perLayer": per_layer}

    async def read_page(self, *, project: str, page_id: str) -> dict[str, Any]:
        """Return a single wiki page (markdown body, nav, toc).

        Wires ``GET /v1/wiki/projects/<slug>/pages/<page_id>``.
        """
        return _as_dict(await self.client.get(f"/v1/wiki/projects/{project}/pages/{page_id}"))

    async def submit_insight(
        self,
        *,
        project: str,
        insight: str,
        anchors: list[str] | None = None,
        labels: list[str] | None = None,
        kind: str = "propositional",
        condense: bool = True,
    ) -> dict[str, Any]:
        """Suggest a memory insight for a wiki project's multiplex graph.

        Wires ``POST /v1/wiki/projects/<slug>/insights``. The server validates
        the suggestion, condenses free text into atomic claims, auto-anchors
        each to the tree-sitter code graph, dedups against existing notes, and
        safely merges. With ``condense=True`` (default) ``insight`` is treated
        as raw text and decomposed into one or more atomic notes; with
        ``condense=False`` it is stored verbatim as a single ≤200-char claim.

        Returns the per-claim ingest result
        ``{ok, claims: [{action, node_id, content, anchors, warnings}]}`` —
        ``action`` is ``created`` / ``merged`` / ``linked`` / ``rejected``.
        """
        body: dict[str, Any] = {
            ("raw" if condense else "content"): insight,
            "condense": condense,
            "kind": kind,
        }
        if anchors:
            body["anchors"] = anchors
        if labels:
            body["labels"] = labels
        return _as_dict(
            await self.client.post(f"/v1/wiki/projects/{project}/insights", json=body)
        )

    async def ask(
        self, *, project: str, question: str, model: str | None = None
    ) -> dict[str, Any]:
        """Ask the wiki a question and return the rendered answer once settled.

        ``POST /v1/wiki/qa`` is SSE-only: we stream it only until the first
        ``meta`` event (emitted synchronously at start) to recover the
        ``answerId``, then bounded-poll ``GET /v1/wiki/qa/<id>`` until the
        snapshot is terminal — its ``status`` reaches a terminal value, or
        (defensively, for snapshots without one) a ``sources`` accept-block is
        present — or :attr:`timeout_s` elapses. A timeout returns the partial
        with ``status: "running"`` AND a real ``answer_id``; call
        :meth:`get_answer` (the ``get_wiki_answer`` tool) to resume it.

        ``model`` is optional — when omitted the body carries no ``model`` and the
        server defaults it (an empty string is never sent).
        """
        body: dict[str, Any] = {"slug": project, "question": question}
        if model:
            body["model"] = model

        answer_id = await self._start_qa(body)
        if not answer_id:
            raise RestError("Wiki QA did not emit a meta event with an answer id.")

        # ``answer_id`` is captured BEFORE polling, so a transport/proxy timeout
        # mid-poll degrades to the resumable handle (#41), never strands.
        return await poll_or_handle(
            answer_id,
            lambda: self._fetch_answer(answer_id),
            self._is_answer_terminal,
            lambda s, t: self._shape_answer(answer_id, s, t),
            timeout_s=self.timeout_s,
            interval_s=self.poll_interval_s,
        )

    async def get_answer(self, *, answer_id: str, detail: str = "answer") -> dict[str, Any]:
        """Fetch a wiki answer by id — resume a timed-out :meth:`ask`, or replay.

        Wires ``GET /v1/wiki/qa/<answer_id>``; the companion that consumes the
        ``answer_id`` :meth:`ask` returns, mirroring ``get_search_run``. ``detail``
        ``full`` also returns the raw ``blocks`` + ``models_used``.
        """
        snapshot = await self._fetch_answer(answer_id)
        out = self._shape_answer(answer_id, snapshot, self._is_answer_terminal(snapshot))
        if detail == "full":
            out["blocks"] = snapshot.get("blocks")
            out["models_used"] = snapshot.get("modelsUsed") or snapshot.get("models_used")
        return out

    async def _fetch_answer(self, answer_id: str) -> dict[str, Any]:
        """Return the QA answer snapshot dict for ``answer_id``."""
        return _as_dict(await self.client.get(f"/v1/wiki/qa/{answer_id}"))

    @classmethod
    def _is_answer_terminal(cls, snapshot: dict[str, Any]) -> bool:
        """True once the snapshot has settled.

        The snapshot ``status`` is AUTHORITATIVE when present — a ``running``
        snapshot is never terminal even if a (premature) ``sources`` block is
        already there (the exact truncation bug). Only when ``status`` is absent
        (an older snapshot) do we fall back to the terminal ``sources`` accept-block.
        """
        status = snapshot.get("status")
        if isinstance(status, str) and status:
            return status in cls.TERMINAL_QA
        return any(
            isinstance(b, dict) and b.get("kind") == "sources"
            for b in _as_list(snapshot.get("blocks"))
        )

    @classmethod
    def _shape_answer(
        cls, answer_id: str, snapshot: dict[str, Any], terminal: bool
    ) -> dict[str, Any]:
        """Render a QA snapshot into the MCP answer shape (answer + citations)."""
        status = snapshot.get("status")
        if not (isinstance(status, str) and status):
            status = "complete" if terminal else "running"
        answer, citations = cls._render_qa_blocks(snapshot.get("blocks"))
        return {
            "answer_id": answer_id,
            "answer": answer,
            "citations": citations,
            "status": status,
        }

    # -- internals: behavior over the atomic state ------------------------

    async def _start_qa(self, body: dict[str, Any]) -> str | None:
        """Stream the QA POST, returning the ``answerId`` from the first meta event.

        The stream is SSE: a ``_SSE_PRIMER`` comment frame, then ``id:``/
        ``event:``/``data:`` lines per frame. We read line-by-line until we parse
        a ``meta`` frame's ``answerId``, then stop iterating (closing the stream).
        """
        import json

        current_event: str | None = None
        async for line in self.client.stream_lines("POST", "/v1/wiki/qa", json=body):
            line = line.strip()
            if not line:
                # A blank line terminates the current SSE frame — reset event
                # state so the next frame's ``event:`` is not conflated with this.
                current_event = None
                continue
            if line.startswith(":"):
                # Comment (``_SSE_PRIMER``/heartbeat) — skip without resetting state.
                continue
            if line.startswith("event:"):
                current_event = line[len("event:") :].strip()
                continue
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                # The meta frame carries ``answerId``; accept it whether or not
                # the ``event:`` line preceded it (robust to frame ordering).
                if current_event in (None, "meta") or "answerId" in obj:
                    answer_id = obj.get("answerId") or obj.get("answer_id") or obj.get("id")
                    if isinstance(answer_id, str) and answer_id:
                        return answer_id
        return None

    @classmethod
    def _render_qa_blocks(cls, blocks: Any) -> tuple[str, list[str]]:
        """Flatten QA answer ``blocks`` into ``(answer_text, citations)``.

        The QA snapshot stores the answer as a list of typed blocks (``p``,
        ``h2``, ``ul``, ``sources`` …) rather than a single string. We join the
        text-bearing blocks into a markdown-ish answer and collect every
        ``sources`` block's items as citations.
        """
        if not isinstance(blocks, list):
            return "", []
        lines: list[str] = []
        citations: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("kind")
            if kind == "sources":
                items = block.get("items")
                if isinstance(items, list):
                    citations.extend(str(i) for i in items)
                continue
            text = cls._block_text(block)
            if text:
                lines.append(text)
        return "\n\n".join(lines), citations

    @classmethod
    def _block_text(cls, block: dict[str, Any]) -> str:
        """Best-effort Markdown rendering for a single answer block.

        All real block kinds emit renderable text; unknown kinds return ``""``
        (silent no-op, forward-compatible with schema additions).

        Block shapes (reference: ``mewbo_graph.wiki.types``):
        - ``p/h2/h3`` — ``{text: InlineNode}``
        - ``ul`` — ``{items: list[InlineNode]}``
        - ``accordion`` — ``{title: str, items: list[str]}``
        - ``table`` — ``{head: list[str], rows: list[list[InlineNode]]}``
        - ``diagram`` — ``{id: str}``
        - ``hr`` — (no extra fields)
        """
        kind = block.get("kind")
        if kind in ("p", "h2", "h3"):
            return cls._inline_text(block.get("text"))
        if kind == "ul":
            items = block.get("items")
            if isinstance(items, list):
                return "\n".join(f"- {cls._inline_text(i)}" for i in items)
        if kind == "accordion":
            items = block.get("items")
            body = "\n".join(str(i) for i in items) if isinstance(items, list) else ""
            return f"{block.get('title', '')}\n{body}".strip()
        if kind == "table":
            return cls._render_table(block)
        if kind == "diagram":
            diagram_id = block.get("id", "")
            return f"```mermaid\n%% diagram {diagram_id}\n```"
        if kind == "hr":
            return "---"
        return ""

    @classmethod
    def _render_table(cls, block: dict[str, Any]) -> str:
        """Render a ``table`` block as a Markdown table.

        ``head`` is a list of plain-string column headers; ``rows`` is a list of
        rows, each being a list of ``InlineNode`` values (coerced via
        :meth:`_inline_text`).
        """
        head = block.get("head")
        rows = block.get("rows")
        if not isinstance(head, list) or not isinstance(rows, list):
            return ""
        header = "| " + " | ".join(str(h) for h in head) + " |"
        separator = "| " + " | ".join("---" for _ in head) + " |"
        data_rows = [
            "| " + " | ".join(cls._inline_text(cell) for cell in row) + " |"
            for row in rows
            if isinstance(row, list)
        ]
        return "\n".join([header, separator] + data_rows)

    @classmethod
    def _inline_text(cls, node: Any) -> str:
        """Coerce an inline node (string, or rich-text dict/list) to plain text.

        Handles all ``InlineNode`` shapes from ``mewbo_graph.wiki.types``:
        - ``str`` → returned as-is
        - ``list[InlineNode]`` → concatenated
        - ``{"text": InlineNode}`` → recurse on ``text`` (used by ``p`` / link)
        - ``{"code": str}`` → `` `code` `` span
        - ``{"link": str, "text": str}`` → ``[text](link)`` Markdown link
        - ``{"kind": "src", "path": str, "lines"?: str}`` → ``path`` or ``path:lines``
        """
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(cls._inline_text(n) for n in node)
        if isinstance(node, dict):
            # Inline code span: {"code": "..."}
            if "code" in node and isinstance(node["code"], str):
                return f"`{node['code']}`"
            # Hyperlink: {"link": "...", "text": "..."}
            if "link" in node and "text" in node:
                return f"[{cls._inline_text(node['text'])}]({node['link']})"
            # Source reference: {"kind": "src", "path": "...", "lines"?: "..."}
            if node.get("kind") == "src" and isinstance(node.get("path"), str):
                path = node["path"]
                lines = node.get("lines")
                return f"{path}:{lines}" if isinstance(lines, str) and lines else path
            # Generic rich inline node: carries visible text under "text".
            if isinstance(node.get("text"), (str, list)):
                return cls._inline_text(node["text"])
        return ""


# ---------------------------------------------------------------------------
# D. Integrations / capability discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntegrationTools:
    """The capability-discovery tool surface — one atomic unit.

    Holds the injected REST :attr:`client` and exposes integration discovery so
    a caller learns what tool ids it can switch on via ``integrations`` in
    :meth:`SessionTools.create`.
    """

    client: RestClient

    async def discover(
        self,
        *,
        project: str | None = None,
        kind: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Return available tools + installed plugins, compact, for discovery.

        Wires ``GET /api/tools`` (optionally scoped to ``project``) +
        ``GET /api/plugins``. Each tool is projected to ``{tool_id, name, kind?,
        enabled?}`` — dropping the auto-generated ``"MCP tool X from Y"``
        boilerplate description that carries zero signal — and each plugin to
        ``{name, version?, enabled?}`` (dropping the long marketplace blurb).
        ``kind``/``enabled`` filter the tool list.
        """
        params = {"project": project} if project else None
        tools_payload = await self.client.get("/api/tools", params=params)
        plugins_payload = await self.client.get("/api/plugins")
        raw_tools = tools_payload.get("tools") if isinstance(tools_payload, dict) else tools_payload
        raw_plugins = (
            plugins_payload.get("plugins")
            if isinstance(plugins_payload, dict)
            else plugins_payload
        )
        tools: list[dict[str, Any]] = []
        for t in _as_list(raw_tools):
            if not isinstance(t, dict):
                continue
            shaped = self._shape_tool(t)
            if self._keep_tool(shaped, kind, enabled):
                tools.append(shaped)
        plugins = [self._shape_plugin(p) for p in _as_list(raw_plugins) if isinstance(p, dict)]
        return {"tools": tools, "tool_count": len(tools), "plugins": plugins}

    @staticmethod
    def _shape_tool(t: dict[str, Any]) -> dict[str, Any]:
        """Compact tool row — the only fields a caller needs to pick ``integrations``."""
        tid = t.get("tool_id") or t.get("id") or t.get("name")
        out: dict[str, Any] = {"tool_id": tid, "name": t.get("name") or tid}
        if t.get("kind") is not None:
            out["kind"] = t.get("kind")
        if t.get("enabled") is not None:
            out["enabled"] = bool(t.get("enabled"))
        return out

    @staticmethod
    def _keep_tool(t: dict[str, Any], kind: str | None, enabled: bool | None) -> bool:
        """Apply the optional ``kind`` / ``enabled`` filters."""
        if kind and t.get("kind") != kind:
            return False
        if enabled is not None and bool(t.get("enabled", True)) != enabled:
            return False
        return True

    @staticmethod
    def _shape_plugin(p: dict[str, Any]) -> dict[str, Any]:
        """Compact plugin row (drop the long marketplace description)."""
        out: dict[str, Any] = {"name": p.get("name")}
        for key in ("version", "enabled"):
            if p.get(key) is not None:
                out[key] = p.get(key)
        return out


# ---------------------------------------------------------------------------
# E. Agentic Search ("Mewbo Search")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchTools:
    """The Mewbo Search tool surface over the REST API — one atomic unit.

    Holds the injected REST :attr:`client` plus the await/projection config as
    state, and exposes the whole feature's three behaviors — discover
    workspaces, run a search, fetch a prior run — as methods over that state.
    Construct it per call with an authenticated client (dependency injection);
    like every tool here it never touches FastMCP, so tests stub only the HTTP
    boundary.

    The ``detail`` tier a caller passes controls its own context spend:
    ``"answer"`` (default, cheapest) returns the cited synthesis + a compact
    result index (``id/source/kind/title/url/relevance``) so citations resolve;
    ``"full"`` adds each result's ``snippet/insight/refs``. The per-source trace
    and decorative fields (related people, images, embeds) are always dropped —
    console-render signal, not search signal for a consuming agent.
    """

    client: RestClient
    # Bounded await for an async run (see :meth:`search`). Held strictly under
    # :data:`PROXY_CEILING_S` (#41) so a slow run returns the resumable ``run_id``
    # as ``status:"running"`` before any transport/proxy timeout strands it.
    timeout_s: float = 25.0
    poll_interval_s: float = 1.5

    # Stateless shared config. ``TERMINAL_STATUSES`` mirrors the contract's
    # ``agentic_search.schemas.TERMINAL_RUN_STATUSES`` — duplicated, never
    # imported: this process talks to the API over REST only and never imports
    # ``mewbo_api`` (the process + dependency boundary). If that set changes,
    # update this mirror.
    BASE: ClassVar[str] = "/api/agentic_search"
    DETAILS: ClassVar[frozenset[str]] = frozenset({"answer", "full"})
    TERMINAL_STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"completed", "failed", "cancelled"}
    )

    # -- behaviors --------------------------------------------------------

    async def list_workspaces(self) -> dict[str, Any]:
        """List saved workspaces, compact for discovery.

        Wires ``GET {BASE}/workspaces``. A workspace is a saved set of sources a
        search fans out across. Each is slimmed to ``{id, name, desc, sources,
        recent_query_count}`` — dropping the console-only ``instructions``
        (untrusted prompt input) and the full history — so the listing stays
        cheap. Pass an ``id`` or ``name`` from here to :meth:`search`.
        """
        payload = _as_dict(await self.client.get(f"{self.BASE}/workspaces"))
        return {"workspaces": [self._shape_workspace(w) for w in _dict_list(payload, "workspaces")]}

    async def search(
        self,
        *,
        query: str,
        workspace: str,
        project: str | None = None,
        detail: str = "answer",
    ) -> dict[str, Any]:
        """Run a search across a workspace and return the cited answer.

        Resolves *workspace* (id or name) → ``POST {BASE}/runs`` → awaits a
        terminal run. The default runner completes synchronously so the POST is
        already terminal; the real async runner returns ``running`` immediately,
        so we poll the snapshot until terminal or :attr:`timeout_s` (then return
        the partial with ``status: "running"`` — call :meth:`get_run` later for
        the rest). Mirrors the ``ask`` kick-off-then-bounded-poll pattern.
        """
        self._check_detail(detail)
        ws = await self._resolve_workspace(workspace)
        body: dict[str, Any] = {"workspace_id": ws.get("id"), "query": query}
        if project:
            body["project"] = project

        created = _as_dict(await self.client.post(f"{self.BASE}/runs", json=body))
        payload = _as_dict(created.get("run"))
        status = str(created.get("status") or payload.get("status") or "running")
        run_id = str(created.get("run_id") or payload.get("run_id") or "")
        ws_name = ws.get("name")

        # Fast path: the start response is already terminal — shape immediately.
        if status in self.TERMINAL_STATUSES or not run_id:
            return self._shape_run(payload, status, ws_name, detail)

        # ``run_id`` captured below so even an EMPTY snapshot (transport timeout
        # degraded to the handle, #41) still surfaces the resumable id + running.
        def _shape(record: dict[str, Any], terminal: bool) -> dict[str, Any]:
            rec = _as_dict(record)
            rec_status = str(rec.get("status") or "running")
            payload = _as_dict(rec.get("payload"))
            payload.setdefault("run_id", run_id)
            return self._shape_run(payload, rec_status, ws_name, detail)

        return await poll_or_handle(
            run_id,
            lambda: self._load_record(run_id),
            lambda rec: str(_as_dict(rec).get("status") or "running") in self.TERMINAL_STATUSES,
            _shape,
            timeout_s=self.timeout_s,
            interval_s=self.poll_interval_s,
        )

    async def get_run(self, *, run_id: str, detail: str = "answer") -> dict[str, Any]:
        """Fetch a prior run's snapshot (replay / deep-link; no re-run).

        Wires ``GET {BASE}/runs/<run_id>``. Same shaping + ``detail`` tiers as
        :meth:`search`. Use it to re-read a run started earlier (e.g. after
        context compaction) or to check on an async run that returned
        ``status: "running"``.
        """
        self._check_detail(detail)
        record = await self._load_record(run_id)
        status = str(record.get("status") or "running")
        return self._shape_run(_as_dict(record.get("payload")), status, None, detail)

    # -- internals: behavior over the atomic state ------------------------

    def _check_detail(self, detail: str) -> None:
        """Reject an unknown detail tier before any REST call."""
        if detail not in self.DETAILS:
            raise ValueError(f"Invalid detail '{detail}'. Use answer|full.")

    async def _load_record(self, run_id: str) -> dict[str, Any]:
        """Return a run's durable ``RunRecord`` dict (``{status, payload, …}``)."""
        snapshot = _as_dict(await self.client.get(f"{self.BASE}/runs/{run_id}"))
        return _as_dict(snapshot.get("run"))

    async def _resolve_workspace(self, ref: str) -> dict[str, Any]:
        """Resolve *ref* (a workspace id OR case-insensitive name) to a workspace.

        Agents think in names; this matches an exact id first, then a unique
        case-insensitive name. Raises :class:`ValueError` (with the candidates)
        on no match or an ambiguous name, rather than searching the wrong one.
        """
        workspaces = _dict_list(
            _as_dict(await self.client.get(f"{self.BASE}/workspaces")), "workspaces"
        )
        for ws in workspaces:
            if ws.get("id") == ref:
                return ws
        matches = [w for w in workspaces if str(w.get("name", "")).lower() == ref.lower()]
        if len(matches) == 1:
            return matches[0]
        names = [str(w.get("name")) for w in workspaces]
        if not matches:
            raise ValueError(f"No workspace matches '{ref}'. Available: {names or 'none'}.")
        raise ValueError(f"Workspace name '{ref}' is ambiguous — use its id. Names: {names}.")

    @staticmethod
    def _shape_workspace(ws: dict[str, Any]) -> dict[str, Any]:
        """Compact projection of a Workspace (no instructions / history)."""
        return {
            "id": ws.get("id"),
            "name": ws.get("name"),
            "desc": ws.get("desc", ""),
            "sources": _as_list(ws.get("sources")),
            "recent_query_count": len(_as_list(ws.get("past_queries"))),
        }

    @classmethod
    def _shape_run(
        cls,
        payload: dict[str, Any],
        status: str,
        workspace_name: str | None,
        detail: str,
    ) -> dict[str, Any]:
        """Project a ``RunPayload`` dict into the compact MCP search result.

        Always the cited synthesis + a result list (compact or content-rich per
        *detail*) + related questions; never the per-source trace or decorative
        fields (console rendering concern, not search signal).
        """
        answer = _as_dict(payload.get("answer"))
        results = [
            cls._shape_result(r, detail)
            for r in _as_list(payload.get("results"))
            if isinstance(r, dict)
        ]
        out: dict[str, Any] = {
            "run_id": payload.get("run_id"),
            "session_id": payload.get("session_id"),
            "workspace_id": payload.get("workspace_id"),
            "query": payload.get("query"),
            "status": status,
            "total_ms": payload.get("total_ms", 0),
            "answer": {
                "tldr": answer.get("tldr", ""),
                "bullets": [
                    {"text": b.get("text", ""), "cites": _as_list(b.get("cites"))}
                    for b in _as_list(answer.get("bullets"))
                    if isinstance(b, dict)
                ],
                "confidence": answer.get("confidence", 0.0),
                "sources_count": answer.get("sources_count", 0),
            },
            "results": results,
            "result_count": len(results),
            "related_questions": _as_list(payload.get("related_questions")),
        }
        if payload.get("error"):
            out["error"] = payload.get("error")
        if workspace_name is not None:
            out["workspace_name"] = workspace_name
        return out

    @staticmethod
    def _shape_result(r: dict[str, Any], detail: str) -> dict[str, Any]:
        """Compact (``answer``) or content-rich (``full``) projection of a result."""
        out: dict[str, Any] = {
            "id": r.get("id"),
            "source": r.get("source"),
            "kind": r.get("kind"),
            "title": r.get("title"),
            "url": r.get("url", ""),
            "relevance": r.get("relevance", 0.0),
        }
        if detail == "full":
            out["snippet"] = r.get("snippet", "")
            out["author"] = r.get("author", "")
            out["timestamp"] = r.get("timestamp", "")
            insight = r.get("insight")
            if isinstance(insight, dict):
                out["insight"] = {
                    "label": insight.get("label", ""),
                    "body": insight.get("body", ""),
                }
            refs = [
                {"title": ref.get("title"), "url": ref.get("url"), "kind": ref.get("kind", "doc")}
                for ref in _dict_list(r, "refs")
            ]
            if refs:
                out["refs"] = refs
        return out


# ---------------------------------------------------------------------------
# F. Structured query — schema-constrained, tool-using synthesis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StructuredQueryTools:
    """Schema-constrained structured-response surface over the REST API.

    Holds the injected :attr:`client` and the bounded-await config. Wires the
    async ``POST /v1/structured`` + ``GET /v1/structured/<run_id>`` run-handle:
    the server runs an agentic session that may call grounding tools, then emits
    a JSON-Schema-validated object — and we await it the same kick-off-then-poll
    way as ``search`` (with a ``get_structured_run`` companion).
    """

    client: RestClient
    # Held strictly under :data:`PROXY_CEILING_S` (#41) so a slow run returns the
    # resumable ``run_id`` as ``status:"running"`` before any transport/proxy
    # timeout strands the caller.
    timeout_s: float = 25.0
    poll_interval_s: float = 2.0
    TERMINAL: ClassVar[frozenset[str]] = frozenset({"completed", "failed", "cancelled"})

    async def query(
        self,
        *,
        query: str,
        schema: dict[str, Any],
        workspace: str | None = None,
        tools: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a schema-constrained synthesis and await the validated object.

        Wires ``POST /v1/structured`` (async run-handle). If the run settles fast
        the object comes straight back; otherwise we bounded-poll
        ``GET /v1/structured/<run_id>`` until terminal or :attr:`timeout_s`,
        returning ``status: "running"`` + the ``run_id`` so the caller can resume
        via :meth:`get_run` (``get_structured_run``). Mirrors ``search`` +
        ``get_search_run``.
        """
        body: dict[str, Any] = {"query": query, "schema": schema}
        if workspace:
            body["workspace"] = workspace
        if tools:
            body["tools"] = tools
        created = _as_dict(await self.client.post("/v1/structured", json=body))
        run_id = str(created.get("run_id") or "")
        # Fast path: the start response already settled — shape immediately.
        if self._is_settled(created) or not run_id:
            return self._shape(created, run_id)
        # ``run_id`` captured before polling, so a transport/proxy timeout
        # mid-poll degrades to the resumable handle (#41), never strands.
        return await poll_or_handle(
            run_id,
            lambda: self._fetch_run(run_id),
            self._is_settled,
            lambda s, t: self._shape(s, run_id),
            timeout_s=self.timeout_s,
            interval_s=self.poll_interval_s,
        )

    async def get_run(self, *, run_id: str) -> dict[str, Any]:
        """Fetch a structured run by id — resume a ``running`` query, or replay.

        Wires ``GET /v1/structured/<run_id>``.
        """
        return self._shape(await self._fetch_run(run_id), run_id)

    async def _fetch_run(self, run_id: str) -> dict[str, Any]:
        """Return the structured run snapshot dict for ``run_id``."""
        return _as_dict(await self.client.get(f"/v1/structured/{run_id}"))

    @classmethod
    def _is_settled(cls, snapshot: Any) -> bool:
        """Terminal when the object is present, an error is set, or status terminal."""
        d = _as_dict(snapshot)
        if d.get("output") is not None or d.get("error") is not None:
            return True
        return str(d.get("status") or "running") in cls.TERMINAL

    @staticmethod
    def _shape(snapshot: Any, run_id: str) -> dict[str, Any]:
        """Project a structured run snapshot into the MCP result."""
        d = _as_dict(snapshot)
        status = str(
            d.get("status") or ("completed" if d.get("output") is not None else "running")
        )
        out: dict[str, Any] = {"run_id": d.get("run_id") or run_id or None, "status": status}
        for key in ("output", "workspace", "error"):
            if d.get(key) is not None:
                out[key] = d.get(key)
        return out


# ---------------------------------------------------------------------------
# G. Projects — discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectTools:
    """Project-discovery surface — one atomic unit.

    Wraps ``GET /api/projects`` so a caller can discover the registered project
    names + git identities to pass to :meth:`SessionTools.create` (closing the
    "undiscoverable project ids" gap).
    """

    client: RestClient

    async def list_projects(self) -> dict[str, Any]:
        """List registered projects (config + managed worktrees), compact.

        Each row carries the resolvable identifiers — ``name`` and, for managed
        worktrees, ``project_id`` — plus the canonical git ``repo: {host, owner,
        name}`` and ``aliases`` so ANY of them resolves in ``create_session``.
        """
        payload = await self.client.get("/api/projects")
        raw = payload.get("projects") if isinstance(payload, dict) else payload
        return {"projects": [self._shape(p) for p in _as_list(raw) if isinstance(p, dict)]}

    @staticmethod
    def _shape(p: dict[str, Any]) -> dict[str, Any]:
        """Keep only the discovery-relevant, resolvable fields of a project row."""
        out: dict[str, Any] = {"name": p.get("name"), "source": p.get("source")}
        for key in (
            "project_id",
            "repo",
            "aliases",
            "is_worktree",
            "branch",
            "available",
            "parent_project_id",
        ):
            if p.get(key) is not None:
                out[key] = p.get(key)
        return out


__all__ = [
    "IntegrationTools",
    "ProjectTools",
    "SearchTools",
    "SessionTools",
    "StructuredQueryTools",
    "WikiTools",
]
