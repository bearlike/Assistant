"""Tests for ``MapSourceJob`` — the map-source lifecycle façade (spec #19 §16.2).

Mirrors ``tests/wiki`` job tests: the ONLY seam mocked is the runtime — a FAKE
``SessionRuntime`` that records ``resolve_session`` / ``append_context_event`` /
``start_async`` calls and never spawns a real session or LLM. The agentic_search
store is the real JSON backend under a tmp dir.

Asserts:

* ``start`` creates a ``queued`` :class:`MapJobRecord`;
* it advertises ``client_capabilities: ["scg"]`` (the capability gate);
* it scopes ``allowed_tools`` to the scg-mapper tool set + auto-approves;
* the UNTRUSTED descriptor stays OUT of the system-prompt extension;
* no secret is persisted (only a redacted ``auth_scope``);
* the driven session settles the lifecycle ``queued → running →
  completed|failed`` AND closes the map-job event log with a terminal event;
* phase transitions dual-write through ``MapJobProgress.emit_phase`` (event log
  AND snapshot), and a fake parse populates node/edge counts on the snapshot;
* the ``scg.enabled`` gate (default off) refuses to start.

NEVER spawns a real LLM/session.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from mewbo_api.agentic_search import store as store_mod
from mewbo_api.agentic_search.scg.map_job import (
    MAPPER_TOOLS,
    MapSourceJob,
    SourceMapInput,
)
from mewbo_api.agentic_search.scg.map_progress import MapJobProgress
from mewbo_api.agentic_search.store import JsonAgenticSearchStore

# ── Fake runtime (no real session / LLM) ────────────────────────────────────


class _StubTaskQueue:
    """The slice of ``TaskQueue`` the drive reads back: ``last_error``."""

    def __init__(self, last_error: str | None = None) -> None:
        self.last_error = last_error


class _FakeRuntime:
    """Records the seam calls ``MapSourceJob.start`` makes; spawns nothing.

    ``resolve_session`` returns a deterministic id derived from the tag so the
    same tag resolves to the same session across calls (the reattach contract).
    ``start_command`` captures the drive target instead of running it — tests
    invoke ``drive()`` explicitly to settle the lifecycle; ``run_sync`` captures
    its kwargs and returns a stub ``TaskQueue``.
    """

    def __init__(self, *, last_error: str | None = None) -> None:
        self.context_events: list[tuple[str, dict[str, object]]] = []
        self.run_sync_kwargs: dict[str, Any] | None = None
        self.command_targets: list[tuple[str, Any]] = []
        self.accept_commands = True
        self._last_error = last_error
        self._by_tag: dict[str, str] = {}

    def resolve_session(self, *, session_tag: str | None = None, **_: object) -> str:
        assert session_tag is not None  # the façade always tags the map session
        return self._by_tag.setdefault(session_tag, f"sess-{session_tag}")

    def append_context_event(self, session_id: str, context: dict[str, object]) -> None:
        self.context_events.append((session_id, context))

    def start_command(self, session_id: str, target: Any) -> bool:
        if not self.accept_commands:
            return False
        self.command_targets.append((session_id, target))
        return True

    def run_sync(self, **kwargs: Any) -> _StubTaskQueue:
        self.run_sync_kwargs = kwargs
        return _StubTaskQueue(last_error=self._last_error)

    def drive(self) -> None:
        """Run the captured background target synchronously (the worker step)."""
        for _, target in self.command_targets:
            target(threading.Event())


@pytest.fixture
def _scg_enabled(monkeypatch):
    """Force ``scg.enabled`` on (it defaults off; the gate is exercised separately).

    The enable gate flows through ``ScgConfig.enabled`` (the single SCG config
    read-point). No ``llm`` config patch: the façade passes ``model`` straight
    through (``None`` resolves canonically downstream, never at the call site).
    """
    import mewbo_api.agentic_search.scg.map_job as map_job_mod

    monkeypatch.setattr(map_job_mod.ScgConfig, "enabled", staticmethod(lambda: True))


def _input(**kw) -> SourceMapInput:
    base = {"source_id": "github", "source_type": "openapi"}
    base.update(kw)
    return SourceMapInput(**base)


# ── start: record creation + capability + scope ─────────────────────────────


def test_start_creates_queued_map_job(tmp_path, _scg_enabled):
    """start persists a queued MapJobRecord carrying the source descriptor ids."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()

    job = MapSourceJob.start(_input(), store=store, runtime=runtime)

    assert job.status == "queued"
    assert job.source_id == "github"
    assert job.source_type == "openapi"
    assert job.phase is None
    assert job.node_count == 0
    assert job.edge_count == 0
    # Persisted (not just returned).
    persisted = store.get_map_job(job.job_id)
    assert persisted is not None
    assert persisted.status == "queued"


