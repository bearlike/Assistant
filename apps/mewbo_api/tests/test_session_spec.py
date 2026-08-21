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
from mewbo_core.session.session_provenance import SessionOrigin
from mewbo_core.session.session_store import SessionStore


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


def _configure_projects(monkeypatch, **paths):
    """Point ``get_config().projects`` at *paths* (name → directory), flag OFF.

    ``allow_external_cwd`` is left at its default false throughout: a project
    rebind resolves a SERVER-known path, so it must work with the external-cwd
    gate shut — that gate exists to stop a caller naming an arbitrary host path,
    which this surface never lets them do.
    """
    from mewbo_core.config import ProjectConfig, get_config

    cfg = get_config()
    projects = {name: ProjectConfig(path=str(path)) for name, path in paths.items()}
    patched = cfg.model_copy(update={"projects": projects})
    monkeypatch.setattr("mewbo_api.backend.get_config", lambda: patched)
    return patched


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
        # A turn's advertisement is used for THAT turn but never persisted onto the
        # spec. It is UNIONED with the session-owned half rather than replacing it:
        # `wiki` is what the session is FOR, so a client that advertises only its own
        # rendering set must not bury it (see SESSION_OWNED_CAPABILITIES). `ask_user`
        # is not carried across — it is interactive-only and this caller did not ask
        # for it — which is what keeps the unattended derivation below unchanged.
        assert spec.run_capabilities(["stlite", "apps"]) == ("wiki", "stlite", "apps")
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
# Reconstruction reads TAGS, not just the context payload
# ---------------------------------------------------------------------------

# The header the console stamps on EVERY request (``api/realClient.ts``
# ``CLIENT_CAPABILITIES``). It declares what that client can RENDER and says
# nothing about what a session is FOR, which is why the classifier has no
# an ``apps`` capability arm — and therefore why the tag has to be read.
CONSOLE_CAPABILITIES = ["stlite", "apps", "ask_user", "generative_ui"]


