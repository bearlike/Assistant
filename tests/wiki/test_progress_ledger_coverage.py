"""Contract gates for the bounded wiki indexing progress ledger.

The ledger describes declared work rather than individual repository units: a
per-unit record would make the job document unbounded on an interactive read
path.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import mongomock
import pytest
from mewbo_core.contracts.progress import ProgressLedger, StepRecord, StepSpec
from mewbo_graph.plugins.wiki import _ctx as ctx_mod, build_graph as build_graph_mod
from mewbo_graph.plugins.wiki._ctx import ProgressReporter
from mewbo_graph.plugins.wiki.build_graph import WikiBuildGraphTool
from mewbo_graph.plugins.wiki.step_plans import GRAPH_STEPS, PHASE_STEPS
from mewbo_graph.wiki.events import LogJobEvent, WikiJobEvent
from mewbo_graph.wiki.resume import ResumePlan
from mewbo_graph.wiki.store import JsonWikiStore, MongoWikiStore
from mewbo_graph.wiki.types import Embedding, Frontmatter, IndexingJob, WikiPage, make_graph_node

from .test_graph_only_indexer import (
    _submission as _graph_only_submission,
    setup,  # noqa: F401 — fixture, reused by name
)
from .test_scoped_refresh import (
    CHANGED_AFTER,
    refresh,  # noqa: F401 — fixture, reused by name
)

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_python_repo"
T0 = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


class _Clock:
    """A hand-wound clock so ledger stamps need no sleep."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Embedder:
    """The graph path's external vector boundary, replaced with deterministic data."""

    model = "test"

    def embed_nodes(self, items: list[tuple[str, str]], *, slug: str = "") -> list[Embedding]:
        return [
            Embedding(
                slug=slug,
                node_id=node_id,
                vector=[0.1, 0.2],
                model=self.model,
                dim=2,
            )
            for node_id, _text in items
        ]


def _new_job() -> IndexingJob:
    return IndexingJob(
        jobId="j1",
        slug="x/y",
        status="scanning",
        scannedCount=0,
        totalCount=0,
        currentFile=None,
    )


@pytest.fixture
def graph_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[dict[str, Any]]]:
    """Run the real graph tool over the tiny checkout with only external legs stubbed."""
    monkeypatch.setattr(ctx_mod, "_PROGRESS_INTERVAL_S", 0.0)
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    monkeypatch.setattr(build_graph_mod, "_embeddings_enabled", lambda: True)
    monkeypatch.setattr(build_graph_mod, "_make_embedder", lambda *_args: _Embedder())
    monkeypatch.setattr(build_graph_mod, "_resolver_available", lambda: False)

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_new_job())
    store.attach_job_session("j1", "sess-1")
    clone_dir = tmp_path / "clones" / "j1"
    clone_dir.mkdir(parents=True)
    for source in FIXTURE.iterdir():
        if source.is_file():
            (clone_dir / source.name).write_bytes(source.read_bytes())

    tool = WikiBuildGraphTool(session_id="sess-1")
    with patch.object(
        build_graph_mod, "_resolve_runtime", return_value=MagicMock(wiki_store=store)
    ):
        asyncio.run(tool.handle(MagicMock(tool_input={})))

    return store, store.load_job_events("j1")


