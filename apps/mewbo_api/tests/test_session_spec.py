"""Contract tests for session purpose-binding (``SessionSpec``) and its routing legs.

Driven from the caller's seam — real Flask routes, real serialisation, real
context persistence — stubbing only ``start_async`` (the I/O boundary) so each
test asserts what the run was actually dispatched WITH.

The defect these exist to pin: ``POST /sessions/<id>/query`` was the only
re-engage path that never read the session's persisted binding. It re-derived
the model from config, dropped the tool ceiling when the request omitted one,
never read the strict-scope flag, fell back to an empty per-session temp dir —
and then PERSISTED those defaults, corrupting every later ``/message`` and
``/recover``.
"""

# mypy: ignore-errors

from types import SimpleNamespace

import pytest
from mewbo_api import backend
from mewbo_api.session_spec import (
    SPEC_CONTEXT_KEY,
    SessionSpec,
    SessionSpecOverrides,
    SessionSpecStore,
)
from mewbo_core.session_provenance import SessionOrigin
from mewbo_core.session_store import SessionStore


def _ready_config():
    """A config the readiness gate accepts (a model and a credential resolve)."""
    llm = SimpleNamespace(default_model="openai/test-model", api_key="sk-test")
    return SimpleNamespace(llm=llm)


def _reset_backend(tmp_path, monkeypatch):
    """Swap module-level stores for temp-dir-backed ones and latch readiness ready.

    Readiness is pinned explicitly rather than left to whatever the developer's
    ``configs/app.json`` happens to hold — otherwise a machine with no configured
    key would 503 every route test in this module.
    """
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store, backend.runtime.session_store
    )
    # Via monkeypatch, NOT a bare assignment: ``_run_readiness`` is a module global
    # that the sibling suites' own reset helper does not restore, so a test leaving
    # a refusing gate behind would 503 every later /query in the session.
    monkeypatch.setattr(
        backend, "_run_readiness", backend.RunReadinessGate(config_reader=_ready_config)
    )


def _bind(session_id, **fields):
    """Persist a binding on *session_id* through the production store."""
    spec = SessionSpec(**fields)
    backend._session_specs.save(session_id, spec)
    return spec


def _capture_start_async(monkeypatch, session_id):
    """Replace ``start_async`` with a recorder; return the captured-kwargs dict."""
    captured = {}

    def fake_start_async(**kwargs):
        captured.update(kwargs)
        return f"{session_id}:r2"

    monkeypatch.setattr(backend.runtime, "start_async", fake_start_async)
    return captured


# ---------------------------------------------------------------------------
# The model itself — binding semantics, no HTTP
# ---------------------------------------------------------------------------