class TestReconstructionClassifiesFromTags:
    """A session with no typed mirror must rebuild the binding its creation would have.

    ``SessionSpec.from_context`` classifies from a context payload alone, and
    ``SessionSpecStore.load`` falls back to it for every session that never had a
    spec written. Without the session's TAGS in that classification a real Apps
    or wiki session reconstructs as ``user``, which un-binds it: ``purpose_bound``
    goes false and the whole ``OVERRIDABLE_WHEN_UNBOUND`` tier re-opens on a
    session whose scope is the entire point.
    """

    def test_an_app_tagged_session_with_no_spec_blob_stays_bound(self, tmp_path, monkeypatch):
        """The live shape: an ``app:<id>`` session whose only context is the console header."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.tag_session(sid, "app:demo-app")
        backend.runtime.append_context_event(
            sid, {"model": "m", "client_capabilities": CONSOLE_CAPABILITIES}
        )

        spec = backend._session_specs.load(sid)

        assert spec.origin is SessionOrigin.APPS
        assert spec.purpose_bound is True
        assert spec.field_editable("cwd") is False
        assert spec.field_editable("allowed_tools") is False

    def test_the_same_context_without_a_tag_is_an_unbound_user_session(
        self, tmp_path, monkeypatch
    ):
        """The console-chat case: the fixed capability header must bind NOTHING.

        This is the regression the removed ``apps`` capability arm caused — every
        ordinary chat classified as an Apps session. Reading tags must not bring
        it back through the store.
        """
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.runtime.append_context_event(
            sid, {"model": "m", "client_capabilities": CONSOLE_CAPABILITIES}
        )

        spec = backend._session_specs.load(sid)

        assert spec.origin is SessionOrigin.USER
        assert spec.purpose_bound is False
        assert spec.field_editable("cwd") is True

    def test_a_wiki_job_tag_binds_a_session_that_wrote_no_capability(
        self, tmp_path, monkeypatch
    ):
        """Tags are the robust signal precisely because an older job stored no capabilities."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.tag_session(sid, "wiki:job:42")
        backend.runtime.append_context_event(sid, {"model": "m"})

        spec = backend._session_specs.load(sid)

        assert spec.origin is SessionOrigin.WIKI
        assert spec.purpose_bound is True

    def test_an_unattended_wake_cannot_bake_a_lost_origin_into_the_blob(
        self, tmp_path, monkeypatch
    ):
        """The durability leg: the wake persists what it loaded, so a misread STICKS.

        ``_reengage_idle_session`` reads the binding and writes it straight back as
        the newest context event — including the typed mirror. A reconstruction that
        dropped the origin would therefore turn a transient misclassification into a
        stored ``origin: "user"`` that no later tag read can undo.
        """
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        backend.session_store.tag_session(sid, "app:demo-app")
        backend.runtime.append_context_event(
            sid, {"model": "m", "client_capabilities": CONSOLE_CAPABILITIES}
        )
        _capture_start_async(monkeypatch, sid)

        backend._reengage_idle_session(sid, "wake", source_platform="trigger")

        persisted = backend._session_specs.load(sid)
        assert persisted.origin is SessionOrigin.APPS
        assert persisted.purpose_bound is True
        assert backend._load_last_context(sid)[SPEC_CONTEXT_KEY]["origin"] == "apps"

    def test_a_store_with_no_tags_reader_still_loads(self):
        """The reader is OPTIONAL: a caller driving the store off a bare event list works."""
        events = [{"type": "context", "payload": {"model": "legacy"}}]
        store = SessionSpecStore(
            load_transcript=lambda _sid: events, append_context_event=lambda *_a: None
        )

        loaded = store.load("s")

        assert loaded.model == "legacy"
        assert loaded.origin is SessionOrigin.USER

    def test_a_failing_tag_read_degrades_instead_of_bricking_the_load(self):
        """A store read is on the request path — an unreadable tag index is not fatal."""

        def _explode(_session_id):
            raise RuntimeError("tag index unavailable")

        store = SessionSpecStore(
            load_transcript=lambda _sid: [
                {"type": "context", "payload": {"client_capabilities": ["wiki"]}}
            ],
            append_context_event=lambda *_a: None,
            load_tags=_explode,
        )

        # The context fallback still classifies; only the tag signal is lost.
        assert store.load("s").origin is SessionOrigin.WIKI

    def test_a_corrupt_typed_mirror_falls_back_to_the_loose_keys_beside_it(self):
        """A stored spec must never brick its session — the guard the comment promises.

        ``from_blob`` raising on a malformed typed mirror must not propagate out
        of ``load()``. The trap: a fallback that re-reads the SAME payload has
        ``SessionSpec.from_context`` find the same corrupt blob under
        ``SPEC_CONTEXT_KEY`` and re-enter the same failing parse. The loose keys
        sitting beside that blob in the same context payload must still resolve.

        The corrupting field is ``strict_tool_scope``, not ``origin``: ``load()``
        passes a freshly re-derived ``origin`` into ``from_blob`` on every call,
        which overwrites whatever the blob itself said BEFORE validation runs —
        so a bad stored ``origin`` alone never raises, it is healed inline. A
        field with no such override is needed to exercise the recovery path this
        test pins.
        """
        events = [
            {
                "type": "context",
                "payload": {
                    SPEC_CONTEXT_KEY: {"strict_tool_scope": "not-a-real-bool"},
                    "model": "loose-model",
                    "mcp_tools": ["a"],
                },
            }
        ]
        store = SessionSpecStore(
            load_transcript=lambda _sid: events, append_context_event=lambda *_a: None
        )

        loaded = store.load("s")

        assert loaded.model == "loose-model"
        assert loaded.allowed_tools == ("a",)