def _drive_full_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Drive clone→…→finalize for a from-scratch index through the REAL tools.

    Extends ``graph_run``'s clone-dir setup all the way to a completed job, so a
    terminal invariant (``pending_groups() == []``) can be checked against the
    WHOLE declared plan — the shape the live regression (a completed job whose
    ``enrich.mint_entities`` step was left ``running``) was only reachable from.
    Factored out of the ``full_run`` fixture so a negative control can call it
    with ``ProgressReporter.settle`` disabled first.
    """
    import mewbo_graph.plugins.wiki.commit_plan as commit_plan_mod
    import mewbo_graph.plugins.wiki.finalize as finalize_mod
    import mewbo_graph.plugins.wiki.mint_entity as mint_mod
    import mewbo_graph.plugins.wiki.submit_page as submit_page_mod
    from mewbo_graph.entities.types import EntityEmbedding
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool
    from mewbo_graph.plugins.wiki.mint_entity import MintEntityTool
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    class _FakeEntityEmbedder:
        model = "fake"

        def embed_query(self, text: str) -> list[float]:
            return [1.0, 0.0]

        def embed_nodes(
            self, items: list[tuple[str, str]], *, slug: str = ""
        ) -> list[EntityEmbedding]:
            return [
                EntityEmbedding(slug=slug, entity_id=nid, vector=[1.0, 0.0], model="fake", dim=2)
                for nid, _text in items
            ]

    monkeypatch.setattr(ctx_mod, "_PROGRESS_INTERVAL_S", 0.0)
    monkeypatch.setenv("MEWBO_WIKI_CLONE_ROOT", str(tmp_path / "clones"))
    monkeypatch.setattr(build_graph_mod, "_embeddings_enabled", lambda: True)
    monkeypatch.setattr(build_graph_mod, "_make_embedder", lambda *_args: _Embedder())
    monkeypatch.setattr(build_graph_mod, "_resolver_available", lambda: False)
    monkeypatch.setattr(mint_mod, "_make_embedder", lambda *_args: _FakeEntityEmbedder())

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_new_job())
    store.attach_job_session("j1", "sess-1")
    store.save_job_submission("j1", {
        "repoUrl": "https://example.com/x/y", "slug": "x/y", "platform": "git",
        "language": "en", "depth": "concise", "model": "test",
        "filterMode": "exclude", "dirs": [], "files": [],
    })
    clone_dir = tmp_path / "clones" / "j1"
    clone_dir.mkdir(parents=True)
    for source in FIXTURE.iterdir():
        if source.is_file():
            (clone_dir / source.name).write_bytes(source.read_bytes())

    runtime = MagicMock(wiki_store=store)

    with patch.object(build_graph_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(WikiBuildGraphTool(session_id="sess-1").handle(MagicMock(tool_input={})))

    with patch.object(mint_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(MintEntityTool(session_id="sess-1").handle(
            MagicMock(tool_input={"name": "Widget", "type": "concept"})
        ))
    assert "error" not in result.content

    with patch.object(commit_plan_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(WikiCommitPlanTool(session_id="sess-1").handle(MagicMock(tool_input={
            "pages": [{"id": "overview", "title": "Overview"}],
            "landingPageId": "overview",
        })))

    with patch.object(submit_page_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(WikiSubmitPageTool(session_id="sess-1").handle(MagicMock(tool_input={
            "pageId": "overview",
            "frontmatter": {"title": "Overview", "slug": "overview"},
            "body": "# Overview",
        })))

    with patch.object(finalize_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(WikiFinalizeTool(session_id="sess-1").handle(
            MagicMock(tool_input={"landingPageId": "overview"})
        ))
    assert "error" not in result.content

    return store


@pytest.fixture
def full_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The ``_drive_full_pipeline`` helper, as a fixture for the positive tests."""
    return _drive_full_pipeline(tmp_path, monkeypatch)


def test_every_declared_step_key_is_unique_and_owned_by_its_phase() -> None:
    """A duplicate key aliases two records, so the entire pipeline owns each key once."""
    keys: list[str] = []
    for phase, specs in PHASE_STEPS.items():
        for spec in specs:
            parts = spec.key.split(".")
            assert len(parts) == 2 and all(parts), spec.key
            assert parts[0] == phase
            assert spec.group == phase
            keys.append(spec.key)

    assert len(keys) == len(set(keys))


