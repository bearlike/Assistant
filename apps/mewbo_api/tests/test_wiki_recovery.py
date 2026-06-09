"""Tests for JobRecovery — restart durability via the existing refresh path."""
from __future__ import annotations

from unittest.mock import MagicMock

from mewbo_api.wiki.recovery import JobRecovery
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob, Project


def _store(tmp_path) -> JsonWikiStore:
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    return store


def _job(store, job_id, slug, status) -> None:
    store.create_job(IndexingJob(
        jobId=job_id, slug=slug, status=status,
        scannedCount=0, totalCount=0, currentFile=None,
    ))


def _project(store, slug) -> None:
    store.create_project(Project(
        slug=slug, source="gitea", lang="Python",
        indexedAt="2026-06-07T00:00:00Z", pages=1, desc="x",
    ))


def _runtime(store) -> MagicMock:
    """A runtime whose session resolution yields a real string (the store
    persists the session_id, so a bare MagicMock would crash the re-drive)."""
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-recovery"
    rt.start_async.return_value = True
    return rt


def test_recover_redrives_resume_once_per_slug(tmp_path):
    store = _store(tmp_path)
    _project(store, "host/a")
    _job(store, "j1", "host/a", "scanning")
    _job(store, "j2", "host/a", "queued")  # same slug
    runtime = _runtime(store)

    refreshed = JobRecovery.recover_interrupted(store, runtime)

    # Checkpoint-aware resume re-drives the FIRST stranded job for the slug
    # (reusing its job_id) and flips it back to a running state ...
    assert store.get_job("j1").status == "scanning"
    # ... the same-slug sibling that is NOT re-driven is left interrupted.
    assert store.get_job("j2").status == "interrupted"
    # ... and resume fires ONCE for the distinct slug.
    assert refreshed == ["host/a"]
    # The re-driven session was started against the reused job_id.
    runtime.start_async.assert_called_once()


def test_recover_ignores_terminal_jobs(tmp_path):
    store = _store(tmp_path)
    _project(store, "host/done")
    _job(store, "jc", "host/done", "complete")
    _job(store, "jf", "host/done", "failed")
    _job(store, "jx", "host/done", "cancelled")
    runtime = _runtime(store)

    refreshed = JobRecovery.recover_interrupted(store, runtime)

    assert refreshed == []
    assert store.get_job("jc").status == "complete"


def test_recover_caps_retries_per_slug(tmp_path):
    store = _store(tmp_path)
    _project(store, "host/loop")
    _job(store, "jl", "host/loop", "scanning")
    # Pre-seed the slug-keyed retry counter at the cap so recovery refuses to
    # re-drive (the counter is slug-keyed, NOT parked on the submission sidecar).
    for _ in range(JobRecovery.MAX_RETRIES):
        store.bump_recovery_attempts("host/loop")
    runtime = _runtime(store)

    refreshed = JobRecovery.recover_interrupted(store, runtime)

    assert refreshed == []  # over the cap — not re-driven
    # A retry-exhausted job is moved to terminal ``failed`` so it stops being a
    # zombie in the active-jobs surface (previously it was left ``interrupted``
    # forever, which kept the project pinned as "still indexing").
    assert store.get_job("jl").status == "failed"
    assert any(e["type"] == "error" for e in store.load_job_events("jl"))


def _wizard_sub(slug):
    """A non-default submission whose dirs/files/depth must survive recovery."""
    return {
        "repoUrl": f"https://{slug}",
        "slug": slug,
        "platform": "gitea",
        "depth": "concise",
        "language": "Python",
        "model": "anthropic/claude-sonnet-4-6",
        "filterMode": "include",
        "dirs": ["src", "docs"],
        "files": ["README.md"],
    }