class TestStoredMirrorOriginIsReDerived:
    """A typed mirror's ``origin`` is re-classified on load, not trusted verbatim.

    ``SessionOrigin`` is documented as derived at read time and never stored,
    but the typed mirror persists it anyway. A blob written under an earlier
    classifier — one that filed every console chat with a
    ``client_capabilities`` header under ``apps`` — keeps that verdict forever
    unless the stored value is re-derived exactly like the reconstruction legs
    already are.
    """

    def test_a_stale_apps_origin_is_healed_to_the_current_classification(self):
        """The live symptom: a blob classified 'apps' by a since-removed arm.

        The current classifier has no capability arm for ``apps`` at all (see
        ``session_provenance.py`` — a real Apps session is tagged ``app:<id>``
        instead), so a console-header-only, untagged session that a PRIOR
        classifier once filed under ``apps`` must re-derive to ``user`` on the
        next load — healing ``purpose_bound`` back to ``False`` and un-blocking
        the project edit the stale verdict was silently refusing.
        """
        console_capabilities = ["stlite", "apps", "ask_user", "generative_ui"]
        events = [
            {
                "type": "context",
                "payload": {
                    SPEC_CONTEXT_KEY: {
                        "origin": "apps",
                        "model": "m",
                        "client_capabilities": console_capabilities,
                    },
                    "model": "m",
                    "client_capabilities": console_capabilities,
                },
            }
        ]
        store = SessionSpecStore(
            load_transcript=lambda _sid: events, append_context_event=lambda *_a: None
        )

        loaded = store.load("s")

        assert loaded.origin is SessionOrigin.USER
        assert loaded.purpose_bound is False

    def test_a_tag_still_wins_over_a_blob_written_under_a_stale_classification(self):
        """Re-derivation reads the SAME signals reconstruction does — tags win.

        A blob stamped ``user`` (e.g. before a tag arm existed) on a session
        that IS tagged must heal FORWARD to the tag's origin, not merely stay
        put — proving this is a real re-classification, not a special-cased
        "apps only" patch.
        """
        events = [
            {
                "type": "context",
                "payload": {
                    SPEC_CONTEXT_KEY: {"origin": "user", "model": "m"},
                    "model": "m",
                },
            }
        ]
        store = SessionSpecStore(
            load_transcript=lambda _sid: events,
            append_context_event=lambda *_a: None,
            load_tags=lambda _sid: ["wiki:job:42"],
        )

        loaded = store.load("s")

        assert loaded.origin is SessionOrigin.WIKI
        assert loaded.purpose_bound is True


# ---------------------------------------------------------------------------
# The binding read is bounded by TYPE, and both sources answer identically
# ---------------------------------------------------------------------------


#: Comfortably past any window a count-bounded tail scan would plausibly pick.
SPEC_TAIL_LENGTH = 512


class _CountingSessionStore(SessionStore):
    """Real store that tallies whole-transcript reads.

    Counting store reads is exact and names the defect in its own assertion; a
    wall-clock assertion would measure the machine and flake under a parallel
    suite (``session/CLAUDE.md`` -> "Asserting cost in a test").
    """

    def __init__(self, root_dir: str) -> None:
        super().__init__(root_dir=root_dir)
        self.load_transcript_calls = 0

    def load_transcript(self, session_id):
        self.load_transcript_calls += 1
        return super().load_transcript(session_id)


def _seeded_store(tmp_path, events, *, tags=()):
    """Persist *events* through a real store and return ``(store, session_id)``."""
    store = _CountingSessionStore(str(tmp_path))
    sid = store.create_session()
    for tag in tags:
        store.tag_session(sid, tag)
    for event in events:
        store.append_event(sid, event)
    return store, sid


@pytest.fixture(params=["bounded", "transcript"])
def spec_store_factory(request, tmp_path):
    """Build a ``SessionSpecStore`` over one corpus through EITHER source.

    The type-bounded reader and the transcript fallback are two spellings of one
    question, and the only way they stay honest is by running the same corpus
    through both — reading one and reasoning about the other is exactly how they
    diverge. Every test taking this fixture therefore asserts the binding twice.
    """

    def build(events, *, tags=()):
        store, sid = _seeded_store(tmp_path, events, tags=tags)
        kwargs = {
            "load_transcript": store.load_transcript,
            "append_context_event": lambda *_a: None,
            "load_tags": store.tags_for_session,
        }
        if request.param == "bounded":
            kwargs["latest_event_of_type"] = (
                lambda session_id, event_type, payload_key: store.latest_event_of_type(
                    session_id, event_type, payload_key=payload_key
                )
            )
        return SessionSpecStore(**kwargs), sid, store

    return build