def test_reporter_declares_the_whole_pipeline_before_a_phase_begins(
    tmp_path: Path,
) -> None:
    """The denominator is fixed before graph work can report partial progress."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_new_job())
    clock = _Clock()
    reporter = ProgressReporter(
        SimpleNamespace(store=store, job_id="j1", slug="x/y"), clock=clock, interval_s=0.0
    )
    job = store.get_job("j1")
    assert job is not None and job.progress is not None
    expected = sum(len(specs) for specs in PHASE_STEPS.values())
    total_weight = job.progress.total_weight
    assert len(job.progress.steps) == expected

    for phase in ("clone", "scan"):
        reporter.finish_group(phase)
    with reporter.step("graph.parse", total=20) as step:
        clock.advance(100)
        step.advance(10, 20)

    job = store.get_job("j1")
    assert job is not None and job.progress is not None
    mid_graph = job.progress
    assert mid_graph.total_weight == total_weight
    assert mid_graph.fraction() < 0.25
    eta = mid_graph.eta_seconds(clock())
    assert eta is not None
    assert eta > 300


def test_a_completed_graph_phase_leaves_every_declared_step_terminal(
    graph_run: tuple[Any, list[dict[str, Any]]]
) -> None:
    """The graph outline never reports a completed parse while later work is pending."""
    store, _events = graph_run

    job = store.get_job("j1")
    assert job is not None and job.progress is not None
    graph_records = {record.key: record for record in job.progress.steps_in("graph")}
    assert set(graph_records) == {spec.key for spec in GRAPH_STEPS}
    assert all(record.terminal for record in graph_records.values())

    # Records remain proportional to the whole hand-written pipeline, never
    # fixture files: a per-unit ledger would make this interactive job read
    # unbounded.
    expected = sum(len(specs) for specs in PHASE_STEPS.values())
    assert len(job.progress.steps) == expected
    assert len(job.progress.steps) <= 40


@pytest.mark.parametrize(
    "key",
    [
        "graph.resolve_scip_index",
        "graph.read_scip_index",
        "graph.index_definitions",
        "graph.resolve_occurrences",
        "graph.validate",
        "graph.persist_nodes",
        "graph.persist_edges",
    ],
)
def test_each_previously_unscoped_graph_operation_has_its_own_terminal_record(
    graph_run: tuple[Any, list[dict[str, Any]]], key: str
) -> None:
    """Resolution, validation, and both writes remain separately visible after parsing."""
    store, _events = graph_run

    job = store.get_job("j1")
    assert job is not None and job.progress is not None
    record = job.progress.find(key)
    assert record is not None
    assert record.terminal


def test_job_store_retains_the_event_discriminator(
    tmp_path: Path,
) -> None:
    """The reader returns the writer's durable type, so parsing never guesses it."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_new_job())
    store.append_job_event("j1", {"type": "log", "level": "info", "text": "line"})

    assert store.load_job_events("j1") == [
        {"idx": 0, "type": "log", "level": "info", "text": "line"}
    ]


def test_every_stored_log_is_attributed_to_a_declared_step(
    graph_run: tuple[Any, list[dict[str, Any]]]
) -> None:
    """The event model exposes every timeline line that escaped a step scope."""
    _store, events = graph_run
    parsed = [WikiJobEvent.parse_stored(event) for event in events]
    logs = [event for event in parsed if isinstance(event, LogJobEvent)]

    assert logs
    assert not [event for event in logs if event.unattributed]
    assert all(event.step in {spec.key for spec in GRAPH_STEPS} for event in logs)


def test_log_event_classifies_an_omitted_step_as_unattributed() -> None:
    """A missing scope survives best-effort persistence for the gate to observe."""
    event = WikiJobEvent.parse({"type": "log", "level": "info", "text": "escaped"})

    assert isinstance(event, LogJobEvent)
    assert event.unattributed


def test_log_event_preserves_an_explicit_phase_opener_without_a_step() -> None:
    """A phase opener is deliberately visible as an unattributed timeline line."""
    event = WikiJobEvent.parse({"type": "log", "level": "info", "text": "starting"})

    assert isinstance(event, LogJobEvent)
    assert event.unattributed
    assert "step" not in event.stored_payload()


