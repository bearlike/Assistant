"""Per-job/commit artifact isolation — attribution, supersede, commit-scoped skip.

Before this, ``upsert_nodes`` merged with no delete-by-slug, so the store was the
UNION of every commit ever indexed for a slug: files deleted months ago were
still served, and a node count could not mean "the graph for THIS commit is
built". Every test here pins one half of the fix — artifacts carry their commit,
a completed re-index reaps the prior commit's, the skip predicate counts only the
job's own commit, and commit-less (QA-minted) rows are preserved. Only ``tmp_path``
is stubbed; the real ``JsonWikiStore`` / ``ResumePlan`` / finalize code runs.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mewbo_graph.entities.types import Entity
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    Frontmatter,
    IndexingJob,
    WikiPage,
    make_graph_node,
)

_A = "a" * 40
_B = "b" * 40


def _store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _node(slug: str, nid: str, *, file: str = "a.py"):
    return make_graph_node(
        slug=slug, node_id=nid, type="Function", name=nid, file=file, range=(0, 1)
    )


def _job(store: JsonWikiStore, *, job_id: str, slug: str, commit: str | None, status: str):
    job = IndexingJob(
        jobId=job_id, slug=slug, status=status,
        scannedCount=0, totalCount=0, currentFile=None, commitSha=commit,
    )
    store.create_job(job)
    return job


# ── attribution + supersede at the store seam ────────────────────────────────


def test_two_commits_do_not_union_after_supersede(tmp_path: Path) -> None:
    """A completed re-index reaps the prior commit's nodes instead of unioning."""
    store = _store(tmp_path)
    slug = "org/repo"
    # Commit A built three nodes; commit B rebuilt the two shared ones (n3's file
    # was deleted, so B never re-emits it). The shared ids overwrite; n3 lingers.
    store.upsert_nodes(
        slug, [_node(slug, "n1"), _node(slug, "n2"), _node(slug, "n3")], commit_sha=_A
    )
    store.upsert_nodes(slug, [_node(slug, "n1"), _node(slug, "n2")], commit_sha=_B)
    assert {n.node_id for n in store.query_graph(slug)} == {"n1", "n2", "n3"}  # the union

    store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    survivors = {n.node_id for n in store.query_graph(slug)}
    assert survivors == {"n1", "n2"}  # n3 (commit A only) is gone — no union
    assert all(n.commit_sha == _B for n in store.query_graph(slug))


def test_file_deleted_between_commits_stops_being_served(tmp_path: Path) -> None:
    """A node whose file was deleted at the new commit is not returned to retrieval.

    ``query_graph`` is exactly what ``HybridRetriever._graph_candidates`` reads, so
    proving it stops returning the stale node proves retrieval stops serving it.
    """
    store = _store(tmp_path)
    slug = "org/repo"
    store.upsert_nodes(
        slug, [_node(slug, "gone", file="deleted.py"), _node(slug, "kept", file="kept.py")],
        commit_sha=_A,
    )
    # Commit B: deleted.py is gone, so only kept.py's node is re-emitted.
    store.upsert_nodes(slug, [_node(slug, "kept", file="kept.py")], commit_sha=_B)

    store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    files = {n.file for n in store.query_graph(slug)}
    assert files == {"kept.py"}
    assert "deleted.py" not in files


def test_supersede_preserves_commitless_entities(tmp_path: Path) -> None:
    """QA-minted (commit-less) entities survive a re-index; prior-commit ones don't.

    The mint tool also runs in a Q&A session, which carries no job — those
    entities are accretive memory, not a per-commit snapshot, so supersede must
    leave every ``None``-stamped row untouched while reaping other commits'.
    """
    store = _store(tmp_path)
    slug = "org/repo"
    store.upsert_entities(slug, [Entity(name="OldConcept", type="concept")], commit_sha=_A)
    store.upsert_entities(slug, [Entity(name="CurrentConcept", type="concept")], commit_sha=_B)
    store.upsert_entities(slug, [Entity(name="QaConcept", type="concept")])  # commit-less

    store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    names = {e.name for e in store.query_entities(slug)}
    assert names == {"CurrentConcept", "QaConcept"}  # OldConcept reaped; QA preserved


