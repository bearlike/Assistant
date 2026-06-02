"""Event-ordering tests for the wiki SSE generator."""
from __future__ import annotations

import json

import pytest
from mewbo_api.wiki.events import WikiSseGenerator
from mewbo_api.wiki.store import JsonWikiStore
from mewbo_api.wiki.types import IndexingJob


def _event_types(frames):
    """Extract the ``event:`` types from SSE frames.

    Each frame may be prefixed with an ``id: <idx>`` line and/or a ``:``
    comment line (the primer + heartbeat padding), so the ``event:`` line
    is not necessarily the first one. Strip the leading non-event lines
    before reading the type.
    """
    types = []
    for frame in frames:
        for line in frame.split("\n"):
            if line.startswith("event:"):
                types.append(line.split(":", 1)[1].strip())
                break
    return types


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def test_generator_emits_queued_then_finalizing_then_complete(store):
    """Replays a pre-populated event log in idx order."""
    job_id = "job-x"
    store.create_job(IndexingJob(jobId=job_id, slug="x/y", status="queued",
                                  scannedCount=0, totalCount=0, currentFile=None))
    store.append_job_event(job_id, {"type": "queued", "jobId": job_id, "slug": "x/y", "totalCount": 3})  # noqa: E501
    store.append_job_event(job_id, {"type": "scanning", "file": "a.py", "index": 1, "totalCount": 3})  # noqa: E501
    store.append_job_event(job_id, {"type": "scanned", "file": "a.py", "index": 1, "totalCount": 3})  # noqa: E501
    store.append_job_event(job_id, {"type": "finalizing", "scannedCount": 3, "totalCount": 3})
    store.append_job_event(job_id, {"type": "complete", "landingPageId": "overview", "pageCount": 1})  # noqa: E501
    store.update_job(job_id, status="complete")

    gen = WikiSseGenerator(store=store, job_id=job_id, max_idle_cycles=2, sleep_s=0)
    output = list(gen.generate())
    assert _event_types(output) == ["queued", "scanning", "scanned", "finalizing", "complete"]


def test_generator_yields_only_events_after_idx(store):
    job_id = "job-y"
    store.create_job(IndexingJob(jobId=job_id, slug="x/y", status="cancelled",
                                  scannedCount=0, totalCount=0, currentFile=None))
    store.append_job_event(job_id, {"type": "queued", "jobId": job_id, "slug": "x/y", "totalCount": 0})  # noqa: E501
    store.append_job_event(job_id, {"type": "cancelled"})
    gen = WikiSseGenerator(store=store, job_id=job_id, after_idx=0, max_idle_cycles=2, sleep_s=0)
    output = list(gen.generate())
    assert _event_types(output) == ["cancelled"]


def test_generator_sse_format(store):
    """Each frame is `id: <idx>\\nevent: <type>\\ndata: <json without type>\\n\\n`.

    The ``id:`` line lets EventSource's auto-reconnect carry the
    ``Last-Event-ID`` header so a flaky proxy drop can resume from the
    same point instead of replaying the whole transcript.
    """
    job_id = "job-z"
    store.create_job(IndexingJob(jobId=job_id, slug="x/y", status="complete",
                                  scannedCount=0, totalCount=0, currentFile=None))
    store.append_job_event(job_id, {"type": "queued", "jobId": job_id, "slug": "x/y", "totalCount": 2})  # noqa: E501
    gen = WikiSseGenerator(store=store, job_id=job_id, max_idle_cycles=1, sleep_s=0)
    frames = list(gen.generate())
    queued_frame = next(f for f in frames if "event: queued" in f)
    lines = queued_frame.rstrip("\n").split("\n")
    assert lines[0] == "id: 0"
    assert lines[1] == "event: queued"
    payload = json.loads(lines[2].removeprefix("data: "))
    assert "type" not in payload
    assert "idx" not in payload
    assert payload["jobId"] == job_id
    assert payload["totalCount"] == 2


