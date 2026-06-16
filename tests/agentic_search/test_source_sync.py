"""``WorkspaceSourceSync`` — virtual-config refresh + auto-map on save (#75).

Drives the real :class:`WorkspaceSourceSync` over a real JSON agentic_search
store, stubbing only the I/O boundaries the issue calls out:

* the descriptor build (``SourceDescriptorBuilder.build`` — would hit a live MCP
  connector) and
* the map drive (``MapSourceJob.start`` — would spawn a real ``SessionRuntime`` /
  LLM).

Asserts the caller-visible side effects: the virtual MCP config is refreshed, and
``MapSourceJob.start`` fires exactly once per NEWLY-enabled, configured,
not-already-mapped/in-flight source — and never for a demo/unknown source, a
disabled SCG, or a missing runtime.

NO real LLM / session / MCP connector is ever touched.

--- Thread synchronisation seam ---

``on_workspace_saved`` now launches the fan-out (step 2 — ``_mappable``/
``_drifted``/``_reenrich`` resolution + ``_start_map`` loop) on a daemon thread
and returns that ``Thread`` (or ``None`` when the SCG gate is off / no runtime).
Tests that assert on fan-out effects call ``_join(thread)`` before the assertion
so they don't race.  The seam is chosen because:

- it is the minimal, honest join point — the thread IS the fan-out;
- it doesn't add any test-only attribute to the production class;
- ``None`` from a gate-off path is accepted without joining (there is nothing to
  wait for — the test that follows asserts that the recorder is EMPTY, which is
  trivially true without joining).

Tests that only assert on the synchronous step-1 effect (virtual-config refresh)
do NOT join — that effect is guaranteed settled before ``on_workspace_saved``
returns.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from mewbo_api.agentic_search import mcp_config as mcp_config_mod, source_sync as sync_mod
from mewbo_api.agentic_search.mcp_config import WorkspaceMcpConfig
from mewbo_api.agentic_search.schemas import MapJobRecord, WorkspaceInput
from mewbo_api.agentic_search.source_sync import NlContextFingerprint, WorkspaceSourceSync
from mewbo_api.agentic_search.store import JsonAgenticSearchStore

_MERGED = {
    "servers": {
        "gitea": {"transport": "streamable_http", "url": "http://x/mcp/Gitea"},
        "internet-search": {"transport": "streamable_http", "url": "http://x/mcp/IS"},
    }
}

# Generous but bounded join timeout so a stalled test fails fast.
_JOIN_TIMEOUT = 5.0  # seconds


def _join(thread: threading.Thread | None, timeout: float = _JOIN_TIMEOUT) -> None:
    """Wait for the background fan-out thread to finish (asserts it did not time out).

    ``None`` (gate-off / no-runtime path) is a no-op because there is nothing to
    wait for in that case.
    """
    if thread is None:
        return
    thread.join(timeout=timeout)
    assert not thread.is_alive(), (
        f"fan-out thread {thread.name!r} did not finish within {timeout}s"
    )


@pytest.fixture()
def store() -> JsonAgenticSearchStore:
    """A fresh JSON agentic_search store under a throwaway temp dir."""
    return JsonAgenticSearchStore(root_dir=Path(tempfile.mkdtemp(prefix="src-sync-")))


@pytest.fixture(autouse=True)
def _stub_merged_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the merged MCP config read so resolve_servers sees our catalog."""
    monkeypatch.setattr(
        mcp_config_mod, "get_merged_mcp_config", lambda project=None: _MERGED
    )


@pytest.fixture
def _scg_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the SCG gate ON (auto-map only runs when scg.enabled)."""
    monkeypatch.setattr(
        sync_mod.ScgConfig, "enabled", staticmethod(lambda: True)
    )


class _FakeRuntime:
    """A non-None runtime sentinel — never called (the map seam is stubbed)."""


@pytest.fixture
def _map_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub BOTH I/O seams: descriptor build + MapSourceJob.start.

    Returns the list of source ids ``MapSourceJob.start`` was called for. The
    descriptor build is stubbed to "succeed" for any configured server and raise
    ``LookupError`` for an unconfigured one (mirroring the real builder).
    """
    import mewbo_api.agentic_search.scg.descriptors as desc_mod
    import mewbo_api.agentic_search.scg.map_job as map_job_mod

    started: list[str] = []

    class _FakeBuilt:
        raw = {"tools": [{"name": "t"}]}

    def _fake_build(self: Any) -> _FakeBuilt:
        if self.source_id not in _MERGED["servers"]:
            raise LookupError(f"{self.source_id} not configured")
        return _FakeBuilt()

    def _fake_start(source: Any, *, store: Any, runtime: Any, model: Any = None) -> Any:
        started.append(source.source_id)
        return MapJobRecord(
            job_id=f"map-{source.source_id}",
            source_id=source.source_id,
            source_type=source.source_type,
            status="queued",
        )

    monkeypatch.setattr(desc_mod.SourceDescriptorBuilder, "build", _fake_build)
    monkeypatch.setattr(map_job_mod.MapSourceJob, "start", staticmethod(_fake_start))
    return started


