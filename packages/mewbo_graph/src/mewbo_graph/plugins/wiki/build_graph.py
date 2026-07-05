"""``wiki_build_graph`` SessionTool — parses cloned tree into graph + embeddings."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.config import get_config_value
from pydantic import BaseModel, ConfigDict

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import emit_log, emit_phase, resolve_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mewbo_core.classes import ActionStep

    from mewbo_graph.wiki.graph import GraphParseResult
    from mewbo_graph.wiki.types import GraphEdge, GraphNode

logging = get_logger(name="mewbo_graph.plugins.wiki.build_graph")


class WikiBuildGraphArgs(BaseModel):
    """Args for wiki_build_graph (no inputs — uses ctx)."""

    model_config = ConfigDict(extra="forbid")


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


def _make_embedder() -> Any:
    """Create an Embedder; isolated so tests can stub it."""
    from mewbo_graph.wiki.embedder import Embedder  # noqa: PLC0415
    return Embedder()


def _embeddings_enabled() -> bool:
    return bool(get_config_value("wiki", "embedding", "enabled", default=True))


class WikiBuildGraphTool(WikiSessionTool):
    """SessionTool: parse cloned files into a graph + embeddings."""

    tool_id = "wiki_build_graph"
    args_cls = WikiBuildGraphArgs
    schema = pydantic_to_openai_tool(WikiBuildGraphArgs, name="wiki_build_graph")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_build_graph`` tool call."""
        ctx = self._job_ctx()
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found")
        parsed_args = self._parse_args(WikiBuildGraphArgs, action_step)
        if isinstance(parsed_args, MockSpeaker):
            return parsed_args

        emit_phase(ctx, "graph")

        # Checkpoint-aware resume (Gitea #54): the persisted graph is reused
        # as-is when an interrupted index already built it (graph + enrich are
        # the ~6-min expensive idempotent phases). Done-detection lives ONLY in
        # ResumePlan (DRY); this is the single-line short-circuit. Still advance
        # the phase to ``enrich`` so progress reflects the (also-skipped) window.
        rp = ctx.resume_plan
        if rp is not None and rp.should_skip("graph"):
            emit_log(ctx, f"Graph already built ({rp.node_count} nodes) — skipped on resume")
            emit_phase(ctx, "enrich")
            return MockSpeaker(content=str({
                "nodeCount": rp.node_count,
                "skipped": "graph already built — reused on resume",
            }))

        # 1. Walk the clone dir.
        repo_root = ctx.clone_dir
        if not repo_root.exists():
            return _err_result("internal", f"clone dir missing: {repo_root}")

        # 2-4. Parse + persist + embed via the shared deterministic core (reused
        # by the zero-LLM GraphOnlyIndexer so tree-sitter parsing + the embedding
        # fallback live in exactly ONE place).
        result = build_graph_core(ctx)

        # Graph is built; advance the phase to ``enrich`` so progress reflects
        # the entity-enrichment fan-out that immediately follows. build_graph is
        # the last tool the indexer calls before spawning the wiki-enricher
        # sub-agents — without this the snapshot sits at ``graph`` for the whole
        # enrich window and then jumps straight to ``plan`` (the ~2-minute
        # "graph plateau" users see). enrich emits no tool of its own, so this
        # is the single deterministic place to mark the transition.
        emit_phase(ctx, "enrich")
        return MockSpeaker(content=str(result))


