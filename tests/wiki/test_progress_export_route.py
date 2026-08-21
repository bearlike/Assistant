"""HTTP and SSE contracts for the declared indexing-progress ledger.

The route reads the real ``IndexingJob`` snapshot from a temporary JSON store,
and the stream reads the same append-only job event log an indexer writes. That
keeps the snapshot, whole-operation export and replay contracts connected.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from mewbo_core.contracts.progress import ProgressLedger, StepSpec
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

API_KEY = "test-progress-key"
JOB_ID = "job-progress"
SLUG = "github.com/example/project"


@pytest.fixture()
def store(tmp_path: Path) -> JsonWikiStore:
    return JsonWikiStore(root_dir=tmp_path / "wiki")


@pytest.fixture()
def client(monkeypatch, store: JsonWikiStore):
    monkeypatch.setenv("MEWBO_MASTER_API_TOKEN", API_KEY)
    monkeypatch.setattr("mewbo_api.backend.MASTER_API_TOKEN", API_KEY, raising=False)

    import mewbo_api.wiki.routes as routes_mod
    from flask import Flask

    app = Flask(__name__)
    app.config["TESTING"] = True
    routes_mod.register(app, SimpleNamespace(wiki_store=store))

    yield app.test_client(), store

    routes_mod._runtime = None
    routes_mod._hook_manager = None


def _headers(**extra: str) -> dict[str, str]:
    return {"X-API-Key": API_KEY, **extra}


def _job(*, progress: ProgressLedger | None = None, status: str = "scanning") -> IndexingJob:
    return IndexingJob(
        jobId=JOB_ID,
        slug=SLUG,
        status=status,
        scannedCount=0,
        totalCount=0,
        currentFile=None,
        phase="scan",
        progress=progress,
    )


def _frames(response) -> list[str]:
    return response.get_data(as_text=True).split("\n\n")


def test_progress_export_returns_the_declared_outline_before_every_step_starts(client) -> None:
    http, store = client
    ledger = ProgressLedger.from_plan([
        StepSpec(key="clone.repository", label="Cloning repository", group="clone"),
        StepSpec(key="scan.files", label="Scanning files", group="scan", unit="files"),
    ])
    store.create_job(_job(progress=ledger))

    response = http.get(f"/v1/wiki/index/{JOB_ID}/progress", headers=_headers())

    assert response.status_code == 200
    body = response.get_json()
    assert {
        key: body[key]
        for key in ("jobId", "slug", "phase", "status", "isActive")
    } == {
        "jobId": JOB_ID,
        "slug": SLUG,
        "phase": "scan",
        "status": "scanning",
        "isActive": True,
    }
    assert body["version"] == 1
    assert body["activeKey"] is None
    assert [step["key"] for step in body["steps"]] == [
        "clone.repository",
        "scan.files",
    ]
    assert [step["state"] for step in body["steps"]] == ["pending", "pending"]
    assert body["groups"] == [
        {"key": "clone", "steps": [body["steps"][0]]},
        {"key": "scan", "steps": [body["steps"][1]]},
    ]


def test_progress_export_returns_an_empty_valid_ledger_for_a_legacy_job(client) -> None:
    http, store = client
    store.create_job(_job())

    response = http.get(f"/v1/wiki/index/{JOB_ID}/progress", headers=_headers())

    assert response.status_code == 200
    body = response.get_json()
    assert body["jobId"] == JOB_ID
    assert body["version"] == 1
    assert body["fraction"] == 0.0
    assert body["etaSeconds"] is None
    assert body["elapsedSeconds"] is None
    assert body["activeKey"] is None
    assert body["groups"] == []
    assert body["steps"] == []


def test_job_snapshots_carry_the_ledger_with_camel_case_clocks_and_no_unset_values(client) -> None:
    http, store = client
    ledger = ProgressLedger.from_plan([
        StepSpec(key="clone.repository", label="Cloning repository", group="clone"),
        StepSpec(key="scan.files", label="Scanning files", group="scan", unit="files"),
    ])
    now = datetime.now(timezone.utc)
    ledger.enter("clone.repository", now)
    ledger.finish("clone.repository", now)
    store.create_job(_job(progress=ledger))

    snapshot = http.get(f"/v1/wiki/index/{JOB_ID}", headers=_headers()).get_json()
    active = http.get("/v1/wiki/jobs/active", headers=_headers()).get_json()
    active_snapshot = next(item for item in active if item["jobId"] == JOB_ID)

    for body in (snapshot, active_snapshot):
        progress = body["progress"]
        completed, pending = progress["steps"]
        assert completed["startedAt"]
        assert completed["endedAt"]
        assert "started_at" not in completed
        assert "ended_at" not in completed
        assert "startedAt" not in pending
        assert "endedAt" not in pending
        assert "current" not in pending
        assert "total" not in pending


def test_progress_job_events_are_forwarded_with_their_payload_intact(client) -> None:
    http, store = client
    store.create_job(_job(status="complete"))
    payload = {
        "version": 1,
        "fraction": 0.4,
        "steps": [{"key": "scan.files", "state": "running"}],
    }
    store.append_job_event(JOB_ID, {"type": "progress", "ledger": payload})
    store.append_job_event(JOB_ID, {"type": "complete", "landingPageId": "overview"})

    response = http.get(f"/v1/wiki/index/{JOB_ID}/stream", headers=_headers())
    progress_frame = next(frame for frame in _frames(response) if "event: progress" in frame)

    lines = progress_frame.split("\n")
    assert lines[0] == "id: 0"
    assert lines[1] == "event: progress"
    assert json.loads(lines[2].removeprefix("data: ")) == {"ledger": payload}


def test_last_event_id_does_not_replay_an_already_delivered_progress_event(client) -> None:
    http, store = client
    store.create_job(_job(status="complete"))
    store.append_job_event(JOB_ID, {"type": "progress", "ledger": {"version": 1}})
    store.append_job_event(JOB_ID, {"type": "complete", "landingPageId": "overview"})

    response = http.get(
        f"/v1/wiki/index/{JOB_ID}/stream",
        headers=_headers(**{"Last-Event-ID": "0"}),
    )
    frames = _frames(response)

    assert not any("event: progress" in frame for frame in frames)
    assert any("id: 1\nevent: complete" in frame for frame in frames)
