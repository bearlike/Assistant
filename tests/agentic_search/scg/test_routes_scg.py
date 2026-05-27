"""Route-level tests for the SCG glue on the Agentic Search HTTP surface.

Mirrors ``apps/mewbo_api/tests/test_agentic_search_runs_routes.py``: drives the
real Flask app through its test client, with the run store + SCG store swapped to
fresh JSON backends under tmp dirs (``reset_for_tests``). The ONLY thing mocked
is the ``MapSourceJob.start`` seam (so no real ``SessionRuntime`` / LLM is ever
spawned) — everything else exercises the real route → store path.

Covers:

* ``POST /sources/<id>/map`` starts a job (record + ``job_id``, 202) when SCG is
  enabled, and 503s when the ``scg.enabled`` gate is off;
* ``GET /scg`` returns node/edge/source counts + the mapped-source list;
* ``GET /sources/<id>/map/events`` replays the map-job event log over SSE
  (REUSING ``RunSseGenerator`` against ``load_map_job_events``);
* with ``scg.enabled=false`` the default :class:`SearchRunner` stays the echo
  replay (the orchestrated runner is NOT registered at init).

NEVER spawns a real LLM/session.
"""

from __future__ import annotations

import pytest
from mewbo_api import backend
from mewbo_api.agentic_search import store as store_mod
from mewbo_api.agentic_search.routes import (
    ScgConfig,
    _maybe_register_orchestrated_runner,
)
from mewbo_api.agentic_search.scg.map_progress import MapJobProgress
from mewbo_api.agentic_search.schemas import MapJobRecord
from mewbo_graph.scg import store as scg_store_mod


@pytest.fixture(autouse=True)
def _reset_stores():
    """Reset both the run store and the SCG structure store between tests."""
    store_mod.reset_for_tests()
    scg_store_mod.reset_for_tests()
    yield
    store_mod.reset_for_tests()
    scg_store_mod.reset_for_tests()


@pytest.fixture
def _scg_on(monkeypatch):
    """Force the ``scg.enabled`` gate ON for routes that read ``ScgConfig``."""
    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: True))


def _auth():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


# ── POST /sources/<id>/map ───────────────────────────────────────────────────


def test_map_source_starts_job(monkeypatch, _scg_on):
    """POST /map starts a MapSourceJob (mocked at the seam) and returns the record."""
    import mewbo_api.agentic_search.scg.map_job as map_job_mod

    captured = {}

    def _fake_start(source, *, store, runtime, model=None, **_):
        captured["source_id"] = source.source_id
        captured["source_type"] = source.source_type
        captured["model"] = model
        rec = MapJobRecord(
            job_id="map-test-1",
            source_id=source.source_id,
            source_type=source.source_type,
            status="queued",
        )
        store.create_map_job(rec)
        return rec

    monkeypatch.setattr(map_job_mod.MapSourceJob, "start", staticmethod(_fake_start))

    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/sources/github/map",
        json={"source_type": "openapi", "model": "fake/model"},
        headers=_auth(),
    )
    assert resp.status_code == 202, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["job_id"] == "map-test-1"
    assert body["job"]["source_id"] == "github"
    assert body["job"]["status"] == "queued"
    # The path id + body were threaded through to the seam.
    assert captured == {
        "source_id": "github",
        "source_type": "openapi",
        "model": "fake/model",
    }


def test_map_source_503_when_disabled(monkeypatch):
    """POST /map returns 503 when scg.enabled is off (the default)."""
    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: False))
    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/sources/github/map",
        json={"source_type": "openapi"},
        headers=_auth(),
    )
    assert resp.status_code == 503
    assert "disabled" in resp.get_json()["message"].lower()


def test_map_source_validation_error_is_400(monkeypatch, _scg_on):
    """A body missing the required source_type is a 400, not a 500."""
    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/sources/github/map", json={}, headers=_auth()
    )
    assert resp.status_code == 400


def test_map_source_requires_auth(_scg_on):
    """The map endpoint guards behind the API key like every other route."""
    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/sources/github/map", json={"source_type": "openapi"}
    )
    assert resp.status_code in (401, 403)


# ── GET /scg ─────────────────────────────────────────────────────────────────