def test_start_advertises_scg_capability(tmp_path, _scg_enabled):
    """start appends client_capabilities:["scg"] so scg-* AgentDefs resolve."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()

    job = MapSourceJob.start(_input(), store=store, runtime=runtime)

    session_id = runtime.resolve_session(session_tag=f"scg:map:{job.job_id}")
    assert (session_id, {"client_capabilities": ["scg"]}) in runtime.context_events


def test_start_scopes_tools_and_auto_approves(tmp_path, _scg_enabled):
    """The driven session gets the scg-mapper tool set + auto-approval."""
    from mewbo_core.permissions import auto_approve

    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()

    MapSourceJob.start(_input(), store=store, runtime=runtime)
    runtime.drive()

    call = runtime.run_sync_kwargs
    assert call["allowed_tools"] == MAPPER_TOOLS
    assert "scg_introspect_source" in call["allowed_tools"]
    assert "scg_finalize_map" in call["allowed_tools"]
    assert call["approval_callback"] is auto_approve


def test_start_uses_session_tag_and_model_override(tmp_path, _scg_enabled):
    """start resolves the scg:map:<id> tag and honours an explicit model."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()

    job = MapSourceJob.start(
        _input(), store=store, runtime=runtime, model="anthropic/opus"
    )
    runtime.drive()

    call = runtime.run_sync_kwargs
    assert call["session_id"] == f"sess-scg:map:{job.job_id}"
    assert call["model_name"] == "anthropic/opus"


def test_start_passes_none_model_through(tmp_path, _scg_enabled):
    """No explicit model → ``model_name=None`` — resolved canonically downstream
    (llm.default_model → engine default), never a provider literal here."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()

    MapSourceJob.start(_input(), store=store, runtime=runtime)
    runtime.drive()

    assert runtime.run_sync_kwargs["model_name"] is None


# ── security: untrusted descriptor + no secrets ─────────────────────────────


def test_descriptor_stays_out_of_system_prompt(tmp_path, _scg_enabled):
    """The UNTRUSTED descriptor must never enter skill_instructions (sys prompt)."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()
    secret_marker = "INJECTED_UNTRUSTED_SCHEMA_TOKEN"

    MapSourceJob.start(
        _input(descriptor={"info": {"title": secret_marker}}),
        store=store,
        runtime=runtime,
    )
    runtime.drive()

    call = runtime.run_sync_kwargs
    # The playbook (trusted system-prompt extension) must not carry the descriptor.
    assert secret_marker not in (call["skill_instructions"] or "")
    # It DOES travel in the user query (the parsed contract), kept separate.
    assert secret_marker in call["user_query"]


