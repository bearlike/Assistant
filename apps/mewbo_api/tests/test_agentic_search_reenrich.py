"""Route-level coverage for "workspace editing is a graph-lifecycle event" (#83).

Drives the real ``POST`` / ``PATCH /workspaces`` routes against the real JSON
agentic_search store and asserts which sources the map+enrich pipeline is driven
for, stubbing ONLY the two I/O boundaries the auto-map crosses:

* the live descriptor build (``SourceDescriptorBuilder.build`` — would hit a real
  MCP connector), and
* the map drive (``MapSourceJob.start`` — would spawn a real session / LLM).

Covers the three gates the issue calls out:

* an **instructions-only** PATCH (no ``sources`` key) re-drives the map for the
  workspace's already-mapped sources (the #83 gap — a prose change is now a
  graph-lifecycle event);
* a **sources** PATCH still maps the newly-enabled source; and
* a **no-op** PATCH fires nothing.

NO real LLM / session / MCP connector is ever touched.
"""

# mypy: ignore-errors

import pytest
from mewbo_api import backend
from mewbo_api.agentic_search import (
    routes as routes_mod,
    source_sync as sync_mod,
    store as store_mod,
)
from mewbo_api.agentic_search.schemas import MapJobRecord

_MERGED = {
    "servers": {
        "gitea": {"transport": "streamable_http", "url": "http://x/mcp/Gitea"},
        "internet-search": {"transport": "streamable_http", "url": "http://x/mcp/IS"},
    }
}


def _auth():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


class _FakeRuntime:
    """A non-None runtime sentinel — never called (the map seam is stubbed)."""


@pytest.fixture(autouse=True)
def _reset_store():
    """Fresh JSON store between tests (seeded demo workspaces dropped per test)."""
    store_mod.reset_for_tests()
    yield
    store_mod.reset_for_tests()


class _MapRecorder:
    """The list of mapped source ids + the mutable GLOBAL-SCG membership stub."""

    def __init__(self) -> None:
        self.started: list[str] = []
        # Source ids the re-enrich path should see as already-mapped in the
        # GLOBAL SCG. Tests append to this before a prose-change PATCH.
        self.mapped: set[str] = set()

    def clear(self) -> None:
        self.started.clear()


@pytest.fixture
def map_recorder(monkeypatch: pytest.MonkeyPatch) -> _MapRecorder:
    """Enable SCG + a runtime, stub both map I/O seams; record mapped source ids.

    Exposes ``.started`` (source ids ``MapSourceJob.start`` was driven for) and
    ``.mapped`` (the GLOBAL-SCG membership the re-enrich path reads — settable per
    test). The descriptor build "succeeds" for a configured server and raises
    ``LookupError`` for an unconfigured id (mirroring the real builder); the merged
    MCP config is stubbed so the virtual-config resolution sees a stable catalog.
    """
    import mewbo_api.agentic_search.mcp_config as mcp_config_mod
    import mewbo_api.agentic_search.scg.descriptors as desc_mod
    import mewbo_api.agentic_search.scg.map_job as map_job_mod

    rec = _MapRecorder()

    monkeypatch.setattr(sync_mod.ScgConfig, "enabled", staticmethod(lambda: True))
    monkeypatch.setattr(routes_mod, "_runtime", _FakeRuntime())
    monkeypatch.setattr(
        mcp_config_mod, "get_merged_mcp_config", lambda project=None: _MERGED
    )
    # The re-enrich gate reads GLOBAL-SCG membership; tests drive it via rec.mapped
    # rather than standing up a live SCG store (monkeypatch restores on teardown).
    monkeypatch.setattr(
        sync_mod.WorkspaceSourceSync,
        "_mapped_source_ids",
        staticmethod(lambda: set(rec.mapped)),
    )

    class _FakeBuilt:
        raw = {"tools": [{"name": "t"}]}

    def _fake_build(self):
        if self.source_id not in _MERGED["servers"]:
            raise LookupError(f"{self.source_id} not configured")
        return _FakeBuilt()

    def _fake_start(source, *, store, runtime, model=None):
        rec.started.append(source.source_id)
        return MapJobRecord(
            job_id=f"map-{source.source_id}",
            source_id=source.source_id,
            source_type=source.source_type,
            status="queued",
        )

    monkeypatch.setattr(desc_mod.SourceDescriptorBuilder, "build", _fake_build)
    monkeypatch.setattr(map_job_mod.MapSourceJob, "start", staticmethod(_fake_start))
    return rec


def _create_workspace(client, **body) -> str:
    """Create a workspace via the route; return its id (fan-out settled)."""
    resp = client.post("/api/agentic_search/workspaces", json=body, headers=_auth())
    assert resp.status_code == 201, resp.get_json()
    # The auto-map fan-out is async (#97) — settle it so a later
    # ``map_recorder.clear()`` can't race the create-time map.
    sync_mod.WorkspaceSourceSync.join_last_fan_out()
    return resp.get_json()["workspace"]["id"]


