"""Extra coverage for WikiStoreBase, JsonWikiStore, and MongoWikiStore.

Targets the uncovered branches identified by coverage analysis:

JsonWikiStore:
- _load_json() malformed-file recovery (lines 459-461).
- _load_events() malformed-JSONL line recovery.
- delete_page() removes index entry even when file is already absent.
- prune_pages() drops obsolete pages, keeps retained ones.
- save_job_plan / get_job_plan / get_job_submitted_count /
  increment_job_submitted_count / save_job_submission / get_job_submission.
- delete_edges_by_source_file returns 0 when file has no nodes.
- list_jobs when jobs_root is absent.
- list_pages for nonexistent slug.
- cancel_job returns False for missing job.
- get_wiki_store / set_wiki_store / reset_for_tests (singleton seam).

MongoWikiStore (via mongomock):
- Full CRUD: projects, pages, jobs, qa, plan, submission, sessions,
  graph/memory not yet (they remain NotImplementedError on Mongo).
- cancel_job, prune_pages (Mongo bulk-delete override).
- _clean_for_model strips extra fields before Pydantic validation.
- create_wiki_store factory for 'mongodb' driver.

WikiStoreBase default raise-paths verified to surface NotImplementedError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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

# ── helpers ────────────────────────────────────────────────────────────────────


def _project(slug: str = "org/repo", indexed_at: str = "2026-01-01T00:00:00Z") -> Project:
    return Project(
        slug=slug,
        source="github",
        lang="Python",
        indexed_at=indexed_at,
        pages=1,
        desc="Test repo",
    )


def _page(page_id: str = "overview") -> WikiPage:
    return WikiPage(
        id=page_id,
        title="Overview",
        frontmatter=Frontmatter(title="Overview", slug=page_id),
        body="# Overview\n",
        toc=[TocEntry(id=page_id, label="Overview", lvl=1)],
        nav=[NavEntry(id=page_id, label="Overview", lvl=1)],
    )


def _job(job_id: str = "job-001", slug: str = "org/repo") -> IndexingJob:
    return IndexingJob(
        job_id=job_id,
        slug=slug,
        status="queued",
        scanned_count=0,
        total_count=5,
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


def _json_store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path)


def _mongo_store():
    from mewbo_graph.wiki.store import MongoWikiStore

    return MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki_extra")


# ── parametrized both backends for core paths ─────────────────────────────────


@pytest.fixture(params=["json", "mongo"])
def store(request, tmp_path):
    if request.param == "json":
        return _json_store(tmp_path / "wiki")
    return _mongo_store()


# ── WikiStoreBase NotImplementedError defaults ─────────────────────────────────


@pytest.mark.parametrize(
    "method,args",
    [
        ("upsert_nodes", ("org/repo", [])),
        ("upsert_edges", ("org/repo", [])),
        ("upsert_embeddings", ("org/repo", [])),
        ("query_graph", ("org/repo",)),
        ("list_edges", ("org/repo",)),
        ("vector_search", ("org/repo", [1.0], 5)),
        ("delete_nodes_by_file", ("org/repo", "a.py")),
        ("delete_edges_by_source_file", ("org/repo", "a.py")),
        ("upsert_memory_nodes", ("org/repo", [])),
        ("get_memory_node", ("org/repo", "nid")),
        ("delete_memory_node", ("org/repo", "nid")),
        ("query_memory", ("org/repo",)),
        ("upsert_memory_edges", ("org/repo", [])),
        ("list_memory_edges", ("org/repo",)),
        ("memories_anchored_to", ("org/repo", [])),
        ("upsert_memory_embeddings", ("org/repo", [])),
        ("memory_vector_search", ("org/repo", [1.0])),
        ("upsert_doc_notes", ("org/repo", [])),
        ("get_doc_note", ("org/repo", "pg")),
        ("list_doc_notes", ("org/repo",)),
        ("delete_doc_note", ("org/repo", "pg")),
        ("upsert_file_manifest", ("org/repo", [])),
        ("get_file_manifest", ("org/repo", "a.py")),
        ("list_file_manifest", ("org/repo",)),
        ("delete_file_manifest", ("org/repo", "a.py")),
        ("_live_anchored_ids", ("org/repo",)),
        # Abstract-entity overlay (default-raise on the base, like memory).
        ("upsert_entities", ("org/repo", [])),
        ("get_entity", ("org/repo", "eid")),
        ("query_entities", ("org/repo",)),
        ("upsert_entity_embeddings", ("org/repo", [])),
        ("entity_vector_search", ("org/repo", [1.0])),
        ("upsert_entity_edges", ("org/repo", [])),
        ("list_entity_edges", ("org/repo",)),
        ("get_entity_recommendations", ("org/repo",)),
    ],
)
def test_base_raises_not_implemented(method: str, args: tuple) -> None:
    """Every base-class default method raises NotImplementedError."""
    from mewbo_graph.wiki.store import WikiStoreBase

    # Instantiate a minimal stub that inherits the base defaults only.
    class _Stub(WikiStoreBase):
        def create_project(self, p): ...  # type: ignore[override]
        def get_project(self, s): ...  # type: ignore[override]
        def list_projects(self): ...  # type: ignore[override]
        def delete_project(self, s): ...  # type: ignore[override]
        def save_page(self, s, p): ...  # type: ignore[override]
        def get_page(self, s, pid): ...  # type: ignore[override]
        def list_pages(self, s): ...  # type: ignore[override]
        def delete_page(self, s, pid): ...  # type: ignore[override]
        def create_job(self, j): ...  # type: ignore[override]
        def get_job(self, jid): ...  # type: ignore[override]
        def update_job(self, jid, **f): ...  # type: ignore[override]
        def list_jobs(self, s=None): ...  # type: ignore[override]
        def append_job_event(self, jid, ev): ...  # type: ignore[override]
        def load_job_events(self, jid, after_idx=-1): ...  # type: ignore[override]
        def cancel_job(self, jid): ...  # type: ignore[override]
        def attach_job_session(self, jid, sid): ...  # type: ignore[override]
        def get_job_session(self, jid): ...  # type: ignore[override]
        def find_job_by_session(self, sid): ...  # type: ignore[override]
        def save_job_plan(self, jid, plan): ...  # type: ignore[override]
        def get_job_plan(self, jid): ...  # type: ignore[override]
        def get_job_submitted_count(self, jid): ...  # type: ignore[override]
        def increment_job_submitted_count(self, jid): ...  # type: ignore[override]
        def save_job_submission(self, jid, sub): ...  # type: ignore[override]
        def get_job_submission(self, jid): ...  # type: ignore[override]
        def save_credentials(self, slug, blob): ...  # type: ignore[override]
        def get_credentials(self, slug): ...  # type: ignore[override]
        def delete_credentials(self, slug): ...  # type: ignore[override]
        def get_recovery_attempts(self, slug): ...  # type: ignore[override]
        def bump_recovery_attempts(self, slug): ...  # type: ignore[override]
        def save_qa(self, qa): ...  # type: ignore[override]
        def update_qa_fields(self, qa): ...  # type: ignore[override]
        def get_qa(self, aid): ...  # type: ignore[override]
        def attach_qa_session(self, aid, sid): ...  # type: ignore[override]
        def get_qa_session(self, aid): ...  # type: ignore[override]
        def find_qa_by_session(self, sid): ...  # type: ignore[override]
        def append_qa_event(self, aid, ev): ...  # type: ignore[override]
        def load_qa_events(self, aid, after_idx=-1): ...  # type: ignore[override]

    stub = _Stub()
    with pytest.raises(NotImplementedError):
        getattr(stub, method)(*args)


# ── JsonWikiStore: _load_json malformed recovery ──────────────────────────────


def test_json_load_json_malformed_file_returns_none(tmp_path: Path) -> None:
    """_load_json() returns None and logs a warning for unparseable JSON."""
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    bad_path = tmp_path / "wiki" / "projects" / "bad.json"
    bad_path.write_text("NOT_VALID_JSON {{{", encoding="utf-8")
    result = store._load_json(bad_path, Project)
    assert result is None


# ── JsonWikiStore: delete_page edge-cases ─────────────────────────────────────


def test_json_delete_page_removes_from_index_when_file_absent(
    tmp_path: Path,
) -> None:
    """delete_page returns True even if the page file is gone, if it's in the index."""
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.save_page("org/repo", _page("overview"))
    # Manually delete the file but leave the index intact.
    page_file = store._page_path("org/repo", "overview")
    page_file.unlink()
    # delete_page should still return True (index entry removed).
    result = store.delete_page("org/repo", "overview")
    assert result is True
    # Confirm it's no longer in the index.
    index = store._load_index("org/repo")
    assert "overview" not in index