def test_recovery_counter_does_not_corrupt_submission(tmp_path):
    """Two restart-recovery cycles: the resume query carries the ORIGINAL
    non-default submission fields, the slug-keyed counter increments per cycle
    (counter lives on its own surface, NOT the submission sidecar), and the
    automatic path does NOT reset the cap (user_initiated=False)."""
    from mewbo_api.wiki import resume as resume_mod

    slug = "git.home/org/repo"
    store = _store(tmp_path)
    _project(store, slug)
    # An original job whose submission has NON-default dirs/files/depth.
    _job(store, "j-orig", slug, "scanning")
    store.save_job_submission("j-orig", _wizard_sub(slug))

    runtime = _runtime(store)

    # Capture the resume query rendered each cycle (the indexer's user_query).
    # Patch the name in the module that USES it (resume.py imported it by name).
    rendered = resume_mod._render_resume_query
    captured: list[str] = []

    def _capture_render(s, job, plan):
        q = rendered(s, job, plan)
        captured.append(q)
        return q

    import unittest.mock as mock
    with mock.patch.object(resume_mod, "_render_resume_query", side_effect=_capture_render):
        # --- Cycle 1 ---
        JobRecovery.recover_interrupted(store, runtime)
        # --- Cycle 2 (simulate the API dying + restarting again) ---
        JobRecovery.recover_interrupted(store, runtime)

    # Both cycles re-drove the slug (the SAME job_id is reused, so both render).
    assert len(captured) == 2
    # The resume query of the SECOND cycle still carries the original non-default
    # fields from the stored submission — NOT a minimal project-derived fallback.
    second = captured[-1]
    assert "concise" in second
    assert "'src', 'docs'" in second
    assert "README.md" in second
    assert "include" in second

    # The slug-keyed counter incremented across the two cycles. The automatic
    # path must NOT reset it (only the user-initiated resume endpoint does).
    assert store.get_recovery_attempts(slug) == 2

    # The original submission sidecar was never polluted with a counter key.
    orig = store.get_job_submission("j-orig")
    assert "_recovery_attempts" not in orig


def test_recovery_counter_caps_across_generations(tmp_path):
    """The slug-keyed cap bounds automatic re-drives — once at MAX_RETRIES,
    recovery stops re-driving the slug (the automatic path never resets it)."""
    slug = "git.home/org/loop"
    store = _store(tmp_path)
    _project(store, slug)
    _job(store, "j0", slug, "scanning")
    store.save_job_submission("j0", _wizard_sub(slug))
    runtime = _runtime(store)

    for _ in range(JobRecovery.MAX_RETRIES + 3):
        JobRecovery.recover_interrupted(store, runtime)

    # Re-drives (each calls start_async once) are bounded by the cap regardless
    # of how many restarts happen.
    assert runtime.start_async.call_count == JobRecovery.MAX_RETRIES
    # After the cap, the job is moved to terminal failed (no perpetual zombie).
    assert store.get_job("j0").status == "failed"


def test_recover_picks_up_interrupted_job(tmp_path):
    """A job left in 'interrupted' (refresh never completed before the API died
    again) IS picked up on the next restart, and is bounded by the cap."""
    store = _store(tmp_path)
    _project(store, "host/i")
    _job(store, "ji", "host/i", "interrupted")  # already interrupted
    runtime = _runtime(store)

    refreshed = JobRecovery.recover_interrupted(store, runtime)
    assert refreshed == ["host/i"]
    assert store.get_recovery_attempts("host/i") == 1


def test_init_wiki_calls_recovery(monkeypatch, tmp_path):
    """init_wiki re-drives interrupted jobs instead of marking them failed."""
    import mewbo_api.wiki as wiki_pkg

    store = _store(tmp_path)
    _project(store, "host/a")
    _job(store, "j1", "host/a", "scanning")
    runtime = _runtime(store)

    called: dict = {}

    def _fake_recover(s, rt):
        called["args"] = (s, rt)
        return ["host/a"]

    monkeypatch.setattr(wiki_pkg.JobRecovery, "recover_interrupted", staticmethod(_fake_recover))
    wiki_pkg._run_recovery(runtime.wiki_store, runtime)
    assert called["args"] == (store, runtime)
    # The legacy reap helper is gone.
    assert not hasattr(wiki_pkg, "_reap_stranded_jobs")
