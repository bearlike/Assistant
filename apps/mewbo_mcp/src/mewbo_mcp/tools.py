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
    LEVELS: ClassVar[frozenset[str]] = frozenset({"overview", "turns", "steps", "full"})

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
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a session, optionally provisioning a worktree, then run *prompt*.

        Default behavior auto-provisions a fresh worktree+branch off the base in
        the target project (``repo``/``project``). When ``branch`` or
        ``worktree`` is supplied, the existing one is targeted instead.
        ``integrations`` maps to the session's ``context.mcp_tools`` allowlist.

        ``tags``: the API supports a SINGLE ``session_tag`` per session, so at
        most one tag may be given. Passing more than one raises ``ValueError``
        rather than silently dropping the rest.

        Wires: ``POST /api/v_projects/<id>/worktrees`` (create-from-base) →
        ``POST /api/sessions`` → ``POST /api/sessions/<id>/query``.
        """
        if tags and len(tags) > 1:
            raise ValueError(
                "create accepts at most one tag (the API stores a single "
                f"session_tag); got {len(tags)}."
            )
        target_project = repo or project
        project_ref: str | None = None

        if target_project:
            if worktree:
                # Caller pinned an existing managed worktree by its project id.
                project_ref = f"managed:{worktree}"
            elif branch:
                # Target an existing branch — create (idempotent) a worktree for it.
                wt = await self._provision_worktree(target_project, branch, base=None)
                project_ref = f"managed:{wt['project_id']}"
            else:
                # Default: fresh worktree+branch off the repo's current base.
                base = await self._current_branch(target_project)
                new_branch = self._auto_branch_name(title or prompt)
                wt = await self._provision_worktree(target_project, new_branch, base=base)
                project_ref = f"managed:{wt['project_id']}"

        context: dict[str, Any] = {}
        if integrations:
            context["mcp_tools"] = list(integrations)

        session_body: dict[str, Any] = {}
        if project_ref:
            session_body["project"] = project_ref
        if context:
            session_body["context"] = context
        # ``POST /api/sessions`` accepts a single ``session_tag`` (>1 rejected above).
        if tags:
            session_body["session_tag"] = tags[0]

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
        """Send a steering message to a running session.

        Wires ``POST /api/sessions/<id>/message``.
        """
        result = await self.client.post(
            f"/api/sessions/{session_id}/message", json={"text": message}
        )
        enqueued = bool(_as_dict(result).get("enqueued"))
        return {"session_id": session_id, "status": "enqueued" if enqueued else "unknown"}

    async def interrupt(self, *, session_id: str) -> dict[str, Any]:
        """Interrupt the current step of a running session.

        Wires ``POST /api/sessions/<id>/interrupt``.
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
    ) -> dict[str, Any]:
        """List sessions, applying client-side filters the API does not.

        Wires ``GET /api/sessions``. The API returns summaries with ``status``,
        ``created_at``, and ``context``; we filter by ``project``/``status``/
        ``since`` locally. (No tag filter — the list endpoint does not return
        per-session tags.)

        ``project`` matches the *underlying repo name*. This is intentionally
        robust to worktree sessions: when :meth:`create` auto-provisions a
        worktree, the API stores ``context.project`` as ``managed:<worktree_id>``
        and ``context.repo`` as the parent repo name. So a session matches when
        ``project`` equals ``context.repo`` OR ``context.project`` (the latter
        for plain config-project sessions, with any ``managed:`` prefix stripped).
        """
        sessions = _dict_list(await self.client.get("/api/sessions"), "sessions")

        def _project_matches(ctx: dict[str, Any]) -> bool:
            # ``repo`` is the parent repo name for worktree sessions; ``project``
            # is the raw ref (a plain name for config projects, ``managed:<id>``
            # for managed/worktree ones). Match either, stripping the prefix.
            repo = str(ctx.get("repo") or "")
            proj = str(ctx.get("project") or "")
            proj_bare = proj[len("managed:") :] if proj.startswith("managed:") else proj
            return project in {repo, proj, proj_bare}

        def _keep(s: dict[str, Any]) -> bool:
            if status and str(s.get("status")) != status:
                return False
            if since and str(s.get("created_at") or "") < since:
                return False
            if project:
                ctx = s.get("context")
                if not isinstance(ctx, dict) or not _project_matches(ctx):
                    return False
            return True

        return {"sessions": [s for s in sessions if _keep(s)]}

    async def history(
        self, *, session_id: str, level: str, turn: int | None = None
    ) -> dict[str, Any]:
        """Tiered read of a session's history.

        ``level`` is one of ``overview`` | ``turns`` | ``steps`` | ``full``. The
        ``steps`` and ``full`` tiers require ``turn`` (1-based). The consuming
        agent picks the tier so it controls its own context spend.
        """
        if level not in self.LEVELS:
            raise ValueError(f"Invalid level '{level}'. Use overview|turns|steps|full.")

        turns, running = await self._load_turns(session_id)

        if level == "overview":
            return self._overview(session_id, turns, running)
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
        # full
        agents = await self._safe_agent_tree(session_id)
        return {
            "session_id": session_id,
            "turn": selected.index,
            "user_text": selected.user_text,
            "assistant_text": selected.assistant_text,
            "done_reason": selected.done_reason,
            "steps": [self._step_full(e) for e in selected.steps],
            "agents": agents,
        }

    async def agent_tree(self, *, session_id: str) -> dict[str, Any]:
        """Return the sub-agent tree with lifecycle state.

        Wires ``GET /api/sessions/<id>/agents``.
        """
        return _as_dict(await self.client.get(f"/api/sessions/{session_id}/agents"))

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

    async def _load_turns(self, session_id: str) -> tuple[list[Turn], bool]:
        """Fetch a session's events and reconstruct turns. Returns (turns, running)."""
        payload = _as_dict(await self.client.get(f"/api/sessions/{session_id}/events"))
        events = _dict_list(payload, "events")
        return build_timeline(events), bool(payload.get("running"))

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

    @staticmethod
    def _overview(session_id: str, turns: list[Turn], running: bool) -> dict[str, Any]:
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
        # Title is derived from the FIRST user message so it stays stable as the
        # conversation grows; the summary tracks the latest assistant reply.
        title = first.user_text[:120] if first else ""
        return {
            "session_id": session_id,
            "title": title,
            "summary": last.assistant_text if last else "",
            "status": "running" if running else (last.done_reason if last else None),
            "running": running,
            "turn_count": len(turns),
            "step_count": total_steps,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
        }

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
            "assistant_text": cls._truncate(turn.assistant_text),
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
        """Full per-step log: inputs, result, and error for the ``full`` tier."""
        payload = cls._event_payload(event)
        return {
            "tool_id": payload.get("tool_id"),
            "operation": payload.get("operation"),
            "tool_input": payload.get("tool_input"),
            "result": payload.get("result"),
            "success": payload.get("success"),
            "summary": payload.get("summary"),
            "error": payload.get("error"),
            "agent_id": payload.get("agent_id"),
            "model": payload.get("model"),
            "ts": event.get("ts"),
        }


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
    # rather than hanging (bounded per the design spec, §8 risks).
    timeout_s: float = 90.0
    poll_interval_s: float = 1.5

    # -- behaviors --------------------------------------------------------

    async def list_projects(self) -> list[Any]:
        """List indexed wiki projects. Wires ``GET /v1/wiki/projects``."""
        result = await self.client.get("/v1/wiki/projects")
        return result if isinstance(result, list) else []

    async def read_structure(self, *, project: str) -> dict[str, Any]:
        """Return a wiki project's page list and knowledge graph.

        Wires ``GET /v1/wiki/projects/<slug>/graph`` (nodes/edges) — the graph's
        nodes double as the project's structure for navigation.
        """
        return _as_dict(await self.client.get(f"/v1/wiki/projects/{project}/graph"))

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
        """Ask the wiki a question and return the rendered answer once ready.

        ``POST /v1/wiki/qa`` is an SSE-only endpoint: it streams events for the
        whole run and never returns a JSON body. We therefore *stream* the POST,
        read frames only until the first ``meta`` event (emitted synchronously at
        start) to recover the ``answerId``, then disconnect and poll
        ``GET /v1/wiki/qa/<answer_id>`` until the answer snapshot has rendered
        blocks (or :attr:`timeout_s` elapses). On timeout we return the partial
        answer with ``status: "running"`` rather than hanging (design spec §8).

        The snapshot's ``blocks`` are written incrementally and carry no terminal
        flag, so we treat the answer as settled once ``blocks`` is non-empty AND
        unchanged across two consecutive polls — a best-effort "rendering has
        stopped" signal rather than a guaranteed completion.

        ``model`` defaults to ``None``; when omitted the body carries no
        ``model`` so the server picks its configured default QA model (an empty
        string is rejected by the route with a 400).
        """
        body: dict[str, Any] = {"slug": project, "question": question}
        if model:
            body["model"] = model

        answer_id = await self._start_qa(body)
        if not answer_id:
            raise RestError("Wiki QA did not emit a meta event with an answer id.")

        deadline = asyncio.get_running_loop().time() + self.timeout_s
        snapshot: dict[str, Any] = {}
        status = "running"
        prev_blocks: list[Any] | None = None
        while True:
            snapshot = _as_dict(await self.client.get(f"/v1/wiki/qa/{answer_id}"))
            blocks = snapshot.get("blocks")
            blocks = blocks if isinstance(blocks, list) else []
            if blocks and blocks == prev_blocks:
                # Non-empty and unchanged since the last poll — rendering settled.
                # (The snapshot fills in incrementally, so a single non-empty read
                # can be a partial answer; requiring stability avoids returning it
                # mislabelled as complete.)
                status = "complete"
                break
            if asyncio.get_running_loop().time() >= deadline:
                status = "running"
                break
            prev_blocks = blocks
            await asyncio.sleep(self.poll_interval_s)

        answer, citations = self._render_qa_blocks(snapshot.get("blocks"))
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
        """Best-effort plain text for a single answer block."""
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
        return ""

    @classmethod
    def _inline_text(cls, node: Any) -> str:
        """Coerce an inline node (string, or rich-text dict/list) to plain text."""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "".join(cls._inline_text(n) for n in node)
        if isinstance(node, dict):
            # Rich inline nodes carry their visible text under ``text``.
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

    async def discover(self, *, project: str | None = None) -> dict[str, Any]:
        """Return available tools and installed plugins for capability discovery.

        Wires ``GET /api/tools`` (optionally scoped to ``project``) +
        ``GET /api/plugins``.
        """
        params = {"project": project} if project else None
        tools_payload = await self.client.get("/api/tools", params=params)
        plugins_payload = await self.client.get("/api/plugins")
        tools = tools_payload.get("tools") if isinstance(tools_payload, dict) else tools_payload
        plugins = (
            plugins_payload.get("plugins")
            if isinstance(plugins_payload, dict)
            else plugins_payload
        )
        return {"tools": tools or [], "plugins": plugins or []}


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
    timeout_s: float = 120.0  # bounded await for an async run (see :meth:`search`)
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

        if status not in self.TERMINAL_STATUSES and run_id:
            payload, status = await self._await_run(run_id)
        return self._shape_run(payload, status, ws.get("name"), detail)

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

    async def _await_run(self, run_id: str) -> tuple[dict[str, Any], str]:
        """Poll the snapshot until terminal or :attr:`timeout_s`; return (payload, status)."""
        deadline = asyncio.get_running_loop().time() + self.timeout_s
        record = await self._load_record(run_id)
        status = str(record.get("status") or "running")
        while status not in self.TERMINAL_STATUSES:
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(self.poll_interval_s)
            record = await self._load_record(run_id)
            status = str(record.get("status") or "running")
        return _as_dict(record.get("payload")), status

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


__all__ = [
    "IntegrationTools",
    "SearchTools",
    "SessionTools",
    "WikiTools",
]