def test_json_delete_page_returns_false_for_entirely_absent(
    tmp_path: Path,
) -> None:
    """delete_page returns False when neither file nor index entry exists."""
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    result = store.delete_page("org/repo", "ghost")
    assert result is False


# ── JsonWikiStore: prune_pages ────────────────────────────────────────────────


def test_json_prune_pages_drops_obsolete_keeps_retained(tmp_path: Path) -> None:
    """prune_pages removes pages not in keep set; returns count of dropped pages."""
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    for pid in ("a", "b", "c"):
        store.save_page("org/repo", _page(pid))

    dropped = store.prune_pages("org/repo", keep=["a", "c"])
    assert dropped == 1
    pages = store.list_pages("org/repo")
    assert {p.id for p in pages} == {"a", "c"}


def test_json_prune_pages_empty_keep_drops_all(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.save_page("org/repo", _page("a"))
    store.save_page("org/repo", _page("b"))
    dropped = store.prune_pages("org/repo", keep=[])
    assert dropped == 2
    assert store.list_pages("org/repo") == []


# ── JsonWikiStore: list_pages for nonexistent slug ────────────────────────────


def test_json_list_pages_nonexistent_slug_returns_empty(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    assert store.list_pages("ghost/slug") == []


# ── JsonWikiStore: list_jobs when jobs_root absent ────────────────────────────


def test_json_list_jobs_absent_root_returns_empty(tmp_path: Path) -> None:
    """list_jobs returns [] when no jobs/ directory exists yet."""
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    import shutil

    shutil.rmtree(store.root_dir / "jobs")
    assert store.list_jobs() == []


# ── JsonWikiStore: job plan / submission / submitted_count ─────────────────────


def test_json_job_plan_round_trip(tmp_path: Path) -> None:
    """save_job_plan / get_job_plan persists and returns the plan list."""
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-plan"))
    plan: list[dict[str, Any]] = [
        {"id": "overview", "title": "Overview"},
        {"id": "arch", "title": "Architecture"},
    ]
    store.save_job_plan("job-plan", plan)
    loaded = store.get_job_plan("job-plan")
    assert loaded is not None
    assert len(loaded) == 2
    assert loaded[0]["title"] == "Overview"


def test_json_get_job_plan_missing_returns_none(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-noplan"))
    assert store.get_job_plan("job-noplan") is None


def test_json_get_job_plan_malformed_returns_none(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-badplan"))
    plan_path = store._job_plan_path("job-badplan")
    plan_path.write_text("{}", encoding="utf-8")  # valid JSON but not a list
    assert store.get_job_plan("job-badplan") is None


def test_json_submitted_count_starts_at_zero(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-cnt"))
    assert store.get_job_submitted_count("job-cnt") == 0


def test_json_increment_submitted_count_monotonic(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-inc"))
    assert store.increment_job_submitted_count("job-inc") == 1
    assert store.increment_job_submitted_count("job-inc") == 2
    assert store.increment_job_submitted_count("job-inc") == 3
    assert store.get_job_submitted_count("job-inc") == 3


def test_json_job_submission_round_trip(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-sub"))
    sub = {"repo_url": "https://github.com/org/repo", "lang": "Python"}
    store.save_job_submission("job-sub", sub)
    loaded = store.get_job_submission("job-sub")
    assert loaded is not None
    assert loaded["repo_url"] == "https://github.com/org/repo"


def test_json_get_job_submission_missing_returns_none(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-nosub"))
    assert store.get_job_submission("job-nosub") is None


def test_json_get_job_submission_malformed_returns_none(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-badsub"))
    sub_path = store._job_submission_path("job-badsub")
    sub_path.write_text("[1, 2, 3]", encoding="utf-8")  # list, not dict
    assert store.get_job_submission("job-badsub") is None


# ── JsonWikiStore: cancel_job edge-cases ──────────────────────────────────────


def test_json_cancel_job_returns_false_for_missing_job(tmp_path: Path) -> None:
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    assert store.cancel_job("ghost-job") is False


# ── JsonWikiStore: delete_edges_by_source_file when file has no nodes ─────────


def test_json_delete_edges_by_source_file_no_nodes_returns_zero(
    tmp_path: Path,
) -> None:
    """Returns 0 (not crash) when the file has no nodes in the graph."""
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    result = store.delete_edges_by_source_file("org/repo", "nonexistent.py")
    assert result == 0


# ── JsonWikiStore: _load_events malformed JSONL line ─────────────────────────


def test_json_load_events_skips_malformed_line(tmp_path: Path) -> None:
    """_load_events skips corrupt JSONL lines without raising."""
    from mewbo_graph.wiki.store import JsonWikiStore

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_job("job-ev-bad"))
    store.append_job_event("job-ev-bad", {"type": "queued"})
    # Append a bad line directly.
    ev_path = store._event_path("jobs", "job-ev-bad")
    with ev_path.open("a", encoding="utf-8") as fh:
        fh.write("NOT_JSON\n")
    store.append_job_event("job-ev-bad", {"type": "done"})
    events = store.load_job_events("job-ev-bad")
    # Only the two good events should appear (bad line skipped).
    types = [e["type"] for e in events]
    assert "queued" in types
    assert "done" in types


# ── MongoWikiStore: project CRUD ──────────────────────────────────────────────


def test_mongo_project_crud() -> None:
    store = _mongo_store()
    assert store.get_project("org/repo") is None

    store.create_project(_project("org/repo", "2026-05-01T00:00:00Z"))
    got = store.get_project("org/repo")
    assert got is not None
    assert got.slug == "org/repo"

    store.create_project(_project("org/repo2", "2027-01-01T00:00:00Z"))
    listed = store.list_projects()
    assert listed[0].slug == "org/repo2"  # sorted newest-first

    assert store.delete_project("org/repo") is True
    assert store.delete_project("org/repo") is False
    assert store.get_project("org/repo") is None


# ── MongoWikiStore: page CRUD + prune_pages ───────────────────────────────────


def test_mongo_page_crud() -> None:
    store = _mongo_store()
    assert store.get_page("org/repo", "overview") is None

    store.save_page("org/repo", _page("overview"))
    got = store.get_page("org/repo", "overview")
    assert got is not None
    assert got.body == "# Overview\n"

    store.save_page("org/repo", _page("arch"))
    assert len(store.list_pages("org/repo")) == 2

    assert store.delete_page("org/repo", "overview") is True
    assert store.delete_page("org/repo", "overview") is False
    assert len(store.list_pages("org/repo")) == 1


def test_mongo_prune_pages_bulk_delete() -> None:
    """MongoWikiStore.prune_pages uses Mongo $nin for a bulk delete."""
    store = _mongo_store()
    for pid in ("a", "b", "c"):
        store.save_page("org/repo", _page(pid))

    dropped = store.prune_pages("org/repo", keep=["a", "c"])
    assert dropped == 1
    assert {p.id for p in store.list_pages("org/repo")} == {"a", "c"}


# ── MongoWikiStore: job lifecycle ─────────────────────────────────────────────


def test_mongo_job_crud() -> None:
    store = _mongo_store()
    assert store.get_job("job-001") is None

    store.create_job(_job("job-001"))
    got = store.get_job("job-001")
    assert got is not None
    assert got.status == "queued"

    updated = store.update_job("job-001", status="scanning", scanned_count=3)
    assert updated.status == "scanning"
    assert updated.scanned_count == 3

    jobs = store.list_jobs(slug="org/repo")
    assert any(j.job_id == "job-001" for j in jobs)


def test_mongo_job_events_append_load() -> None:
    store = _mongo_store()
    store.create_job(_job("job-ev"))
    idx0 = store.append_job_event("job-ev", {"type": "queued"})
    idx1 = store.append_job_event("job-ev", {"type": "scanning"})
    assert idx0 == 0
    assert idx1 == 1
    all_ev = store.load_job_events("job-ev")
    assert len(all_ev) == 2
    tail = store.load_job_events("job-ev", after_idx=0)
    assert len(tail) == 1
    assert tail[0]["type"] == "scanning"


def test_mongo_cancel_job() -> None:
    store = _mongo_store()
    store.create_job(_job("job-c"))
    assert store.cancel_job("job-c") is True
    assert store.cancel_job("job-c") is False
    assert store.get_job("job-c").status == "cancelled"  # type: ignore[union-attr]


def test_mongo_cancel_job_missing_returns_false() -> None:
    store = _mongo_store()
    assert store.cancel_job("ghost") is False


# ── MongoWikiStore: job session mapping ───────────────────────────────────────


def test_mongo_job_session_round_trip() -> None:
    store = _mongo_store()
    store.create_job(_job("job-sess"))
    assert store.get_job_session("job-sess") is None
    assert store.find_job_by_session("sess-1") is None

    store.attach_job_session("job-sess", "sess-1")
    assert store.get_job_session("job-sess") == "sess-1"
    assert store.find_job_by_session("sess-1") == "job-sess"

    store.attach_job_session("job-sess", "sess-2")
    assert store.get_job_session("job-sess") == "sess-2"
    assert store.find_job_by_session("sess-2") == "job-sess"
    assert store.find_job_by_session("sess-1") is None


def test_mongo_get_job_session_for_missing_job() -> None:
    store = _mongo_store()
    assert store.get_job_session("ghost") is None


def test_mongo_find_job_by_session_for_missing_session() -> None:
    store = _mongo_store()
    assert store.find_job_by_session("ghost-sess") is None


# ── MongoWikiStore: job plan, submission, submitted count ─────────────────────


def test_mongo_job_plan_round_trip() -> None:
    store = _mongo_store()
    store.create_job(_job("job-plan"))
    assert store.get_job_plan("job-plan") is None

    plan = [{"id": "overview", "title": "Overview"}, {"id": "arch", "title": "Arch"}]
    store.save_job_plan("job-plan", plan)
    loaded = store.get_job_plan("job-plan")
    assert loaded is not None
    assert len(loaded) == 2


def test_mongo_get_job_plan_missing_job_returns_none() -> None:
    store = _mongo_store()
    assert store.get_job_plan("ghost") is None


def test_mongo_submitted_count_and_increment() -> None:
    store = _mongo_store()
    store.create_job(_job("job-inc"))
    assert store.get_job_submitted_count("job-inc") == 0
    assert store.increment_job_submitted_count("job-inc") == 1
    assert store.increment_job_submitted_count("job-inc") == 2
    assert store.get_job_submitted_count("job-inc") == 2


def test_mongo_get_submitted_count_missing_job_returns_zero() -> None:
    store = _mongo_store()
    assert store.get_job_submitted_count("ghost") == 0


def test_mongo_increment_submitted_count_missing_job_raises() -> None:
    store = _mongo_store()
    with pytest.raises(KeyError):
        store.increment_job_submitted_count("ghost")


def test_mongo_job_submission_round_trip() -> None:
    store = _mongo_store()
    store.create_job(_job("job-sub"))
    assert store.get_job_submission("job-sub") is None

    sub = {"repo_url": "https://github.com/org/repo"}
    store.save_job_submission("job-sub", sub)
    loaded = store.get_job_submission("job-sub")
    assert loaded is not None
    assert loaded["repo_url"] == "https://github.com/org/repo"


def test_mongo_get_job_submission_missing_job_returns_none() -> None:
    store = _mongo_store()
    assert store.get_job_submission("ghost") is None


# ── MongoWikiStore: QA lifecycle ──────────────────────────────────────────────


def test_mongo_qa_crud() -> None:
    store = _mongo_store()
    assert store.get_qa("ans-001") is None

    store.save_qa(_qa("ans-001"))
    got = store.get_qa("ans-001")
    assert got is not None
    assert got.answer_id == "ans-001"


def test_mongo_qa_events_append_load() -> None:
    store = _mongo_store()
    store.save_qa(_qa("ans-ev"))
    idx0 = store.append_qa_event("ans-ev", {"type": "meta"})
    idx1 = store.append_qa_event("ans-ev", {"type": "done"})
    assert idx0 == 0
    assert idx1 == 1
    all_ev = store.load_qa_events("ans-ev")
    assert len(all_ev) == 2
    tail = store.load_qa_events("ans-ev", after_idx=0)
    assert len(tail) == 1
    assert tail[0]["type"] == "done"


def test_mongo_qa_session_round_trip() -> None:
    store = _mongo_store()
    store.save_qa(_qa("ans-sess"))
    assert store.get_qa_session("ans-sess") is None
    assert store.find_qa_by_session("s1") is None

    store.attach_qa_session("ans-sess", "s1")
    assert store.get_qa_session("ans-sess") == "s1"
    assert store.find_qa_by_session("s1") == "ans-sess"

    store.attach_qa_session("ans-sess", "s2")
    assert store.get_qa_session("ans-sess") == "s2"
    assert store.find_qa_by_session("s2") == "ans-sess"
    assert store.find_qa_by_session("s1") is None


def test_mongo_get_qa_session_missing_returns_none() -> None:
    store = _mongo_store()
    assert store.get_qa_session("ghost") is None


def test_mongo_find_qa_by_session_missing_returns_none() -> None:
    store = _mongo_store()
    assert store.find_qa_by_session("ghost-sess") is None


# ── MongoWikiStore: graph upsert_nodes is overridden and works ────────────────


def test_mongo_upsert_nodes_empty_list_is_noop() -> None:
    """MongoWikiStore.upsert_nodes is overridden and accepts an empty list."""
    store = _mongo_store()
    # Should not raise — the Mongo driver does override this method.
    store.upsert_nodes("org/repo", [])
    # Query returns empty (nothing was upserted).
    result = store.query_graph("org/repo")
    assert result == []


# ── Singleton seam: get_wiki_store / set_wiki_store / reset_for_tests ──────────


def test_get_wiki_store_lazy_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_wiki_store() constructs a store on first call."""
    import mewbo_graph.wiki.store as store_mod
    from mewbo_graph.wiki.store import JsonWikiStore

    store_mod.set_wiki_store(None)
    monkeypatch.setattr(
        store_mod,
        "create_wiki_store",
        lambda: JsonWikiStore(root_dir=tmp_path / "lazy"),
    )
    s = store_mod.get_wiki_store()
    assert isinstance(s, JsonWikiStore)
    # Second call returns the same instance.
    assert store_mod.get_wiki_store() is s
    store_mod.set_wiki_store(None)


def test_set_wiki_store_overrides_singleton(tmp_path: Path) -> None:
    import mewbo_graph.wiki.store as store_mod
    from mewbo_graph.wiki.store import JsonWikiStore

    custom = JsonWikiStore(root_dir=tmp_path / "custom")
    store_mod.set_wiki_store(custom)
    assert store_mod.get_wiki_store() is custom
    store_mod.set_wiki_store(None)


def test_reset_for_tests_with_root_dir(tmp_path: Path) -> None:
    """reset_for_tests(root_dir=...) pins a store at the given path."""
    import mewbo_graph.wiki.store as store_mod
    from mewbo_graph.wiki.store import JsonWikiStore

    root = tmp_path / "fixed_root"
    store = store_mod.reset_for_tests(root_dir=root)
    assert isinstance(store, JsonWikiStore)
    assert store.root_dir == root
    assert store_mod.get_wiki_store() is store
    store_mod.set_wiki_store(None)


def test_reset_for_tests_without_root_dir() -> None:
    """reset_for_tests() with no argument uses the default config path."""
    import mewbo_graph.wiki.store as store_mod
    from mewbo_graph.wiki.store import JsonWikiStore

    store = store_mod.reset_for_tests()
    assert isinstance(store, JsonWikiStore)
    store_mod.set_wiki_store(None)


# ── factory: create_wiki_store for 'mongodb' driver ──────────────────────────


def test_create_wiki_store_mongodb_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_wiki_store() returns MongoWikiStore when driver is 'mongodb'."""
    import mewbo_graph.wiki.store as store_mod
    from mewbo_graph.wiki.store import MongoWikiStore

    def _fake_cfg(*keys, default=None):
        if "driver" in keys:
            return "mongodb"
        return default

    monkeypatch.setattr(store_mod, "get_config_value", _fake_cfg)
    # MongoWikiStore.__init__ calls MongoClient + ping; stub out the client path.
    original_init = MongoWikiStore.__init__

    def _patched_init(self, *, client=None, uri=None, database=None):
        # Force the pre-built mongomock client regardless of uri.
        original_init(self, client=client or mongomock.MongoClient(), database="test_factory")

    monkeypatch.setattr(MongoWikiStore, "__init__", _patched_init)
    from mewbo_graph.wiki.store import create_wiki_store

    store = create_wiki_store()
    assert isinstance(store, MongoWikiStore)


# ── _clean_for_model strips extra Mongo-internal fields ──────────────────────


def test_clean_for_model_strips_extra_fields() -> None:
    """_clean_for_model removes bookkeeping keys not declared on IndexingJob.

    Verified at two altitudes:
    1. Direct call to _clean_for_model (private helper, white-box).
    2. Round-trip through MongoWikiStore.create_job / get_job — confirms that
       the _id, event_count, etc. inserted by create_job are silently stripped
       and the returned model is clean (no validation error, core fields intact).
    """
    from mewbo_graph.wiki.store import _clean_for_model

    doc = {
        "_id": "mongo-internal",
        "job_id": "job-001",
        "slug": "org/repo",
        "status": "queued",
        "scanned_count": 0,
        "total_count": 5,
        "current_file": None,
        "event_count": 42,  # bookkeeping — must be stripped
        "session_id": "sess-x",  # bookkeeping — must be stripped
        "plan": [],  # bookkeeping — must be stripped
    }
    clean = _clean_for_model(doc, IndexingJob)
    assert "_id" not in clean
    assert "event_count" not in clean
    assert "session_id" not in clean
    assert "plan" not in clean
    # Core fields survive.
    assert clean["job_id"] == "job-001"
    # Should validate without error.
    job = IndexingJob.model_validate(clean)
    assert job.job_id == "job-001"

    # ── Round-trip through the real store consumption site ─────────────────
    # create_job stores event_count + all job fields; get_job must return a
    # clean IndexingJob with no ValidationError despite those extras.
    store = _mongo_store()
    raw_job = _job("job-clean-rt", slug="org/clean")
    store.create_job(raw_job)
    # Inject extra bookkeeping fields directly into the document so the strip
    # path is exercised even if create_job didn't write all of them.
    store._col("wiki_jobs").update_one(
        {"job_id": "job-clean-rt"},
        {"$set": {"session_id": "sess-extra", "plan": [], "submitted_pages": 3}},
    )
    retrieved = store.get_job("job-clean-rt")
    assert retrieved is not None
    assert retrieved.job_id == "job-clean-rt"
    assert retrieved.slug == "org/clean"
    # The model must have validated cleanly — if extra fields leaked through,
    # IndexingJob (ConfigDict extra="forbid") would have raised during get_job.


# ── MongoWikiStore: list_jobs filtered by slug ────────────────────────────────


def test_mongo_list_jobs_filtered_by_slug() -> None:
    store = _mongo_store()
    store.create_job(_job("job-a", slug="org/a"))
    store.create_job(_job("job-b", slug="org/b"))
    jobs_a = store.list_jobs(slug="org/a")
    assert len(jobs_a) == 1
    assert jobs_a[0].job_id == "job-a"
    all_jobs = store.list_jobs()
    assert len(all_jobs) == 2


# ── Resume sidecar + recovery-cap reset (Gitea #54, both backends) ─────────────


def test_resume_plan_roundtrips(store) -> None:
    """save_resume_plan / get_resume_plan round-trip on both backends; absent → None."""
    store.create_job(_job("job-resume", slug="org/repo"))
    assert store.get_resume_plan("job-resume") is None
    blob = {"skip": ["graph", "plan"], "pages_done": ["a"], "pages_remaining": ["b"]}
    store.save_resume_plan("job-resume", blob)
    assert store.get_resume_plan("job-resume") == blob
    # Overwrite replaces, never merges.
    store.save_resume_plan("job-resume", {"skip": []})
    assert store.get_resume_plan("job-resume") == {"skip": []}


def test_reset_recovery_attempts_clears_counter(store) -> None:
    """A user-initiated resume resets the slug-keyed auto-recovery cap to 0."""
    store.bump_recovery_attempts("org/repo")
    store.bump_recovery_attempts("org/repo")
    assert store.get_recovery_attempts("org/repo") == 2
    store.reset_recovery_attempts("org/repo")
    assert store.get_recovery_attempts("org/repo") == 0
    # Idempotent on an already-clear slug.
    store.reset_recovery_attempts("org/repo")
    assert store.get_recovery_attempts("org/repo") == 0
