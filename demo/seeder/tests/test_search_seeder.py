"""Tests for the agentic-search seeder (``mewbo_demo_seeder.search``).

Drives the real code paths: the Pydantic ``SearchBundle`` contract, T0-offset
rebasing against an INJECTED fixed clock (no time patching — the seeder takes
``t0`` as a field), a full ``seed()`` into mongomock-backed stores asserting the
durable snapshot + replayable event log the console reads, and the SCG /
memory layers projected through the REAL ``ScgGraphView`` so the landing
capability-graph band counts are asserted end to end.

All three stores (agentic_search / SCG / wiki-memory) accept a ``client=``
mongomock injection, so — unlike the session/trigger stores — no ``MongoClient``
monkeypatch is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest
from mewbo_api.agentic_search.store import MongoAgenticSearchStore
from mewbo_demo_seeder.search import SearchBundle, SearchSeeder
from mewbo_graph.scg.graph_view import ScgGraphView
from mewbo_graph.scg.store import MongoScgStore
from mewbo_graph.wiki.store import MongoWikiStore
from pydantic import ValidationError

T0 = datetime(2026, 7, 14, 9, 30, 0, tzinfo=timezone.utc)

_BUNDLE_PATH = (
    Path(__file__).resolve().parents[1] / "bundles" / "search-poc.json"
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def search_store() -> MongoAgenticSearchStore:
    """A MongoAgenticSearchStore backed by mongomock (client injection)."""
    return MongoAgenticSearchStore(client=mongomock.MongoClient(), database="test_search")


@pytest.fixture
def scg_store() -> MongoScgStore:
    """A MongoScgStore backed by mongomock."""
    return MongoScgStore(client=mongomock.MongoClient(), database="test_search")


@pytest.fixture
def wiki_store() -> MongoWikiStore:
    """A MongoWikiStore backed by mongomock (the connector memory layer)."""
    return MongoWikiStore(client=mongomock.MongoClient(), database="test_search")


@pytest.fixture
def canonical() -> SearchBundle:
    """The committed demo bundle, validated."""
    return SearchBundle.model_validate(json.loads(_BUNDLE_PATH.read_text()))


def _minimal() -> dict:
    """A tiny but complete two-workspace + one-run + SCG bundle as raw JSON."""
    return {
        "workspaces": [
            {
                "id": "ws-a",
                "name": "Alpha",
                "sources": ["github", "filesystem"],
                "created_offset": -864000,
                "past_queries": [
                    {"q": "hello", "ran_at_offset": -3600, "results": 3,
                     "run_id": "run-a", "status": "completed"}
                ],
            },
            {"id": "ws-b", "name": "Beta", "sources": ["web"], "created_offset": -172800},
        ],
        "runs": [
            {
                "run_id": "run-a",
                "workspace_id": "ws-a",
                "query": "hello",
                "tier": "auto",
                "model": "gpt-oss-120b",
                "created_offset": -3600,
                "total_ms": 4200,
                "source_ids": ["github"],
                "answer": {"tldr": "Hi.", "bullets": [{"text": "b", "cites": ["r1"]}],
                           "confidence": 0.8, "sources_count": 1},
                "results": [{"id": "r1", "source": "github", "kind": "code",
                             "relevance": 0.9, "title": "anthropics/claude-code",
                             "url": "github.com/anthropics/claude-code", "snippet": "s",
                             "meta": {"stars": 133000, "language": "Python"}}],
                "trace": [{"id": "a1", "agent_id": "a1", "name": "scg-search",
                           "source_id": "github", "slot": 0, "kind": "scg-path-probe",
                           "model": "gpt-oss-120b", "results_count": 1, "returned_count": 2,
                           "steps": 3, "duration_ms": 4200, "input_tokens": 1200,
                           "output_tokens": 300,
                           "lines": [{"t_ms": 100, "glyph": ">", "text": "search"}]}],
                "related_questions": ["How does it compare to Copilot?"],
            }
        ],
        "scg": {
            "memory_workspace": "ws-a",
            "sources": [{"source_id": "github", "tools": [
                {"name": "search_repositories", "input_fields": ["q"], "required": ["q"]},
                {"name": "list_commits"},
            ]}],
            "memory": [
                {"content": "ranks by stars", "anchor": "github#search_repositories",
                 "relates_to": [1]},
                {"content": "confirms activity", "anchor": "github#list_commits",
                 "polarity": "dead_end"},
            ],
        },
    }


# ---------------------------------------------------------------------------
# bundle contract (validation at definition)
# ---------------------------------------------------------------------------


def test_minimal_bundle_round_trips():
    """A well-formed bundle validates and preserves its structure."""
    bundle = SearchBundle.model_validate(_minimal())
    assert [w.id for w in bundle.workspaces] == ["ws-a", "ws-b"]
    assert bundle.runs[0].session_id == "agentic_search:run:run-a"


def test_bundle_rejects_unknown_field():
    """An unknown key is a clean ValidationError (extra=forbid at every boundary)."""
    raw = _minimal()
    raw["workspaces"][0]["token"] = "smuggled"
    with pytest.raises(ValidationError):
        SearchBundle.model_validate(raw)


def test_bundle_rejects_run_referencing_unknown_workspace():
    """A run must point at a workspace the bundle declares."""
    raw = _minimal()
    raw["runs"][0]["workspace_id"] = "ghost"
    with pytest.raises(ValidationError):
        SearchBundle.model_validate(raw)


def test_bundle_rejects_pastquery_referencing_unknown_run():
    """A past-query chip's run_id must resolve to a seeded run."""
    raw = _minimal()
    raw["workspaces"][0]["past_queries"][0]["run_id"] = "ghost"
    with pytest.raises(ValidationError):
        SearchBundle.model_validate(raw)