def test_supersede_is_idempotent(tmp_path: Path) -> None:
    """A second supersede for the same commit finds nothing left to reap."""
    store = _store(tmp_path)
    slug = "org/repo"
    store.upsert_nodes(slug, [_node(slug, "n1")], commit_sha=_A)
    store.upsert_nodes(slug, [_node(slug, "n2")], commit_sha=_B)

    first = store.supersede_graph_artifacts(slug, keep_commit_sha=_B)
    second = store.supersede_graph_artifacts(slug, keep_commit_sha=_B)

    assert first["nodes"] == 1
    assert second["nodes"] == 0


# ── commit-scoped skip predicate ─────────────────────────────────────────────


def test_skip_predicate_answers_per_commit_not_per_union(tmp_path: Path) -> None:
    """"graph for THIS commit built" — not "some commit built a graph for the slug"."""
    store = _store(tmp_path)
    slug = "org/repo"
    # The graph in the store belongs to commit A.
    store.upsert_nodes(slug, [_node(slug, "n1"), _node(slug, "n2")], commit_sha=_A)
    store.upsert_entities(slug, [Entity(name="Widget", type="concept")], commit_sha=_A)

    # A resume of the job that BUILT commit A skips graph + enrich.
    built = ResumePlan.build(
        store, _job(store, job_id="jA", slug=slug, commit=_A, status="interrupted")
    )
    assert "graph" in built.skip
    assert "enrich" in built.skip

    # A resume of a DIFFERENT commit sees no graph for itself — the union of A's
    # nodes must not read as "commit B's graph is built".
    not_built = ResumePlan.build(
        store, _job(store, job_id="jB", slug=slug, commit=_B, status="interrupted")
    )
    assert "graph" not in not_built.skip
    assert "enrich" not in not_built.skip


def test_count_graph_nodes_matches_only_its_commit(tmp_path: Path) -> None:
    """The store count that backs the predicate is exact, not a union count."""
    store = _store(tmp_path)
    slug = "org/repo"
    store.upsert_nodes(slug, [_node(slug, "n1"), _node(slug, "n2")], commit_sha=_A)
    store.upsert_nodes(slug, [_node(slug, "n3")], commit_sha=_B)

    assert store.count_graph_nodes(slug, commit_sha=_A) == 2
    assert store.count_graph_nodes(slug, commit_sha=_B) == 1
    assert store.count_graph_nodes(slug, commit_sha="c" * 40) == 0
    assert len(store.query_graph(slug)) == 3  # the union is still 3 until supersede


# ── finalize drives the supersede end-to-end ─────────────────────────────────


def _page(pid: str) -> WikiPage:
    return WikiPage(
        id=pid, title=pid, frontmatter=Frontmatter(title=pid, slug=pid),
        body="x", toc=[], nav=[],
    )


def test_finalize_supersedes_prior_commit_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``wiki_finalize`` reaps prior-commit artifacts once it completes.

    Wires the whole seam: a job at commit B finalizes over a store still holding
    commit A's straggler node, and the completed index leaves only B's graph.
    """
    import mewbo_graph.plugins.wiki.finalize as finalize_mod
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool

    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    store = _store(tmp_path)
    slug = "org/repo"

    _job(store, job_id="jobB", slug=slug, commit=_B, status="finalizing")
    store.attach_job_session("jobB", "sessB")
    store.save_job_submission("jobB", {
        "repoUrl": f"https://git.example.com/{slug}", "slug": slug,
        "platform": "gitea", "language": "Python", "filterMode": "exclude",
        "dirs": [], "files": [],
    })
    # The store holds commit A's straggler AND commit B's fresh node.
    store.upsert_nodes(slug, [_node(slug, "straggler")], commit_sha=_A)
    store.upsert_nodes(slug, [_node(slug, "current")], commit_sha=_B)
    store.save_job_plan("jobB", [{"id": "overview", "title": "Overview"}])
    store.save_page(slug, _page("overview"), commit_sha=_B, job_id="jobB")

    tool = WikiFinalizeTool(session_id="sessB")
    runtime = SimpleNamespace(wiki_store=store)
    step = SimpleNamespace(tool_input={"landingPageId": "overview"})
    # Stub the platform description fetch so finalize never touches the network.
    with patch.object(finalize_mod, "_resolve_runtime", return_value=runtime), \
         patch.object(finalize_mod, "_fetch_description", return_value=""):
        result = asyncio.run(tool.handle(step))

    assert "error" not in result.content
    assert store.get_job("jobB").status == "complete"
    survivors = {n.node_id for n in store.query_graph(slug)}
    assert survivors == {"current"}  # the commit-A straggler was superseded