class TestBindingReadIsTypeBounded:
    """``SessionSpecStore.load`` reads the ONE context event it needs, not a transcript.

    It is the hottest read in the app — ``/query``, ``/message``, ``/recover`` and
    every unattended trigger fire load the binding first — so it must not pull
    the whole session to pick one payload out of the tail (measured on the
    deployed store's largest session: 10,296 documents / 14.5 MB, against 1
    document / 0.2 KB for the type-bounded read).

    The correctness half is what these tests exist for. A binding read that
    returns the wrong payload does not merely mislabel a session: it can drop
    ``purpose_bound`` to false and re-open the whole ``OVERRIDABLE_WHEN_UNBOUND``
    tier on a session whose scope is the entire point.
    """

    def test_the_newest_spec_carrying_event_wins_over_a_newer_bare_one(
        self, spec_store_factory
    ):
        """THE ordering a two-read form could get backwards.

        A gating-only context re-injection (capabilities, no mirror) is appended
        AFTER the binding on every recovered session. "Newest context event" and
        "newest context event carrying the mirror" are therefore different
        questions, and answering the second with the first blanks the binding.

        ``surface`` is the load-bearing assertion, and it is why this test is not
        satisfied by any read that merely lands on the right EVENT.
        ``to_context_payload`` writes the loose legacy keys beside the mirror, so
        ``model``/``slug``/``allowed_tools`` all resolve identically whether the
        winner was parsed as a blob or reconstructed from the keys next to it —
        a wrong implementation coincides with the right answer on every one of
        them. ``surface`` lives ONLY in the blob, so it fails unless the newest
        MIRROR was found and parsed AS a mirror.
        """
        bound = SessionSpec(
            model="bound", allowed_tools=["wiki_finalize"], slug="pinned", surface="console"
        )
        store, sid, _ = spec_store_factory(
            [
                {"type": "context", "payload": SessionSpec(model="older").to_context_payload()},
                {"type": "context", "payload": bound.to_context_payload()},
                {"type": "user", "payload": {"text": "q"}},
                {"type": "context", "payload": {"client_capabilities": ["wiki"]}},
            ]
        )

        loaded = store.load(sid)

        assert loaded.model == "bound"
        assert loaded.slug == "pinned"
        assert loaded.allowed_tools == ("wiki_finalize",)
        assert loaded.surface == "console", "the mirror was not parsed as a mirror"

    def test_a_corrupt_blob_reconstructs_from_ITS_OWN_payload(self, spec_store_factory):
        """The recovery leg must read the loose keys BESIDE the blob that failed.

        Reconstructing from a DIFFERENT context event would silently answer with
        another turn's binding — indistinguishable from a correct answer at the
        point where it matters. The corrupt event is deliberately sandwiched
        between two decoys, each carrying a named model, so answering from either
        neighbour fails loudly instead of coinciding with the right answer: an
        older-context reader yields ``older-decoy`` and a newest-context-overall
        reader yields ``newer-decoy``.

        The corrupting field is ``strict_tool_scope``, not ``origin``: ``load``
        overwrites ``origin`` before validation on every call, so a bad stored
        origin is healed inline rather than raising.
        """
        store, sid, _ = spec_store_factory(
            [
                {"type": "context", "payload": {"model": "older-decoy", "mcp_tools": ["nope"]}},
                {
                    "type": "context",
                    "payload": {
                        SPEC_CONTEXT_KEY: {"strict_tool_scope": "not-a-real-bool"},
                        "model": "loose-model",
                        "mcp_tools": ["a"],
                    },
                },
                {"type": "context", "payload": {"model": "newer-decoy"}},
            ]
        )

        loaded = store.load(sid)

        assert loaded.model == "loose-model"
        assert loaded.allowed_tools == ("a",)

    def test_no_mirror_anywhere_falls_back_to_the_newest_context_payload(
        self, spec_store_factory
    ):
        """The legacy leg reads ONE payload — the newest — and never merges.

        Merging every context event would resurrect a field the user cleared (a
        removed project sticking forever), which is why the generic context
        reader does not merge either. The older payload's ``project`` must NOT
        survive onto the reconstruction.
        """
        store, sid, _ = spec_store_factory(
            [
                {"type": "context", "payload": {"model": "old", "project": "Cleared"}},
                {"type": "context", "payload": {"model": "newest", "mcp_tools": ["a"]}},
            ]
        )

        loaded = store.load(sid)

        assert loaded.model == "newest"
        assert loaded.allowed_tools == ("a",)
        assert loaded.project is None

    def test_the_binding_survives_a_tail_longer_than_any_window(self, spec_store_factory):
        """THE regression a count-bounded tail scan fails.

        A long run since the last re-engagement buries the binding arbitrarily
        far back. A window-bounded read reports "no binding", the reconstruction
        classifies a tagged session from an empty payload, and a purpose-bound
        session silently becomes editable. That is a wrong ANSWER, not a slow one.
        """
        bound = SessionSpec(model="buried", allowed_tools=["wiki_finalize"])
        events = [{"type": "context", "payload": bound.to_context_payload()}]
        events += [
            {"type": "assistant", "payload": {"text": str(index)}}
            for index in range(SPEC_TAIL_LENGTH)
        ]
        store, sid, _ = spec_store_factory(events, tags=["wiki:job:42"])

        loaded = store.load(sid)

        assert loaded.model == "buried", "the binding was bounded away by a window, not by the type"
        assert loaded.allowed_tools == ("wiki_finalize",)
        assert loaded.purpose_bound is True

    def test_a_session_with_no_context_at_all_still_loads(self, spec_store_factory):
        """No binding yet is an answer, not a failure — and tags still classify it."""
        store, sid, _ = spec_store_factory(
            [{"type": "user", "payload": {"text": "q"}}], tags=["wiki:job:42"]
        )

        loaded = store.load(sid)

        assert loaded.model is None
        assert loaded.origin is SessionOrigin.WIKI

    def test_a_non_dict_mirror_value_reconstructs_from_the_payload_carrying_it(
        self, spec_store_factory
    ):
        """The ONE shape where the type-bounded read differs from a full scan.

        The store narrows on "the key is SET", deliberately coarser than "the key
        holds a blob" — it cannot know what a caller's key means. So a payload
        whose ``session_spec`` is a non-dict value is SELECTED here, where the old
        backwards scan skipped it and kept looking for a real blob further back.

        Nothing writes such a value (``to_context_payload`` always stores a dict),
        and the two forms agree whenever it is the only context event. Pinned so
        the behaviour is a decision rather than an accident: it reconstructs from
        the payload it found, exactly as the corrupt-blob leg does.
        """
        store, sid, _ = spec_store_factory(
            [
                {"type": "context", "payload": SessionSpec(model="older").to_context_payload()},
                {"type": "context", "payload": {SPEC_CONTEXT_KEY: "junk", "model": "beside-it"}},
            ]
        )

        assert store.load(sid).model == "beside-it"

    def test_an_unreadable_store_degrades_to_no_binding(self, tmp_path):
        """A binding read sits on the run path — a storage fault must not brick it."""

        def _explode(*_args):
            raise RuntimeError("mongo down")

        store = SessionSpecStore(
            load_transcript=_explode,
            append_context_event=lambda *_a: None,
            latest_event_of_type=_explode,
        )

        loaded = store.load("s")

        assert loaded.model is None
        assert loaded.origin is SessionOrigin.USER