def test_scg_rejects_unanchored_memory():
    """A memory note must anchor onto a capability this graph seeds."""
    raw = _minimal()
    raw["scg"]["memory"][0]["anchor"] = "github#does_not_exist"
    with pytest.raises(ValidationError):
        SearchBundle.model_validate(raw)


# ---------------------------------------------------------------------------
# offset rebasing (injected fixed T0)
# ---------------------------------------------------------------------------


def test_workspace_offsets_rebase_against_injected_t0(search_store):
    """created_at / updated_at / ran_at are T0 + the bundle offsets."""
    bundle = SearchBundle.model_validate(_minimal())
    SearchSeeder(search_store=search_store, bundle=bundle, t0=T0).seed()

    ws = search_store.get_workspace("ws-a")
    assert ws.created_at == (T0 + timedelta(seconds=-864000)).isoformat()
    assert ws.updated_at == ws.created_at  # updated_offset defaults to created
    assert ws.past_queries[0].ran_at == (T0 + timedelta(seconds=-3600)).isoformat()
    # The chip deep-links to its run.
    assert ws.past_queries[0].run_id == "run-a"


def test_run_created_at_rebases_and_status_is_completed(search_store):
    """The durable run record rebases its timestamps and reads terminal."""
    bundle = SearchBundle.model_validate(_minimal())
    SearchSeeder(search_store=search_store, bundle=bundle, t0=T0).seed()

    rec = search_store.get_run("run-a")
    assert rec.status == "completed"
    assert rec.created_at == (T0 + timedelta(seconds=-3600)).isoformat()
    assert rec.completed_at > rec.created_at
    assert rec.session_id == "agentic_search:run:run-a"


# ---------------------------------------------------------------------------
# snapshot self-sufficiency + replayable event log
# ---------------------------------------------------------------------------


def test_run_snapshot_is_self_sufficient(search_store):
    """GET /runs/<id> renders with no other context — payload carries everything."""
    bundle = SearchBundle.model_validate(_minimal())
    SearchSeeder(search_store=search_store, bundle=bundle, t0=T0).seed()

    payload = search_store.get_run("run-a").payload
    assert payload.status == "completed"
    assert [r.id for r in payload.results] == ["r1"]
    assert payload.results[0].meta == {"stars": 133000, "language": "Python"}
    assert [a.agent_id for a in payload.trace] == ["a1"]
    assert payload.related_questions == ["How does it compare to Copilot?"]


def test_event_log_replays_the_full_normalized_stream(search_store):
    """The seeded event log is the echo-runner sequence a deep-link replays."""
    bundle = SearchBundle.model_validate(_minimal())
    SearchSeeder(search_store=search_store, bundle=bundle, t0=T0).seed()

    events = search_store.load_run_events("run-a")
    types = [e["type"] for e in events]
    assert types == [
        "run_started", "agent_start", "agent_line", "agent_done",
        "result", "answer_ready", "related_questions", "run_done",
    ]
    # The terminal frame closes the SSE immediately (no dangling stream).
    assert events[-1]["type"] == "run_done"
    assert events[-1]["status"] == "completed"


def test_agent_events_carry_lane_instrument_fields(search_store):
    """A replayed lane keeps kind/model (on start) + steps/tokens (on done)."""
    bundle = SearchBundle.model_validate(_minimal())
    SearchSeeder(search_store=search_store, bundle=bundle, t0=T0).seed()

    events = search_store.load_run_events("run-a")
    start = next(e for e in events if e["type"] == "agent_start")
    done = next(e for e in events if e["type"] == "agent_done")
    assert start["kind"] == "scg-path-probe" and start["model"] == "gpt-oss-120b"
    assert done["steps"] == 3 and done["input_tokens"] == 1200
    assert done["results_count"] == 1 and done["returned_count"] == 2


