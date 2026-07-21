#!/usr/bin/env python3
"""Unit tests for the wiki seeder (demo-as-code).

Drives the REAL ``WikiSeeder.seed()`` against a mongomock-backed
``MongoWikiStore`` — the established repo pattern (``tests/wiki/test_store_mongo``)
— asserting the console's read seams see a populated world, and that a re-seed is
byte-identical (the determinism contract the zero-diff screenshot gate rests on).
Everything is validated from the CALLER's site (the store contracts the API
routes read through), stubbing only the Mongo I/O boundary.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest
from mewbo_demo_seeder.wiki import WikiSeedBundle, WikiSeeder
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.store import MongoWikiStore

_BUNDLE_PATH = Path(__file__).resolve().parents[1] / "bundles" / "wiki-poc.json"
_T0 = datetime(2026, 7, 14, 9, 30, 0, tzinfo=timezone.utc)

_GROVE = "github.com/bearlike/Grove"
_ASSISTANT = "github.com/bearlike/Assistant"
# Which IndexingJob.status values the ``GET /v1/wiki/jobs/active`` route keeps.
_ACTIVE = {"queued", "scanning", "finalizing", "interrupted"}
# ...and which ``GET /v1/wiki/jobs/recoverable`` keeps (before the is_noop filter).
_RECOVERABLE = {"failed", "interrupted", "cancelled"}


@pytest.fixture
def bundle() -> WikiSeedBundle:
    """The committed demo bundle, validated through the Pydantic trust boundary."""
    raw = json.loads(_BUNDLE_PATH.read_text(encoding="utf-8"))
    return WikiSeedBundle.model_validate(raw)


@pytest.fixture
def store() -> MongoWikiStore:
    """A MongoWikiStore over an in-memory mongomock client (no real Mongo)."""
    return MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki_seed")


def _seed(store: MongoWikiStore, bundle: WikiSeedBundle) -> None:
    WikiSeeder(wiki_store=store, bundle=bundle, t0=_T0).seed()


# ── The bundle itself validates + carries the reference content ────────────────


def test_bundle_validates_and_matches_reference_contract(bundle: WikiSeedBundle) -> None:
    """The committed fixture loads and pins the flow-author-facing content."""
    assert len(bundle.projects) == 12
    by_slug = {p.slug: p for p in bundle.projects}

    grove = by_slug[_GROVE]
    assert grove.primary is True
    assert grove.landing_page_id == "grove-overview"
    assert grove.graph_nodes == 760
    # Card advertises 72 pages while only ~10 are authored (page_count override).
    assert grove.page_count == 72
    assert len(grove.pages) == 10
    assert grove.page_count >= len(grove.pages)

    # The overview page carries a mermaid flowchart for screenshot 02.
    overview = next(p for p in grove.pages if p.id == "grove-overview")
    assert "```mermaid" in overview.body

    # 1 in-flight (active) job + 16 recoverable ones for a full landing.
    assert len(bundle.jobs) == 17
    statuses = [j.status for j in bundle.jobs]
    assert statuses.count("finalizing") == 1
    # Recoverable jobs are failed/cancelled (NOT interrupted — that is ALSO active,
    # which would exclude them from the "Incomplete indexes" band).
    recoverable = [j for j in bundle.jobs if j.status in {"failed", "cancelled"}]
    assert len(recoverable) == 16
    assert all(j.resume_graph_nodes > 0 for j in recoverable)
    inflight = next(j for j in bundle.jobs if j.status == "finalizing")
    assert inflight.resume_graph_nodes == 0  # the active job seeds no resume graph

    # Two stored Q&A answers (Assistant + Grove).
    assert {a.answer_id for a in bundle.qa} == {
        "qa-assistant-what-is-this-for-0001",
        "qa-grove-worktree-isolation-0001",
    }


# ── The landing gallery (list_projects) ────────────────────────────────────────


def test_seed_populates_landing_gallery(store: MongoWikiStore, bundle: WikiSeedBundle) -> None:
    """``list_projects`` (the landing read) returns every seeded project, dated."""
    _seed(store, bundle)
    projects = store.list_projects()
    assert len(projects) == 12
    # indexed_at is rebased off T0 (deterministic, never now()).
    grove = store.get_project(_GROVE)
    assert grove is not None
    assert grove.indexed_at == "2026-07-12T09:30:00+00:00"  # T0 - 2 days
    assert grove.pages == 72  # card count (override)
    assert grove.graph_only is False
    # Sorted by indexed_at desc → the 3-hours-ago Assistant leads.
    assert projects[0].slug == _ASSISTANT


# ── A project page view (get_page + list_pages + get_project) ──────────────────


def test_seed_populates_grove_pages(store: MongoWikiStore, bundle: WikiSeedBundle) -> None:
    """The page-view read seam sees Grove's authored pages with real bodies."""
    _seed(store, bundle)
    pages = store.list_pages(_GROVE)
    assert len(pages) == 10
    landing = store.get_page(_GROVE, "grove-overview")
    assert landing is not None
    assert landing.title == "Grove Overview"
    assert "```mermaid" in landing.body
    # Frontmatter sources round-trip (the SourceCard rail).
    assert landing.frontmatter.relevant_sources is not None


