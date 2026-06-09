"""EntityMinter — the DRY write core for abstract entities.

normalize → resolve (shared ladder) → upsert-with-provenance. The deterministic
id makes every write an UPSERT, so a re-index converges and never duplicates.
A ``merge`` decision adds the surface name as an alias + appends a mention; a
``flag`` decision writes a new entity with ``status=needs_review`` plus a SAME_AS
edge to the flagged neighbour. All collaborators are injected.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from mewbo_graph._util import utc_now_iso

from .resolver import EntityResolver, LadderDecision
from .types import Entity, EntityEmbedding, EntityMention, EntityRelation

if TYPE_CHECKING:
    from mewbo_graph.wiki.embedder import EmbedderProtocol
    from mewbo_graph.wiki.store import WikiStoreBase


class EntityMinter:
    """Upsert extracted entities into the multiplex with resolution + provenance."""

    def __init__(
        self,
        *,
        store: WikiStoreBase,
        embedder: EmbedderProtocol,
        resolver: EntityResolver,
        clock: Callable[[], str] | None = None,
    ) -> None:
        """Wire collaborators (all injected); ``clock`` is overridable for tests."""
        self._store = store
        self._embedder = embedder
        self._resolver = resolver
        self._clock = clock or utc_now_iso

    def upsert(
        self,
        extracted: Entity,
        *,
        source: str,
        slug: str,
        insight_id: str | None = None,
    ) -> Entity:
        """Resolve *extracted* and upsert it (create / merge / flag)."""
        now = self._clock()
        mention = EntityMention(
            source=source, insight_id=insight_id, ts=now, surface_name=extracted.name
        )
        decision = self._resolver.resolve(slug, extracted)

        if decision.action == "merge" and decision.target_id:
            return self._apply_merge(slug, extracted, decision, mention)
        if decision.action == "flag" and decision.target_id:
            return self._apply_flag(slug, extracted, decision, mention)
        return self._apply_new(slug, extracted, mention, status="active")

    def _apply_new(
        self, slug: str, extracted: Entity, mention: EntityMention, *, status: str
    ) -> Entity:
        # Deterministic id ⇒ a re-mint of the SAME surface is an idempotent
        # convergence, NOT a fresh node: fold the mention into the existing
        # record so provenance accumulates instead of being overwritten.
        prior = self._store.get_entity(slug, extracted.id)
        if prior is not None:
            labels = list(dict.fromkeys([*prior.labels, *extracted.labels]))
            entity = prior.model_copy(
                update={"mentions": [*prior.mentions, mention], "labels": labels}
            )
        else:
            entity = extracted.model_copy(update={"mentions": [mention], "status": status})
        self._store.upsert_entities(slug, [entity])
        self._embed(slug, entity)
        return entity

    def _apply_merge(
        self,
        slug: str,
        extracted: Entity,
        decision: LadderDecision,
        mention: EntityMention,
    ) -> Entity:
        target = self._store.get_entity(slug, decision.target_id or "")
        if target is None:  # raced/absent — fall back to a fresh insert
            return self._apply_new(slug, extracted, mention, status="active")
        aliases = list(dict.fromkeys([*target.aliases, extracted.name]))
        labels = list(dict.fromkeys([*target.labels, *extracted.labels]))
        survivor = target.model_copy(
            update={
                "aliases": aliases,
                "labels": labels,
                "mentions": [*target.mentions, mention],
                "description": target.description or extracted.description,
            }
        )
        self._store.upsert_entities(slug, [survivor])
        self._embed(slug, survivor)
        return survivor

    def _apply_flag(
        self,
        slug: str,
        extracted: Entity,
        decision: LadderDecision,
        mention: EntityMention,
    ) -> Entity:
        entity = self._apply_new(slug, extracted, mention, status="needs_review")
        if decision.target_id:
            self._store.upsert_entity_edges(
                slug,
                [
                    EntityRelation(
                        source_id=entity.id, target_id=decision.target_id, type="SAME_AS"
                    )
                ],
            )
        return entity

    def _embed(self, slug: str, entity: Entity) -> None:
        try:
            rows = self._embedder.embed_nodes([(entity.id, entity.name)], slug=slug)
        except Exception:
            return
        if rows:
            vector = list(rows[0].vector)
            self._store.upsert_entity_embeddings(
                slug,
                [
                    EntityEmbedding(
                        slug=slug,
                        entity_id=entity.id,
                        vector=vector,
                        model=getattr(self._embedder, "model", ""),
                        dim=len(vector),
                    )
                ],
            )


__all__ = ["EntityMinter"]
