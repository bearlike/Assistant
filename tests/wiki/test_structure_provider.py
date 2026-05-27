"""CodeStructureProvider — entity_key ↔ code-node resolution."""
from __future__ import annotations

import pytest
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.structure_provider import (
    CodeStructureProvider,
    StructureProvider,
    entity_key_for_node,
)
from mewbo_graph.wiki.types import GraphNode

SLUG = "org/repo"


def _gn(nid: str, typ: str, name: str, f: str = "auth.py") -> GraphNode:
    return GraphNode(slug=SLUG, node_id=nid, type=typ, name=name, file=f, range=(0, 9))


@pytest.fixture
def store(tmp_path):
    s = JsonWikiStore(root_dir=tmp_path / "wiki")
    s.upsert_nodes(
        SLUG,
        [
            _gn("fA", "File", "auth.py"),
            _gn("cA", "Class", "AuthService"),
            _gn("mA", "Method", "verify"),
        ],
    )
    return s


def test_entity_key_for_file_node_is_bare_path() -> None:
    assert entity_key_for_node(_gn("f", "File", "auth.py")) == "auth.py"


def test_entity_key_for_symbol_node_is_file_hash_name() -> None:
    assert entity_key_for_node(_gn("c", "Class", "AuthService")) == "auth.py#AuthService"


def test_resolve_symbol(store) -> None:
    provider = CodeStructureProvider(store)
    node = provider.resolve(SLUG, "auth.py#AuthService")
    assert node is not None and node.node_id == "cA"


def test_resolve_file(store) -> None:
    provider = CodeStructureProvider(store)
    node = provider.resolve(SLUG, "auth.py")
    assert node is not None and node.type == "File"


def test_resolve_missing_returns_none(store) -> None:
    provider = CodeStructureProvider(store)
    assert provider.resolve(SLUG, "auth.py#Nonexistent") is None


def test_resolve_many_omits_misses(store) -> None:
    provider = CodeStructureProvider(store)
    got = provider.resolve_many(
        SLUG, ["auth.py#AuthService", "auth.py#verify", "auth.py#ghost"]
    )
    assert set(got) == {"auth.py#AuthService", "auth.py#verify"}
    assert got["auth.py#verify"].node_id == "mA"


def test_entity_key_of_node_id(store) -> None:
    provider = CodeStructureProvider(store)
    assert provider.entity_key_of(SLUG, "cA") == "auth.py#AuthService"
    assert provider.entity_key_of(SLUG, "ghost") is None


def test_code_provider_satisfies_protocol(store) -> None:
    provider = CodeStructureProvider(store)
    assert isinstance(provider, StructureProvider)