class TestBindingReadCost:
    """The wiring is the feature: a correct read that costs a transcript is the defect."""

    def test_the_bounded_reader_never_materialises_the_transcript(self, tmp_path):
        """Injected, ``load`` must not touch ``load_transcript`` even once."""
        events = [
            {"type": "context", "payload": SessionSpec(model="bound").to_context_payload()},
        ] + [{"type": "assistant", "payload": {"text": str(i)}} for i in range(64)]
        session_store, sid = _seeded_store(tmp_path, events)
        spec_store = SessionSpecStore(
            load_transcript=session_store.load_transcript,
            append_context_event=lambda *_a: None,
            load_tags=session_store.tags_for_session,
            latest_event_of_type=(
                lambda session_id, event_type, payload_key: (
                    session_store.latest_event_of_type(
                        session_id, event_type, payload_key=payload_key
                    )
                )
            ),
        )
        session_store.load_transcript_calls = 0

        assert spec_store.load(sid).model == "bound"
        assert session_store.load_transcript_calls == 0, (
            "the binding read fell back to a whole-transcript scan"
        )

    def test_has_typed_mirror_answers_from_the_same_bounded_read(self, spec_store_factory):
        """The projection's ``source`` field and the binding must agree, always.

        A route scanning the transcript itself was a second way of asking one
        question. Both directions are pinned here: a saved spec reports a mirror
        even when a later gating-only event is newer, and a legacy session that
        never had one reports none.
        """
        bound, sid, _ = spec_store_factory(
            [
                {"type": "context", "payload": SessionSpec(model="m").to_context_payload()},
                {"type": "context", "payload": {"client_capabilities": ["wiki"]}},
            ]
        )
        assert bound.has_typed_mirror(sid) is True

        legacy, legacy_sid, _ = spec_store_factory(
            [{"type": "context", "payload": {"model": "legacy", "mcp_tools": ["a"]}}]
        )
        assert legacy.has_typed_mirror(legacy_sid) is False

    def test_the_production_store_is_wired_to_the_bounded_reader(
        self, tmp_path, monkeypatch
    ):
        """The wiring a docstring cannot enforce.

        The reader is optional so the documented in-memory mode still works,
        which means a production site that forgets it degrades SILENTLY to the
        old cost with a correct answer and no failing test. This is that test:
        it drives the real ``backend._session_specs`` and asserts the hot read
        never materialises a transcript.
        """
        _reset_backend(tmp_path, monkeypatch)
        counting = _CountingSessionStore(str(tmp_path / "counted"))
        # Via monkeypatch, unlike ``_reset_backend``'s own bare assignments: this
        # swaps in a store that COUNTS, and leaving a counting store bound for the
        # rest of the session would make an unrelated suite's reads accumulate on it.
        monkeypatch.setattr(backend, "session_store", counting)
        monkeypatch.setattr(
            backend, "runtime", backend.SessionRuntime(session_store=counting)
        )
        sid = counting.create_session()
        backend.runtime.append_context_event(
            sid, SessionSpec(model="bound").to_context_payload()
        )
        counting.load_transcript_calls = 0

        assert backend._session_specs.load(sid).model == "bound"
        assert counting.load_transcript_calls == 0, (
            "backend._session_specs lost its type-bounded reader"
        )

    def test_load_last_context_is_bounded_and_still_reads_the_newest_payload(
        self, tmp_path, monkeypatch
    ):
        """``_load_last_context`` rides the SAME request paths as the spec load.

        It sits beside it on ``/query``, ``/message``, ``/recover`` and every
        unattended fire, so leaving it reading whole transcripts would have kept
        the cost the spec fix removed. Behaviour is unchanged: the NEWEST context
        payload verbatim, `{}` when there is none, and a fresh COPY each call —
        callers merge into it (``setdefault``) before persisting the result as
        the next context event.
        """
        _reset_backend(tmp_path, monkeypatch)
        counting = _CountingSessionStore(str(tmp_path / "counted"))
        monkeypatch.setattr(backend, "session_store", counting)
        monkeypatch.setattr(
            backend, "runtime", backend.SessionRuntime(session_store=counting)
        )
        sid = counting.create_session()
        backend.runtime.append_context_event(sid, {"model": "older", "project": "Cleared"})
        backend.runtime.append_context_event(sid, {"model": "newest"})
        for index in range(SPEC_TAIL_LENGTH):
            counting.append_event(sid, {"type": "assistant", "payload": {"text": str(index)}})
        counting.load_transcript_calls = 0

        payload = backend._load_last_context(sid)

        assert payload == {"model": "newest"}, "a merge or a window would not answer this"
        assert counting.load_transcript_calls == 0, "still reading the whole transcript"
        payload["model"] = "mutated"
        assert backend._load_last_context(sid)["model"] == "newest", "callers got a live mapping"

    def test_load_last_context_is_empty_for_a_session_with_no_context(
        self, tmp_path, monkeypatch
    ):
        """`{}` is the "no context yet" answer every caller already branches on."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        assert backend._load_last_context(sid) == {}


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
# The project→directory binding: resolved once at creation, re-resolved on /query
# ---------------------------------------------------------------------------


class TestProjectDirectoryBinding:
    """A session bound to a project must RUN in that project's directory.

    Two independent legs of the same defect. Creation resolved the project's
    real directory and then threw it away, keeping only the NAME; ``/query``
    was the one re-engage path with no project→directory rung, so it fell
    straight through to an empty per-session temp dir. Together they meant the
    same session ran in the right directory on ``/message`` and in a scratch
    directory on ``/query``.
    """

    def test_creation_against_a_configured_project_persists_its_cwd(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The resolved directory was used as a truth test and then discarded."""
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "demo-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Demo=project_dir)

        resp = client.post("/api/sessions", headers=auth_headers, json={"project": "Demo"})
        sid = resp.get_json()["session_id"]

        spec = backend._session_specs.load(sid)
        assert spec.project == "Demo"
        assert spec.cwd == str(project_dir), "the resolved directory must persist, not re-derive"

    def test_query_resolves_the_bound_project_when_cwd_is_null(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A session predating the creation fix carries a project NAME and no cwd.

        Every sibling re-engage path (``/message``, the diff endpoints, the
        idle restart) resolves it through ``_resolve_session_cwd``; ``/query``
        had no such rung and ran the turn in ``session_temp_dir`` instead.
        """
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "demo-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Demo=project_dir)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m", project="Demo", cwd=None)
        captured = _capture_start_async(monkeypatch, sid)

        resp = client.post(
            f"/api/sessions/{sid}/query", headers=auth_headers, json={"query": "continue"}
        )

        assert resp.status_code == 202
        assert captured["cwd"] == str(project_dir)

    def test_query_still_falls_back_to_the_temp_dir_with_no_binding_at_all(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The temp dir stays the LAST rung — a session naming nothing still runs."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        captured = _capture_start_async(monkeypatch, sid)

        client.post(f"/api/sessions/{sid}/query", headers=auth_headers, json={"query": "go"})

        assert captured["cwd"] == backend.session_temp_dir(sid)


# ---------------------------------------------------------------------------
# The explicit rebind — the ONE sanctioned way a bound session changes project
# ---------------------------------------------------------------------------


class TestProjectRebindRoute:
    """``PUT /api/sessions/<id>/project`` — a deliberate action, not a turn side-effect.

    ``editable.project`` is false on a purpose-bound session and that is
    CORRECT: it stops a stray request silently re-scoping a session mid-turn.
    The cost was that a mis-bound session could not be fixed at all. This route
    is the deliberate door, and it re-resolves the directory through the same
    project resolver creation uses so the binding can never go half-written.
    """

    def test_rebind_persists_both_the_project_and_its_directory(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """Half a binding is the original defect; the new door must not re-open it."""
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "target-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Target=project_dir)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m", project="Stale", cwd=str(tmp_path))

        resp = client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "Target"}
        )

        assert resp.status_code == 200
        spec = backend._session_specs.load(sid)
        assert spec.project == "Target"
        assert spec.cwd == str(project_dir)

    def test_rebind_returns_the_same_projection_shape_as_get_spec(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The console swaps the response straight into its cache; the shapes must match."""
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "target-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Target=project_dir)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m")

        put_body = client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "Target"}
        ).get_json()
        get_body = client.get(f"/api/sessions/{sid}/spec", headers=auth_headers).get_json()

        assert put_body == get_body
        assert put_body["spec"]["project"] == "Target"
        assert put_body["spec"]["cwd"] == str(project_dir)

    def test_the_rebound_directory_is_what_the_next_turn_runs_in(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A rebind nobody's next run honours would be a cosmetic write."""
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "target-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Target=project_dir)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m")
        client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "Target"}
        )
        captured = _capture_start_async(monkeypatch, sid)

        client.post(f"/api/sessions/{sid}/query", headers=auth_headers, json={"query": "go"})

        assert captured["cwd"] == str(project_dir)

    def test_rebind_preserves_every_other_bound_field(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """It rebinds the project — it is not a general spec PATCH in disguise."""
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "target-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Target=project_dir)
        sid = backend.session_store.create_session()
        _bind(
            sid,
            origin=SessionOrigin.WIKI,
            model="openai/bound",
            allowed_tools=["wiki_finalize"],
            strict_tool_scope=True,
            skill_instructions="PLAYBOOK",
            capabilities=["wiki"],
        )

        resp = client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "Target"}
        )

        assert resp.status_code == 200
        spec = backend._session_specs.load(sid)
        assert spec.project == "Target"
        assert spec.model == "openai/bound"
        assert spec.allowed_tools == ("wiki_finalize",)
        assert spec.strict_tool_scope is True
        assert spec.skill_instructions == "PLAYBOOK"
        assert spec.capabilities == ("wiki",)
        assert spec.origin is SessionOrigin.WIKI

    @pytest.mark.parametrize("smuggled", ["cwd", "allowed_tools", "model", "origin", "slug"])
    def test_a_smuggled_server_owned_field_is_a_clean_400(
        self, client, auth_headers, tmp_path, monkeypatch, smuggled
    ):
        """The load-bearing half: ``extra="forbid"`` turns a smuggle into a refusal.

        Without it a client could hand this route a ``cwd`` and anchor a session
        at an arbitrary host path through the one door that writes ``cwd``
        directly — or rename a field and get a silent no-op instead of an error.
        """
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "target-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Target=project_dir)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m")

        resp = client.put(
            f"/api/sessions/{sid}/project",
            headers=auth_headers,
            json={"project": "Target", smuggled: "smuggled-value"},
        )

        assert resp.status_code == 400
        assert smuggled in resp.get_json()["error"]["reason"]
        assert backend._session_specs.load(sid).project is None

    def test_an_unknown_project_is_a_400_and_writes_nothing(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """A failed rebind must leave the previous binding intact, not half-applied."""
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "target-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Target=project_dir)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m", project="Target", cwd=str(project_dir))

        resp = client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "Ghost"}
        )

        assert resp.status_code == 400
        assert "Ghost" in resp.get_json()["error"]["reason"]
        spec = backend._session_specs.load(sid)
        assert spec.project == "Target"
        assert spec.cwd == str(project_dir)

    def test_a_pathless_project_is_a_400(self, client, auth_headers, tmp_path, monkeypatch):
        """A configured project with no directory cannot anchor a session."""
        _reset_backend(tmp_path, monkeypatch)
        _configure_projects(monkeypatch, Pathless="")
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m")

        resp = client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "Pathless"}
        )

        assert resp.status_code == 400

    def test_an_empty_project_name_is_a_400(self, client, auth_headers, tmp_path, monkeypatch):
        """Blank is not a request to unbind — this route only ever binds."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m")

        resp = client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "   "}
        )

        assert resp.status_code == 400

    def test_a_terminated_session_is_410_with_the_shared_envelope(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The console matches this body byte-for-byte; it must not be hand-rolled."""
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "target-repo"
        project_dir.mkdir()
        _configure_projects(monkeypatch, Target=project_dir)
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m")
        monkeypatch.setattr(backend.runtime, "is_terminated", lambda _sid: True)

        resp = client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "Target"}
        )

        assert resp.status_code == 410
        assert resp.get_json() == {"error": backend.ApiResponseKit.TERMINATED_ERROR_BODY}

    def test_an_unknown_session_404s(self, client, auth_headers, tmp_path, monkeypatch):
        """An unknown id must 404 rather than mint a binding on a session that is not there."""
        _reset_backend(tmp_path, monkeypatch)
        resp = client.put(
            "/api/sessions/nope/project", headers=auth_headers, json={"project": "Target"}
        )
        assert resp.status_code == 404

    def test_rebind_works_with_the_external_cwd_gate_shut(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """``allow_external_cwd`` gates caller-NAMED paths; a project path is server-known.

        The whole point of the wire model refusing ``cwd`` is that this route
        can only ever resolve a directory the server already configured, so the
        gate has nothing to refuse here and must not block a legitimate rebind.
        """
        _reset_backend(tmp_path, monkeypatch)
        project_dir = tmp_path / "target-repo"
        project_dir.mkdir()
        patched = _configure_projects(monkeypatch, Target=project_dir)
        assert patched.api.allow_external_cwd is False
        sid = backend.session_store.create_session()
        _bind(sid, origin=SessionOrigin.WIKI, model="m")

        resp = client.put(
            f"/api/sessions/{sid}/project", headers=auth_headers, json={"project": "Target"}
        )

        assert resp.status_code == 200
        assert backend._session_specs.load(sid).cwd == str(project_dir)

    def test_an_ordinary_query_still_cannot_rebind_a_bound_session(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """The guard this route exists BESIDE, not instead of.

        Adding a deliberate door must not weaken the ordinary request path: a
        turn that happens to carry a different project on a purpose-bound
        session is still refused, and the run still uses the bound directory.

        ``capabilities=["wiki"]`` matters here beyond the model construction:
        origin is now RE-DERIVED on every load, so a WIKI binding must carry
        the same signal (a tag or, as here, the capability arm)
        a real wiki session would — otherwise this fixture would silently
        re-classify as an unbound USER session on the very next read and the
        refusal this test exists to pin would stop firing.
        """
        _reset_backend(tmp_path, monkeypatch)
        bound_dir = tmp_path / "bound-repo"
        bound_dir.mkdir()
        other_dir = tmp_path / "other-repo"
        other_dir.mkdir()
        _configure_projects(monkeypatch, Bound=bound_dir, Other=other_dir)
        sid = backend.session_store.create_session()
        _bind(
            sid,
            origin=SessionOrigin.WIKI,
            model="m",
            project="Bound",
            cwd=str(bound_dir),
            capabilities=["wiki"],
        )
        captured = _capture_start_async(monkeypatch, sid)

        resp = client.post(
            f"/api/sessions/{sid}/query",
            headers=auth_headers,
            json={"query": "go", "project": "Other"},
        )

        assert resp.status_code == 202
        assert captured["cwd"] == str(bound_dir)
        spec = backend._session_specs.load(sid)
        assert spec.project == "Bound"
        assert spec.cwd == str(bound_dir)


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
            "denied_tools": None,
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
            "denied_tools": True,
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
        """An opted-in ladder must not revert to config policy on an unattended wake."""
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

        Reading the binding AFTER that append is how an unattended wake loses its
        model, tool ceiling and playbook while keeping only its capabilities.
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
        """The run must be refused up front, not persisted seconds before the first
        model call dies."""
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