def test_nl_context_seeds_user_query_not_system_prompt(tmp_path, _scg_enabled):
    """Workspace NL context rides the user query as UNTRUSTED, never the playbook."""
    from mewbo_api.agentic_search.scg.map_job import SourceNlContext

    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()
    instr = "PREFER_GITEA_SEARCH_ISSUES_FOR_REPO_LOOKUPS"
    desc = "incident-triage-workspace-prose"

    MapSourceJob.start(
        _input(
            descriptor={"tools": [{"name": "search_issues"}]},
            nl_context=SourceNlContext(
                workspace_instructions=instr, workspace_description=desc
            ),
        ),
        store=store,
        runtime=runtime,
    )
    runtime.drive()

    call = runtime.run_sync_kwargs
    # The NL context is data in the user turn (the enrich seed)...
    assert instr in call["user_query"]
    assert desc in call["user_query"]
    assert "UNTRUSTED" in call["user_query"]
    # ...and NEVER in the trusted system-prompt extension (the playbook).
    assert instr not in (call["skill_instructions"] or "")


def test_no_nl_context_keeps_user_query_free_of_enrich_block(tmp_path, _scg_enabled):
    """A map with no NL context renders no WORKSPACE NL CONTEXT block (legacy parity)."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()
    MapSourceJob.start(
        _input(descriptor={"tools": [{"name": "search_issues"}]}),
        store=store,
        runtime=runtime,
    )
    runtime.drive()
    assert "WORKSPACE NL CONTEXT" not in runtime.run_sync_kwargs["user_query"]


def test_no_secret_persisted_only_auth_scope(tmp_path, _scg_enabled):
    """auth_scope is a redacted descriptor; the snapshot never holds a token."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()

    job = MapSourceJob.start(
        _input(auth_scope="oauth:repo"), store=store, runtime=runtime
    )

    persisted = store.get_map_job(job.job_id)
    # MapJobRecord has no token field at all — security by schema (extra=forbid).
    assert not hasattr(persisted, "token")
    dumped = persisted.model_dump()
    assert "token" not in dumped
    assert "auth_scope" not in dumped  # not even the redacted scope is persisted


def test_start_refuses_when_scg_disabled(tmp_path, monkeypatch):
    """The scg.enabled gate (default off) refuses to start a map job."""
    import mewbo_api.agentic_search.scg.map_job as map_job_mod

    monkeypatch.setattr(
        map_job_mod.ScgConfig, "enabled", staticmethod(lambda: False)
    )
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()

    with pytest.raises(RuntimeError, match="SCG is disabled"):
        MapSourceJob.start(_input(), store=store, runtime=runtime)
    # Nothing was created or started.
    assert store.list_map_jobs() == []
    assert runtime.command_targets == []


# ── phase progress: dual write + fake parse counts ──────────────────────────


def test_phases_transition_via_emit_phase_dual_write(tmp_path, _scg_enabled):
    """The mapper's phase sequence dual-writes the event log AND the snapshot."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()
    job = MapSourceJob.start(_input(), store=store, runtime=runtime)

    # Simulate the scg-mapper driving its deterministic state machine. Each
    # emit_phase is the SCG analogue of the wiki emit_phase (event + snapshot).
    phases = ["connect", "introspect", "parse", "link", "finalize"]
    idxs = [MapJobProgress.emit_phase(store, job.job_id, p) for p in phases]

    # Event-log side: monotonic idx, full ordered sequence.
    assert idxs == [0, 1, 2, 3, 4]
    events = store.load_map_job_events(job.job_id)
    assert [e["name"] for e in events] == phases
    # Snapshot side: tracks the latest phase.
    assert store.get_map_job(job.job_id).phase == "finalize"


def test_fake_parse_populates_node_and_edge_counts(tmp_path, _scg_enabled):
    """A fake parse step writes node/edge counts onto the snapshot (dual-read)."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()
    job = MapSourceJob.start(_input(), store=store, runtime=runtime)

    # Phase advances, then the parse result is recorded on the coarse snapshot —
    # mirroring scg_build_structure → update_map_job(node_count=..., edge_count=...).
    MapJobProgress.emit_phase(store, job.job_id, "parse")
    store.update_map_job(
        job.job_id, status="running", node_count=9, edge_count=4
    )
    MapJobProgress.emit_phase(store, job.job_id, "finalize")
    store.update_map_job(job.job_id, status="completed", completed_at="2026-06-06T00:00:00Z")

    final = store.get_map_job(job.job_id)
    assert final.status == "completed"
    assert final.phase == "finalize"
    assert final.node_count == 9
    assert final.edge_count == 4
    # Both transports agree: the event log carried both phases.
    assert [e["name"] for e in store.load_map_job_events(job.job_id)] == [
        "parse",
        "finalize",
    ]


