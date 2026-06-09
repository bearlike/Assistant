"""EntityAnchorResolver — lets notes anchor to entities and back (multiplex glue).

Conforms to the wiki ``AnchorResolver`` runtime-checkable Protocol
(``resolve`` / ``resolve_many`` / ``entity_key_of``) over ``entity:<id>`` keys,
exactly like ``ScgAnchorResolver``. Tested against an in-memory FAKE store.
"""
from __future__ import annotations

from mewbo_graph.entities.anchor import EntityAnchorResolver, entity_key_for
from mewbo_graph.entities.types import Entity
from mewbo_graph.wiki.memory import AnchorResolver

SLUG = "org/repo"


class FakeStore:
    """Minimal store exposing ``get_entity`` — the only surface the resolver needs."""

    def __init__(self):
        self._entities: dict[str, dict[str, Entity]] = {}

    def upsert_entities(self, slug, entities):
        bucket = self._entities.setdefault(slug, {})
        for e in entities:
            bucket[e.id] = e

    def get_entity(self, slug, entity_id):
        return self._entities.get(slug, {}).get(entity_id)


def test_entity_key_form():
    e = Entity(name="Ada", type="person")
    assert entity_key_for(e) == f"entity:{e.id}"


def test_resolver_conforms_to_anchor_protocol():
    r = EntityAnchorResolver(FakeStore())
    assert isinstance(r, AnchorResolver)  # runtime_checkable Protocol


def test_resolve_returns_entity_or_none():
    s = FakeStore()
    ada = Entity(name="Ada", type="person")
    s.upsert_entities(SLUG, [ada])
    r = EntityAnchorResolver(s)
    assert r.resolve(SLUG, entity_key_for(ada)) == ada
    assert r.resolve(SLUG, "entity:missing") is None
    assert r.resolve(SLUG, "not-an-entity-key") is None  # wrong prefix → None


def test_resolve_many_returns_live_entities():
    s = FakeStore()
    ada = Entity(name="Ada", type="person")
    s.upsert_entities(SLUG, [ada])
    r = EntityAnchorResolver(s)
    key = entity_key_for(ada)
    resolved = r.resolve_many(SLUG, [key, "entity:missing"])
    assert key in resolved and "entity:missing" not in resolved
    assert resolved[key] == ada


def test_resolve_many_dedups_repeated_keys():
    s = FakeStore()
    ada = Entity(name="Ada", type="person")
    s.upsert_entities(SLUG, [ada])
    r = EntityAnchorResolver(s)
    key = entity_key_for(ada)
    resolved = r.resolve_many(SLUG, [key, key])
    assert list(resolved) == [key]


def test_entity_key_of_roundtrips():
    s = FakeStore()
    ada = Entity(name="Ada", type="person")
    s.upsert_entities(SLUG, [ada])
    r = EntityAnchorResolver(s)
    assert r.entity_key_of(SLUG, ada.id) == entity_key_for(ada)
    assert r.entity_key_of(SLUG, "missing") is None
