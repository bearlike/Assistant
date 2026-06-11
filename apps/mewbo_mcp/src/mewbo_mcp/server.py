"""FastMCP server exposing the Mewbo REST API as MCP tools.

Streamable-HTTP transport, mounted at ``/mcp``. Every tool authenticates the
caller's Bearer token locally (KeyStore or master token), then forwards that
*same* token to REST as ``X-API-Key`` (token pass-through). The tool bodies
live in :mod:`mewbo_mcp.tools`; this module only wires FastMCP → auth → a
per-call :class:`~mewbo_mcp.rest.RestClient`, and wraps every tool in the shared
structured-error envelope (:func:`_enveloped`).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import tools
from .auth import AuthError, authenticate
from .config import McpConfig
from .rest import RestClient, RestError

_INSTRUCTIONS = (
    "Mewbo MCP exposes a Mewbo instance to external agents. Create and steer "
    "agent sessions, read their history at four detail tiers "
    "(overview/turns/steps/full) so you control your own context spend, query "
    "the Agentic Wiki, and run Mewbo Search across saved multi-source "
    "workspaces (search → cited answer + results). Long-running tools (search, "
    "ask_wiki, structured_query) return a run/answer id you can re-fetch with "
    "the matching get_* tool if they time out; failures come back as a "
    "structured {error: {code, reason, retryable}} object, never a raw "
    "exception. Authenticate with your Mewbo API key as a Bearer token; the "
    "same key is forwarded to the REST API."
)


def _retryable(status_code: int | None) -> bool:
    """A transport failure or 5xx is worth retrying; a 4xx is the caller's to fix."""
    return status_code is None or status_code >= 500


def _error_result(code: object, reason: str, *, retryable: bool) -> dict[str, Any]:
    """The single structured error envelope every tool returns on failure."""
    return {
        "error": {
            "code": code if code is not None else "error",
            "reason": reason,
            "retryable": retryable,
        }
    }


