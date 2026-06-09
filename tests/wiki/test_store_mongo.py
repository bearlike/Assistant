"""Full-coverage tests for MongoWikiStore and factory (MongoDB path).

Uses mongomock so no real MongoDB is required.
Covers the same 10 scenarios as test_store_json.py so both backends
behave identically.
"""
from __future__ import annotations

import mongomock
import pytest
from mewbo_graph.wiki.types import (
    Frontmatter,
    IndexingJob,
    NavEntry,
    Project,
    QaAnswer,
    TocEntry,
    WikiPage,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _project(slug: str = "org/repo", indexed_at: str = "2026-01-01T00:00:00Z") -> Project:
    return Project(
        slug=slug,
        source="github",
        lang="Python",
        indexed_at=indexed_at,
        pages=5,
        desc="Test repo",
    )


def _page(page_id: str = "overview") -> WikiPage:
    return WikiPage(
        id=page_id,
        title="Overview",
        frontmatter=Frontmatter(title="Overview", slug=page_id),
        body="# Overview\n\n```mermaid\ngraph TD;\nA-->B;\n```\n",
        toc=[TocEntry(id="overview", label="Overview", lvl=1)],
        nav=[NavEntry(id="overview", label="Overview", lvl=1)],
    )


def _job(job_id: str = "job-001", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="queued",
        scanned_count=0,
        total_count=10,
        current_file=None,
    )


def _qa(answer_id: str = "ans-001") -> QaAnswer:
    return QaAnswer(
        answer_id=answer_id,
        from_page_id="overview",
        summary_sources=["src/main.py"],
        model="anthropic/claude-sonnet-4-6",
        blocks=[],
    )


def _store():
    """Return a MongoWikiStore backed by an in-memory mongomock client."""
    from mewbo_graph.wiki.store import MongoWikiStore

    return MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki")


# ── 1. Project CRUD ────────────────────────────────────────────────────────────


def test_project_crud_mongo() -> None:
    store = _store()

    # get on missing → None
    assert store.get_project("org/repo") is None

    # create + get
    p = _project("org/repo", indexed_at="2026-05-01T00:00:00Z")
    store.create_project(p)
    got = store.get_project("org/repo")
    assert got is not None
    assert got.slug == "org/repo"
    assert got.indexed_at == "2026-05-01T00:00:00Z"

    # list returns it
    projects = store.list_projects()
    assert len(projects) == 1
    assert projects[0].slug == "org/repo"

    # add a second project — list is sorted by indexed_at desc
    p2 = _project("org/repo2", indexed_at="2027-01-01T00:00:00Z")
    store.create_project(p2)
    listed = store.list_projects()
    assert listed[0].slug == "org/repo2"  # newer first
    assert listed[1].slug == "org/repo"

    # delete — returns True first time, False second time (idempotent)
    assert store.delete_project("org/repo") is True
    assert store.delete_project("org/repo") is False
    assert store.get_project("org/repo") is None
    assert len(store.list_projects()) == 1


# ── 2. Page CRUD ───────────────────────────────────────────────────────────────


def test_page_crud_mongo() -> None:
    store = _store()

    # missing → None
    assert store.get_page("org/repo", "overview") is None

    # save + get — body roundtrips verbatim (mermaid fences preserved)
    pg = _page("overview")
    store.save_page("org/repo", pg)
    got = store.get_page("org/repo", "overview")
    assert got is not None
    assert got.body == pg.body
    assert "```mermaid" in got.body

    # list_pages
    pages = store.list_pages("org/repo")
    assert len(pages) == 1
    assert pages[0].id == "overview"

    # save a second page
    pg2 = _page("architecture")
    store.save_page("org/repo", pg2)
    assert len(store.list_pages("org/repo")) == 2

    # overwrite same (slug, page_id)
    pg_updated = WikiPage(
        id="overview",
        title="Overview v2",
        frontmatter=Frontmatter(title="Overview v2", slug="overview"),
        body="new body",
        toc=[],
        nav=[],
    )
    store.save_page("org/repo", pg_updated)
    got2 = store.get_page("org/repo", "overview")
    assert got2 is not None
    assert got2.title == "Overview v2"
    assert len(store.list_pages("org/repo")) == 2  # no duplicate

    # slugs with slashes work verbatim in Mongo
    store.save_page("org/sub/repo", pg)
    assert store.get_page("org/sub/repo", "overview") is not None


# ── 3. Job CRUD ────────────────────────────────────────────────────────────────


def test_job_crud_mongo() -> None:
    store = _store()

    # missing → None
    assert store.get_job("job-001") is None

    # create + get
    job = _job("job-001", slug="org/repo")
    store.create_job(job)
    got = store.get_job("job-001")
    assert got is not None
    assert got.job_id == "job-001"
    assert got.status == "queued"

    # update_job — partial merge (only passed fields change)
    updated = store.update_job("job-001", status="scanning", scanned_count=3)
    assert updated.status == "scanning"
    assert updated.scanned_count == 3
    assert updated.total_count == 10  # untouched

    # persisted after re-fetch
    refetched = store.get_job("job-001")
    assert refetched is not None
    assert refetched.status == "scanning"
    assert refetched.scanned_count == 3

    # list_jobs (optional convenience)
    jobs = store.list_jobs(slug="org/repo")
    assert any(j.job_id == "job-001" for j in jobs)


# ── 4. Job event append / load ─────────────────────────────────────────────────


def test_job_events_append_load_mongo() -> None:
    client = mongomock.MongoClient()
    from mewbo_graph.wiki.store import MongoWikiStore

    store = MongoWikiStore(client=client, database="test_wiki")
    store.create_job(_job("job-ev"))

    # append returns monotonically increasing idx starting at 0
    idx0 = store.append_job_event("job-ev", {"type": "queued", "slug": "org/repo"})
    idx1 = store.append_job_event("job-ev", {"type": "scanning", "file": "a.py"})
    idx2 = store.append_job_event("job-ev", {"type": "scanned", "file": "a.py"})
    assert idx0 == 0
    assert idx1 == 1
    assert idx2 == 2

    # load_job_events(after_idx=-1) returns all
    all_events = store.load_job_events("job-ev")
    assert len(all_events) == 3
    assert all_events[0]["type"] == "queued"

    # after_idx=0 → events with idx > 0
    tail = store.load_job_events("job-ev", after_idx=0)
    assert len(tail) == 2
    assert tail[0]["type"] == "scanning"

    # persist check — re-instantiate store sharing the same client/db
    store2 = MongoWikiStore(client=client, database="test_wiki")
    persisted = store2.load_job_events("job-ev")
    assert len(persisted) == 3


# ── 5. cancel_job ─────────────────────────────────────────────────────────────


def test_cancel_job_mongo() -> None:
    store = _store()
    store.create_job(_job("job-cancel"))

    # cancel returns True first time, appends terminal event, sets status
    result = store.cancel_job("job-cancel")
    assert result is True

    job = store.get_job("job-cancel")
    assert job is not None
    assert job.status == "cancelled"

    events = store.load_job_events("job-cancel")
    cancelled_events = [e for e in events if e.get("type") == "cancelled"]
    assert len(cancelled_events) == 1

    # idempotent — second cancel returns False, no second cancelled event
    result2 = store.cancel_job("job-cancel")
    assert result2 is False
    events2 = store.load_job_events("job-cancel")
    assert len([e for e in events2 if e.get("type") == "cancelled"]) == 1


# ── 6. QA save / load / events ────────────────────────────────────────────────


def test_qa_save_load_mongo() -> None:
    store = _store()

    # missing → None
    assert store.get_qa("ans-001") is None

    # save + get
    qa = _qa("ans-001")
    store.save_qa(qa)
    got = store.get_qa("ans-001")
    assert got is not None
    assert got.answer_id == "ans-001"
    assert got.model == "anthropic/claude-sonnet-4-6"

    # append_qa_event returns monotonically increasing idx
    idx0 = store.append_qa_event("ans-001", {"type": "meta", "answerId": "ans-001"})
    idx1 = store.append_qa_event("ans-001", {"type": "summary_ready", "sources": []})
    assert idx0 == 0
    assert idx1 == 1

    # load_qa_events(after_idx=-1) returns all
    all_events = store.load_qa_events("ans-001")
    assert len(all_events) == 2

    # after_idx filtering
    tail = store.load_qa_events("ans-001", after_idx=0)
    assert len(tail) == 1
    assert tail[0]["type"] == "summary_ready"


# ── 8. Factory returns MongoWikiStore when configured ─────────────────────────


def test_factory_returns_mongo_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import mewbo_graph.wiki.store as store_mod
    from mewbo_graph.wiki.store import MongoWikiStore, create_wiki_store

    def _cfg(*keys: str, default: str = "") -> str:
        # Return "mongodb" for storage.driver; provide uri/database for the rest
        if keys == ("storage", "driver"):
            return "mongodb"
        if keys == ("storage", "mongodb", "uri"):
            return "mongodb://localhost:27017"
        if keys == ("storage", "mongodb", "database"):
            return "test_wiki"
        return default

    monkeypatch.setattr(store_mod, "get_config_value", _cfg)
    # Patch pymongo.MongoClient so the factory uses mongomock
    monkeypatch.setattr("pymongo.MongoClient", mongomock.MongoClient)

    result = create_wiki_store()
    assert isinstance(result, MongoWikiStore)


# ── 9. Graph + embedding persistence ─────────────────────────────────────────


def test_upsert_and_query_nodes_mongo() -> None:
    from mewbo_graph.wiki.types import GraphNode

    store = _store()
    nodes = [
        GraphNode(slug="x/y", node_id="n1", type="File", name="a.py",
                  file="a.py", range=(0, 100), docstring=None),
        GraphNode(slug="x/y", node_id="n2", type="Function", name="foo",
                  file="a.py", range=(10, 50), docstring="Does foo"),
        GraphNode(slug="x/y", node_id="n3", type="Class", name="Bar",
                  file="b.py", range=(0, 80), docstring=None),
    ]
    store.upsert_nodes("x/y", nodes)

    # No filter
    all_nodes = store.query_graph("x/y")
    assert len(all_nodes) == 3

    # Filter by type
    only_fns = store.query_graph("x/y", node_type="Function")
    assert len(only_fns) == 1
    assert only_fns[0].name == "foo"

    # Filter by name_match (substring, case-insensitive)
    matched = store.query_graph("x/y", name_match="ba")  # matches "Bar"
    assert len(matched) == 1
    assert matched[0].name == "Bar"


def test_upsert_nodes_overwrites_existing_mongo() -> None:
    from mewbo_graph.wiki.types import GraphNode

    store = _store()
    n = GraphNode(slug="x/y", node_id="n1", type="File", name="a.py",
                  file="a.py", range=(0, 100), docstring="v1")
    store.upsert_nodes("x/y", [n])
    n2 = GraphNode(slug="x/y", node_id="n1", type="File", name="a.py",
                   file="a.py", range=(0, 100), docstring="v2")
    store.upsert_nodes("x/y", [n2])
    result = store.query_graph("x/y")
    assert len(result) == 1
    assert result[0].docstring == "v2"


def test_upsert_and_neighbors_via_edges_mongo() -> None:
    from mewbo_graph.wiki.types import GraphEdge, GraphNode

    store = _store()
    store.upsert_nodes("x/y", [
        GraphNode(slug="x/y", node_id="n1", type="File", name="a.py", file="a.py", range=(0, 50)),
        GraphNode(slug="x/y", node_id="n2", type="Function", name="foo", file="a.py",
                  range=(10, 40)),
        GraphNode(slug="x/y", node_id="n3", type="Function", name="bar", file="a.py",
                  range=(40, 50)),
    ])
    store.upsert_edges("x/y", [
        GraphEdge(slug="x/y", source="n1", target="n2", type="CONTAINS"),
        GraphEdge(slug="x/y", source="n2", target="n3", type="CALLS"),
    ])
    neighbors = store.query_graph("x/y", neighbors_of="n2")
    names = sorted([n.name for n in neighbors])
    assert names == ["a.py", "bar"]


def test_vector_search_returns_top_k_by_cosine_mongo() -> None:
    from mewbo_graph.wiki.types import Embedding

    store = _store()
    items = [
        Embedding(slug="x/y", node_id="n1", vector=[1.0, 0.0], model="m", dim=2),
        Embedding(slug="x/y", node_id="n2", vector=[0.0, 1.0], model="m", dim=2),
        Embedding(slug="x/y", node_id="n3", vector=[0.7, 0.7], model="m", dim=2),
    ]
    store.upsert_embeddings("x/y", items)
    hits = store.vector_search("x/y", qvec=[1.0, 0.0], k=2)
    ids = [h.node_id for h in hits]
    assert ids[0] == "n1"  # exact match comes first
    assert ids[1] == "n3"  # 0.707
    assert len(hits) == 2


def test_vector_search_empty_pool_returns_empty_mongo() -> None:
    store = _store()
    hits = store.vector_search("nothing/here", qvec=[1.0, 0.0, 0.0], k=5)
    assert hits == []


def test_graph_isolated_by_slug_mongo() -> None:
    from mewbo_graph.wiki.types import GraphNode

    store = _store()
    store.upsert_nodes("a/b", [GraphNode(slug="a/b", node_id="x", type="File",
                                         name="a", file="a", range=(0, 1))])
    store.upsert_nodes("c/d", [GraphNode(slug="c/d", node_id="x", type="File",
                                         name="c", file="c", range=(0, 1))])
    assert len(store.query_graph("a/b")) == 1
    assert store.query_graph("a/b")[0].name == "a"
    assert len(store.query_graph("c/d")) == 1
    assert store.query_graph("c/d")[0].name == "c"


# ── 10. attach_job_session / get_job_session ──────────────────────────────────


def test_attach_get_job_session_mongo() -> None:
    store = _store()
    store.create_job(_job("job-sess"))

    # missing → None
    assert store.get_job_session("job-sess") is None

    # attach then get
    store.attach_job_session("job-sess", "session-abc-123")
    assert store.get_job_session("job-sess") == "session-abc-123"

    # overwrite is idempotent
    store.attach_job_session("job-sess", "session-xyz-456")
    assert store.get_job_session("job-sess") == "session-xyz-456"

    # persist — re-instantiate sharing the same client/db
    client = mongomock.MongoClient()
    from mewbo_graph.wiki.store import MongoWikiStore

    store2 = MongoWikiStore(client=client, database="test_wiki2")
    store2.create_job(_job("job-sess"))
    store2.attach_job_session("job-sess", "session-new-789")
    assert store2.get_job_session("job-sess") == "session-new-789"


# ── 11. find_job_by_session (reverse lookup) ──────────────────────────────────


def test_find_job_by_session_mongo() -> None:
    store = _store()
    store.create_job(_job("job-rev"))

    # unknown session → None
    assert store.find_job_by_session("sess-unknown") is None

    # attach then find
    store.attach_job_session("job-rev", "sess-abc")
    assert store.find_job_by_session("sess-abc") == "job-rev"

    # wrong session still returns None
    assert store.find_job_by_session("sess-other") is None


# ── 12. QA session round-trip ─────────────────────────────────────────────────


def test_qa_session_round_trip_mongo() -> None:
    store = _store()
    qa = _qa("ans-rt")
    store.save_qa(qa)

    # missing → None
    assert store.get_qa_session("ans-rt") is None
    assert store.find_qa_by_session("sess-qa-1") is None

    # attach + forward lookup
    store.attach_qa_session("ans-rt", "sess-qa-1")
    assert store.get_qa_session("ans-rt") == "sess-qa-1"

    # reverse lookup
    assert store.find_qa_by_session("sess-qa-1") == "ans-rt"

    # overwrite session
    store.attach_qa_session("ans-rt", "sess-qa-2")
    assert store.get_qa_session("ans-rt") == "sess-qa-2"
    assert store.find_qa_by_session("sess-qa-2") == "ans-rt"
    assert store.find_qa_by_session("sess-qa-1") is None


# ── 13. find_session_isolated_per_slug ────────────────────────────────────────


def test_find_session_isolated_per_slug_mongo() -> None:
    store = _store()

    store.create_job(_job("job-A", slug="org/a"))
    store.create_job(_job("job-B", slug="org/b"))
    store.attach_job_session("job-A", "sess-1")
    store.attach_job_session("job-B", "sess-2")

    # each session maps to its own job
    assert store.find_job_by_session("sess-1") == "job-A"
    assert store.find_job_by_session("sess-2") == "job-B"

    # sessions don't cross-contaminate
    assert store.find_job_by_session("sess-2") != "job-A"
    assert store.find_job_by_session("sess-1") != "job-B"


# ── 14. Repository credential CRUD ─────────────────────────────────────────────


def test_mongo_credential_crud() -> None:
    store = _store()
    assert store.get_credentials("org/repo") is None
    store.save_credentials("org/repo", {"kind": "token", "value": "ghp_x", "username": None})
    assert store.get_credentials("org/repo") == {
        "kind": "token", "value": "ghp_x", "username": None,
    }
    assert store.delete_credentials("org/repo") is True
    assert store.delete_credentials("org/repo") is False
    assert store.get_credentials("org/repo") is None


# ── 15. Slug-keyed recovery counter ────────────────────────────────────────────


def test_mongo_recovery_counter() -> None:
    store = _store()
    assert store.get_recovery_attempts("org/repo") == 0
    assert store.bump_recovery_attempts("org/repo") == 1
    assert store.bump_recovery_attempts("org/repo") == 2
    assert store.get_recovery_attempts("org/repo") == 2
    # Per-slug isolation.
    assert store.get_recovery_attempts("org/other") == 0
