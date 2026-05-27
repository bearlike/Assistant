"""FastMCP server exposing the Mewbo REST API as MCP tools.

Streamable-HTTP transport, mounted at ``/mcp``. Every tool authenticates the
caller's Bearer token locally (KeyStore or master token), then forwards that
*same* token to REST as ``X-API-Key`` (token pass-through). The tool bodies
live in :mod:`mewbo_mcp.tools`; this module only wires FastMCP → auth → a
per-call :class:`~mewbo_mcp.rest.RestClient`.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import tools
from .auth import AuthError, authenticate
from .config import McpConfig
from .rest import RestClient

_INSTRUCTIONS = (
    "Mewbo MCP exposes a Mewbo instance to external agents. Create and steer "
    "agent sessions, read their history at four detail tiers "
    "(overview/turns/steps/full) so you control your own context spend, query "
    "the Agentic Wiki, and run Mewbo Search across saved multi-source "
    "workspaces (search → cited answer + results). Authenticate with your "
    "Mewbo API key as a Bearer token; the same key is forwarded to the REST API."
)


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
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Mewbo session and start it on ``prompt``.

        By default this auto-provisions a FRESH git worktree + branch off the
        target repo's base branch, isolating the run. Pass ``branch`` to target
        an existing branch (a worktree is created for it), or ``worktree`` (a
        managed worktree's project id) to reuse an existing one. ``project``
        and ``repo`` are aliases for the target repository name. ``integrations``
        is a list of tool ids to allow (the session's ``mcp_tools`` allowlist).
        ``mode`` may be ``plan`` or ``act``. Returns ``{session_id, status}``.
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
                tags=tags,
            )

    @mcp.tool()
    async def send_followup(ctx: Context, session_id: str, message: str) -> dict[str, Any]:
        """Send a follow-up / steering message into a running session.

        The message is queued and drained as a user turn between steps.
        """
        async with _client(ctx) as client:
            return await tools.SessionTools(client).send_followup(
                session_id=session_id, message=message
            )

    @mcp.tool()
    async def interrupt_session(ctx: Context, session_id: str) -> dict[str, Any]:
        """Interrupt the current step of a running session (graceful pause)."""
        async with _client(ctx) as client:
            return await tools.SessionTools(client).interrupt(session_id=session_id)

    # -- B. Sessions — discover & read (tiered) ---------------------------

    @mcp.tool()
    async def list_sessions(
        ctx: Context,
        project: str | None = None,
        status: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """List sessions, optionally filtered by project, status, or since-time.

        ``since`` is an ISO-8601 timestamp; only sessions created at or after it
        are returned. ``status`` matches the session's terminal/running state.
        """
        async with _client(ctx) as client:
            return await tools.SessionTools(client).list_sessions(
                project=project, status=status, since=since
            )

    @mcp.tool()
    async def get_session_history(
        ctx: Context,
        session_id: str,
        level: str,
        turn: int | None = None,
    ) -> dict[str, Any]:
        """Read a session's history at one of four detail tiers.

        ``level``:
        - ``overview`` — title, summary, status, turn/step counts, token totals
          (cheapest).
        - ``turns`` — one row per turn: truncated user/assistant text,
          done_reason, step count, per-turn tokens.
        - ``steps`` (needs ``turn``, 1-based) — per-step ``tool_id → summary``
          previews; no full results.
        - ``full`` (needs ``turn``) — full step logs (tool_input, result,
          error) plus the sub-agent tree.

        Pick the smallest tier that answers your question to conserve context.
        """
        async with _client(ctx) as client:
            return await tools.SessionTools(client).history(
                session_id=session_id, level=level, turn=turn
            )

    @mcp.tool()
    async def get_agent_tree(ctx: Context, session_id: str) -> dict[str, Any]:
        """Return the session's sub-agent tree with per-agent lifecycle state."""
        async with _client(ctx) as client:
            return await tools.SessionTools(client).agent_tree(session_id=session_id)

    # -- C. Wiki — query & ask --------------------------------------------

    @mcp.tool()
    async def list_wiki_projects(ctx: Context) -> list[Any]:
        """List the indexed Agentic Wiki projects available to query."""
        async with _client(ctx) as client:
            return await tools.WikiTools(client).list_projects()

    @mcp.tool()
    async def read_wiki_structure(ctx: Context, project: str) -> dict[str, Any]:
        """Return a wiki project's structure: its knowledge graph nodes/edges."""
        async with _client(ctx) as client:
            return await tools.WikiTools(client).read_structure(project=project)

    @mcp.tool()
    async def read_wiki_page(ctx: Context, project: str, page_id: str) -> dict[str, Any]:
        """Return a single wiki page's markdown body, navigation, and TOC."""
        async with _client(ctx) as client:
            return await tools.WikiTools(client).read_page(project=project, page_id=page_id)

    @mcp.tool()
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
    async def ask_wiki(
        ctx: Context, project: str, question: str, model: str | None = None
    ) -> dict[str, Any]:
        """Ask a wiki project a natural-language question and await the answer.

        Starts a wiki Q&A run and polls until the answer renders (bounded
        timeout). ``model`` is optional — leave it unset to use the server's
        configured default QA model. Returns ``{answer, citations, status}``;
        ``status: "running"`` means the answer was not ready within the
        timeout — call again to get the rest.
        """
        async with _client(ctx) as client:
            return await tools.WikiTools(client).ask(
                project=project, question=question, model=model
            )

    # -- D. Integrations / capability discovery ---------------------------

    @mcp.tool()
    async def list_integrations(ctx: Context, project: str | None = None) -> dict[str, Any]:
        """List available tools and installed plugins for capability discovery.

        Use this to learn what tool ids you can pass to ``create_session``'s
        ``integrations`` argument.
        """
        async with _client(ctx) as client:
            return await tools.IntegrationTools(client).discover(project=project)

    # -- E. Mewbo Search — multi-source workspace search ------------------

    @mcp.tool()
    async def list_search_workspaces(ctx: Context) -> dict[str, Any]:
        """List saved Mewbo Search workspaces (id, name, sources, recent-query count).

        A workspace is a saved set of sources (connectors) a search fans out
        across. Use this to discover what you can search, then pass a workspace
        id or name to ``search``.
        """
        async with _client(ctx) as client:
            return await tools.SearchTools(client).list_workspaces()

    @mcp.tool()
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