# ── virtual config refresh (always, even with auto-map off) ─────────────────


def test_refreshes_virtual_config_even_when_scg_disabled(
    store: JsonAgenticSearchStore,
) -> None:
    """The virtual MCP config is refreshed regardless of the SCG gate.

    Step 1 is synchronous — no join needed.
    """
    WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea", "demo-web"],
        prev_sources=None,
        runtime=None,  # auto-map off
    )
    names = WorkspaceMcpConfig.attached_server_names(store, "ws-1")
    # Only the configured server resolves into the virtual config.
    assert names == ["gitea"]


# ── auto-map fires for newly-enabled configured sources ─────────────────────


def test_auto_maps_newly_enabled_configured_sources(
    store: JsonAgenticSearchStore, _scg_on: None, _map_recorder: list[str]
) -> None:
    """A create with two configured sources maps both exactly once."""
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea", "internet-search"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert sorted(_map_recorder) == ["gitea", "internet-search"]


def test_only_newly_enabled_sources_are_mapped(
    store: JsonAgenticSearchStore, _scg_on: None, _map_recorder: list[str]
) -> None:
    """On an update, only the sources NOT in the prior selection are mapped."""
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea", "internet-search"],
        prev_sources=["gitea"],  # gitea was already enabled
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == ["internet-search"]


def test_demo_or_unconfigured_source_not_mapped(
    store: JsonAgenticSearchStore, _scg_on: None, _map_recorder: list[str]
) -> None:
    """A demo/unconfigured id (no MCP connector) is skipped, not mapped."""
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["demo-web", "gitea"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == ["gitea"]  # demo-web raised LookupError in the builder


# ── idempotency: already-mapped / in-flight sources are skipped ─────────────


def test_already_in_flight_source_not_remapped(
    store: JsonAgenticSearchStore, _scg_on: None, _map_recorder: list[str]
) -> None:
    """A source with a queued/running map job is not re-started."""
    store.create_map_job(
        MapJobRecord(
            job_id="existing",
            source_id="gitea",
            source_type="mcp_tool_list",
            status="running",
        )
    )
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea", "internet-search"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    _join(t)
    # gitea is in-flight → skipped; only internet-search maps.
    assert _map_recorder == ["internet-search"]


def test_already_mapped_source_not_remapped(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
    _map_recorder: list[str],
) -> None:
    """A source already present in the GLOBAL SCG is not re-mapped (shared graph)."""
    monkeypatch.setattr(
        WorkspaceSourceSync, "_mapped_source_ids", staticmethod(lambda: {"gitea"})
    )
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea", "internet-search"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == ["internet-search"]


def test_failed_job_does_not_block_remap(
    store: JsonAgenticSearchStore, _scg_on: None, _map_recorder: list[str]
) -> None:
    """A terminal (failed) prior job does NOT block a re-map (reachability fix)."""
    store.create_map_job(
        MapJobRecord(
            job_id="old-failed",
            source_id="gitea",
            source_type="mcp_tool_list",
            status="failed",
        )
    )
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == ["gitea"]


# ── gates: disabled SCG / no runtime never auto-map ─────────────────────────


def test_no_automap_when_scg_disabled(
    store: JsonAgenticSearchStore, _map_recorder: list[str]
) -> None:
    """SCG disabled (the default) → config refreshed, but no map fired.

    Returns None — no join needed.
    """
    result = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    assert result is None
    assert _map_recorder == []
    assert WorkspaceMcpConfig.attached_server_names(store, "ws-1") == ["gitea"]


def test_no_automap_without_runtime(
    store: JsonAgenticSearchStore, _scg_on: None, _map_recorder: list[str]
) -> None:
    """No wired runtime → no map (the drive seam needs one).

    Returns None — no join needed.
    """
    result = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea"],
        prev_sources=None,
        runtime=None,
    )
    assert result is None
    assert _map_recorder == []


# ── auto re-map on tool-list drift (#81-C) ──────────────────────────────────
#
# The live builder stub advertises a single tool ``t``; the drift check hashes
# that live list and compares it to the ``schema_version`` stamped on the mapped
# SCG source. Stub ``_stored_manifest_hashes`` to control the mapped side so the
# tests don't depend on a shared global SCG store.