class TestSessionSpecModel:
    """Behaviour that lives ON the model: overrides, three-state tools, projections."""

    def test_omitted_fields_inherit_rather_than_reset(self):
        """A request that declares nothing must not clear a single bound field."""
        spec = SessionSpec(
            origin=SessionOrigin.WIKI,
            model="openai/bound",
            allowed_tools=["wiki_clone_repo"],
            strict_tool_scope=True,
            cwd="/srv/repo",
            skill_instructions="PLAYBOOK",
        )
        merged, refused = spec.merge_request_overrides(
            SessionSpecOverrides.from_request_context({})
        )
        assert merged == spec
        assert refused == ()

    def test_model_is_overridable_but_tool_set_is_not_on_a_bound_session(self):
        """Choosing a model is the user's call; swapping a bound session's tools is not."""
        spec = SessionSpec(origin=SessionOrigin.WIKI, model="a", allowed_tools=["wiki_finalize"])
        merged, refused = spec.merge_request_overrides(
            SessionSpecOverrides.from_request_context(
                {"model": "b", "mcp_tools": ["shell", "read_file"]}
            )
        )
        assert merged.model == "b"
        assert merged.allowed_tools == ("wiki_finalize",)
        assert refused == ("allowed_tools",)

    def test_merge_request_overrides_revalidates_declared_values(self):
        """``model_copy(update=...)`` skips field validators; the merge must not.

        A blank ``model``, a non-positive ``session_step_budget`` and an empty
        ``fallback_models`` ladder must each collapse to ``None`` on the merged
        spec exactly as they would on direct construction, rather than landing
        on the merged spec unnormalized.
        """
        spec = SessionSpec(
            origin=SessionOrigin.USER,
            model="a",
            fallback_models=("x", "y"),
            session_step_budget=20,
        )
        merged, refused = spec.merge_request_overrides(
            SessionSpecOverrides.from_request_context(
                {"model": "  ", "session_step_budget": -5, "fallback_models": []}
            )
        )
        assert merged.model is None
        assert merged.session_step_budget is None
        assert merged.fallback_models is None
        assert refused == ()

    def test_unbound_session_still_accepts_a_per_turn_tool_set(self):
        """A plain console chat keeps supplying its tool set per turn — do not lock it."""
        spec = SessionSpec(origin=SessionOrigin.USER, model="a")
        assert spec.purpose_bound is False
        merged, refused = spec.merge_request_overrides(
            SessionSpecOverrides.from_request_context({"mcp_tools": ["x", "y"]})
        )
        assert merged.allowed_tools == ("x", "y")
        assert refused == ()

    def test_allowed_tools_is_three_state(self):
        """`[]` grants no MCP tool and must never collapse into `None` (grants every tool)."""
        assert SessionSpec(allowed_tools=[]).allowed_tools == ()
        assert SessionSpec().allowed_tools is None
        payload = SessionSpec(allowed_tools=[]).to_context_payload()
        assert payload["mcp_tools"] == []
        assert SessionSpec.from_context(payload).allowed_tools == ()

    def test_editable_map_is_fail_closed_and_matches_enforcement(self):
        """The projection a client trusts must agree with what the merge actually allows."""
        bound = SessionSpec(origin=SessionOrigin.WIKI)
        editable = bound.editable_fields()
        assert editable["model"] is True
        assert editable["fallback_models"] is True
        assert editable["allowed_tools"] is False
        assert editable["cwd"] is False
        # Never-overridable fields are absent from the map entirely, not reported true.
        assert "capabilities" not in editable
        assert "origin" not in editable
        for field, allowed in editable.items():
            assert bound.field_editable(field) is allowed

    def test_a_refused_override_cannot_return_through_the_context_passthrough(self):
        """The spec owns its wire keys, so a caller merging leftovers cannot re-add them."""
        assert "mcp_tools" in SessionSpec.SPEC_OWNED_CONTEXT_KEYS
        assert "client_capabilities" in SessionSpec.SPEC_OWNED_CONTEXT_KEYS
        assert SPEC_CONTEXT_KEY in SessionSpec.SPEC_OWNED_CONTEXT_KEYS

    def test_interactive_capabilities_never_reach_an_unattended_fire(self):
        """A turn-scoped `ask_user` must not survive into a fire with nobody to answer."""
        spec = SessionSpec(capabilities=["wiki", "ask_user"])
        assert spec.unattended_capabilities() == ("wiki",)
        # A turn's advertisement is used for THAT turn but never persisted onto the spec.
        assert spec.run_capabilities(["stlite", "apps"]) == ("stlite", "apps")
        round_tripped = SessionSpec.from_context(
            spec.to_context_payload(capabilities=["stlite", "apps"])
        )
        assert round_tripped.capabilities == ("wiki", "ask_user")

    def test_legacy_wiki_qa_context_reconstructs_its_binding(self):
        """The convention this formalizes: a QA session's scope persisted as loose keys."""
        spec = SessionSpec.from_context(
            {
                "client_capabilities": ["wiki"],
                "mcp_tools": ["wiki_list_pages", "wiki_emit_answer"],
                "strict_tool_scope": True,
                "session_step_budget": 50,
                "skill_instructions": "QA PLAYBOOK",
            }
        )
        assert spec.origin is SessionOrigin.WIKI
        assert spec.allowed_tools == ("wiki_list_pages", "wiki_emit_answer")
        assert spec.strict_tool_scope is True
        assert spec.session_step_budget == 50
        assert spec.purpose_bound is True

    def test_store_prefers_the_typed_mirror_over_a_later_gating_only_event(self):
        """A capability re-injection appends a gating-only event; it is not the binding."""
        spec = SessionSpec(model="bound", allowed_tools=["a"], capabilities=["wiki"])
        events = [
            {"type": "context", "payload": spec.to_context_payload()},
            {"type": "user", "payload": {"text": "q"}},
            {"type": "context", "payload": {"client_capabilities": ["wiki"]}},
        ]
        store = SessionSpecStore(
            load_transcript=lambda _sid: events, append_context_event=lambda *_a: None
        )
        loaded = store.load("s")
        assert loaded.model == "bound"
        assert loaded.allowed_tools == ("a",)

    def test_a_stored_spec_from_another_build_stays_loadable(self):
        """An unknown key must never brick the session the spec describes."""
        spec = SessionSpec.from_blob({"model": "m", "some_future_field": 1})
        assert spec.model == "m"

    def test_overrides_forbid_unknown_fields(self):
        """extra="forbid" at the boundary: a smuggled field is a clean failure."""
        with pytest.raises(Exception):
            SessionSpecOverrides(model="m", not_a_field=True)


