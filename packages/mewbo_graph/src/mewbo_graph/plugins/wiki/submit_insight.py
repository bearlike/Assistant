"""``wiki_submit_insight`` SessionTool — ingest one atomic memory note.

The in-session write surface for the multiplex memory layer (Gitea #13 §8).
Works for both the indexer and the Q&A agent: the source is derived from
whichever wiki ctx the session resolves to. The tool is deterministic — it
does NOT call an LLM (no condense, dedup tier-3 disabled); agents are
instructed to submit pre-atomized claims, and the shared ``InsightIngestor``
still runs exact + fuzzy dedup, anchor resolution, and embedding. Raw,
human-authored insights take the LLM-backed REST/MCP path instead.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.config import get_config_value
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import (
    emit_log,
    resolve_job_ctx,
    resolve_qa_ctx,
    resolve_runtime,
)
from mewbo_graph.wiki.memory_types import MAX_INSIGHT_CHARS

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.submit_insight")


class WikiSubmitInsightArgs(BaseModel):
    """Arguments for ``wiki_submit_insight``."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        ...,
        min_length=1,
        max_length=MAX_INSIGHT_CHARS,
        description="ONE atomic claim about the codebase (≤200 chars, no pronouns)",
    )
    anchors: list[str] = Field(
        default_factory=list,
        description="entity_keys this note grounds to, as 'path/file.py#Qualified.Name'",
    )
    links: list[str] = Field(
        default_factory=list,
        description="node_ids of related memory notes to RELATES-link",
    )
    kind: Literal["propositional", "prescriptive"] = Field(
        default="propositional",
        description="propositional = a fact; prescriptive = a rule/should-do",
    )
    labels: list[str] = Field(default_factory=list, description="free-form topic tags")
    entity_recommendations: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Optional abstract-entity resolution PRIORS the next pass consults"
            " (never a hard mutation): [{action, subjects, type?, rationale}]"
            " where action is merge|distinct|retype|create"
        ),
    )


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


def _memory_enabled() -> bool:
    """Gate the memory layer on ``wiki.memory.enabled`` (default True)."""
    return bool(get_config_value("wiki", "memory", "enabled", default=True))


class WikiSubmitInsightTool(WikiSessionTool):
    """SessionTool: ingest one atomic memory note into the multiplex graph."""

    tool_id = "wiki_submit_insight"
    args_cls = WikiSubmitInsightArgs
    schema: dict[str, Any] = pydantic_to_openai_tool(
        WikiSubmitInsightArgs, name="wiki_submit_insight"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_submit_insight`` tool call."""
        if not _memory_enabled():
            return _err_result(
                "validation", "memory layer disabled (wiki.memory.enabled=false)"
            )

        runtime = self._runtime()
        if runtime is None:
            return _err_result("internal", "wiki runtime not available")

        # Indexer sessions resolve a job ctx; Q&A sessions resolve a qa ctx.
        job_ctx = resolve_job_ctx(self._session_id, runtime)
        qa_ctx = resolve_qa_ctx(self._session_id, runtime) if job_ctx is None else None
        ctx = job_ctx or qa_ctx
        if ctx is None:
            return _err_result("internal", "wiki ctx not found for this session")
        source = "indexer" if job_ctx is not None else "qa"

        args = self._parse_args(WikiSubmitInsightArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        from mewbo_graph.wiki.memory import InsightIngestor  # noqa: PLC0415

        # Deterministic in-session ingest: no LLM (agents pre-atomize), embedder
        # defaults via from_store (BM25-only if no backend).
        ingestor = InsightIngestor.from_store(ctx.store)
        result = ingestor.ingest(
            ctx.slug,
            args.content,
            anchors=args.anchors,
            links=args.links,
            kind=args.kind,
            labels=args.labels,
            condense=False,
            source=source,
            author_agent=f"wiki-{source}",
            session_id=ctx.session_id,
        )

        if job_ctx is not None:
            claim = result.claims[0] if result.claims else None
            if claim is not None:
                emit_log(job_ctx, f"Insight {claim.action}: {claim.content[:80]}")

        # Persist any abstract-entity recommendations as resolution priors. The
        # EntityResolver consults them on its next pass; a malformed rec is
        # non-fatal — skip it rather than fail the whole insight submission.
        saved_recs = self._save_entity_recommendations(ctx, args.entity_recommendations)

        payload = {
            "ok": result.ok,
            "claims": [c.model_dump() for c in result.claims],
            "entity_recommendations_saved": saved_recs,
        }
        return MockSpeaker(content=json.dumps(payload))

    @staticmethod
    def _save_entity_recommendations(ctx: Any, raw_recs: list[dict[str, Any]]) -> int:
        """Persist valid ``EntityRecommendation`` priors; skip malformed ones."""
        if not raw_recs:
            return 0
        from mewbo_graph.entities.types import EntityRecommendation  # noqa: PLC0415

        saved = 0
        for raw in raw_recs:
            try:
                rec = EntityRecommendation.model_validate(raw)
            except Exception:  # noqa: BLE001 — a malformed rec is non-fatal
                continue
            ctx.store.save_entity_recommendation(ctx.slug, rec)
            saved += 1
        return saved


__all__ = ["WikiSubmitInsightArgs", "WikiSubmitInsightTool"]