def _live_hash() -> str:
    """The ManifestHash of the stubbed live tool list (single tool ``t``)."""
    from mewbo_graph.scg.manifest import ManifestHash

    return ManifestHash.of_tool_list([{"name": "t"}])


def test_drifted_already_mapped_source_is_remapped(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
    _map_recorder: list[str],
) -> None:
    """An already-mapped source whose live tool list drifted IS re-mapped."""
    # gitea is mapped (so _mappable skips it) with a STALE hash → live ≠ stored.
    monkeypatch.setattr(
        WorkspaceSourceSync, "_mapped_source_ids", staticmethod(lambda: {"gitea"})
    )
    monkeypatch.setattr(
        WorkspaceSourceSync,
        "_stored_manifest_hashes",
        staticmethod(lambda: {"gitea": "STALE-HASH"}),
    )
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea"],
        prev_sources=["gitea"],  # already enabled — only the drift path can map it
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == ["gitea"]


def test_unchanged_already_mapped_source_is_not_remapped(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
    _map_recorder: list[str],
) -> None:
    """An already-mapped source whose live list matches the stored hash is left alone."""
    monkeypatch.setattr(
        WorkspaceSourceSync, "_mapped_source_ids", staticmethod(lambda: {"gitea"})
    )
    monkeypatch.setattr(
        WorkspaceSourceSync,
        "_stored_manifest_hashes",
        staticmethod(lambda: {"gitea": _live_hash()}),  # live == stored → no drift
    )
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea"],
        prev_sources=["gitea"],
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == []


def test_drift_remap_skipped_when_already_in_flight(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
    _map_recorder: list[str],
) -> None:
    """A drifted source with a live map job is not re-stacked."""
    monkeypatch.setattr(
        WorkspaceSourceSync, "_mapped_source_ids", staticmethod(lambda: {"gitea"})
    )
    monkeypatch.setattr(
        WorkspaceSourceSync,
        "_stored_manifest_hashes",
        staticmethod(lambda: {"gitea": "STALE-HASH"}),
    )
    store.create_map_job(
        MapJobRecord(
            job_id="inflight",
            source_id="gitea",
            source_type="mcp_tool_list",
            status="running",
        )
    )
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-1",
        new_sources=["gitea"],
        prev_sources=["gitea"],
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == []


# ── NL-context enrichment is carried into the map contract (#81-B) ──────────


