"""Route-level tests for the SCG glue on the Agentic Search HTTP surface.

Mirrors ``apps/mewbo_api/tests/test_agentic_search_runs_routes.py``: drives the
real Flask app through its test client, with the run store + SCG store swapped to
fresh JSON backends under tmp dirs (``reset_for_tests``). The ONLY thing mocked
is the ``MapSourceJob.start`` seam (so no real ``SessionRuntime`` / LLM is ever
spawned) — everything else exercises the real route → store path.

Covers:

* ``POST /sources/<id>/map`` starts a job (record + ``job_id``, 202) when SCG is
  enabled, and 503s when the ``scg.enabled`` gate is off;
* ``GET /sources/<id>/map/jobs`` lists snapshots latest-first and
  ``GET /sources/<id>/map/jobs/<job_id>`` returns one (404 on mismatch);
* ``GET /scg`` returns node/edge/source counts + the mapped-source list;
* ``GET /sources/<id>/map/events`` replays the map-job event log over SSE
  (REUSING ``RunSseGenerator`` against ``load_map_job_events``);
* ``POST /runs`` accepts + echoes the per-run ``tier`` budget knob;
* :func:`get_search_runner` resolves PER RUN — echo while disabled / unmapped,
  orchestrated once a source is mapped (no restart), explicit override wins.

NEVER spawns a real LLM/session.
"""

from __future__ import annotations

import pytest
from mewbo_api import backend
from mewbo_api.agentic_search import store as store_mod
from mewbo_api.agentic_search.routes import ScgConfig
from mewbo_api.agentic_search.runner import set_search_runner
from mewbo_api.agentic_search.scg.map_progress import MapJobProgress
from mewbo_api.agentic_search.schemas import MapJobRecord
from mewbo_graph.scg import store as scg_store_mod


@pytest.fixture(autouse=True)
def _reset_stores():
    """Reset the run store, SCG structure store, and runner override between tests."""
    store_mod.reset_for_tests()
    scg_store_mod.reset_for_tests()
    set_search_runner(None)
    yield
    store_mod.reset_for_tests()
    scg_store_mod.reset_for_tests()
    set_search_runner(None)


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


def test_map_source_text_type_rejected_422(monkeypatch, _scg_on):
    """source_type "text" 422s up-front — its provider is never registered.

    ``StructureProviderRegistry.with_defaults()`` excludes the schemaless
    ``LlmStructureProvider`` (it needs an injected LLM), so a "text" map job
    would always fail in-session at ``scg_build_structure``. The contract is
    honest instead: a structured 422 before any job/session starts.
    """
    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/sources/github/map",
        json={"source_type": "text", "descriptor": {"description": "a blurb"}},
        headers=_auth(),
    )
    assert resp.status_code == 422
    assert "not yet supported" in resp.get_json()["message"]
    # No job record was created.
    assert store_mod.get_store().list_map_jobs(source_id="github") == []


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


# ── map-job snapshot routes ──────────────────────────────────────────────────


def test_list_map_jobs_latest_first(_scg_on):
    """GET /sources/<id>/map/jobs returns {"jobs": [...]} latest-first."""
    st = store_mod.get_store()
    st.create_map_job(
        MapJobRecord(
            job_id="map-old", source_id="github", source_type="openapi",
            created_at="2026-06-01T00:00:00+00:00",
        )
    )
    st.create_map_job(
        MapJobRecord(
            job_id="map-new", source_id="github", source_type="openapi",
            created_at="2026-06-02T00:00:00+00:00",
        )
    )
    st.create_map_job(
        MapJobRecord(job_id="map-other", source_id="linear", source_type="openapi")
    )

    client = backend.app.test_client()
    resp = client.get("/api/agentic_search/sources/github/map/jobs", headers=_auth())
    assert resp.status_code == 200
    jobs = resp.get_json()["jobs"]
    assert [j["job_id"] for j in jobs] == ["map-new", "map-old"]


def test_get_map_job_snapshot_and_404s(_scg_on):
    """GET /sources/<id>/map/jobs/<job_id> returns {"job": ...}; 404 on mismatch."""
    st = store_mod.get_store()
    st.create_map_job(
        MapJobRecord(job_id="map-1", source_id="github", source_type="openapi")
    )

    client = backend.app.test_client()
    resp = client.get(
        "/api/agentic_search/sources/github/map/jobs/map-1", headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.get_json()["job"]["job_id"] == "map-1"
    assert resp.get_json()["job"]["status"] == "queued"

    # Unknown job id → 404; known job under the WRONG source → 404.
    assert (
        client.get(
            "/api/agentic_search/sources/github/map/jobs/ghost", headers=_auth()
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/agentic_search/sources/linear/map/jobs/map-1", headers=_auth()
        ).status_code
        == 404
    )


# ── POST /runs tier knob ─────────────────────────────────────────────────────


def _any_workspace_id(client) -> str:
    """A seeded demo workspace id (the store seeds when empty)."""
    body = client.get("/api/agentic_search/workspaces", headers=_auth()).get_json()
    return body["workspaces"][0]["id"]


def test_post_run_accepts_and_echoes_tier():
    """POST /runs threads the tier onto the run payload (echo runner ignores it)."""
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q", "tier": "deep"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["run"]["tier"] == "deep"


def test_post_run_threads_tier_to_runner_record():
    """The runner receives the tier ON THE RUN RECORD — never frozen on the
    runner instance (the per-run budget knob contract)."""
    from mewbo_api.agentic_search.runner import EchoSearchRunner

    seen: dict[str, str] = {}

    class _CaptureRunner(EchoSearchRunner):
        def start(self, run, workspace, *, store, runtime=None, source_platform=None):
            seen["tier"] = run.tier
            return super().start(
                run, workspace, store=store, runtime=runtime,
                source_platform=source_platform,
            )

    set_search_runner(_CaptureRunner())
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q", "tier": "fast"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert seen["tier"] == "fast"


def test_post_run_defaults_tier_from_config():
    """An absent tier falls back to the configured scg default (``auto``)."""
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["run"]["tier"] == "auto"


def test_post_run_invalid_tier_400s():
    """A tier outside fast|auto|deep is a 400."""
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q", "tier": "turbo"},
        headers=_auth(),
    )
    assert resp.status_code == 400
    assert "tier" in resp.get_json()["message"]