# ── lifecycle settle: queued → running → completed|failed ──────────────────


def test_driven_session_settles_completed(tmp_path, _scg_enabled):
    """A clean session end settles the job ``completed`` + a terminal event.

    Regression: nothing ever moved the status off ``queued`` and the map SSE
    log never received a terminal event, so streams only died by idle timeout.
    """
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()
    job = MapSourceJob.start(_input(), store=store, runtime=runtime)
    assert store.get_map_job(job.job_id).status == "queued"

    runtime.drive()

    final = store.get_map_job(job.job_id)
    assert final.status == "completed"
    assert final.started_at is not None
    assert final.completed_at is not None
    assert final.error is None
    events = store.load_map_job_events(job.job_id)
    assert events[-1]["type"] == "run_done"
    assert events[-1]["status"] == "completed"


def test_session_error_settles_failed(tmp_path, _scg_enabled):
    """A session that ends with ``last_error`` settles ``failed`` + error event."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime(last_error="connector auth failed")
    job = MapSourceJob.start(_input(), store=store, runtime=runtime)

    runtime.drive()

    final = store.get_map_job(job.job_id)
    assert final.status == "failed"
    assert final.error == {"code": "agent_error", "message": "connector auth failed"}
    events = store.load_map_job_events(job.job_id)
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["code"] == "agent_error"


def test_drive_exception_settles_failed(tmp_path, _scg_enabled):
    """A crashed drive (run_sync raises) still settles ``failed`` — never queued."""

    class _Boom(_FakeRuntime):
        def run_sync(self, **kwargs: Any) -> _StubTaskQueue:
            raise RuntimeError("session blew up")

    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _Boom()
    job = MapSourceJob.start(_input(), store=store, runtime=runtime)

    runtime.drive()

    final = store.get_map_job(job.job_id)
    assert final.status == "failed"
    assert final.error == {"code": "internal", "message": "session blew up"}
    assert store.load_map_job_events(job.job_id)[-1]["type"] == "error"


def test_refused_start_settles_failed_busy(tmp_path, _scg_enabled):
    """A registry refusal (session busy) settles ``failed`` instead of zombie-queued."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()
    runtime.accept_commands = False

    job = MapSourceJob.start(_input(), store=store, runtime=runtime)

    assert job.status == "failed"
    assert job.error["code"] == "busy"
    assert store.get_map_job(job.job_id).status == "failed"
    assert store.load_map_job_events(job.job_id)[-1]["type"] == "error"


# ── get ─────────────────────────────────────────────────────────────────────


def test_get_returns_snapshot_or_none(tmp_path, _scg_enabled):
    """get returns the snapshot for a known job, None otherwise."""
    store = JsonAgenticSearchStore(root_dir=tmp_path)
    runtime = _FakeRuntime()
    job = MapSourceJob.start(_input(), store=store, runtime=runtime)

    assert MapSourceJob.get(job.job_id, store=store).job_id == job.job_id
    assert MapSourceJob.get("ghost", store=store) is None


def test_reset_for_tests_isolation():
    """A fresh store after reset_for_tests never sees a prior map job."""
    from mewbo_api.agentic_search.schemas import MapJobRecord

    store_mod.reset_for_tests()
    store_a = store_mod.get_store()
    store_a.create_map_job(
        MapJobRecord(job_id="map-x", source_id="github", source_type="openapi")
    )
    assert store_a.get_map_job("map-x") is not None

    store_mod.reset_for_tests()
    store_b = store_mod.get_store()
    assert store_b is not store_a
    assert store_b.get_map_job("map-x") is None