def test_workspace_nl_context_rides_the_map_input(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
) -> None:
    """A workspace's instructions/description are carried on SourceMapInput.nl_context."""
    import mewbo_api.agentic_search.scg.descriptors as desc_mod
    import mewbo_api.agentic_search.scg.map_job as map_job_mod
    from mewbo_api.agentic_search.schemas import MapJobRecord, WorkspaceInput

    captured: list[Any] = []

    class _FakeBuilt:
        raw = {"tools": [{"name": "t"}]}

    def _fake_build(self: Any) -> _FakeBuilt:
        return _FakeBuilt()

    def _fake_start(source: Any, *, store: Any, runtime: Any, model: Any = None) -> Any:
        captured.append(source)
        return MapJobRecord(
            job_id="m", source_id=source.source_id, source_type=source.source_type,
            status="queued",
        )

    monkeypatch.setattr(desc_mod.SourceDescriptorBuilder, "build", _fake_build)
    monkeypatch.setattr(map_job_mod.MapSourceJob, "start", staticmethod(_fake_start))

    ws = store.create_workspace(
        WorkspaceInput(
            name="W", desc="incident triage workspace",
            sources=["gitea"], instructions="prefer gitea#search_issues for repo lookups",
        )
    )
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id=ws.id,
        new_sources=["gitea"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert captured, "the map drive was never invoked"
    nl = captured[0].nl_context
    assert nl is not None
    assert nl.workspace_instructions == "prefer gitea#search_issues for repo lookups"
    assert nl.workspace_description == "incident triage workspace"


def test_no_nl_context_when_workspace_has_no_prose(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
) -> None:
    """A prose-less workspace carries no nl_context (byte-identical legacy contract)."""
    import mewbo_api.agentic_search.scg.descriptors as desc_mod
    import mewbo_api.agentic_search.scg.map_job as map_job_mod
    from mewbo_api.agentic_search.schemas import MapJobRecord, WorkspaceInput

    captured: list[Any] = []

    class _FakeBuilt:
        raw = {"tools": [{"name": "t"}]}

    monkeypatch.setattr(
        desc_mod.SourceDescriptorBuilder, "build", lambda self: _FakeBuilt()
    )
    monkeypatch.setattr(
        map_job_mod.MapSourceJob,
        "start",
        staticmethod(
            lambda source, *, store, runtime, model=None: (
                captured.append(source)
                or MapJobRecord(
                    job_id="m", source_id=source.source_id,
                    source_type=source.source_type, status="queued",
                )
            )
        ),
    )
    ws = store.create_workspace(WorkspaceInput(name="W", sources=["gitea"]))
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id=ws.id,
        new_sources=["gitea"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert captured and captured[0].nl_context is None


# ── NlContextFingerprint — the prose digest (#83) ───────────────────────────


def test_fingerprint_blank_prose_is_empty_sentinel() -> None:
    """All-blank prose hashes to the empty sentinel (compares equal across saves)."""
    assert NlContextFingerprint.of(instructions="", desc="") == ""
    assert NlContextFingerprint.of(instructions="   ", desc="\n\t") == ""


def test_fingerprint_normalises_whitespace() -> None:
    """A trailing newline / collapsed whitespace is not an enrich-worthy change."""
    a = NlContextFingerprint.of(instructions="be thorough", desc="x")
    b = NlContextFingerprint.of(instructions="be thorough\n", desc="x")
    c = NlContextFingerprint.of(instructions="be   thorough", desc="x")
    assert a == b == c


def test_fingerprint_changes_with_prose() -> None:
    """A real prose edit perturbs the digest."""
    a = NlContextFingerprint.of(instructions="prefer RFCs", desc="")
    b = NlContextFingerprint.of(instructions="prefer chat", desc="")
    assert a != b and a and b


def test_fingerprint_is_field_aware() -> None:
    """Moving text between instructions and desc is a change (distinct roles)."""
    a = NlContextFingerprint.of(instructions="triage", desc="")
    b = NlContextFingerprint.of(instructions="", desc="triage")
    assert a != b


# ── re-enrich on a prose change with sources unchanged (#83) ─────────────────


def test_prose_change_redrives_already_mapped_source(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
    _map_recorder: list[str],
) -> None:
    """An instructions edit (sources unchanged, no drift) re-enriches mapped sources."""
    monkeypatch.setattr(
        WorkspaceSourceSync, "_mapped_source_ids", staticmethod(lambda: {"gitea"})
    )
    # No drift: the stored hash equals the stub's live tool list.
    monkeypatch.setattr(
        WorkspaceSourceSync,
        "_stored_manifest_hashes",
        staticmethod(lambda: {"gitea": _live_hash()}),
    )
    ws = store.create_workspace(
        WorkspaceInput(name="W", sources=["gitea"], instructions="v1")
    )
    # Seed a prior virtual config with the v1 fingerprint (as a real save would).
    WorkspaceMcpConfig.save(
        store,
        ws.id,
        ["gitea"],
        nl_fingerprint=NlContextFingerprint.of(instructions="v1", desc=""),
    )
    # Now the workspace prose changes to v2 (the route applies the update first).
    store.update_workspace(ws.id, {"instructions": "v2 — prefer search_issues"})

    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id=ws.id,
        new_sources=["gitea"],
        prev_sources=["gitea"],  # unchanged selection — only the prose moved
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == ["gitea"]
    # The new fingerprint is persisted so the NEXT identical save is a no-op.
    assert WorkspaceMcpConfig.nl_fingerprint_of(store, ws.id) == (
        NlContextFingerprint.of(instructions="v2 — prefer search_issues", desc="")
    )


def test_unchanged_prose_does_not_redrive(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
    _map_recorder: list[str],
) -> None:
    """Re-saving with the SAME prose + sources re-enriches nothing."""
    monkeypatch.setattr(
        WorkspaceSourceSync, "_mapped_source_ids", staticmethod(lambda: {"gitea"})
    )
    monkeypatch.setattr(
        WorkspaceSourceSync,
        "_stored_manifest_hashes",
        staticmethod(lambda: {"gitea": _live_hash()}),
    )
    ws = store.create_workspace(
        WorkspaceInput(name="W", sources=["gitea"], instructions="steady")
    )
    WorkspaceMcpConfig.save(
        store,
        ws.id,
        ["gitea"],
        nl_fingerprint=NlContextFingerprint.of(instructions="steady", desc=""),
    )
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id=ws.id,
        new_sources=["gitea"],
        prev_sources=["gitea"],
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == []


def test_prose_change_skips_unmapped_source(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
    _map_recorder: list[str],
) -> None:
    """A prose change on a not-yet-mapped enabled source fires no re-enrich.

    The re-enrich path only re-drives sources already in the GLOBAL SCG — a
    never-mapped source is the first-enable path (which only runs for NEWLY
    enabled sources), not a re-enrich.
    """
    monkeypatch.setattr(
        WorkspaceSourceSync, "_mapped_source_ids", staticmethod(lambda: set())
    )
    ws = store.create_workspace(
        WorkspaceInput(name="W", sources=["gitea"], instructions="v1")
    )
    WorkspaceMcpConfig.save(
        store,
        ws.id,
        ["gitea"],
        nl_fingerprint=NlContextFingerprint.of(instructions="v1", desc=""),
    )
    store.update_workspace(ws.id, {"instructions": "v2"})
    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id=ws.id,
        new_sources=["gitea"],
        prev_sources=["gitea"],  # already enabled → not newly mappable either
        runtime=_FakeRuntime(),
    )
    _join(t)
    assert _map_recorder == []


# ── NEW: background fan-out tests (route promptness + error isolation) ────────


def test_on_workspace_saved_returns_before_builder_unblocks(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
) -> None:
    """``on_workspace_saved`` returns (201 path) before the builder MCP handshake completes.

    The descriptor build is replaced with a blocking stub gated by a threading
    ``Event``.  We verify:

    1. ``on_workspace_saved`` returns a live thread BEFORE the event is set.
    2. Setting the event lets the thread complete.
    3. After joining, ``MapSourceJob.start`` was invoked (the job was started).
    """
    import mewbo_api.agentic_search.scg.descriptors as desc_mod
    import mewbo_api.agentic_search.scg.map_job as map_job_mod

    build_gate = threading.Event()
    started: list[str] = []

    class _FakeBuilt:
        raw = {"tools": [{"name": "t"}]}

    def _blocking_build(self: Any) -> _FakeBuilt:
        build_gate.wait()  # blocks until the test releases it
        if self.source_id not in _MERGED["servers"]:
            raise LookupError(f"{self.source_id} not configured")
        return _FakeBuilt()

    def _fake_start(source: Any, *, store: Any, runtime: Any, model: Any = None) -> Any:
        started.append(source.source_id)
        return MapJobRecord(
            job_id=f"map-{source.source_id}",
            source_id=source.source_id,
            source_type=source.source_type,
            status="queued",
        )

    # Patch single binding — the module source_sync imports from
    monkeypatch.setattr(desc_mod.SourceDescriptorBuilder, "build", _blocking_build)
    monkeypatch.setattr(map_job_mod.MapSourceJob, "start", staticmethod(_fake_start))

    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-prompt",
        new_sources=["gitea"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )

    # on_workspace_saved must have returned while the build is still blocked.
    assert t is not None, "expected a Thread (SCG enabled + runtime wired)"
    assert t.is_alive(), "fan-out thread should still be running (build gate closed)"

    # Release the gate and wait for the thread to finish.
    build_gate.set()
    _join(t)

    # After joining, the job was started.
    assert started == ["gitea"]


def test_one_source_builder_error_does_not_affect_others(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonAgenticSearchStore,
    _scg_on: None,
) -> None:
    """An exception inside ``SourceDescriptorBuilder.build`` for one source does not
    prevent the other sources in the same fan-out from being mapped.
    """
    import mewbo_api.agentic_search.scg.descriptors as desc_mod
    import mewbo_api.agentic_search.scg.map_job as map_job_mod

    started: list[str] = []

    class _FakeBuilt:
        raw = {"tools": [{"name": "t"}]}

    def _selective_build(self: Any) -> _FakeBuilt:
        if self.source_id == "gitea":
            raise RuntimeError("simulated MCP timeout for gitea")
        return _FakeBuilt()

    def _fake_start(source: Any, *, store: Any, runtime: Any, model: Any = None) -> Any:
        started.append(source.source_id)
        return MapJobRecord(
            job_id=f"map-{source.source_id}",
            source_id=source.source_id,
            source_type=source.source_type,
            status="queued",
        )

    monkeypatch.setattr(desc_mod.SourceDescriptorBuilder, "build", _selective_build)
    monkeypatch.setattr(map_job_mod.MapSourceJob, "start", staticmethod(_fake_start))

    t = WorkspaceSourceSync.on_workspace_saved(
        store=store,
        workspace_id="ws-iso",
        new_sources=["gitea", "internet-search"],
        prev_sources=None,
        runtime=_FakeRuntime(),
    )
    _join(t)

    # gitea failed, but internet-search must still have been mapped.
    assert started == ["internet-search"]
