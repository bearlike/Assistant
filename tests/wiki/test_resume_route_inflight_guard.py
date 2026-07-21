"""Route-level contract for ``POST /v1/wiki/index/<job_id>/resume``: the
in-flight guard, the restart-flag plumbing, and the ResumeCountError
mapping (the fail-closed count reads).

Mirrors the fixture shape in ``tests/wiki/test_routes.py`` (a stub runtime +
Flask test client over the registered wiki blueprint) rather than importing
its module-scoped fixtures directly, so this file stays a self-contained new
addition instead of reaching into another test module's private helpers.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

API_KEY = "test-key-123"


@pytest.fixture()
def store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def runtime_stub(store):
    rt = MagicMock()
    rt.wiki_store = store
    rt.resolve_session.return_value = "sess-stub"
    rt.start_async.return_value = True
    rt.cancel.return_value = True
    rt.is_running.return_value = False
    return rt


@pytest.fixture()
def client(tmp_path: Path, monkeypatch, store, runtime_stub):
    monkeypatch.setenv("MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask
    from mewbo_api.wiki.routes import register

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    register(flask_app, runtime_stub)

    yield flask_app.test_client(), store, runtime_stub

    routes_mod._runtime = None


def _seed_resumable_job(store, *, job_id="rj1", slug="org/repo"):
    from mewbo_graph.wiki.types import IndexingJob

    store.create_job(IndexingJob(
        jobId=job_id, slug=slug, status="interrupted",
        scannedCount=0, totalCount=0, currentFile=None,
        model="anthropic/claude-sonnet-4-6", commitSha="deadbeef",
    ))
    return job_id


def test_resume_index_in_flight_is_409(client):
    """POST /index/<id>/resume while the job's session is still running → 409.

    Mirrors the mapping ``POST /qa`` already applies to
    ``WikiQaSession.follow_up``'s equivalent in-flight ``RuntimeError``.
    """
    c, store, runtime_stub = client
    job_id = _seed_resumable_job(store)
    store.attach_job_session(job_id, "sess-running")
    runtime_stub.is_running.return_value = True

    resp = c.post(f"/v1/wiki/index/{job_id}/resume", headers={"X-Api-Key": API_KEY})

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "validation"
    runtime_stub.start_async.assert_not_called()


# ── The restart flag is reachable from the route ───────────────────────────


def test_resume_index_restart_flag_reaches_for_restart_plan(client):
    """POST {"restart": true} must drive ResumePlan.for_restart(), not the
    default checkpoint resume — the whole point of wiring this flag through."""
    from mewbo_graph.wiki.types import make_graph_node

    c, store, runtime_stub = client
    job_id = _seed_resumable_job(store)
    # Seed a populated graph — a checkpoint resume would normally SKIP it;
    # restart=True must rebuild regardless.
    store.upsert_nodes("org/repo", [
        make_graph_node(
            slug="org/repo", node_id="n1", type="Function", name="f",
            file="a.py", range=(0, 1),
        ),
    ])

    resp = c.post(
        f"/v1/wiki/index/{job_id}/resume",
        json={"restart": True},
        headers={"X-Api-Key": API_KEY},
    )

    assert resp.status_code == 202
    kw = runtime_stub.start_async.call_args.kwargs
    assert "RESTART" in kw["user_query"]


def test_resume_index_default_restart_false_is_a_checkpoint_resume(client):
    """Omitting the body (or restart: false) is the existing checkpoint-resume
    default — no behaviour change for every caller that doesn't opt in."""
    c, store, runtime_stub = client
    job_id = _seed_resumable_job(store)

    resp = c.post(f"/v1/wiki/index/{job_id}/resume", headers={"X-Api-Key": API_KEY})

    assert resp.status_code == 202
    kw = runtime_stub.start_async.call_args.kwargs
    assert "RESUME" in kw["user_query"]
    assert "RESTART" not in kw["user_query"]


def test_resume_index_rejects_unknown_body_fields(client):
    """extra="forbid" on the request model — a typo must 400, not silently no-op
    into the default resume behaviour."""
    c, store, _ = client
    job_id = _seed_resumable_job(store)

    resp = c.post(
        f"/v1/wiki/index/{job_id}/resume",
        json={"restrat": True},
        headers={"X-Api-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "validation"


# ── ResumeCountError → 503, distinct from the 409 in-flight mapping ─────────


def test_resume_index_count_failure_is_503(client, monkeypatch):
    """A store-read failure the resume decision depends on must refuse with a
    clear, distinct status — never silently rebuild, never collide with the
    409 in-flight mapping (ResumeCountError IS a RuntimeError subclass)."""
    import mewbo_api.wiki.routes as routes_mod
    from mewbo_graph.wiki.resume import ResumeCountError

    c, store, runtime_stub = client
    job_id = _seed_resumable_job(store)

    def _raise(*args, **kwargs):
        raise ResumeCountError("graph node count read failed for org/repo: boom")

    monkeypatch.setattr(routes_mod.WikiResume, "resume", staticmethod(_raise))

    resp = c.post(f"/v1/wiki/index/{job_id}/resume", headers={"X-Api-Key": API_KEY})

    assert resp.status_code == 503
    assert resp.get_json()["code"] == "network"
    runtime_stub.start_async.assert_not_called()