# ── POST /runs per-run model override ────────────────────────────────────────


def test_post_run_threads_model_override_to_record_and_echo():
    """An explicit ``model`` rides the RUN RECORD (the drive-time seam reads
    ``run.model or ScgConfig.model_for_tier``) and is echoed on the payload."""
    from mewbo_api.agentic_search.runner import EchoSearchRunner

    seen: dict[str, str | None] = {}

    class _CaptureRunner(EchoSearchRunner):
        def start(self, run, workspace, *, store, runtime=None, source_platform=None):
            seen["model"] = run.model
            return super().start(
                run, workspace, store=store, runtime=runtime,
                source_platform=source_platform,
            )

    set_search_runner(_CaptureRunner())
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q", "model": "openai/gpt-5.5"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert seen["model"] == "openai/gpt-5.5"
    assert resp.get_json()["run"]["model"] == "openai/gpt-5.5"


@pytest.mark.parametrize("bad_model", [7, "", "   ", ["a"]])
def test_post_run_non_string_model_is_ignored(bad_model):
    """A non-string / blank ``model`` is ignored — the tier's configured model
    applies, never a 400 (the /v1/structured override stance)."""
    client = backend.app.test_client()
    ws_id = _any_workspace_id(client)
    resp = client.post(
        "/api/agentic_search/runs",
        json={"workspace_id": ws_id, "query": "q", "model": bad_model},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["run"]["model"] is None


# ── GET /tiers — tier→model presets for the composer ────────────────────────


def test_get_tiers_returns_resolved_model_per_tier():
    """GET /tiers exposes the tier budget knobs + the model preset each runs on
    (the composer's coupled tier/model pickers read this)."""
    client = backend.app.test_client()
    resp = client.get("/api/agentic_search/tiers", headers=_auth())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["default_tier"] in {"fast", "auto", "deep"}
    assert set(body["tiers"]) == {"fast", "auto", "deep"}
    # Every tier resolves to a non-empty model name — exactly what the drive
    # would run (`run.model or ScgConfig.model_for_tier(tier)` falling back to
    # `llm.default_model`), so the FE never shows a blank preset.
    for tier, model in body["tiers"].items():
        assert isinstance(model, str) and model, f"tier {tier} resolved blank"


def test_get_tiers_blank_mapping_falls_back_to_default_model(monkeypatch):
    """A blank tier mapping resolves to ``llm.default_model`` — mirroring the
    drive-time fallback, never an empty string on the wire."""
    monkeypatch.setattr(ScgConfig, "model_for_tier", staticmethod(lambda _t: None))
    client = backend.app.test_client()
    resp = client.get("/api/agentic_search/tiers", headers=_auth())
    assert resp.status_code == 200
    tiers = resp.get_json()["tiers"]
    from mewbo_core.config import get_config_value

    expected = str(get_config_value("llm", "default_model") or "")
    assert expected, "test precondition: llm.default_model is configured"
    assert all(model == expected for model in tiers.values())


def test_get_tiers_requires_api_key():
    """No API key → 401 like every other route on the namespace."""
    client = backend.app.test_client()
    resp = client.get("/api/agentic_search/tiers")
    assert resp.status_code == 401


# ── per-run runner resolution (no startup registration) ─────────────────────


def test_runner_resolves_echo_when_scg_disabled(monkeypatch):
    """With scg.enabled=false every run resolves to the echo replay."""
    from mewbo_api.agentic_search.runner import EchoSearchRunner, get_search_runner

    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: False))

    assert isinstance(get_search_runner(), EchoSearchRunner)


def test_runner_resolves_orchestrated_once_source_mapped(monkeypatch):
    """Mapping the first source flips resolution to orchestrated — NO restart.

    Regression: the swap used to happen only at init, so a process that mapped
    its first source stayed in echo mode until restarted.
    """
    from mewbo_api.agentic_search.runner import EchoSearchRunner, get_search_runner
    from mewbo_api.agentic_search.scg.orchestrated_runner import (
        OrchestratedSearchRunner,
    )
    from mewbo_graph.scg.types import SourceDescriptor

    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: True))

    # Empty graph → echo (nothing to route).
    assert isinstance(get_search_runner(), EchoSearchRunner)

    scg_store_mod.get_scg_store().upsert_source(
        SourceDescriptor(source_id="github", source_type="openapi", raw={})
    )

    # Same live process, next resolution → orchestrated.
    assert isinstance(get_search_runner(), OrchestratedSearchRunner)


def test_explicit_runner_override_wins(monkeypatch):
    """A set_search_runner override (the test seam) beats per-run resolution."""
    from mewbo_api.agentic_search.runner import EchoSearchRunner, get_search_runner
    from mewbo_graph.scg.types import SourceDescriptor

    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: True))
    scg_store_mod.get_scg_store().upsert_source(
        SourceDescriptor(source_id="github", source_type="openapi", raw={})
    )
    pinned = EchoSearchRunner()
    set_search_runner(pinned)

    assert get_search_runner() is pinned
