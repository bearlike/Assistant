"""Abstract-entity SessionTools — deterministic ops the enricher/QA agents drive.

Consistent with the SCG "ops-are-tools, agent-is-the-loop" rule: resolution +
provenance run INSIDE the tool; the agent only decides what to mint/relate.
``mint_entity`` upserts (resolve → create/merge/flag); ``resolve_entity`` is a
read-only dedup-check that mints nothing; ``relate_entities`` writes one typed
edge. All three share the wiki SessionTool base (ctor, runtime/ctx resolution,
arg validation, structured errors) — only ``handle`` + the args schema differ.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.config import get_config_value
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import (
    resolve_job_ctx,
    resolve_qa_ctx,
    resolve_runtime,
)

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.mint_entity")


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


def _make_embedder() -> Any:
    """Construct an Embedder or None; isolated so tests can stub it offline."""
    from mewbo_graph.wiki.embedder import make_embedder_or_none  # noqa: PLC0415

    return make_embedder_or_none()


def _entities_enabled() -> bool:
    """Gate the entity layer on ``wiki.memory.enabled`` (shares the memory gate)."""
    return bool(get_config_value("wiki", "memory", "enabled", default=True))


class MintEntityArgs(BaseModel):
    """Arguments for ``mint_entity``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ..., min_length=1, description="Entity name as it appears in source"
    )
    type: str = Field(
        ...,
        min_length=1,
        description=(
            "Soft type: person/project/product/organization/concept/student/team"
            " or another noun"
        ),
    )
    description: str = Field(default="", description="One-line description")
    aliases: list[str] = Field(
        default_factory=list, description="Known alternate names"
    )
    anchors: list[str] = Field(
        default_factory=list,
        description="entity_keys to ground to ('file.py#Name' or 'entity:<id>')",
    )
    labels: list[str] = Field(
        default_factory=list,
        description=(
            "Arbitrary UML-style tags: stereotype/role/user-story facets, e.g."
            " 'actor', 'user-story', 'aggregate-root'"
        ),
    )


class RelateEntitiesArgs(BaseModel):
    """Arguments for ``relate_entities``."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., min_length=1, description="source entity id")
    target: str = Field(..., min_length=1, description="target entity id")
    relation_type: str = Field(
        ..., min_length=1, description="typed verb e.g. owns/works_on/enrolls_in"
    )
    description: str = Field(default="", description="optional relation description")


class ResolveEntityArgs(BaseModel):
    """Arguments for ``resolve_entity`` (a read-only dedup probe)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="candidate entity name")
    type: str | None = Field(
        default=None, description="optional soft type to scope the probe"
    )


def _ctx_for(tool: WikiSessionTool) -> Any | None:
    """Resolve a job ctx, else a QA ctx, for *tool*'s session (or None)."""
    runtime = tool._runtime()
    if runtime is None:
        return None
    job = resolve_job_ctx(tool._session_id, runtime)
    return job or resolve_qa_ctx(tool._session_id, runtime)


class _EntityBuilder:
    """Atomic builder: compose the entity engine over a store (offline-safe).

    Stateless static factory shared by the entity tools. ``build_resolver`` is
    the read-only primitive; ``build_minter`` adds the write core on top so the
    read-only ``resolve_entity`` probe never constructs + discards a minter.
    """

    @staticmethod
    def _embedder() -> Any:
        """Resolve an embedder, falling back to the BM25-only null object."""
        embedder = _make_embedder()
        if embedder is None:
            from mewbo_graph.wiki.memory import _NullEmbedder  # noqa: PLC0415

            embedder = _NullEmbedder()
        return embedder

    @staticmethod
    def build_resolver(store: Any) -> Any:
        """Build an ``EntityResolver`` over *store* (read-only path)."""
        from mewbo_graph.entities.resolver import EntityResolver  # noqa: PLC0415

        embedder = _EntityBuilder._embedder()
        return EntityResolver(store=store, embedder=embedder)

    @staticmethod
    def build_minter(store: Any) -> Any:
        """Build an ``EntityMinter`` (resolve → upsert) over *store* (write path)."""
        from mewbo_graph.entities.minter import EntityMinter  # noqa: PLC0415

        embedder = _EntityBuilder._embedder()
        from mewbo_graph.entities.resolver import EntityResolver  # noqa: PLC0415

        resolver = EntityResolver(store=store, embedder=embedder)
        return EntityMinter(store=store, embedder=embedder, resolver=resolver)