# ── The Code Galaxy graph (query_graph + list_edges) ───────────────────────────


def test_seed_populates_grove_graph_deterministically(
    store: MongoWikiStore, bundle: WikiSeedBundle
) -> None:
    """The graph endpoint reads a dense, deterministic node/edge set for Grove."""
    _seed(store, bundle)
    nodes = store.query_graph(_GROVE)
    edges = store.list_edges(_GROVE)
    assert len(nodes) == 760
    assert len(edges) > 760  # CONTAINS + CALLS
    # Every edge endpoint resolves to a real node (no dangling refs the view drops).
    ids = {n.node_id for n in nodes}
    assert all(e.source in ids and e.target in ids for e in edges)
    # File nodes anchor the folder-collapse hierarchy.
    assert any(n.type == "File" for n in nodes)
    assert {n.type for n in nodes} >= {"File", "Class", "Method", "Function"}
    # Secondary projects carry no graph (only Grove needs the Code Galaxy).
    assert store.query_graph(_ASSISTANT) == []


# ── The indexing-progress card (get_job + load_job_events) ─────────────────────


def test_seed_populates_indexing_jobs(store: MongoWikiStore, bundle: WikiSeedBundle) -> None:
    """One active in-flight job backs the progress card; the rest fill the band."""
    _seed(store, bundle)
    jobs = store.list_jobs()

    # "Indexing now" = the single finalizing job (shot 09's active card).
    active = [j for j in jobs if j.status in _ACTIVE]
    assert [j.job_id for j in active] == ["job-demo-project-64-0001"]

    job = store.get_job("job-demo-project-64-0001")
    assert job is not None
    assert job.status == "finalizing"
    assert job.phase == "finalize"
    assert job.pages_submitted == job.total_pages == 18
    assert job.model == "claude-sonnet-4-6"
    # The progress timeline reads the append-only log; the milestone lines land.
    events = store.load_job_events("job-demo-project-64-0001")
    assert len(events) == 7
    assert all(e["type"] == "log" for e in events)
    assert events[0]["text"].startswith("Cloned 104 files")
    assert events[-1]["text"] == "Embedded 715 nodes (dim=3072)"


