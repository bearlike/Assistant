"""``wiki_build_graph`` SessionTool — parses cloned tree into graph + embeddings."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import emit_log, emit_phase, resolve_job_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.config import get_config_value
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.build_graph")


class WikiBuildGraphArgs(BaseModel):
    """Args for wiki_build_graph (no inputs — uses ctx)."""

    model_config = ConfigDict(extra="forbid")


def _resolve_runtime():
    """Late-import runtime; monkeypatched in tests."""
    try:
        from mewbo_api.wiki.routes import _runtime  # noqa: PLC0415
        return _runtime
    except ImportError:  # pragma: no cover
        return None


def _make_embedder():
    """Create an Embedder; isolated so tests can stub it."""
    from mewbo_api.wiki.embedder import Embedder  # noqa: PLC0415
    return Embedder()


def _embeddings_enabled() -> bool:
    return bool(get_config_value("wiki", "embedding", "enabled", default=True))


class WikiBuildGraphTool:
    """SessionTool: parse cloned files into a graph + embeddings."""

    tool_id = "wiki_build_graph"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema = pydantic_to_openai_tool(WikiBuildGraphArgs, name="wiki_build_graph")

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise the tool with the owning session id and optional event logger."""
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        v, self._terminate = self._terminate, False
        return v

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_build_graph`` tool call."""
        runtime = _resolve_runtime()
        ctx = resolve_job_ctx(self._session_id, runtime) if runtime else None
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found")
        try:
            WikiBuildGraphArgs.model_validate(action_step.tool_input or {})
        except ValidationError as ve:
            return _err_result("validation", str(ve))

        emit_phase(ctx, "graph")

        # 1. Walk the clone dir.
        repo_root = ctx.clone_dir
        if not repo_root.exists():
            return _err_result("internal", f"clone dir missing: {repo_root}")

        # 2. Parse with GraphIndex.
        from mewbo_api.wiki.graph import GraphIndex  # noqa: PLC0415

        files = [p for p in repo_root.rglob("*") if p.is_file() and ".git" not in p.parts]
        emit_log(ctx, f"Parsing {len(files)} files with tree-sitter…")
        gi = GraphIndex()
        parsed = gi.parse_repo(slug=ctx.slug, repo_root=repo_root, files=files)
        emit_log(ctx, f"Built graph: {len(parsed.nodes)} nodes, {len(parsed.edges)} edges")

        # 3. Persist graph.
        ctx.store.upsert_nodes(ctx.slug, parsed.nodes)
        ctx.store.upsert_edges(ctx.slug, parsed.edges)

        # 4. Embed nodes if enabled. Embedding failures are non-fatal —
        # retrieval falls back to BM25 + 1-hop graph traversal, which is
        # still useful. This lets the indexer run against LLM proxies that
        # don't expose an embedding model.
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
        return MockSpeaker(content=str(result))


def _node_text_for_embedding(node) -> str:
    parts = [node.name]
    if node.docstring:
        parts.append(node.docstring)
    if node.file and node.file != node.name:
        parts.append(node.file)
    return " — ".join(parts)


def _lang_from_ext(file_path: str) -> str:
    from mewbo_api.wiki.graph import _LANG_BY_EXT  # noqa: PLC0415
    return _LANG_BY_EXT.get(os.path.splitext(file_path)[1].lower(), "")


__all__ = [
    "WikiBuildGraphArgs",
    "WikiBuildGraphTool",
]