class MintEntityTool(WikiSessionTool):
    """SessionTool: mint (resolve → upsert) one abstract entity."""

    tool_id = "mint_entity"
    args_cls = MintEntityArgs
    schema: dict[str, Any] = pydantic_to_openai_tool(MintEntityArgs, name="mint_entity")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``mint_entity`` tool call (resolution runs inside)."""
        if not _entities_enabled():
            return _err_result(
                "validation", "entity layer disabled (wiki.memory.enabled=false)"
            )
        ctx = _ctx_for(self)
        if ctx is None:
            return _err_result("internal", "wiki ctx not found for this session")
        args = self._parse_args(MintEntityArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        from mewbo_graph.entities.types import Entity  # noqa: PLC0415

        minter = _EntityBuilder.build_minter(ctx.store)
        extracted = Entity(
            name=args.name,
            type=args.type,
            description=args.description,
            aliases=args.aliases,
            labels=args.labels,
        )
        entity = minter.upsert(extracted, source=ctx.slug, slug=ctx.slug)
        self._anchor_entity(ctx, entity, args.anchors)
        return MockSpeaker(
            content=json.dumps({"ok": True, "entity": entity.model_dump()})
        )

    @staticmethod
    def _anchor_entity(ctx: Any, entity: Any, anchors: list[str]) -> None:
        """Write entity→AST/entity ANCHORS edges for each resolvable anchor key.

        Ties the entity layer into the AST layer: ``file#Name``/``path`` keys
        resolve to a code node_id via ``CodeStructureProvider``; ``entity:<id>``
        keys resolve to another entity via ``EntityAnchorResolver``. A key that
        resolves to neither is skipped silently (ungrounded). NOTE for the
        graph-view agent: a resulting ``EntityRelation.target_id`` may reference
        either an entity id or an AST node_id — consumers MUST classify it by
        set-membership against the loaded id sets, NEVER by id length.
        """
        if not anchors:
            return
        from mewbo_graph.entities.anchor import EntityAnchorResolver  # noqa: PLC0415
        from mewbo_graph.entities.types import EntityRelation  # noqa: PLC0415
        from mewbo_graph.wiki.structure_provider import (  # noqa: PLC0415
            CodeStructureProvider,
        )

        # Pre-split by key namespace (same convention as graph.py): an
        # ``entity:<id>`` key can never match a code ``entity_key_for_node``, so
        # handing it to CodeStructureProvider only defeats its early-exit and
        # forces a full O(graph_nodes) scan. Each resolver sees only its own keys.
        code_keys = [k for k in anchors if not k.startswith("entity:")]
        ent_keys = [k for k in anchors if k.startswith("entity:")]
        code_nodes = CodeStructureProvider(ctx.store).resolve_many(ctx.slug, code_keys)
        entity_nodes = EntityAnchorResolver(ctx.store).resolve_many(ctx.slug, ent_keys)
        edges = []
        for key in dict.fromkeys(anchors):
            node = code_nodes.get(key)
            target_id = node.node_id if node is not None else None
            if target_id is None:
                ent = entity_nodes.get(key)
                target_id = ent.id if ent is not None else None
            if target_id:
                edges.append(
                    EntityRelation(
                        source_id=entity.id, target_id=target_id, type="ANCHORS"
                    )
                )
        if edges:
            ctx.store.upsert_entity_edges(ctx.slug, edges)


class ResolveEntityTool(WikiSessionTool):
    """SessionTool: dedup-check a name+type against existing entities (no write)."""

    tool_id = "resolve_entity"
    args_cls = ResolveEntityArgs
    schema: dict[str, Any] = pydantic_to_openai_tool(
        ResolveEntityArgs, name="resolve_entity"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``resolve_entity`` probe — surfaces a match, mints nothing."""
        ctx = _ctx_for(self)
        if ctx is None:
            return _err_result("internal", "wiki ctx not found for this session")
        args = self._parse_args(ResolveEntityArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        from mewbo_graph.entities.types import Entity  # noqa: PLC0415

        resolver = _EntityBuilder.build_resolver(ctx.store)
        probe = Entity(name=args.name, type=args.type or "concept")
        decision = resolver.resolve(ctx.slug, probe)
        match = None
        if decision.target_id:
            found = ctx.store.get_entity(ctx.slug, decision.target_id)
            if found is not None:
                match = {
                    "id": found.id,
                    "name": found.name,
                    "type": found.type,
                    "action": decision.action,
                    "score": round(decision.score, 4),
                }
        return MockSpeaker(content=json.dumps({"match": match}))


class RelateEntitiesTool(WikiSessionTool):
    """SessionTool: write one typed relationship edge between two entities."""

    tool_id = "relate_entities"
    args_cls = RelateEntitiesArgs
    schema: dict[str, Any] = pydantic_to_openai_tool(
        RelateEntitiesArgs, name="relate_entities"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``relate_entities`` tool call (one typed, deterministic edge)."""
        if not _entities_enabled():
            return _err_result(
                "validation", "entity layer disabled (wiki.memory.enabled=false)"
            )
        ctx = _ctx_for(self)
        if ctx is None:
            return _err_result("internal", "wiki ctx not found for this session")
        args = self._parse_args(RelateEntitiesArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        from mewbo_graph.entities.types import EntityRelation  # noqa: PLC0415

        rel = EntityRelation(
            source_id=args.source,
            target_id=args.target,
            type=args.relation_type,
            description=args.description,
        )
        ctx.store.upsert_entity_edges(ctx.slug, [rel])
        return MockSpeaker(
            content=json.dumps({"ok": True, "relation": rel.model_dump()})
        )


__all__ = [
    "MintEntityArgs",
    "MintEntityTool",
    "RelateEntitiesArgs",
    "RelateEntitiesTool",
    "ResolveEntityArgs",
    "ResolveEntityTool",
]