def _enveloped(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Wrap a tool so a REST/validation failure returns a structured envelope.

    Every tool returns ``{"error": {code, reason, retryable}}`` instead of
    raising — a small/cheap agent can act on that, never on a raw transport
    exception, a leaked HTML page, or a stack trace. ``RestError`` carries the
    HTTP status; a ``ValueError`` (bad argument) maps to ``bad_request``. Auth
    failures deliberately propagate (a distinct protocol-level signal).
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except RestError as exc:
            return _error_result(
                exc.status_code, str(exc), retryable=_retryable(exc.status_code)
            )
        except ValueError as exc:
            return _error_result("bad_request", str(exc), retryable=False)

    return wrapper


def build_server(config: McpConfig | None = None) -> FastMCP:
    """Construct and configure the FastMCP server with all tools registered."""
    cfg = config or McpConfig.from_env()
    mcp: FastMCP = FastMCP(
        name="mewbo-mcp",
        instructions=_INSTRUCTIONS,
        host=cfg.host,
        port=cfg.port,
        streamable_http_path="/mcp",
    )

    def _client(ctx: Context) -> RestClient:
        """Authenticate the call and return a token-bearing REST client."""
        token = authenticate(ctx)
        return RestClient(cfg.api_url, token)

    # -- A. Sessions — create & control -----------------------------------

    @mcp.tool()
    @_enveloped
    async def create_session(
        ctx: Context,
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
        """Create a Mewbo session and start it on ``prompt``.

        By default this auto-provisions a FRESH git worktree + branch off the
        target repo's base branch, isolating the run. Pass ``branch`` to target
        an existing branch, or ``worktree`` (a managed worktree's project id) to
        reuse one. ``project``/``repo`` is a registered project NAME or its git
        identity ``host/owner/repo`` / ``owner/repo`` (see ``list_projects``).
        ``integrations`` is a list of tool ids to allow. ``mode`` may be ``plan``
        or ``act``. ``tag`` is one optional session tag; ``idempotency_key`` tags
        the session so a retry is identifiable/reapable. Returns
        ``{session_id, status}`` — worktree lifecycle is system-owned.
        """
        async with _client(ctx) as client:
            return await tools.SessionTools(client).create(
                prompt=prompt,
                project=project,
                repo=repo,
                branch=branch,
                worktree=worktree,
                integrations=integrations,
                mode=mode,
                title=title,
                tag=tag,
                idempotency_key=idempotency_key,
            )

    @mcp.tool()
    @_enveloped
    async def send_followup(ctx: Context, session_id: str, message: str) -> dict[str, Any]:
        """Send a follow-up / steering message into a session.

        Queued as a user turn when the session is running; when it is idle or
        finished the message RE-ENGAGES it (starts a fresh run) and the new
        ``run_id`` is returned. Only a terminated session rejects.
        """
        async with _client(ctx) as client:
            return await tools.SessionTools(client).send_followup(
                session_id=session_id, message=message
            )

    @mcp.tool()
    @_enveloped
    async def interrupt_session(ctx: Context, session_id: str) -> dict[str, Any]:
        """Interrupt the current step of a session (graceful no-op when idle)."""
        async with _client(ctx) as client:
            return await tools.SessionTools(client).interrupt(session_id=session_id)

    # -- B. Sessions — discover & read (tiered) ---------------------------

    @mcp.tool()
    @_enveloped
    async def list_sessions(
        ctx: Context,
        project: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List sessions (newest-first, capped at ``limit``), projected compact.

        Filters by ``project`` (repo name/identity), ``status``, or ``since`` (an
        ISO-8601 timestamp). Each row is ``{session_id, title, project, status,
        done_reason, created_at, origin?, archived?}``.
        """
        async with _client(ctx) as client:
            return await tools.SessionTools(client).list_sessions(
                project=project, status=status, since=since, limit=limit
            )

    @mcp.tool()
    @_enveloped
    async def get_session_history(
        ctx: Context,
        session_id: str,
        level: str,
        turn: int | None = None,
        step_offset: int = 0,
    ) -> dict[str, Any]:
        """Read a session's history at one of four detail tiers.

        ``level``:
        - ``overview`` — title, summary, authoritative status, turn/step counts,
          token totals (cheapest).
        - ``turns`` — one row per turn: truncated user/assistant text,
          done_reason, step count, per-turn tokens.
        - ``steps`` (needs ``turn``, 1-based) — per-step ``tool_id → summary``
          previews; no full results.
        - ``full`` (needs ``turn``) — full step logs (tool_input, result,
          error); references ``get_agent_tree`` rather than inlining it. Steps are
          paged (20 per call from ``step_offset``); when more remain the result
          carries ``next_step_offset`` — pass it back to read the next page.

        Pick the smallest tier that answers your question to conserve context.
        """
        async with _client(ctx) as client:
            return await tools.SessionTools(client).history(
                session_id=session_id, level=level, turn=turn, step_offset=step_offset
            )

    @mcp.tool()
    @_enveloped
    async def get_agent_tree(ctx: Context, session_id: str) -> dict[str, Any]:
        """Return the session's sub-agent tree with per-agent lifecycle state.

        During the post-create spin-up window returns ``{status: "initializing"}``
        (retry shortly) rather than a transport error.
        """
        async with _client(ctx) as client:
            return await tools.SessionTools(client).agent_tree(session_id=session_id)

    # -- C. Wiki — query & ask --------------------------------------------

    @mcp.tool()
    @_enveloped
    async def list_wiki_projects(ctx: Context) -> list[Any]:
        """List the indexed Agentic Wiki projects available to query."""
        async with _client(ctx) as client:
            return await tools.WikiTools(client).list_projects()

    @mcp.tool()
    @_enveloped
    async def read_wiki_structure(
        ctx: Context,
        project: str,
        detail: str = "stats",
        layer: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Return a wiki project's structure (knowledge graph) at a detail tier.

        ``detail``: ``stats`` (default, compact counts) | ``nodes`` (+ node list,
        optionally one ``layer``, capped by ``limit``) | ``full`` (+ edges). The
        default is never the full multi-hundred-KB graph dump.
        """
        async with _client(ctx) as client:
            return await tools.WikiTools(client).read_structure(
                project=project, detail=detail, layer=layer, limit=limit
            )

    @mcp.tool()
    @_enveloped
    async def read_wiki_page(ctx: Context, project: str, page_id: str) -> dict[str, Any]:
        """Return a single wiki page's markdown body, navigation, and TOC."""
        async with _client(ctx) as client:
            return await tools.WikiTools(client).read_page(project=project, page_id=page_id)

    @mcp.tool()
    @_enveloped
    async def submit_insight(
        ctx: Context,
        project: str,
        insight: str,
        anchors: list[str] | None = None,
        labels: list[str] | None = None,
        kind: str = "propositional",
        condense: bool = True,
    ) -> dict[str, Any]:
        """Suggest a memory insight to enrich a wiki project's knowledge graph.

        Use this to teach the wiki a durable fact or rule about the codebase.
        The server condenses ``insight`` into atomic claims, anchors each to
        the relevant code (tree-sitter graph), dedups against existing notes,
        and safely merges. ``anchors`` (optional) are ``path/file.py#Name``
        entity keys to ground the note; when omitted the server auto-anchors
        by embedding similarity. ``condense=False`` stores ``insight`` verbatim
        as a single ≤200-char claim instead of decomposing it. Returns the
        per-claim result (``action`` ∈ created/merged/linked/rejected).
        """
        async with _client(ctx) as client:
            return await tools.WikiTools(client).submit_insight(
                project=project,
                insight=insight,
                anchors=anchors,
                labels=labels,
                kind=kind,
                condense=condense,
            )

    @mcp.tool()
    @_enveloped
    async def ask_wiki(
        ctx: Context, project: str, question: str, model: str | None = None
    ) -> dict[str, Any]:
        """Ask a wiki project a natural-language question and await the answer.

        Starts a wiki Q&A run and polls until the answer settles (bounded
        timeout). ``model`` is OPTIONAL — leave it unset to use the server's
        configured default QA model. Returns ``{answer_id, answer, citations,
        status}``; ``status: "running"`` means the answer was not ready within
        the timeout — call ``get_wiki_answer(answer_id)`` to resume it.
        """
        async with _client(ctx) as client:
            return await tools.WikiTools(client).ask(
                project=project, question=question, model=model
            )

    @mcp.tool()
    @_enveloped
    async def get_wiki_answer(
        ctx: Context, answer_id: str, detail: str = "answer"
    ) -> dict[str, Any]:
        """Fetch a wiki answer by id — resume a timed-out ``ask_wiki``, or replay.

        Same shape as ``ask_wiki`` (consumes the ``answer_id`` it returns).
        ``detail=full`` also returns the raw blocks + models used.
        """
        async with _client(ctx) as client:
            return await tools.WikiTools(client).get_answer(answer_id=answer_id, detail=detail)

    @mcp.tool()
    @_enveloped
    async def structured_query(
        ctx: Context,
        query: str,
        schema: dict[str, Any],
        workspace: str | None = None,
        tool_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get a JSON-Schema-validated object answer to a request.

        The server runs an agentic session: the model may call grounding tools
        (wiki/code search, graph traversal, entity resolution) before emitting
        the final answer as an object that validates against ``schema``.
        ``workspace`` (optional) is a wiki slug OR a Mewbo Search workspace
        (id/name): a mapped search workspace runs GRAPH-FIRST over the Source
        Capability Graph — route → probe each pathway → aggregate → emit — and
        the result carries ``provenance`` (which recipes routed, which probes
        ran). ``tool_ids`` (optional) is the grounding-tool allowlist. Returns
        ``{run_id, status, output?, provenance?}`` — ``status: "running"`` means
        it did not finish within the await budget; call
        ``get_structured_run(run_id)`` for the object.
        """
        async with _client(ctx) as client:
            return await tools.StructuredQueryTools(client).query(
                query=query, schema=schema, workspace=workspace, tools=tool_ids
            )

    @mcp.tool()
    @_enveloped
    async def get_structured_run(ctx: Context, run_id: str) -> dict[str, Any]:
        """Fetch a structured run by id — resume a ``running`` ``structured_query``.

        Same shape as ``structured_query`` (consumes the ``run_id`` it returns).
        """
        async with _client(ctx) as client:
            return await tools.StructuredQueryTools(client).get_run(run_id=run_id)

    # -- D. Integrations / capability discovery ---------------------------

    @mcp.tool()
    @_enveloped
    async def list_integrations(
        ctx: Context,
        project: str | None = None,
        kind: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """List available tools and installed plugins for capability discovery.

        Use this to learn what tool ids you can pass to ``create_session``'s
        ``integrations`` argument. Tools are projected compact (``tool_id``,
        ``name``, ``kind?``, ``enabled?``); ``kind``/``enabled`` filter them.
        """
        async with _client(ctx) as client:
            return await tools.IntegrationTools(client).discover(
                project=project, kind=kind, enabled=enabled
            )

    @mcp.tool()
    @_enveloped
    async def list_projects(ctx: Context) -> dict[str, Any]:
        """List registered projects + their git identities for ``create_session``.

        Each row carries ``name`` (and managed ``project_id``) plus the canonical
        git ``repo: {host, owner, name}`` and ``aliases`` — any of which resolves
        as ``create_session``'s ``project``/``repo`` argument.
        """
        async with _client(ctx) as client:
            return await tools.ProjectTools(client).list_projects()

    # -- E. Mewbo Search — multi-source workspace search ------------------

    @mcp.tool()
    @_enveloped
    async def list_search_workspaces(
        ctx: Context, query: str | None = None
    ) -> dict[str, Any]:
        """List saved Mewbo Search workspaces (id, name, sources, recent-query count).

        A workspace is a saved set of sources (connectors) a search fans out
        across. ``query`` optionally narrows the listing (case-insensitive
        substring over name, description, and past-query text). Use this to
        discover what you can search, then pass a workspace id or name to
        ``search``.
        """
        async with _client(ctx) as client:
            return await tools.SearchTools(client).list_workspaces(query=query)

    @mcp.tool()
    @_enveloped
    async def search(
        ctx: Context,
        query: str,
        workspace: str,
        project: str | None = None,
        detail: str = "answer",
    ) -> dict[str, Any]:
        """Run a Mewbo Search across a workspace and return the cited answer.

        ``workspace`` is a workspace id or name (see ``list_search_workspaces``).
        ``detail``: ``answer`` (default, cheapest) returns the synthesis + a
        compact result index so citations resolve; ``full`` adds each result's
        snippet/insight/refs. Returns ``{run_id, status, answer, results,
        related_questions, …}``. ``status: "running"`` means the run did not
        finish within the await budget — call ``get_search_run(run_id)`` for the
        rest.
        """
        async with _client(ctx) as client:
            return await tools.SearchTools(client).search(
                query=query, workspace=workspace, project=project, detail=detail
            )

    @mcp.tool()
    @_enveloped
    async def get_search_run(
        ctx: Context, run_id: str, detail: str = "answer"
    ) -> dict[str, Any]:
        """Fetch a prior Mewbo Search run by id (replay / deep-link; no re-run).

        Same shape + ``detail`` tiers as ``search``. Use it to re-read a run
        after context compaction, or to poll an async run that returned
        ``status: "running"``.
        """
        async with _client(ctx) as client:
            return await tools.SearchTools(client).get_run(run_id=run_id, detail=detail)

    return mcp


def main() -> None:
    """Entry point: run the MCP server over streamable-HTTP."""
    server = build_server()
    server.run(transport="streamable-http")


# Surface AuthError so test/import sites don't have to reach into .auth.
__all__ = ["AuthError", "build_server", "main"]


if __name__ == "__main__":
    main()