def build_graph_core(ctx: Any) -> dict[str, object]:
    """Parse the clone dir into a graph, persist it, and embed nodes.

    The DETERMINISTIC heart of the ``graph`` phase — zero LLM. Extracted from
    ``WikiBuildGraphTool.handle`` so the agent-driven indexer AND the zero-LLM
    ``GraphOnlyIndexer`` share one tree-sitter parse + persist + embedding-
    fallback path (DRY: tree-sitter parsing and the BM25 degrade live HERE, not
    duplicated). Operates purely over a job ctx (``slug``/``clone_dir``/``store``
    + ``emit_log``); does NOT emit phase events (the caller owns the phase
    transitions). Returns the result summary dict the tool serialises.
    """
    repo_root = ctx.clone_dir

    # 1. Parse with GraphIndex.
    from mewbo_graph.wiki.graph import GraphIndex  # noqa: PLC0415

    files = [p for p in repo_root.rglob("*") if p.is_file() and ".git" not in p.parts]
    emit_log(ctx, f"Parsing {len(files)} files with tree-sitter…")
    gi = GraphIndex()
    parsed = gi.parse_repo(slug=ctx.slug, repo_root=repo_root, files=files)
    # Replace tree-sitter's name-matched PYTHON relationship edges with the
    # faithful resolver's exact edges when scip-python is available. No-op (the
    # parse passes straight through) when the resolver can't run — see helper.
    parsed = _apply_resolver(ctx.slug, repo_root, parsed)
    emit_log(ctx, f"Built graph: {len(parsed.nodes)} nodes, {len(parsed.edges)} edges")

    # 1b. Validate the whole graph ONCE at ingest (schema v2 — Gitea #188): node-id
    # uniqueness + referential integrity + CPG endpoint rules. A malformed graph
    # fails loudly HERE rather than corrupting the persisted store silently.
    from pydantic import ValidationError  # noqa: PLC0415

    from mewbo_graph.wiki.types import CodeGraph  # noqa: PLC0415

    try:
        code_graph = CodeGraph(nodes=parsed.nodes, edges=parsed.edges)
    except ValidationError as exc:
        raise ValueError(
            f"graph schema validation failed for {ctx.slug}: {exc}"
        ) from exc

    # 2. Persist the validated graph (store API unchanged — flat node/edge lists).
    ctx.store.upsert_nodes(ctx.slug, code_graph.nodes)
    ctx.store.upsert_edges(ctx.slug, code_graph.edges)

    # 3. Embed nodes if enabled. Embedding failures are non-fatal — retrieval
    # falls back to BM25 + 1-hop graph traversal, which is still useful. This
    # lets the indexer run against LLM proxies that don't expose an embedding
    # model.
    embedded_count = 0
    embedding_error: str | None = None
    if _embeddings_enabled() and parsed.nodes:
        try:
            embedder = _make_embedder()
            items = [(n.node_id, _node_text_for_embedding(n)) for n in parsed.nodes]
            emit_log(ctx, f"Embedding {len(items)} nodes via {embedder.model}…")
            embeddings = embedder.embed_nodes(items, slug=ctx.slug)
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            embedding_error = str(exc)
            logging.warning(
                "wiki_build_graph: embeddings unavailable; falling back to "
                "BM25-only retrieval. Reason: {}",
                embedding_error,
            )
            embeddings = []
            emit_log(
                ctx,
                f"Embeddings unavailable ({embedding_error}); falling back to BM25",
                level="warn",
            )
        if embeddings:
            ctx.store.upsert_embeddings(ctx.slug, embeddings)
            embedded_count = len(embeddings)
            emit_log(
                ctx,
                f"Embedded {embedded_count} nodes (dim={embeddings[0].dim})",
            )
    elif not _embeddings_enabled():
        emit_log(ctx, "Embeddings disabled (wiki.embedding.enabled=false)", level="warn")

    languages = sorted({_lang_from_ext(n.file) for n in parsed.nodes if n.type != "Module"})

    result: dict[str, object] = {
        "nodeCount": len(parsed.nodes),
        "edgeCount": len(parsed.edges),
        "embeddedCount": embedded_count,
        "languages": [lang for lang in languages if lang],
        "skippedCount": len(parsed.skipped),
    }
    if embedding_error is not None:
        result["embeddingWarning"] = (
            "Embeddings unavailable — retrieval will use BM25 + graph only."
        )
    return result


# Edge kinds the faithful resolver SUPERSEDES for Python sources. tree-sitter
# emits these cross-file edges by NAME (every same-named symbol collapses onto
# one node); the resolver replaces them with exact, real-id REFERENCES/EXTENDS.
_RESOLVER_SUPERSEDED_EDGES = frozenset({"IMPORTS", "CALLS", "EXTENDS"})
_PYTHON_SUFFIXES = (".py", ".pyi")