def test_scg_introspection_returns_counts_and_sources(_scg_on):
    """GET /scg reports node/edge/source counts + the mapped-source list."""
    from mewbo_graph.scg.types import (
        ScgEdge,
        ScgNode,
        SourceDescriptor,
    )

    scg = scg_store_mod.get_scg_store()
    scg.upsert_source(
        SourceDescriptor(source_id="github", source_type="openapi", raw={})
    )
    def _node(name: str) -> ScgNode:
        return ScgNode(
            source_key=f"github#{name}",
            kind="entity_type",
            source_id="github",
            name=name,
        )

    scg.upsert_nodes([_node("Repo"), _node("Issue")])
    scg.upsert_edges(
        [ScgEdge(source="github#Repo", target="github#Issue", kind="HAS_ENTITY")]
    )

    client = backend.app.test_client()
    resp = client.get("/api/agentic_search/scg", headers=_auth())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enabled"] is True
    assert body["counts"]["sources"] == 1
    assert body["counts"]["nodes"] == 2
    assert body["counts"]["edges"] == 1
    assert body["sources"] == [{"source_id": "github", "source_type": "openapi"}]


def test_scg_introspection_503_when_disabled(monkeypatch):
    """GET /scg returns 503 when the feature is off (never touches the store)."""
    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: False))
    client = backend.app.test_client()
    resp = client.get("/api/agentic_search/scg", headers=_auth())
    assert resp.status_code == 503


# ── GET /sources/<id>/map/events (SSE) ───────────────────────────────────────


def test_map_events_replay_the_event_log(monkeypatch, _scg_on):
    """The map SSE stream replays the dual-written phase event log."""
    # Keep the idle loop tiny so the generator closes promptly in CI (the map
    # job has no terminal event, so it relies on the idle threshold to close).
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_SLEEP", "0.01")
    monkeypatch.setenv("MEWBO_AGENTIC_SSE_MAX_IDLE", "3")

    st = store_mod.get_store()
    st.create_map_job(
        MapJobRecord(job_id="map-sse-1", source_id="github", source_type="openapi")
    )
    for phase in ("connect", "introspect", "parse", "link", "finalize"):
        MapJobProgress.emit_phase(st, "map-sse-1", phase)

    client = backend.app.test_client()
    resp = client.get(
        "/api/agentic_search/sources/github/map/events", headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    body = resp.get_data(as_text=True)
    assert "event: phase" in body
    # Phases replay in deposit order — connect first, finalize last.
    assert body.index('"name": "connect"') < body.index('"name": "finalize"')


def test_map_events_404_when_no_job(_scg_on):
    """SSE 404s when the source has no map job (before opening a stream)."""
    client = backend.app.test_client()
    resp = client.get(
        "/api/agentic_search/sources/ghost/map/events", headers=_auth()
    )
    assert resp.status_code == 404


def test_map_events_unknown_job_id_404s(_scg_on):
    """An explicit ?job_id= for an unknown job is a 404."""
    client = backend.app.test_client()
    resp = client.get(
        "/api/agentic_search/sources/github/map/events?job_id=nope",
        headers=_auth(),
    )
    assert resp.status_code == 404


# ── default runner stays Echo while disabled ────────────────────────────────


def test_default_runner_stays_echo_when_scg_disabled(monkeypatch):
    """With scg.enabled=false the orchestrated runner is NOT registered at init."""
    from mewbo_api.agentic_search.runner import (
        EchoSearchRunner,
        get_search_runner,
        set_search_runner,
    )

    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: False))
    set_search_runner(EchoSearchRunner())  # known starting point

    _maybe_register_orchestrated_runner()

    assert isinstance(get_search_runner(), EchoSearchRunner)


def test_orchestrated_runner_registered_when_enabled_and_mapped(monkeypatch):
    """SCG enabled + a mapped source swaps in the OrchestratedSearchRunner."""
    from mewbo_api.agentic_search.runner import (
        EchoSearchRunner,
        get_search_runner,
        set_search_runner,
    )
    from mewbo_api.agentic_search.scg.orchestrated_runner import (
        OrchestratedSearchRunner,
    )
    from mewbo_graph.scg.types import SourceDescriptor

    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: True))
    set_search_runner(EchoSearchRunner())
    scg_store_mod.get_scg_store().upsert_source(
        SourceDescriptor(source_id="github", source_type="openapi", raw={})
    )

    _maybe_register_orchestrated_runner()

    assert isinstance(get_search_runner(), OrchestratedSearchRunner)
    # Restore the echo default so other tests are unaffected.
    set_search_runner(EchoSearchRunner())


def test_orchestrated_runner_not_registered_when_no_source(monkeypatch):
    """SCG enabled but an EMPTY graph keeps the echo runner (nothing to route)."""
    from mewbo_api.agentic_search.runner import (
        EchoSearchRunner,
        get_search_runner,
        set_search_runner,
    )

    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: True))
    set_search_runner(EchoSearchRunner())

    _maybe_register_orchestrated_runner()

    assert isinstance(get_search_runner(), EchoSearchRunner)