def test_seed_populates_recoverable_band(store: MongoWikiStore, bundle: WikiSeedBundle) -> None:
    """The landing "Incomplete indexes — N resumable" band is non-empty.

    Reproduces the exact ``/jobs/recoverable`` filter + ``LandingScreen``
    subtraction that were returning 0: a recoverable STATUS is not enough — the
    job's ``ResumePlan`` must be non-no-op (reusable artifacts), and the job must
    not also be active. Each recoverable slug carries a seeded graph, so its plan
    reuses ``graph`` and it survives both gates.
    """
    _seed(store, bundle)
    jobs = store.list_jobs()
    active_slugs = {j.slug for j in jobs if j.status in _ACTIVE}

    # The route's own predicate: recoverable status AND ResumePlan non-no-op.
    recoverable = [
        j
        for j in jobs
        if j.status in _RECOVERABLE and not ResumePlan.build(store, j).is_noop()
    ]
    # LandingScreen.visibleRecoverable = recoverable MINUS active slugs.
    visible = [j for j in recoverable if j.slug not in active_slugs]
    assert len(visible) == 16

    # Spot-check the reused-artifact hint the band renders (skip=['graph']).
    plan = ResumePlan.build(store, store.get_job("job-vault-sync-0001"))
    assert plan.is_noop() is False
    assert "graph" in plan.skip
    assert plan.node_count > 0
    # These slugs are mid-index repos — recoverable WITHOUT a finalized Project.
    assert store.get_project("git.example.com/acme/vault-sync") is None


# ── A stored Q&A answer (get_qa) ───────────────────────────────────────────────


def test_seed_populates_qa_answers(store: MongoWikiStore, bundle: WikiSeedBundle) -> None:
    """The stored-answer deep link reads a complete, block-based answer."""
    _seed(store, bundle)
    answer = store.get_qa("qa-assistant-what-is-this-for-0001")
    assert answer is not None
    assert answer.status == "complete"
    assert answer.question == "What is this project for?"
    assert answer.from_page_id == "overview"
    assert answer.slug == _ASSISTANT
    assert answer.model == "claude-sonnet-4-6"
    assert answer.models_used == ["claude-sonnet-4-6"]
    # At least one prose block (LiveBlocks needs one to paint) + a terminal sources.
    kinds = [b.root.kind for b in answer.blocks]
    assert "p" in kinds and "h2" in kinds and "table" in kinds
    assert kinds[-1] == "sources"
    assert "wiki:overview" in answer.summary_sources


# ── Determinism: a re-seed is byte-identical ───────────────────────────────────


def test_reseed_is_byte_identical(bundle: WikiSeedBundle) -> None:
    """Seeding twice into the same store leaves an identical raw Mongo state.

    This is the whole point: the zero-diff screenshot gate can only pass if every
    ``demo-seed`` yields the same world. Snapshots the raw collections (``_id``
    stripped) so even the internal ``event_count`` counter must match — which it
    does because jobs are create-if-absent and every other write is a keyed upsert.
    """
    client = mongomock.MongoClient()
    store = MongoWikiStore(client=client, database="idem")

    def snapshot() -> str:
        db = client["idem"]
        out: dict[str, list] = {}
        for name in sorted(db.list_collection_names()):
            docs = sorted(
                ({k: v for k, v in d.items() if k != "_id"} for d in db[name].find()),
                key=lambda d: json.dumps(d, sort_keys=True, default=str),
            )
            out[name] = docs
        return json.dumps(out, sort_keys=True, default=str)

    WikiSeeder(wiki_store=store, bundle=bundle, t0=_T0).seed()
    first = snapshot()
    WikiSeeder(wiki_store=store, bundle=bundle, t0=_T0).seed()
    assert snapshot() == first


# ── The env-driven construction path the orchestrator's __main__ will use ──────


def test_env_driven_construction_via_monkeypatched_pymongo(
    monkeypatch: pytest.MonkeyPatch, bundle: WikiSeedBundle
) -> None:
    """``MongoWikiStore(uri=…, database=…)`` works when pymongo is mongomock.

    The orchestrator's ``__main__`` builds the store from
    ``MEWBO_MONGODB_URI``/``MEWBO_MONGODB_DATABASE`` (the wiki store does NOT read
    those env vars itself), then injects it. This exercises that exact path with
    pymongo swapped for mongomock.
    """
    monkeypatch.setattr("pymongo.MongoClient", mongomock.MongoClient)
    store = MongoWikiStore(uri="mongodb://demo-mongo:27017", database="wiki_demo")
    WikiSeeder(wiki_store=store, bundle=bundle, t0=_T0).seed()
    assert store.get_project(_GROVE) is not None
    assert len(store.list_projects()) == 12