def _resolver_available() -> bool:
    """True when the faithful Python symbol resolver can run (both scip binaries).

    Isolated so tests can force the resolver on/off without the external CLIs.
    The ``wiki/resolve`` package is pure-Python (its scip backends are
    subprocesses) but lives behind the ``mewbo-graph[resolve]`` extra, so the
    import is guarded — an install without it degrades cleanly to tree-sitter-only
    edges, exactly as today.
    """
    try:
        from mewbo_graph.wiki.resolve import ScipPythonResolver  # noqa: PLC0415
    except ImportError:
        return False
    return ScipPythonResolver.is_available()


def _make_resolver(slug: str) -> Any:
    """Construct the Python symbol resolver for *slug*; isolated for test injection."""
    from mewbo_graph.wiki.resolve import ScipPythonResolver  # noqa: PLC0415

    return ScipPythonResolver(slug)


def _apply_resolver(
    slug: str, repo_root: Path, result: GraphParseResult
) -> GraphParseResult:
    """Swap tree-sitter's name-matched Python edges for the resolver's exact ones.

    The tree-sitter extractor emits cross-file IMPORTS/CALLS/EXTENDS edges *by
    name* — every same-named symbol collapses onto one node. When the faithful
    ``ScipPythonResolver`` is available it resolves those EXACTLY (real ``node_id``
    targets, ``target_name=None``), so for PYTHON source files we drop the
    synthetic name-matched edges and substitute the resolver's. Everything else is
    untouched: ``CONTAINS`` (structural, all languages) and the non-Python
    synthetic edges (Go/Rust/TS keep tree-sitter resolution until they get a
    backend) flow through unchanged. The resolver's descriptor-keyed ``External``
    nodes are persisted alongside the real nodes so its real-id edges survive the
    view's ``target in node_ids`` endpoint check.

    Degrades to a byte-identical ``result`` when the resolver can't run (binaries
    absent / extra uninstalled) or indexes nothing (``not res.available``) — NO
    behaviour change from the tree-sitter-only path.
    """
    from mewbo_graph.wiki.graph import GraphParseResult  # noqa: PLC0415

    if not _resolver_available():
        return result
    res = _make_resolver(slug).resolve(repo_root, result.nodes)
    if not res.available:
        return result

    node_by_id = {n.node_id: n for n in result.nodes}
    kept_edges = [
        e for e in result.edges if not _is_superseded_python_edge(e, node_by_id)
    ]
    return GraphParseResult(
        nodes=result.nodes + res.externals,
        edges=kept_edges + res.edges,
        skipped=result.skipped,
    )


def _is_superseded_python_edge(
    edge: GraphEdge, node_by_id: dict[str, GraphNode]
) -> bool:
    """True for a synthetic IMPORTS/CALLS/EXTENDS edge whose source is a Python file.

    The edge's ``source`` is the in-repo node the relationship originates at (the
    File node for IMPORTS/CALLS, the subclass node for EXTENDS); reading its
    ``file`` tells us the source language. An unmappable source (shouldn't happen
    for these synthetic kinds) is conservatively KEPT — only edges we KNOW the
    resolver supersedes are dropped.
    """
    if edge.type not in _RESOLVER_SUPERSEDED_EDGES:
        return False
    src = node_by_id.get(edge.source)
    return src is not None and src.file.endswith(_PYTHON_SUFFIXES)


def _node_text_for_embedding(node: GraphNode) -> str:
    parts = [node.name]
    if node.docstring:
        parts.append(node.docstring)
    if node.file and node.file != node.name:
        parts.append(node.file)
    return " — ".join(parts)


def _lang_from_ext(file_path: str) -> str:
    from mewbo_graph.wiki.graph import _LANG_BY_EXT  # noqa: PLC0415
    return _LANG_BY_EXT.get(os.path.splitext(file_path)[1].lower(), "")


__all__ = [
    "WikiBuildGraphArgs",
    "WikiBuildGraphTool",
    "build_graph_core",
]