def test_generator_idle_closes_without_terminal(store):
    """Generator exits after max_idle_cycles if no terminal event arrives."""
    job_id = "job-idle"
    store.create_job(IndexingJob(jobId=job_id, slug="x/y", status="queued",
                                  scannedCount=0, totalCount=0, currentFile=None))
    # No events at all — generator must close after max_idle_cycles
    gen = WikiSseGenerator(store=store, job_id=job_id, max_idle_cycles=3, sleep_s=0)
    output = list(gen.generate())
    # No real event frames emitted (the primer + heartbeats are comments
    # / heartbeat frames, not real events)
    assert [t for t in _event_types(output) if t != "heartbeat"] == []


def test_generator_heartbeat_emitted(store):
    """A heartbeat is emitted every heartbeat_every idle cycles."""
    job_id = "job-hb"
    store.create_job(IndexingJob(jobId=job_id, slug="x/y", status="queued",
                                  scannedCount=0, totalCount=0, currentFile=None))
    # heartbeat_every=2 → after 2 idle cycles a heartbeat frame is emitted
    gen = WikiSseGenerator(
        store=store, job_id=job_id,
        max_idle_cycles=5, sleep_s=0, heartbeat_every=2,
    )
    output = list(gen.generate())
    hb_frames = [f for f in output if "event: heartbeat" in f]
    assert len(hb_frames) >= 1


def test_heartbeat_emitted_after_idle(store):
    """Heartbeat frames are interspersed when there are no new events.

    Setup: pre-populate one real event then nothing more.  With
    heartbeat_every=2 and max_idle_cycles=5 we expect at least 2
    heartbeat frames in the output (at idle cycle 2 and 4).
    """
    job_id = "job-hb2"
    store.create_job(IndexingJob(jobId=job_id, slug="a/b", status="queued",
                                  scannedCount=0, totalCount=1, currentFile=None))
    store.append_job_event(  # noqa: E501
        job_id, {"type": "queued", "jobId": job_id, "slug": "a/b", "totalCount": 1}
    )

    gen = WikiSseGenerator(
        store=store, job_id=job_id,
        max_idle_cycles=5, sleep_s=0, heartbeat_every=2,
    )
    output = list(gen.generate())
    hb_frames = [f for f in output if "event: heartbeat" in f]
    real_frames = [f for f in output if "event: queued" in f]
    # One real event + at least one heartbeat (at idle=2, idle=4)
    assert len(real_frames) == 1
    assert len(hb_frames) >= 1


def test_resume_replays_queued_and_scanned(store):
    """after_idx=-1 replays all; after_idx=2 skips first 3 events (idx 0,1,2)."""
    job_id = "job-resume"
    store.create_job(IndexingJob(jobId=job_id, slug="x/y", status="queued",
                                  scannedCount=0, totalCount=2, currentFile=None))
    store.append_job_event(job_id, {"type": "queued", "jobId": job_id, "slug": "x/y", "totalCount": 2})  # idx 0  # noqa: E501
    store.append_job_event(job_id, {"type": "scanning", "file": "a.py", "index": 1, "totalCount": 2})    # idx 1  # noqa: E501
    store.append_job_event(job_id, {"type": "scanned",  "file": "a.py", "index": 1, "totalCount": 2})    # idx 2  # noqa: E501
    store.append_job_event(job_id, {"type": "scanning", "file": "b.py", "index": 2, "totalCount": 2})    # idx 3  # noqa: E501
    store.append_job_event(job_id, {"type": "scanned",  "file": "b.py", "index": 2, "totalCount": 2})    # idx 4  # noqa: E501
    store.update_job(job_id, status="queued")

    def _types(gen):
        return _event_types(list(gen.generate()))

    # Replay from start (after_idx=-1) — all 5 events including queued
    types_all = _types(WikiSseGenerator(
        store=store, job_id=job_id, after_idx=-1, max_idle_cycles=2, sleep_s=0,
    ))
    assert types_all == ["queued", "scanning", "scanned", "scanning", "scanned"]

    # Resume from after idx=2 — only idx 3 and 4 emitted
    types_resume = _types(WikiSseGenerator(
        store=store, job_id=job_id, after_idx=2, max_idle_cycles=2, sleep_s=0,
    ))
    assert types_resume == ["scanning", "scanned"]