# ---------------------------------------------------------------------------
# B2 — /query loads the persisted spec instead of re-deriving
# ---------------------------------------------------------------------------


class TestQueryReadsPersistedSpec:
    """``POST /sessions/<id>/query`` must re-engage on the binding, not on defaults."""

    def test_followup_omitting_model_tools_and_cwd_runs_with_the_persisted_spec(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The required contract: absence inherits the binding, it does not reset it.

        This is the observed failure verbatim — a console follow-up into a bound
        session arrived on a different model, with an unrelated tool set, no
        playbook, and a cwd pointing at an empty temp dir.
        """
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(
            sid,
            origin=SessionOrigin.WIKI,
            model="openai/bound-model",
            allowed_tools=["wiki_clone_repo", "wiki_finalize"],
            strict_tool_scope=True,
            skill_instructions="INDEXER PLAYBOOK",
            cwd=str(tmp_path),
            capabilities=["wiki"],
            session_step_budget=42,
        )
        captured = _capture_start_async(monkeypatch, sid)

        resp = client.post(
            f"/api/sessions/{sid}/query", headers=auth_headers, json={"query": "continue"}
        )

        assert resp.status_code == 202
        assert captured["model_name"] == "openai/bound-model"
        assert captured["allowed_tools"] == ["wiki_clone_repo", "wiki_finalize"]
        assert captured["strict_tool_scope"] is True
        assert captured["skill_instructions"] == "INDEXER PLAYBOOK"
        assert captured["cwd"] == str(tmp_path)
        assert captured["session_step_budget"] == 42

    def test_followup_does_not_persist_a_default_model_over_the_binding(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The corruption leg: the re-derived default was written back onto the session."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="openai/bound-model")
        _capture_start_async(monkeypatch, sid)

        client.post(f"/api/sessions/{sid}/query", headers=auth_headers, json={"query": "go"})

        assert backend._session_specs.load(sid).model == "openai/bound-model"
        assert backend._load_last_context(sid)["model"] == "openai/bound-model"

    def test_a_deliberate_model_change_is_applied_and_persisted(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A user changing the model is legitimate on any session, bound or not."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="openai/old")
        captured = _capture_start_async(monkeypatch, sid)

        client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "go", "context": {"model": "openai/new"}},
        )

        assert captured["model_name"] == "openai/new"
        assert backend._session_specs.load(sid).model == "openai/new"

    def test_a_bound_session_refuses_a_request_tool_swap(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """108 unrelated tools arriving on a 16-id indexer session is the reported drift."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(
            sid,
            origin=SessionOrigin.WIKI,
            model="m",
            allowed_tools=["wiki_finalize"],
            strict_tool_scope=True,
        )
        captured = _capture_start_async(monkeypatch, sid)

        client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "go", "context": {"mcp_tools": ["shell", "read_file", "grep"]}},
        )

        assert captured["allowed_tools"] == ["wiki_finalize"]
        assert backend._session_specs.load(sid).allowed_tools == ("wiki_finalize",)

    def test_an_unbound_console_session_still_scopes_per_turn(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """Regression guard: locking every session would strand ordinary chat."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.USER, model="m")
        captured = _capture_start_async(monkeypatch, sid)

        client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "go", "context": {"mcp_tools": ["srv__a", "srv__b"]}},
        )

        assert captured["allowed_tools"] == ["srv__a", "srv__b"]

    def test_session_creation_records_the_binding(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The binding is written at creation, the one moment intent is unambiguous."""
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post(
            "/api/sessions",
            headers={**auth_headers, "X-Mewbo-Capabilities": "wiki"},
            json={"context": {"model": "openai/chosen"}},
        )
        sid = resp.get_json()["session_id"]

        spec = backend._session_specs.load(sid)
        assert spec.model == "openai/chosen"
        assert spec.capabilities == ("wiki",)


# ---------------------------------------------------------------------------
# The GET projection a console hydrates from
# ---------------------------------------------------------------------------


class TestSpecProjectionRoute:
    """``GET /api/sessions/<id>/spec`` — the binding plus a fail-closed editable map."""

    def test_projection_shape_for_a_bound_session(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The exact wire contract a composer builds against."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(
            sid,
            origin=SessionOrigin.WIKI,
            surface="console",
            model="openai/bound",
            fallback_models=["openai/rescue"],
            allowed_tools=["wiki_finalize"],
            strict_tool_scope=True,
            capabilities=["wiki"],
            skill_instructions="PLAYBOOK",
            session_step_budget=50,
            project="Assistant",
            cwd=str(tmp_path),
        )

        body = client.get(f"/api/sessions/{sid}/spec", headers=auth_headers).get_json()

        assert body["session_id"] == sid
        assert body["source"] == "spec"
        assert body["spec"] == {
            "origin": "wiki",
            "surface": "console",
            "purpose_bound": True,
            "project": "Assistant",
            "slug": None,
            "cwd": str(tmp_path),
            "model": "openai/bound",
            "fallback_models": ["openai/rescue"],
            "allowed_tools": ["wiki_finalize"],
            "strict_tool_scope": True,
            "capabilities": ["wiki"],
            "skill_instructions_present": True,
            "session_step_budget": 50,
            "mode": None,
        }
        assert body["editable"] == {
            "project": False,
            "slug": False,
            "cwd": False,
            "model": True,
            "fallback_models": True,
            "allowed_tools": False,
            "strict_tool_scope": False,
            "skill_instructions": False,
            "session_step_budget": False,
            "mode": True,
        }

    def test_playbook_text_is_not_projected(self, client, auth_headers, tmp_path, monkeypatch):
        """A whole AgentDef body would dominate the response; presence is what a client needs."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(sid, skill_instructions="SECRET PLAYBOOK BODY")

        raw = client.get(f"/api/sessions/{sid}/spec", headers=auth_headers).get_data(as_text=True)

        assert "SECRET PLAYBOOK BODY" not in raw
        assert '"skill_instructions_present": true' in raw

    def test_legacy_session_reports_a_reconstruction(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A session predating bindings still projects one, flagged as reconstructed."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.runtime.append_context_event(sid, {"model": "legacy", "mcp_tools": ["a"]})

        body = client.get(f"/api/sessions/{sid}/spec", headers=auth_headers).get_json()

        assert body["source"] == "legacy_context"
        assert body["spec"]["model"] == "legacy"

    def test_unknown_session_404s(self, client, auth_headers, tmp_path, monkeypatch):
        """An unknown id must 404, never render an empty-but-successful binding."""
        _reset_backend(tmp_path, monkeypatch)
        assert client.get("/api/sessions/nope/spec", headers=auth_headers).status_code == 404


# ---------------------------------------------------------------------------
# Unattended fires — ladder + per-fire capability re-derivation
# ---------------------------------------------------------------------------


class TestUnattendedFire:
    """The trigger/apps wake path re-derives from the purpose, not from the last turn."""

    def test_fire_re_derives_capabilities_and_drops_ask_user(
        self, tmp_path, monkeypatch
    ):
        """One interactive turn widened capabilities; every later fire inherited them.

        ``ask_user`` is the case that matters: a tool that BLOCKS until a human
        answers, bound on a run with no human attached.
        """
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.APPS, model="m", capabilities=["apps"])
        # An interactive turn widens the advertised capabilities.
        backend.runtime.append_context_event(
            sid, {"client_capabilities": ["apps", "stlite", "ask_user"]}
        )
        captured = _capture_start_async(monkeypatch, sid)

        backend._reengage_idle_session(sid, "wake", source_platform="trigger")

        assert captured["model_name"] == "m"
        assert backend._load_last_context(sid)["client_capabilities"] == ["apps"]

    def test_fire_carries_the_persisted_ladder(self, tmp_path, monkeypatch):
        """An opted-in ladder used to revert to config policy on every unattended wake."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(sid, model="primary", fallback_models=["rescue-a", "rescue-b"])
        captured = _capture_start_async(monkeypatch, sid)

        backend._reengage_idle_session(sid, "wake", source_platform="trigger")

        assert captured["fallback_models"] == ("rescue-a", "rescue-b")

    def test_fire_keeps_the_binding_a_gating_reinjection_would_have_blanked(
        self, tmp_path, monkeypatch
    ):
        """Capability re-injection appends a gating-only event; the wake must not read it.

        Reading the binding AFTER that append is how an unattended wake used to lose
        its model, tool ceiling and playbook while keeping only its capabilities.
        """
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(
            sid,
            model="bound",
            allowed_tools=["wiki_finalize"],
            strict_tool_scope=True,
            skill_instructions="PLAYBOOK",
            capabilities=["wiki"],
        )
        captured = _capture_start_async(monkeypatch, sid)

        backend._reengage_idle_session(sid, "wake", source_platform="trigger")

        assert captured["model_name"] == "bound"
        assert captured["allowed_tools"] == ["wiki_finalize"]
        assert captured["strict_tool_scope"] is True
        assert captured["skill_instructions"] == "PLAYBOOK"

    def test_a_pipeline_allowlist_still_wins_over_the_binding(self, tmp_path, monkeypatch):
        """An app pipeline's least-privilege scope is authoritative over the session's."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(sid, model="m", allowed_tools=["wide_a", "wide_b"])
        captured = _capture_start_async(monkeypatch, sid)

        backend._reengage_idle_session(
            sid,
            "wake",
            source_platform="trigger",
            allowed_tools_override=["narrow"],
            strict_scope_override=True,
        )

        assert captured["allowed_tools"] == ["narrow"]
        assert captured["strict_tool_scope"] is True


# ---------------------------------------------------------------------------
# Readiness gate
# ---------------------------------------------------------------------------


class TestRunReadinessGate:
    """A process whose config has not resolved must refuse, not accept and die."""

    def test_unresolved_config_refuses_with_a_retryable_503(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The run used to be persisted seconds before the first model call died."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()

        def _not_loaded():
            raise RuntimeError("config has not resolved yet")

        monkeypatch.setattr(
            backend, "_run_readiness", backend.RunReadinessGate(config_reader=_not_loaded)
        )
        started = _capture_start_async(monkeypatch, sid)

        resp = client.post(f"/api/sessions/{sid}/query", headers=auth_headers, json={"query": "q"})

        assert resp.status_code == 503
        assert resp.get_json()["error"]["retryable"] is True
        assert started == {}, "an unready process must not accept the run at all"

    def test_a_keyless_config_is_NOT_refused(self):
        """The gate must never masquerade a misconfiguration as 'retry shortly'.

        An empty ``api_key`` is a legitimate steady state — a proxy at ``api_base``
        can authenticate on the caller's behalf — and is in any case
        indistinguishable from a key that simply has not loaded. A run that fails
        for a configured reason must fail with THAT reason.
        """
        gate = backend.RunReadinessGate(
            config_reader=lambda: SimpleNamespace(
                llm=SimpleNamespace(default_model="m", api_key="", api_base="")
            )
        )
        assert gate.check() is None

    def test_a_probe_bug_fails_open(self):
        """A gate whose own bug can refuse traffic is worse than the window it closes."""
        gate = backend.RunReadinessGate(config_reader=lambda: SimpleNamespace())
        assert gate.check() is None

    def test_readiness_latches_after_the_first_success(self):
        """Readiness is monotonic in a process; no request after the first re-probes."""
        calls = []

        def _reader():
            calls.append(1)
            return _ready_config()

        gate = backend.RunReadinessGate(config_reader=_reader)
        assert gate.check() is None
        assert gate.check() is None
        assert len(calls) == 1