def test_log_event_rejects_unknown_payload_fields() -> None:
    """The persistence boundary refuses a writer's unowned payload shape."""
    with pytest.raises(ValueError, match="extra"):
        WikiJobEvent.parse({"type": "log", "level": "info", "text": "x", "bad": True})


def test_log_event_normalizes_the_legacy_warning_spelling() -> None:
    """The console's ``warn`` spelling is retained across older timeline lines."""
    event = WikiJobEvent.parse({"type": "log", "level": "warning", "text": "x"})

    assert isinstance(event, LogJobEvent)
    assert event.level == "warn"


def test_log_event_rejects_an_unknown_render_level() -> None:
    """A renderer-facing level is closed rather than silently defaulted."""
    with pytest.raises(ValueError, match="level"):
        WikiJobEvent.parse({"type": "log", "level": "notice", "text": "x"})


def test_unknown_stored_event_type_does_not_break_history_parsing() -> None:
    """A newer writer's event does not prevent reading older known events."""
    assert WikiJobEvent.parse_stored({"type": "future", "payload": "x"}) is None


def test_stored_event_round_trip_preserves_its_sse_payload_shape() -> None:
    """Validation never adds a key the SSE generator would forward."""
    event = WikiJobEvent.parse({"type": "progress", "ledger": {"version": 1}})

    assert event.stored_payload() == {"type": "progress", "ledger": {"version": 1}}


