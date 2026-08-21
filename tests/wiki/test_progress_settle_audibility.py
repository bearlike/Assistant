"""Settling the ledger must not also settle the question.

``ProgressReporter.settle`` closes whatever is still open when an index ends,
so a completed job reports a full bar. That is necessary and it is also the
easiest place in this subsystem to hide a defect: a step whose work ran but
reported nothing looks identical, after settling, to a phase the run never
reached. One of those is a run shape and the other is a reporting bug, and an
uninstrumented second implementation of the scan phase was found only because
a finished job still showed its steps ``pending``.

These tests pin the discriminator: a step left unreported while its OWN phase
completed is named, not quietly relabelled.
"""
from __future__ import annotations

from types import SimpleNamespace

from mewbo_core.contracts.progress import StepSpec
from mewbo_graph.plugins.wiki._ctx import ProgressReporter
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob


def _reporter() -> ProgressReporter:
    """A reporter with no job, so persistence no-ops and the ledger is the subject."""
    return ProgressReporter(SimpleNamespace(job_id="", slug="acme/repo", store=None))


def _stored_reporter(tmp_path) -> tuple[ProgressReporter, JsonWikiStore]:
    """A reporter over a real store, so emitted timeline events are readable."""
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(
        IndexingJob(
            jobId="j1",
            slug="acme/repo",
            status="scanning",
            scannedCount=0,
            totalCount=0,
            currentFile=None,
        )
    )
    ctx = SimpleNamespace(job_id="j1", slug="acme/repo", store=store)
    return ProgressReporter(ctx, interval_s=0.0), store


def _warnings(store: JsonWikiStore) -> list[dict]:
    return [
        event
        for event in store.load_job_events("j1")
        if event.get("type") == "log" and event.get("level") == "warn"
    ]


def _declare(progress: ProgressReporter) -> None:
    progress.declare([
        StepSpec(key="scan.discover", label="Discovering source files", group="scan"),
        StepSpec(key="scan.inspect_files", label="Inspecting source files", group="scan"),
        StepSpec(key="scan.persist_manifest", label="Persisting file manifest", group="scan"),
        StepSpec(key="pages.write", label="Writing wiki pages", group="pages"),
    ])


def _by_key(progress: ProgressReporter) -> dict:
    return {record.key: record for record in progress._ledger.steps}


def test_a_phase_the_run_never_reached_settles_quietly() -> None:
    """A wholly untouched group is a run shape, not a defect — say nothing."""
    progress = _reporter()
    _declare(progress)

    progress.settle()

    records = _by_key(progress)
    assert records["pages.write"].state == "skipped"
    assert records["pages.write"].note == "not reached this run"
    assert progress.settled_unreported == []


def test_a_step_silent_while_its_phase_completed_is_named() -> None:
    """The scan-shaped defect: the phase ran, one implementation reported nothing."""
    progress = _reporter()
    _declare(progress)
    with progress.step("scan.discover"):
        pass

    progress.settle()

    records = _by_key(progress)
    # Still terminal, so the bar reaches 100% — but the note refuses to claim
    # the work was skipped, and the caller is handed the names to surface.
    assert records["scan.inspect_files"].state == "skipped"
    assert records["scan.inspect_files"].note == "never reported — its phase ran without it"
    assert progress.settled_unreported == ["scan.inspect_files", "scan.persist_manifest"]
    # A group nothing touched is still quiet even when a SIBLING group is loud.
    assert records["pages.write"].note == "not reached this run"


def test_an_open_fan_out_step_closes_done_without_complaint() -> None:
    """A step opened by ``report`` has no closing scope; finalize is its end."""
    progress = _reporter()
    _declare(progress)
    progress.report("pages.write", 8, 8)

    progress.settle()

    records = _by_key(progress)
    assert records["pages.write"].state == "done"
    # No note: `note` explains a skip or a failure, and the contract rejects
    # one on a step that finished its work. Closing late is not an excuse.
    assert records["pages.write"].note == ""
    # Doing work and closing late is not a reporting gap.
    assert "pages.write" not in progress.settled_unreported


def test_the_gap_lands_on_the_job_timeline_not_only_on_the_reporter(tmp_path) -> None:
    """A property no one reads is not a warning. It has to reach the timeline.

    This is the assertion that makes the whole discriminator worth having: the
    operator is already watching the activity log, and that is where a phase
    that ran without reporting has to say so.
    """
    progress, store = _stored_reporter(tmp_path)
    _declare(progress)
    with progress.step("scan.discover"):
        pass

    progress.settle()

    warnings = _warnings(store)
    assert len(warnings) == 1
    assert "scan.inspect_files" in warnings[0]["text"]
    assert "scan.persist_manifest" in warnings[0]["text"]
    # The untouched group is a run shape, so it stays out of the warning.
    assert "pages.write" not in warnings[0]["text"]


def test_an_ordinary_settle_says_nothing(tmp_path) -> None:
    """A closed fan-out and an unreached phase are both normal — stay quiet."""
    progress, store = _stored_reporter(tmp_path)
    _declare(progress)
    progress.report("pages.write", 8, 8)

    progress.settle()

    assert _warnings(store) == []


def test_settling_is_idempotent() -> None:
    """Finalize can run twice on a resumed job; the second pass adds nothing."""
    progress = _reporter()
    _declare(progress)
    with progress.step("scan.discover"):
        pass
    progress.settle()
    first = progress.settled_unreported

    progress.settle()

    # Every record was already terminal, so nothing is re-closed and nothing
    # is re-reported — a second finalize must not invent a second gap.
    assert progress.settled_unreported == []
    assert first == ["scan.inspect_files", "scan.persist_manifest"]
    assert all(record.terminal for record in progress._ledger.steps)