def _patch_workspace(client, ws_id: str, body: dict):
    """PATCH a workspace via the route; settle the async fan-out (#97)."""
    resp = client.patch(
        f"/api/agentic_search/workspaces/{ws_id}", json=body, headers=_auth()
    )
    sync_mod.WorkspaceSourceSync.join_last_fan_out()
    return resp


# ── sources PATCH → newly-enabled source mapped ─────────────────────────────


def test_sources_patch_maps_newly_enabled_source(map_recorder: _MapRecorder) -> None:
    """Adding a live source via PATCH maps exactly the newly-enabled one."""
    client = backend.app.test_client()
    ws_id = _create_workspace(client, name="W", sources=["gitea"])
    map_recorder.clear()  # ignore the create-time map of gitea

    resp = _patch_workspace(client, ws_id, {"sources": ["gitea", "internet-search"]})
    assert resp.status_code == 200
    # gitea was already enabled → only internet-search is newly-enabled + mapped.
    assert map_recorder.started == ["internet-search"]


# ── instructions-only PATCH → re-enrich driven (the #83 gap) ────────────────


def test_instructions_only_patch_redrives_map(map_recorder: _MapRecorder) -> None:
    """An instructions-only PATCH re-drives the map for the mapped source (#83).

    The source list is unchanged and the tool list didn't drift, so the ONLY
    reason a map fires is the changed NL-context prose. The mapped source is
    present in the GLOBAL SCG (the re-enrich path's precondition).
    """
    map_recorder.mapped = {"gitea"}
    client = backend.app.test_client()
    ws_id = _create_workspace(
        client, name="W", sources=["gitea"], instructions="original"
    )
    map_recorder.clear()

    resp = _patch_workspace(
        client, ws_id, {"instructions": "prefer gitea#search_issues for repo lookups"}
    )
    assert resp.status_code == 200
    # No sources key in the body, no tool drift — the prose change is the sole
    # trigger; the already-mapped gitea source is re-enriched.
    assert map_recorder.started == ["gitea"]


def test_description_only_patch_redrives_map(map_recorder: _MapRecorder) -> None:
    """A desc-only edit also counts as an enrich-worthy prose change (#83)."""
    map_recorder.mapped = {"gitea"}
    client = backend.app.test_client()
    ws_id = _create_workspace(client, name="W", sources=["gitea"], desc="old")
    map_recorder.clear()

    resp = _patch_workspace(client, ws_id, {"desc": "incident triage workspace"})
    assert resp.status_code == 200
    assert map_recorder.started == ["gitea"]


def test_unmapped_source_not_reenriched_on_prose_change(
    map_recorder: _MapRecorder,
) -> None:
    """A prose change on a NOT-yet-mapped source fires no re-enrich (#83 gate).

    The re-enrich path only re-drives sources already in the GLOBAL SCG; an
    unmapped source's first map is the first-enable path, not a re-enrich.
    """
    map_recorder.mapped = set()  # gitea is NOT mapped
    client = backend.app.test_client()
    ws_id = _create_workspace(
        client, name="W", sources=["gitea"], instructions="original"
    )
    map_recorder.clear()

    resp = _patch_workspace(client, ws_id, {"instructions": "changed prose"})
    assert resp.status_code == 200
    assert map_recorder.started == []


# ── no-op PATCH → nothing fires ─────────────────────────────────────────────


def test_noop_instructions_patch_fires_nothing(map_recorder: _MapRecorder) -> None:
    """Re-saving the SAME instructions (whitespace-equivalent) re-enriches nothing."""
    map_recorder.mapped = {"gitea"}
    client = backend.app.test_client()
    ws_id = _create_workspace(
        client, name="W", sources=["gitea"], instructions="be thorough"
    )
    map_recorder.clear()

    # A trailing-newline difference is normalised away by the fingerprint, so the
    # prose is unchanged → no re-enrich.
    resp = _patch_workspace(client, ws_id, {"instructions": "be thorough\n"})
    assert resp.status_code == 200
    assert map_recorder.started == []


def test_noop_sources_patch_fires_nothing(map_recorder: _MapRecorder) -> None:
    """A PATCH re-sending the unchanged source list maps nothing (idempotent)."""
    map_recorder.mapped = {"gitea"}
    client = backend.app.test_client()
    ws_id = _create_workspace(client, name="W", sources=["gitea"])
    map_recorder.clear()

    resp = _patch_workspace(client, ws_id, {"sources": ["gitea"]})
    assert resp.status_code == 200
    # gitea is already enabled (not newly), already mapped (no re-map), unchanged
    # prose (no re-enrich), no drift → nothing fires.
    assert map_recorder.started == []


def test_unrelated_name_patch_fires_nothing(map_recorder: _MapRecorder) -> None:
    """Renaming a workspace is not a graph-lifecycle event — nothing fires."""
    map_recorder.mapped = {"gitea"}
    client = backend.app.test_client()
    ws_id = _create_workspace(
        client, name="W", sources=["gitea"], instructions="keep"
    )
    map_recorder.clear()

    resp = _patch_workspace(client, ws_id, {"name": "Renamed"})
    assert resp.status_code == 200
    assert map_recorder.started == []