def test_a_rejected_step_finish_does_not_mask_a_body_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected terminal record drops progress, not the index body's error."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_new_job())
    reporter = ProgressReporter(
        SimpleNamespace(store=store, job_id="j1"), clock=_Clock(), interval_s=0.0
    )
    reporter.declare((StepSpec(key="graph.rejects", label="Rejects", group="graph"),))

    def reject_finish(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("invalid terminal state")

    monkeypatch.setattr(StepRecord, "finish", reject_finish)
    with pytest.raises(RuntimeError, match="body failure"):
        with reporter.step("graph.rejects"):
            raise RuntimeError("body failure")

    job = store.get_job("j1")
    assert job is not None and job.progress is not None
    assert job.progress.find("graph.rejects") is None
    failures = [
        event
        for event in store.load_job_events("j1")
        if event["type"] == "progress_error"
    ]
    assert failures == [
        {
            "idx": failures[0]["idx"],
            "type": "progress_error",
            "operation": "finish",
            "error": "invalid terminal state",
            "step": "graph.rejects",
        }
    ]


def test_a_raised_step_is_persisted_as_failed_and_the_exception_propagates(
    tmp_path: Path,
) -> None:
    """A failed body closes its scope durably rather than stranding it as running."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(_new_job())
    clock = _Clock()
    reporter = ProgressReporter(
        SimpleNamespace(store=store, job_id="j1"), clock=clock, interval_s=0.0
    )
    reporter.declare((StepSpec(key="graph.fails", label="Fails", group="graph"),))

    with pytest.raises(RuntimeError, match="broken boundary"):
        with reporter.step("graph.fails"):
            raise RuntimeError("broken boundary")

    job = store.get_job("j1")
    assert job is not None and job.progress is not None
    record = job.progress.find("graph.fails")
    assert record is not None
    assert record.state == "failed"
    assert record.ended_at == IndexingJob.format_stamp(clock.now)
    assert record.note == "broken boundary"


@pytest.mark.parametrize("driver", ["json", "mongo"])
def test_a_persisted_ledger_round_trips_through_both_store_drivers(
    driver: str, tmp_path: Path
) -> None:
    """The stored snapshot retains each record's terminal state and counters."""
    if driver == "json":
        store: Any = JsonWikiStore(root_dir=tmp_path / "wiki")
    else:
        store = MongoWikiStore(client=mongomock.MongoClient(), database="test_wiki")
    store.create_job(_new_job())

    clock = _Clock()
    ledger = ProgressLedger.from_plan(
        (StepSpec(key="graph.parse", label="Parsing", group="graph", unit="files"),)
    )
    ledger.enter("graph.parse", clock())
    ledger.advance("graph.parse", current=2, total=3, detail="module.py")
    clock.advance(1)
    ledger.finish("graph.parse", clock())
    store.update_job("j1", progress=ledger)

    job = store.get_job("j1")
    assert job is not None and job.progress is not None
    record = job.progress.find("graph.parse")
    assert record is not None
    assert record.state == "done"
    assert (record.current, record.total, record.detail) == (3, 3, "module.py")
    assert record.started_at == IndexingJob.format_stamp(T0)
    assert record.ended_at == IndexingJob.format_stamp(clock())


# ── Terminal invariant: a completed job's ledger is never left open ──────────
#
# The fixed full-pipeline plan (declared once, up front) makes "the bar stalls
# below 100% forever" a NEW failure mode a partial declaration could not
# produce: a step opened with no closing scope, or a phase this run's shape
# never touches, now strands real weight in the denominator unless something
# closes it. Each run shape gets its own drive through the REAL terminal tool.


def test_full_run_settles_the_uninstrumented_enrich_fan_out_at_finalize(
    full_run: Any,
) -> None:
    """The live regression: ``report()`` opens ``enrich.mint_entities`` with no
    closing ``with`` scope, so a completed job kept it ``running`` forever and
    the bar stalled at 82%. ``WikiFinalizeTool`` must settle it to ``done``.
    """
    store = full_run
    job = store.get_job("j1")
    assert job is not None and job.status == "complete"
    assert job.progress is not None
    enrich = job.progress.find("enrich.mint_entities")
    assert enrich is not None and enrich.state == "done"
    assert job.progress.pending_groups() == []
    assert job.progress.active is None
    assert job.progress.fraction() == pytest.approx(1.0)


def _drive_resumed_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A resume's ``skip_group`` calls close graph/enrich/plan, but nothing
    closes ``pages.write``: ``commit_plan``'s plan-skip branch returns BEFORE
    ``set_total``, and ``submit_page``'s per-page skip never touches the
    aggregate at all. Finalize must settle it too, honestly, as ``skipped``.
    Factored out of the positive test so a negative control can call it with
    ``ProgressReporter.settle`` disabled first.
    """
    import mewbo_graph.plugins.wiki.build_graph as build_graph_mod
    import mewbo_graph.plugins.wiki.commit_plan as commit_plan_mod
    import mewbo_graph.plugins.wiki.finalize as finalize_mod
    import mewbo_graph.plugins.wiki.mint_entity as mint_mod
    import mewbo_graph.plugins.wiki.submit_page as submit_page_mod
    from mewbo_graph.plugins.wiki.build_graph import WikiBuildGraphTool
    from mewbo_graph.plugins.wiki.commit_plan import WikiCommitPlanTool
    from mewbo_graph.plugins.wiki.finalize import WikiFinalizeTool
    from mewbo_graph.plugins.wiki.mint_entity import MintEntityTool
    from mewbo_graph.plugins.wiki.submit_page import WikiSubmitPageTool

    monkeypatch.setattr(ctx_mod, "_PROGRESS_INTERVAL_S", 0.0)
    slug = "org/repo"
    commit = "c" * 40
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(IndexingJob(
        jobId="job-resumed", slug=slug, status="scanning",
        scannedCount=0, totalCount=0, currentFile=None, commitSha=commit,
    ))
    store.attach_job_session("job-resumed", "sess-resumed")
    store.save_job_submission("job-resumed", {
        "repoUrl": "https://example.com/org/repo", "slug": slug, "platform": "git",
        "language": "en", "depth": "concise", "model": "test",
        "filterMode": "exclude", "dirs": [], "files": [],
    })
    store.upsert_nodes(
        slug,
        [
            make_graph_node(
                slug=slug, node_id="n1", type="File", name="a.py", file="a.py", range=(0, 1)
            )
        ],
        commit_sha=commit,
    )
    store.save_job_plan("job-resumed", [{"id": "overview", "title": "Overview"}])
    store.save_page(slug, WikiPage(
        id="overview", title="Overview",
        frontmatter=Frontmatter(title="Overview", slug="overview"),
        body="# Overview", toc=[], nav=[],
    ))
    resume_plan = ResumePlan(
        skip=frozenset({"graph", "enrich", "plan"}),
        pages_done=frozenset({"overview"}),
        pages_remaining=(),
        node_count=1,
        entity_count=0,
        total_pages=1,
    )
    store.save_resume_plan("job-resumed", resume_plan.to_persisted())

    runtime = MagicMock(wiki_store=store)

    with patch.object(build_graph_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(WikiBuildGraphTool(session_id="sess-resumed").handle(MagicMock(tool_input={})))
    with patch.object(mint_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(MintEntityTool(session_id="sess-resumed").handle(
            MagicMock(tool_input={"name": "Widget", "type": "concept"})
        ))
    with patch.object(commit_plan_mod, "_resolve_runtime", return_value=runtime):
        asyncio.run(WikiCommitPlanTool(session_id="sess-resumed").handle(MagicMock(tool_input={
            "pages": [{"id": "overview", "title": "Overview"}],
            "landingPageId": "overview",
        })))
    with patch.object(submit_page_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(
            WikiSubmitPageTool(session_id="sess-resumed").handle(MagicMock(tool_input={
                "pageId": "overview",
                "frontmatter": {"title": "Overview", "slug": "overview"},
                "body": "# regenerated",
            }))
        )
    assert "skipped" in result.content
    page = store.get_page(slug, "overview")
    assert page is not None and page.body == "# Overview"

    with patch.object(finalize_mod, "_resolve_runtime", return_value=runtime):
        result = asyncio.run(WikiFinalizeTool(session_id="sess-resumed").handle(
            MagicMock(tool_input={"landingPageId": "overview"})
        ))
    assert "error" not in result.content

    return store


def test_resumed_run_settles_every_group_its_skip_branches_leave_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _drive_resumed_pipeline(tmp_path, monkeypatch)

    job = store.get_job("job-resumed")
    assert job is not None and job.status == "complete"
    assert job.progress is not None
    pages_write = job.progress.find("pages.write")
    assert pages_write is not None and pages_write.state == "skipped"
    assert job.progress.pending_groups() == []
    assert job.progress.active is None
    assert job.progress.fraction() == pytest.approx(1.0)


# ── Negative controls: prove ``settle()`` is load-bearing, not vacuous ───────
#
# Each positive test above passes on the FIXED code; that alone does not prove
# it would fail without the fix — a test whose assertion an unmodified build
# already satisfies is worthless. These disable exactly the mechanism the fix
# added at the real call site (never a test-side simulation) and reproduce the
# original symptom, so the positive assertion is verifiably not vacuous.


def test_settle_is_load_bearing_for_a_completed_full_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling ``settle()`` reproduces the live regression exactly: a
    completed job whose ``enrich.mint_entities`` step is still ``running``.
    """
    monkeypatch.setattr(ctx_mod.ProgressReporter, "settle", lambda self: None)
    store = _drive_full_pipeline(tmp_path, monkeypatch)

    job = store.get_job("j1")
    assert job is not None and job.status == "complete"
    assert job.progress is not None
    enrich = job.progress.find("enrich.mint_entities")
    assert enrich is not None and enrich.state == "running"
    assert job.progress.active is not None
    assert job.progress.fraction() < 1.0


def test_settle_is_load_bearing_for_a_completed_resumed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling ``settle()`` reproduces the second regression: a resumed
    job's ``pages.write`` aggregate, never opened by any skip branch, stays
    ``pending`` on an otherwise-completed job.
    """
    monkeypatch.setattr(ctx_mod.ProgressReporter, "settle", lambda self: None)
    store = _drive_resumed_pipeline(tmp_path, monkeypatch)

    job = store.get_job("job-resumed")
    assert job is not None and job.status == "complete"
    assert job.progress is not None
    pages_write = job.progress.find("pages.write")
    assert pages_write is not None and pages_write.state == "pending"
    # ``pages`` is the group the two skip branches (commit_plan, submit_page)
    # leave pending; clone/scan are ALSO pending here because this drive never
    # calls those tools at all — a fact about the test's own shape, not about
    # ``settle()``, so the assertion names only the group the fix targets.
    assert "pages" in job.progress.pending_groups()
    assert job.progress.fraction() < 1.0


def test_settle_is_load_bearing_for_a_completed_scoped_refresh(tmp_path: Path) -> None:
    """The scoped-refresh finalizer is its OWN method (not ``WikiFinalizeTool``),
    so it needs its own proof: with ``settle()`` disabled, a completed scoped
    refresh leaves graph/enrich/plan pending — those groups are declared for
    every run shape but this one never reports through the ledger at all.
    """
    import subprocess

    from mewbo_graph.plugins.wiki import _jobless as jobless_mod
    from mewbo_graph.plugins.wiki._ctx import ProgressReporter as reporter_cls, build_jobless_ctx
    from mewbo_graph.plugins.wiki.scoped_refresh import ScopedRefreshRunner

    from .test_scoped_refresh import (
        CHANGED_BEFORE,
        NEW_COMMIT,
        SLUG,
        _seed_prior_index,
        _submission,
    )

    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(IndexingJob(
        jobId="j-refresh", slug=SLUG, status="queued",
        scannedCount=0, totalCount=0, currentFile=None,
    ))
    _seed_prior_index(store, changed_body=CHANGED_BEFORE)

    def _fake_clone(cmd, *_a, **_kw):
        from pathlib import Path as _Path

        dest = _Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "kept.py").write_text(
            "def untouched():\n    return 1\n", encoding="utf-8"
        )
        (dest / "changed.py").write_text(
            "def moved():\n    return 2\n\n\ndef added_later():\n    return 3\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with (
        patch.object(subprocess, "run", _fake_clone),
        patch.object(
            jobless_mod, "_git_rev_parse",
            lambda d, args: NEW_COMMIT if args == ["HEAD"] else "main",
        ),
        patch.object(reporter_cls, "settle", lambda self: None),
    ):
        ctx = build_jobless_ctx(job_id="j-refresh", slug=SLUG, store=store)
        ScopedRefreshRunner(ctx, _submission()).run()

    job = store.get_job("j-refresh")
    assert job is not None and job.status == "complete"
    assert job.progress is not None
    assert set(job.progress.pending_groups()) >= {"graph", "enrich", "plan"}
    assert job.progress.fraction() < 1.0


def test_graph_only_run_settles_every_group(setup: Any) -> None:  # noqa: F811
    """The zero-LLM shape never touches enrich/plan/pages through the ledger."""
    from mewbo_graph.plugins.wiki.graph_only import GraphOnlyIndexer, build_graph_only_ctx

    store, slug = setup
    ctx = build_graph_only_ctx(job_id="j1", slug=slug, store=store)
    GraphOnlyIndexer(ctx, _graph_only_submission()).run()

    job = store.get_job("j1")
    assert job is not None and job.status == "complete"
    assert job.progress is not None
    assert job.progress.pending_groups() == []
    assert job.progress.active is None
    assert job.progress.fraction() == pytest.approx(1.0)


def test_scoped_refresh_run_settles_every_group(refresh: Any) -> None:  # noqa: F811
    """The incremental Free tier declares the full plan but reports none of
    graph/enrich/plan through the ledger — only its own finalize can settle it.
    """
    store = refresh(CHANGED_AFTER)

    job = store.get_job("j-refresh")
    assert job is not None and job.status == "complete"
    assert job.progress is not None
    assert job.progress.pending_groups() == []
    assert job.progress.active is None
    assert job.progress.fraction() == pytest.approx(1.0)
