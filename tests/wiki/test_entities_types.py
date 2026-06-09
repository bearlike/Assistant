"""Entity domain models — deterministic id, soft type, per-mention provenance.

Mirrors ``test_memory_types.py`` conventions: extra-forbid enforcement and the
deterministic id derivation (``Entity.compute_id`` / ``EntityRelation.compute_id``)
that the idempotent-upsert convergence guarantee relies on.
"""
from __future__ import annotations

import hashlib

import pytest
from mewbo_graph.entities.types import (
    Entity,
    EntityEmbedding,
    EntityFilter,
    EntityMention,
    EntityRecommendation,
    EntityRelation,
    normalize_entity_name,
)
from pydantic import ValidationError


def test_normalize_trims_lowers_collapses_ws_strips_punct():
    assert normalize_entity_name("  Ada   Lovelace! ") == "ada lovelace"
    assert normalize_entity_name("ACME, Inc.") == "acme inc"


def test_entity_id_is_sha1_of_normalized_name_pipe_type():
    e = Entity(name="Ada Lovelace", type="person")
    expected = hashlib.sha1(b"ada lovelace|person").hexdigest()
    assert e.normalized_name == "ada lovelace"
    assert e.id == expected


def test_entity_id_overwrites_supplied_id():
    e = Entity(id="bogus", name="Ada Lovelace", type="person")
    assert e.id == hashlib.sha1(b"ada lovelace|person").hexdigest()


def test_entity_id_is_stable_across_surface_variants():
    a = Entity(name="ACME, Inc.", type="organization")
    b = Entity(name="  acme   inc ", type="organization")
    assert a.id == b.id  # deterministic-id idempotency


def test_entity_type_is_soft_freeform_string():
    e = Entity(name="Robotics 101", type="course")  # not in seed vocab
    assert e.type == "course"


def test_entity_rejects_extra_fields():
    with pytest.raises(ValidationError):
        Entity(name="X", type="concept", bogus=1)


def test_entity_default_status_active():
    assert Entity(name="X", type="concept").status == "active"


def test_relation_id_is_sha1_of_source_type_target():
    r = EntityRelation(source_id="aaa", target_id="bbb", type="works_on")
    expected = hashlib.sha1(b"aaa|works_on|bbb").hexdigest()
    assert r.id == expected


def test_relation_rejects_extra_fields():
    with pytest.raises(ValidationError):
        EntityRelation(source_id="a", target_id="b", type="t", bogus=1)


def test_mention_and_recommendation_roundtrip():
    m = EntityMention(
        source="auth.py", insight_id=None, ts="2026-06-07T00:00:00Z", surface_name="Ada"
    )
    e = Entity(name="Ada", type="person", mentions=[m])
    assert e.mentions[0].surface_name == "Ada"
    rec = EntityRecommendation(
        action="merge",
        subjects=["ada|person", "a. lovelace|person"],
        type=None,
        rationale="same person",
    )
    assert rec.action == "merge"
    assert rec.subjects[0] == "ada|person"


def test_recommendation_rejects_unknown_action():
    with pytest.raises(ValidationError):
        EntityRecommendation(action="bogus", subjects=["a"])


def test_embedding_roundtrip():
    emb = EntityEmbedding(slug="org/repo", entity_id="e1", vector=[1.0, 0.0], model="f", dim=2)
    assert emb.dim == 2
    assert EntityEmbedding.model_validate(emb.model_dump()) == emb


def test_filter_matches_type_status_labels():
    e = Entity(name="A", type="person", status="active", labels=["x", "y"])
    assert EntityFilter().matches(e)
    assert EntityFilter(type="person").matches(e)
    assert not EntityFilter(type="project").matches(e)
    assert EntityFilter(status="active").matches(e)
    assert not EntityFilter(status="needs_review").matches(e)
    assert EntityFilter(labels=["x"]).matches(e)
    assert not EntityFilter(labels=["z"]).matches(e)
