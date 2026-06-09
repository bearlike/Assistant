"""ResumePlan.build — checkpoint detection from real store artifacts (Gitea #54)."""
from __future__ import annotations

import pytest
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Frontmatter,
    GraphNode,
    IndexingJob,
    WikiPage,
)


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _job(store, *, job_id="j1", slug="org/repo", status="interrupted"):
    job = IndexingJob(
        jobId=job_id, slug=slug, status=status,
        scannedCount=0, totalCount=0, currentFile=None,
    )
    store.create_job(job)
    return job


def _node(slug, nid, name="f"):
    return GraphNode(slug=slug, node_id=nid, type="Function", name=name, file="a.py", range=(0, 1))


def _page(pid):
    return WikiPage(
        id=pid, title=pid, frontmatter=Frontmatter(title=pid, slug=pid),
        body="x", toc=[], nav=[],
    )


def _plan(*ids):
    return [{"id": pid, "title": pid} for pid in ids]


def test_empty_graph_forces_full_rebuild(store):
    """No graph / no plan / no pages → empty skip set (resume == rebuild)."""
    job = _job(store)
    plan = ResumePlan.build(store, job)
    assert plan.skip == frozenset()
    assert plan.pages_done == frozenset()
    assert plan.pages_remaining == ()
    assert plan.is_noop()
    assert not plan.should_skip("graph")


def test_populated_graph_skips_graph(store):
    job = _job(store)
    store.upsert_nodes("org/repo", [_node("org/repo", "n1"), _node("org/repo", "n2")])
    plan = ResumePlan.build(store, job)
    assert plan.should_skip("graph")
    assert plan.node_count == 2
    assert not plan.should_skip("enrich")  # no entities yet


def test_committed_plan_skips_plan(store):
    job = _job(store)
    store.save_job_plan("j1", _plan("overview", "arch"))
    plan = ResumePlan.build(store, job)
    assert plan.should_skip("plan")
    assert plan.total_pages == 2
    # No pages persisted yet → both remaining.
    assert plan.pages_done == frozenset()
    assert plan.pages_remaining == ("overview", "arch")


def test_pages_done_and_remaining_computed_from_plan_and_store(store):
    job = _job(store)
    store.save_job_plan("j1", _plan("a", "b", "c"))
    store.save_page("org/repo", _page("a"))
    store.save_page("org/repo", _page("c"))
    plan = ResumePlan.build(store, job)
    assert plan.pages_done == frozenset({"a", "c"})
    # Order preserved from the plan; only the missing one remains.
    assert plan.pages_remaining == ("b",)


def test_the_6_of_7_interrupted_at_pages_scenario(store):
    """The exact #54 failure: interrupted at ``pages`` with graph populated +
    6/7 plan pages written → skip graph/enrich/plan; only the 1 missing page
    remains; finalize follows."""
    job = _job(store, status="interrupted")
    # graph built ...
    store.upsert_nodes("org/repo", [_node("org/repo", f"n{i}") for i in range(5)])
    # ... entities minted (enrich done) ...
    from mewbo_graph.entities.types import Entity
    store.upsert_entities("org/repo", [Entity(name="Widget", type="concept")])
    # ... plan committed (7 pages) ...
    ids = [f"p{i}" for i in range(7)]
    store.save_job_plan("j1", _plan(*ids))
    # ... 6 of 7 pages written (p3 missing).
    for pid in ids:
        if pid != "p3":
            store.save_page("org/repo", _page(pid))

    plan = ResumePlan.build(store, job)
    assert plan.should_skip("graph")
    assert plan.should_skip("enrich")
    assert plan.should_skip("plan")
    assert plan.pages_remaining == ("p3",)
    assert len(plan.pages_done) == 6
    assert not plan.is_noop()
    # Summary tells the agent to reuse + write only the remaining page.
    summary = plan.summary()
    assert "p3" in summary
    assert "SKIP wiki_build_graph" in summary
    assert "SKIP wiki_commit_plan" in summary


def test_persisted_roundtrip_is_cheap_rebuild(store):
    """to_persisted → from_persisted reconstructs an equivalent plan (the
    per-tool-call cheap path that avoids a graph re-query)."""
    job = _job(store)
    store.upsert_nodes("org/repo", [_node("org/repo", "n1")])
    store.save_job_plan("j1", _plan("a", "b"))
    store.save_page("org/repo", _page("a"))
    built = ResumePlan.build(store, job)

    data = built.to_persisted()
    rebuilt = ResumePlan.from_persisted(data)
    assert rebuilt is not None
    assert rebuilt.skip == built.skip
    assert rebuilt.pages_done == built.pages_done
    assert rebuilt.pages_remaining == built.pages_remaining
    assert rebuilt.node_count == built.node_count
    assert rebuilt.total_pages == built.total_pages


def test_from_persisted_none_and_empty_yield_none():
    assert ResumePlan.from_persisted(None) is None
    assert ResumePlan.from_persisted({}) is None