# ---------------------------------------------------------------------------
# idempotency (byte-identical re-seed)
# ---------------------------------------------------------------------------


def test_reseed_is_byte_identical(search_store, scg_store, wiki_store):
    """Seeding twice leaves an identical event log + snapshot (no duplicates)."""
    bundle = SearchBundle.model_validate(_minimal())
    seeder = SearchSeeder(
        search_store=search_store, bundle=bundle, t0=T0,
        scg_store=scg_store, wiki_store=wiki_store,
    )
    seeder.seed()
    first_events = search_store.load_run_events("run-a")
    first_snap = search_store.get_run("run-a").model_dump()
    seeder.seed()
    assert search_store.load_run_events("run-a") == first_events  # no re-append
    assert search_store.get_run("run-a").model_dump() == first_snap


# ---------------------------------------------------------------------------
# SCG capability-graph band (through the real graph view)
# ---------------------------------------------------------------------------


def test_scg_graph_band_counts(canonical, search_store, scg_store, wiki_store):
    """OSS Repo Scout's capability graph renders 86 nodes / 95 edges / 32 memory."""
    report = SearchSeeder(
        search_store=search_store, bundle=canonical, t0=T0,
        scg_store=scg_store, wiki_store=wiki_store,
    ).seed()
    assert report.memory_notes == 32
    assert report.scg_sources == [
        "github", "huggingface", "gitmcp", "deepwiki", "context7", "internet-search",
    ]

    oss = next(w for w in canonical.workspaces if w.id == "ws-oss-repo-scout")
    wire = ScgGraphView.for_scope(scg_store, wiki_store, list(oss.sources)).to_wire()
    stats = wire["stats"]
    assert stats["totalNodes"] == 86
    assert stats["totalEdges"] == 95
    assert stats["perLayer"] == {"schema": 54, "memory": 32, "entity": 0}
    # 6 mapped source hubs + 48 capability nodes; filesystem is the unmapped 7th
    # (zero schema nodes -> rendered as a ghost by the route, never a real node).
    assert stats["kinds"] == {"source": 6, "capability": 48}
    schema_source_ids = {n["data"].get("sourceId") for n in wire["nodes"]}
    assert "filesystem" not in schema_source_ids
    assert set(wire["scope"]) == set(oss.sources)


def test_seed_without_graph_stores_still_seeds_workspaces_and_runs(canonical, search_store):
    """A graph-less install seeds the full search world; only the SCG is skipped."""
    report = SearchSeeder(search_store=search_store, bundle=canonical, t0=T0).seed()
    assert len(report.workspaces) == 8
    assert len(report.runs) == 10
    assert report.scg_nodes == 0 and report.memory_notes == 0
    assert search_store.get_run("run-oss-agents").status == "completed"


# ---------------------------------------------------------------------------
# canonical bundle
# ---------------------------------------------------------------------------


def test_canonical_bundle_shape(canonical):
    """The committed bundle carries the contract workspaces + runs."""
    assert [w.name for w in canonical.workspaces][:3] == [
        "OSS Repo Scout", "Knowledge graph", "Beacon Ops",
    ]
    assert {r.run_id for r in canonical.runs} >= {
        "run-oss-agents", "run-cc-plugins", "run-bearlike",
    }
    # The two Claude-Code research runs own OSS Repo Scout's chips.
    oss = next(w for w in canonical.workspaces if w.id == "ws-oss-repo-scout")
    linked = {pq.run_id for pq in oss.past_queries if pq.run_id}
    assert {"run-oss-agents", "run-cc-plugins", "run-bearlike"} <= linked


def test_canonical_full_seed_reports(canonical, search_store, scg_store, wiki_store):
    """The whole bundle seeds 8 workspaces, 10 runs, 54 SCG nodes, 32 memory."""
    report = SearchSeeder(
        search_store=search_store, bundle=canonical, t0=T0,
        scg_store=scg_store, wiki_store=wiki_store,
    ).seed()
    assert len(report.workspaces) == 8
    assert len(report.runs) == 10
    assert report.scg_nodes == 54  # 6 source hubs + 48 capabilities
    assert report.memory_notes == 32
    # Every run's snapshot is a completed, self-sufficient deep-link target.
    for run in canonical.runs:
        rec = search_store.get_run(run.run_id)
        assert rec.status == "completed"
        assert rec.payload is not None
