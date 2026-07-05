"""GraphOnlyIndexer tests — deterministic, zero-LLM repository onboarding.

The indexer must: clone → scan → build the AST graph → finalize, producing a
populated graph + ZERO pages + ``graph_only=True``, invoking NO LLM. We stub the
two real I/O boundaries (git clone via ``subprocess.run`` populating the clone
dir from a fixture, and the embedder) per tests/CLAUDE.md — no network, no model.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from mewbo_graph.plugins.wiki import build_graph as build_graph_mod, graph_only as graph_only_mod
from mewbo_graph.plugins.wiki.graph_only import GraphOnlyIndexer, build_graph_only_ctx
from mewbo_graph.wiki.errors import DocumentationUnavailableError
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob, WizardSubmission

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_python_repo"


def _submission(graph_only: bool = True) -> WizardSubmission:
    return WizardSubmission.model_validate({
        "repoUrl": "https://example.com/o/r",
        "slug": "example.com/o/r",
        "platform": "git",
        "depth": "comprehensive",
        "language": "en",
        "model": "unused-in-graph-only",
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
        "graphOnly": graph_only,
    })


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A store + job + a fake git clone that copies the fixture into the clone dir."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    slug = "example.com/o/r"
    job = IndexingJob(
        jobId="j1", slug=slug, status="queued",
        scannedCount=0, totalCount=0, currentFile=None,
    )
    store.create_job(job)

    def _fake_clone(cmd, *a, **kw):
        # The clone target dir is the last positional arg in the git command.
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        for src in FIXTURE.iterdir():
            if src.is_file():
                (dest / src.name).write_bytes(src.read_bytes())
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_clone)
    # Git metadata + description fetch are best-effort I/O — stub them away.
    monkeypatch.setattr(graph_only_mod, "_git_rev_parse", lambda d, a: "abc1234")
    monkeypatch.setattr(graph_only_mod, "_fetch_description", lambda **k: "")
    # No embedder hits the proxy.
    monkeypatch.setattr(build_graph_mod, "_make_embedder", lambda: MagicMock(
        model="stub", embed_nodes=MagicMock(return_value=[])
    ))
    return store, slug


def test_graph_only_clone_honors_submission_ref(setup, monkeypatch):
    """``submission.ref`` threads ``--branch <ref> --single-branch`` into the clone."""
    store, slug = setup
    captured: list = []

    def _capturing_clone(cmd, *a, **kw):
        captured.append(list(cmd))
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        for src in FIXTURE.iterdir():
            if src.is_file():
                (dest / src.name).write_bytes(src.read_bytes())
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _capturing_clone)

    ctx = build_graph_only_ctx(job_id="j1", slug=slug, store=store)
    sub = _submission().model_copy(update={"ref": "feature/x"})
    GraphOnlyIndexer(ctx, sub).run()

    clone_cmd = next(c for c in captured if "clone" in c)
    assert "--branch" in clone_cmd
    assert clone_cmd[clone_cmd.index("--branch") + 1] == "feature/x"
    assert "--single-branch" in clone_cmd


def test_graph_only_builds_graph_zero_pages_and_flag(setup):
    """A graph-only run produces a populated graph, zero pages, graph_only=True."""
    store, slug = setup
    ctx = build_graph_only_ctx(job_id="j1", slug=slug, store=store)
    GraphOnlyIndexer(ctx, _submission()).run()

    # Graph populated (AST nodes from the fixture).
    assert len(store.query_graph(slug)) > 0
    # Zero documentation pages.
    assert store.list_pages(slug) == []
    # Job complete, project stamped graph_only.
    job = store.get_job("j1")
    assert job.status == "complete"
    project = store.get_project(slug)
    assert project is not None
    assert project.graph_only is True
    assert project.pages == 0


def test_graph_only_invokes_no_llm(setup):
    """The deterministic path must never construct a chat model / run an agent."""
    store, slug = setup
    ctx = build_graph_only_ctx(job_id="j1", slug=slug, store=store)
    # If any of these were touched, the run took the LLM path — fail loudly.
    with patch("mewbo_core.llm.build_chat_model", side_effect=AssertionError("LLM used")):
        GraphOnlyIndexer(ctx, _submission()).run()
    assert store.get_job("j1").status == "complete"


def test_graph_only_emits_phase_progress_events(setup):
    """The same phase/queued/complete events the agent path emits must fire."""
    store, slug = setup
    ctx = build_graph_only_ctx(job_id="j1", slug=slug, store=store)
    GraphOnlyIndexer(ctx, _submission()).run()

    events = store.load_job_events("j1", after_idx=-1)
    phases = [e["name"] for e in events if e.get("type") == "phase"]
    # Deterministic pipeline: clone → scan → graph → finalize (NO enrich/plan/pages).
    assert phases == ["clone", "scan", "graph", "finalize"]
    types = {e.get("type") for e in events}
    assert "queued" in types
    assert "complete" in types
    # The complete event reports zero pages.
    complete = next(e for e in events if e.get("type") == "complete")
    assert complete["pageCount"] == 0


def test_graph_only_doc_read_raises_documentation_unavailable(setup):
    """Reading page content on the finished graph-only project raises the error."""
    store, slug = setup
    ctx = build_graph_only_ctx(job_id="j1", slug=slug, store=store)
    GraphOnlyIndexer(ctx, _submission()).run()

    with pytest.raises(DocumentationUnavailableError) as exc:
        store.get_page(slug, "graph")
    assert exc.value.slug == slug
    # list_pages stays safe (returns []) — only doc-CONTENT reads raise.
    assert store.list_pages(slug) == []


def test_graph_only_empty_graph_fails(setup, monkeypatch):
    """A run whose graph build yields no nodes must NOT complete (graph gate)."""
    store, slug = setup

    # Fake clone produces an empty (non-parseable) tree → zero graph nodes.
    def _empty_clone(cmd, *a, **kw):
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "README").write_text("not source code", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _empty_clone)
    ctx = build_graph_only_ctx(job_id="j1", slug=slug, store=store)
    GraphOnlyIndexer(ctx, _submission()).run()

    job = store.get_job("j1")
    assert job.status == "failed"
    assert store.get_project(slug) is None


def test_graph_only_cancel_mid_run_stops_indexer(setup, monkeypatch):
    """A cancel landing between phases halts the indexer before finalize.

    Cooperative cancellation contract: the daemon re-reads job status at each
    phase boundary. We land an out-of-band cancel during the graph phase (via the
    embedder stub) so the pre-finalize check bails — the run must NOT publish a
    project nor clobber the cancel with ``complete``.
    """
    store, slug = setup

    def _cancel_during_embed(*a, **k):
        store.cancel_job("j1")  # ← out-of-band cancel, mid graph-phase
        return []

    monkeypatch.setattr(build_graph_mod, "_make_embedder", lambda: SimpleNamespace(
        model="stub", embed_nodes=_cancel_during_embed
    ))
    ctx = build_graph_only_ctx(job_id="j1", slug=slug, store=store)
    GraphOnlyIndexer(ctx, _submission()).run()

    # Graph WAS built (the phase ran), but finalize was skipped: cancel survives,
    # no project published, no complete/failed clobber.
    assert len(store.query_graph(slug)) > 0
    assert store.get_job("j1").status == "cancelled"
    assert store.get_project(slug) is None
    # A "cancelled — stopping" log marks the cooperative stop.
    events = store.load_job_events("j1", after_idx=-1)
    assert any(
        e.get("type") == "log" and "cancelled" in e.get("text", "").lower()
        for e in events
    )
