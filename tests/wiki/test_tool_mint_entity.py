"""mint_entity / relate_entities / resolve_entity SessionTools.

Drives the real tool ``handle`` path through a real ``JsonWikiStore`` +
``EntityMinter``/``EntityResolver``; only the runtime/ctx seam and the embedder
I/O boundary are stubbed (per the repo's testing philosophy). Resolution and
provenance run INSIDE the tool — the agent only decides what to mint/relate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

from mewbo_core.classes import ActionStep
from mewbo_graph.entities.types import EntityEmbedding
from mewbo_graph.plugins.wiki import mint_entity as mod
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import GraphNode

SLUG = "org/repo"


class _FakeEmbedder:
    model = "fake"

    def embed_query(self, text):
        return [1.0, 0.0]

    def embed_nodes(self, items, *, slug=""):
        return [
            EntityEmbedding(slug=slug, entity_id=nid, vector=[1.0, 0.0], model="fake", dim=2)
            for nid, _t in items
        ]


class _DistinctEmbedder:
    """Vectors that differ per surface name → the resolver keeps entities apart.

    The shared ``_FakeEmbedder`` returns one constant vector, which deliberately
    forces a merge in the dedup-match test; tests that mint several distinct
    entities use this one so resolution doesn't collapse them.
    """

    model = "fake"

    @staticmethod
    def _vec(text):
        # A one-hot vector keyed on the name → distinct names are orthogonal
        # (cosine 0), so the resolver never spuriously merges them.
        hot = hashlib.sha1(text.encode()).digest()[0] % 16
        return [1.0 if i == hot else 0.0 for i in range(16)]

    def embed_query(self, text):
        return self._vec(text)

    def embed_nodes(self, items, *, slug=""):
        return [
            EntityEmbedding(
                slug=slug, entity_id=nid, vector=self._vec(name), model="fake", dim=2
            )
            for nid, name in items
        ]


def _job_ctx(store):
    return SimpleNamespace(
        slug=SLUG, store=store, session_id="s1", job_id="j1", clone_dir=None
    )


def _patch_ctx(monkeypatch, store, embedder=None):
    monkeypatch.setattr(mod, "_resolve_runtime", lambda: SimpleNamespace(wiki_store=store))
    monkeypatch.setattr(mod, "resolve_job_ctx", lambda sid, rt: _job_ctx(store))
    monkeypatch.setattr(mod, "resolve_qa_ctx", lambda sid, rt: None)
    monkeypatch.setattr(mod, "_make_embedder", lambda: embedder or _FakeEmbedder())


def _run(tool, tool_input):
    step = ActionStep(tool_id=tool.tool_id, operation="call", tool_input=tool_input)
    return asyncio.run(tool.handle(step))


def test_mint_entity_creates_and_returns_id(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store)
    res = _run(mod.MintEntityTool("s1"), {"name": "Ada Lovelace", "type": "person"})
    payload = json.loads(res.content)
    assert payload["ok"] is True
    eid = payload["entity"]["id"]
    stored = store.get_entity(SLUG, eid)
    assert stored is not None
    # Provenance was stamped inside the tool (one mention from this mint).
    assert stored.mentions and stored.mentions[0].source == SLUG


def test_mint_entity_is_idempotent_on_resurface(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store)
    _run(mod.MintEntityTool("s1"), {"name": "Ada", "type": "person"})
    _run(mod.MintEntityTool("s1"), {"name": "ada", "type": "person"})  # same id
    assert len(store.query_entities(SLUG)) == 1


def test_resolve_entity_dedup_check_surfaces_match(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store)
    _run(mod.MintEntityTool("s1"), {"name": "Ada Lovelace", "type": "person"})
    res = _run(mod.ResolveEntityTool("s1"), {"name": "Augusta Ada King", "type": "person"})
    payload = json.loads(res.content)
    # Identical FakeEmbedder vector ⇒ a merge/flag match is surfaced; mints nothing.
    assert payload["match"] is not None
    assert payload["match"]["action"] in {"merge", "flag"}
    assert len(store.query_entities(SLUG)) == 1  # resolve_entity never writes


def test_resolve_entity_no_match_is_none(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store)
    res = _run(mod.ResolveEntityTool("s1"), {"name": "Nobody", "type": "person"})
    assert json.loads(res.content)["match"] is None


def test_relate_entities_writes_typed_edge(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store)
    a = json.loads(
        _run(mod.MintEntityTool("s1"), {"name": "Ada", "type": "person"}).content
    )["entity"]["id"]
    b = json.loads(
        _run(mod.MintEntityTool("s1"), {"name": "Analytical Engine", "type": "project"}).content
    )["entity"]["id"]
    res = _run(
        mod.RelateEntitiesTool("s1"),
        {"source": a, "target": b, "relation_type": "works_on"},
    )
    payload = json.loads(res.content)
    assert payload["ok"] is True
    edges = store.list_entity_edges(SLUG, source_id=a)
    assert edges and edges[0].type == "works_on" and edges[0].target_id == b


def test_mint_entity_missing_ctx_errors(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    monkeypatch.setattr(mod, "_resolve_runtime", lambda: SimpleNamespace(wiki_store=store))
    monkeypatch.setattr(mod, "resolve_job_ctx", lambda sid, rt: None)
    monkeypatch.setattr(mod, "resolve_qa_ctx", lambda sid, rt: None)
    res = _run(mod.MintEntityTool("s1"), {"name": "Ada", "type": "person"})
    assert "error" in res.content


def test_mint_entity_validation_error_on_blank_name(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store)
    res = _run(mod.MintEntityTool("s1"), {"name": "", "type": "person"})
    assert "validation" in res.content


def test_mint_entity_labels_round_trip_through_store(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store)
    res = _run(
        mod.MintEntityTool("s1"),
        {"name": "Operator", "type": "role", "labels": ["actor", "persona"]},
    )
    eid = json.loads(res.content)["entity"]["id"]
    stored = store.get_entity(SLUG, eid)
    assert stored is not None
    assert stored.labels == ["actor", "persona"]


def test_mint_entity_labels_union_on_idempotent_resurface(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    # Same surface (id-identical) ⇒ the _apply_new idempotent-fold path. A
    # re-index re-mints the same name, so newly-added labels must accrue too.
    _patch_ctx(monkeypatch, store)
    first = json.loads(
        _run(
            mod.MintEntityTool("s1"), {"name": "Ada", "type": "person", "labels": ["actor"]}
        ).content
    )["entity"]["id"]
    _run(
        mod.MintEntityTool("s1"),
        {"name": "ada", "type": "person", "labels": ["persona"]},
    )
    assert len(store.query_entities(SLUG)) == 1  # converged, not a new node
    stored = store.get_entity(SLUG, first)
    assert stored is not None
    # Both labels survive, first-seen order — the fold unions, never drops.
    assert stored.labels == ["actor", "persona"]


def test_mint_entity_labels_union_on_merge(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    # The constant _FakeEmbedder yields one vector for every entity, so a second
    # DISTINCT-id surface resolves to a MERGE into the first — the path the
    # review flagged: _apply_merge previously updated aliases but dropped labels.
    _patch_ctx(monkeypatch, store)
    first = json.loads(
        _run(
            mod.MintEntityTool("s1"),
            {"name": "Ada Lovelace", "type": "person", "labels": ["actor"]},
        ).content
    )["entity"]["id"]
    _run(
        mod.MintEntityTool("s1"),
        {"name": "Augusta Ada King", "type": "person", "labels": ["persona"]},
    )
    assert len(store.query_entities(SLUG)) == 1  # merged into the survivor
    survivor = store.get_entity(SLUG, first)
    assert survivor is not None
    assert survivor.labels == ["actor", "persona"]


def test_mint_user_story_entity_and_relation(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store, embedder=_DistinctEmbedder())
    actor = json.loads(
        _run(mod.MintEntityTool("s1"), {"name": "Operator", "type": "role"}).content
    )["entity"]["id"]
    story = json.loads(
        _run(
            mod.MintEntityTool("s1"),
            {
                "name": "re-index a private repo",
                "type": "user-story",
                "labels": ["user-story"],
            },
        ).content
    )["entity"]["id"]
    story_entity = store.get_entity(SLUG, story)
    assert story_entity is not None and story_entity.labels == ["user-story"]
    _run(
        mod.RelateEntitiesTool("s1"),
        {"source": actor, "target": story, "relation_type": "wants"},
    )
    edges = store.list_entity_edges(SLUG, source_id=actor)
    assert edges and edges[0].type == "wants" and edges[0].target_id == story


def test_mint_entity_anchors_write_ast_anchors_edge(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.upsert_nodes(
        SLUG,
        [
            GraphNode(
                slug=SLUG,
                node_id="n_authsvc",
                type="Class",
                name="AuthService",
                file="auth.py",
                range=(0, 9),
            )
        ],
    )
    _patch_ctx(monkeypatch, store)
    eid = json.loads(
        _run(
            mod.MintEntityTool("s1"),
            {
                "name": "Authentication",
                "type": "concept",
                "anchors": ["auth.py#AuthService"],
            },
        ).content
    )["entity"]["id"]
    edges = store.list_entity_edges(SLUG, source_id=eid)
    # The anchor resolved to the AST node_id (NOT an entity id) → ANCHORS edge.
    assert edges and edges[0].type == "ANCHORS"
    assert edges[0].target_id == "n_authsvc"


def test_mint_entity_anchors_to_another_entity(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store, embedder=_DistinctEmbedder())
    target = json.loads(
        _run(mod.MintEntityTool("s1"), {"name": "Auth", "type": "concept"}).content
    )["entity"]["id"]
    src = json.loads(
        _run(
            mod.MintEntityTool("s1"),
            {"name": "Login", "type": "concept", "anchors": [f"entity:{target}"]},
        ).content
    )["entity"]["id"]
    edges = store.list_entity_edges(SLUG, source_id=src)
    assert edges and edges[0].type == "ANCHORS" and edges[0].target_id == target


def test_mint_entity_unresolvable_anchor_skipped_silently(tmp_path, monkeypatch):
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    _patch_ctx(monkeypatch, store)
    eid = json.loads(
        _run(
            mod.MintEntityTool("s1"),
            {"name": "Ghost", "type": "concept", "anchors": ["nope.py#Missing"]},
        ).content
    )["entity"]["id"]
    assert store.list_entity_edges(SLUG, source_id=eid) == []
