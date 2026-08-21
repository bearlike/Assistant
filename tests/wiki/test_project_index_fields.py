"""The two operator-set, index-time project fields, end to end.

``custom_instructions`` (free-text guidance appended to the indexer playbook)
and ``mcp_servers`` (external MCP servers attached for the run) are the same
shape of field as ``fallback_models``: optional, operator-set, and required to
survive a reindex. That last property is what these tests mostly exist for.

**The failure mode being guarded is a SILENT one, and it is not "the field
does not work".** ``WikiIndexingJob.refresh`` rebuilds its submission from the
durable ``ProjectSettings`` record, so a field added to ``WizardSubmission``
and forgotten in ``from_submission``/``to_submission`` works perfectly on the
first index and is dropped from every index after it, with no error anywhere.
A test that only round-trips the submission would pass throughout. So the
round-trip here goes through the RECORD, in both directions.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mewbo_api.wiki.jobs import (
    INDEXER_TOOLS,
    WikiIndexingJob,
    _compose_skill_instructions,
    _load_indexer_playbook,
    _render_user_query,
)
from mewbo_api.wiki.settings import ProjectSettingsPatch, WikiProjectSettings
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import (
    MAX_CUSTOM_INSTRUCTIONS_CHARS,
    ProjectSettings,
    WizardSubmission,
)

SERVERS = {"schema-registry": {"url": "https://registry.example.com/mcp"}}


def _submission(**overrides) -> WizardSubmission:
    base = dict(
        repoUrl="https://github.com/bearlike/Assistant",
        slug="github.com/bearlike/Assistant",
        platform="github",
        depth="comprehensive",
        language="en",
        model="anthropic/claude-sonnet-4-6",
        filterMode="exclude",
        dirs=[],
        files=[],
    )
    base.update(overrides)
    return WizardSubmission(**base)


@pytest.fixture
def store(tmp_path):
    return JsonWikiStore(root_dir=tmp_path / "wiki")


# ── The four-hop round trip ───────────────────────────────────────────────


class TestRoundTrip:
    """Submission → record → submission, which is the path a refresh replays."""

    def test_embedding_model_survives_the_record(self) -> None:
        """Refresh must replay the project's vector model, not the server default."""
        replayed = ProjectSettings.from_submission(
            _submission(embeddingModel="openai/text-embedding-3-large")
        ).to_submission()
        assert replayed.embedding_model == "openai/text-embedding-3-large"

    def test_both_fields_survive_the_record(self) -> None:
        sub = _submission(customInstructions="Describe the Compose layer", mcpServers=SERVERS)
        replayed = ProjectSettings.from_submission(sub).to_submission()
        assert replayed.custom_instructions == "Describe the Compose layer"
        assert replayed.mcp_servers == SERVERS

    def test_unset_stays_unset_rather_than_becoming_empty(self) -> None:
        """``None`` and ``{}``/``""`` must not be collapsed on the way through.

        An empty value would read as "the operator cleared it" to a later
        reader; absence has to stay absence.
        """
        replayed = ProjectSettings.from_submission(_submission()).to_submission()
        assert replayed.custom_instructions is None
        assert replayed.mcp_servers is None

    def test_the_record_copies_the_server_map_rather_than_aliasing_it(self) -> None:
        """Mutating the submission after the fact must not reach the record."""
        servers = {"a": {"url": "https://a.example.com/mcp"}}
        sub = _submission(mcpServers=servers)
        settings = ProjectSettings.from_submission(sub)
        servers["b"] = {"url": "https://b.example.com/mcp"}
        assert settings.mcp_servers == {"a": {"url": "https://a.example.com/mcp"}}

    def test_a_desc_override_still_survives_alongside_the_new_fields(self) -> None:
        """The carried-forward ``desc`` contract is untouched by the additions."""
        settings = ProjectSettings.from_submission(
            _submission(customInstructions="guidance"), desc="hand-edited"
        )
        assert settings.desc == "hand-edited"
        assert settings.custom_instructions == "guidance"


# ── Validation, at every boundary that accepts these fields ───────────────


class TestValidation:
    """One rule per field, delegated to by all three models."""

    @pytest.mark.parametrize(
        "model_factory",
        [
            lambda **kw: _submission(**kw),
            lambda **kw: ProjectSettings(
                slug="github.com/o/r",
                platform="github",
                model="m",
                depth="concise",
                language="en",
                filterMode="exclude",
                **kw,
            ),
            lambda **kw: ProjectSettingsPatch.model_validate(kw),
        ],
        ids=["submission", "settings", "patch"],
    )
    def test_the_same_rules_hold_on_all_three_models(self, model_factory) -> None:
        """A value one hop accepts must be a value the next hop accepts.

        Three separate models validate this field — the wizard POST body, the
        durable record, and the PATCH body. If they disagreed, a value could be
        accepted at the wire and then rejected when the record was written,
        which surfaces as a 500 rather than a 400.
        """
        assert model_factory(customInstructions="  trimmed  ").custom_instructions == "trimmed"
        assert model_factory(customInstructions="   ").custom_instructions is None
        assert model_factory(mcpServers={}).mcp_servers is None
        assert model_factory(mcpServers=SERVERS).mcp_servers == SERVERS

        with pytest.raises(Exception):
            model_factory(customInstructions="x" * (MAX_CUSTOM_INSTRUCTIONS_CHARS + 1))
        with pytest.raises(Exception):
            model_factory(mcpServers={"a": "not-an-object"})
        with pytest.raises(Exception):
            model_factory(mcpServers={"  ": {}})

    def test_the_length_message_states_the_per_page_reason(self) -> None:
        """The cap is surprising without its reason, so the reason is in the message.

        An operator reading "over the 4000-character limit" has no way to know
        the cost is paid per PAGE rather than per index, which is the whole
        reason a generous-sounding cap exists at all.
        """
        with pytest.raises(Exception) as exc:
            _submission(customInstructions="x" * (MAX_CUSTOM_INSTRUCTIONS_CHARS + 1))
        assert "per page" in str(exc.value)


# ── The API surface ───────────────────────────────────────────────────────


class TestSettingsSurface:
    def test_both_fields_are_index_time_and_carry_a_wire_name(self) -> None:
        """A field missing from either map is invisible or mis-keyed on the wire.

        ``INDEX_TIME_FIELDS`` is what 410s them on a catalog project (which has
        no indexing pipeline to configure); ``WIRE_NAMES`` is what makes a field
        error land on the input the client actually rendered.
        """
        for field, wire in [
            ("custom_instructions", "customInstructions"),
            ("mcp_servers", "mcpServers"),
        ]:
            assert field in WikiProjectSettings.INDEX_TIME_FIELDS
            assert WikiProjectSettings._wire(field) == wire

    def test_omitted_and_explicit_null_are_different_requests(self) -> None:
        """The distinction ``ref``/``fallbackModels`` already carry.

        An absent key leaves what is stored alone; an explicit null clears it.
        Collapsing them would make "leave my servers attached" indistinguishable
        from "detach every server".
        """
        assert ProjectSettingsPatch.model_validate({}).changes() == {}
        cleared = ProjectSettingsPatch.model_validate(
            {"customInstructions": None, "mcpServers": None}
        ).changes()
        assert cleared == {"custom_instructions": None, "mcp_servers": None}

    def test_the_read_dto_reports_both_fields(self, store) -> None:
        """The console seeds its form from this payload; an absent key reads as unset."""
        from mewbo_graph.wiki.types import Project

        slug = "github.com/bearlike/Assistant"
        store.create_project(
            Project(
                slug=slug,
                source="github",
                lang="en",
                desc="",
                repoUrl="https://github.com/bearlike/Assistant",
                pages=1,
                indexedAt="2020-01-01T00:00:00Z",
            )
        )
        store.save_project_settings(
            slug,
            ProjectSettings.from_submission(
                _submission(customInstructions="be terse", mcpServers=SERVERS)
            ),
        )
        dto = WikiProjectSettings(store, developer_mode=lambda: False).read(slug)
        assert dto["customInstructions"] == "be terse"
        assert dto["mcpServers"] == ["schema-registry"]
        assert dto["editable"]["customInstructions"] is True
        assert dto["editable"]["mcpServers"] is True

    def test_the_read_dto_never_echoes_an_mcp_secret(self, store) -> None:
        """The GET is ``wiki.read``; the PATCH that sets this is ``wiki.admin``.

        A standard MCP entry carries credentials in its ``env`` block, so
        returning the stored value verbatim handed any reader a secret an admin
        set — a privilege inversion, and the opposite of the rule the
        ``credential`` field in this same DTO already follows.

        Asserted on the SERIALISED payload rather than the field, because the
        leak is "a secret reaches the wire", not "a particular key is shaped
        wrongly" — a future field that re-introduced the entry under another
        name would slip past a key-by-key check.
        """
        import json

        from mewbo_graph.wiki.types import Project

        slug = "github.com/bearlike/Assistant"
        secret = "ghp_thisMustNeverReachTheWire"
        store.create_project(
            Project(
                slug=slug,
                source="github",
                lang="en",
                desc="",
                repoUrl="https://github.com/bearlike/Assistant",
                pages=1,
                indexedAt="2020-01-01T00:00:00Z",
            )
        )
        store.save_project_settings(
            slug,
            ProjectSettings.from_submission(
                _submission(
                    mcpServers={
                        "gh": {"command": "npx", "env": {"GITHUB_TOKEN": secret}}
                    }
                )
            ),
        )
        dto = WikiProjectSettings(store, developer_mode=lambda: False).read(slug)
        assert secret not in json.dumps(dto)
        assert "GITHUB_TOKEN" not in json.dumps(dto)
        assert "npx" not in json.dumps(dto)
        # The NAME still reaches the client — that is what the form renders.
        assert dto["mcpServers"] == ["gh"]


# ── custom_instructions reaches the prompt ────────────────────────────────


class TestSkillInstructions:
    def test_no_guidance_leaves_the_playbook_byte_identical(self) -> None:
        """A project that sets nothing must get exactly what it got before.

        ``skill_instructions`` was single-valued, so taking that slot means a
        compose rule; this is the assertion that the rule is inert when there is
        nothing to compose.
        """
        assert _compose_skill_instructions(None) == _load_indexer_playbook()
        assert _compose_skill_instructions("") == _load_indexer_playbook()

    def test_guidance_is_appended_after_the_playbook_and_subordinated_to_it(self) -> None:
        """Order and framing are the guard against guidance overriding grounding.

        The text reaches the page-writer fan-out, so guidance that could switch
        off citation would produce exactly the ungrounded pages the playbook
        exists to prevent. It is appended (never prepended, never a
        replacement) and explicitly ranked below the playbook.
        """
        composed = _compose_skill_instructions("Prefer UI vocabulary.")
        playbook = _load_indexer_playbook()
        assert composed.startswith(playbook)
        assert composed.index("Prefer UI vocabulary.") > composed.index("GUIDANCE, not authority")
        assert "the playbook wins" in composed

    def test_the_rendered_user_query_is_untouched_by_either_field(self) -> None:
        """Guidance goes to ``skill_instructions``, NOT the submission block.

        Two reasons, and the second is why this is a test rather than a comment:
        the rendered query is a key-value parameter block, which is the wrong
        frame for operator intent — and it has golden render tests keyed on its
        exact string, so a field leaking into it would churn them.
        """
        plain = _render_user_query(_submission())
        loaded = _render_user_query(
            _submission(customInstructions="Prefer UI vocabulary.", mcpServers=SERVERS)
        )
        assert loaded == plain


# ── mcp_servers reaches the registry AND the allowlist ────────────────────


class TestAttachedMcpServers:
    """The app hands over CONFIG; core attaches it and admits its tool ids."""

    def test_the_servers_are_passed_as_config_not_a_resolved_tool_list(
        self, store
    ) -> None:
        """Discovery must not happen on the index-start request path.

        Resolving an attached server's tool ids means live MCP discovery — a
        network/subprocess cost. ``Orchestrator.__init__`` already runs on the
        background run thread, which is the offline side of the
        acceptance/execution boundary, so the config travels and the resolution
        happens there. A caller that resolved ids itself would be paying for
        discovery inside ``POST /v1/wiki/index``.

        The allowlist therefore stays exactly ``INDEXER_TOOLS`` here: widening it
        is core's job, at the seam that knows what discovery actually found.
        """
        runtime = MagicMock()
        runtime.wiki_store = store
        runtime.resolve_session.return_value = "sess-1"

        WikiIndexingJob.start(_submission(mcpServers=SERVERS), runtime=runtime)

        kwargs = runtime.start_async.call_args.kwargs
        assert kwargs["session_mcp_servers"] == SERVERS
        assert kwargs["allowed_tools"] == INDEXER_TOOLS
        assert kwargs["strict_tool_scope"] is True

    def test_a_project_attaching_nothing_passes_none(self, store) -> None:
        """The unchanged path — byte-identical to a run started before this."""
        runtime = MagicMock()
        runtime.wiki_store = store
        runtime.resolve_session.return_value = "sess-2"

        WikiIndexingJob.start(_submission(), runtime=runtime)

        kwargs = runtime.start_async.call_args.kwargs
        assert kwargs["session_mcp_servers"] is None
        assert kwargs["allowed_tools"] == INDEXER_TOOLS


class TestCoreAdmitsAttachedServers:
    """The core half: attached servers reach the registry AND the allowlist.

    Tested against ``Orchestrator`` directly rather than through a live run — the
    two facts worth pinning are what the registry is built WITH and which ids
    survive the allowlist, and both are readable without a model.
    """

    def _orchestrator(self, monkeypatch, *, servers, specs):
        import mewbo_core.loop.orchestrator as orch

        seen: dict = {}

        def _fake_registry(*, cwd, extra_mcp_servers=None):
            seen["cwd"] = cwd
            seen["extra"] = extra_mcp_servers
            registry = MagicMock()
            registry.list_specs.return_value = specs
            return registry

        monkeypatch.setattr(orch, "get_or_build_registry", _fake_registry)
        monkeypatch.setattr(orch, "create_session_store", lambda: MagicMock())
        instance = orch.Orchestrator.__new__(orch.Orchestrator)
        instance._session_mcp_servers = dict(servers or {})
        instance._tool_registry = _fake_registry(
            cwd=None, extra_mcp_servers=instance._session_mcp_servers or None
        )
        return instance, seen

    @staticmethod
    def _spec(tool_id: str, server: str):
        spec = MagicMock()
        spec.tool_id = tool_id
        spec.metadata = {"server": server}
        return spec

    def test_attached_server_tool_ids_are_admitted(self, monkeypatch) -> None:
        """Without this the tools are discovered and then filtered back out.

        ``filter_specs`` matches ids EXACTLY — no prefix, no wildcard — and drops
        an unrecognised id in SILENCE. A caller cannot name the ids itself
        because they are not knowable until discovery has run, so attaching the
        server IS the grant.
        """
        instance, _ = self._orchestrator(
            monkeypatch,
            servers=SERVERS,
            specs=[
                self._spec("mcp__schema_registry__lookup", "schema-registry"),
                self._spec("mcp__other__thing", "some-other-server"),
            ],
        )
        assert instance._session_mcp_tool_ids() == ["mcp__schema_registry__lookup"]

    def test_a_run_attaching_nothing_resolves_no_ids_and_walks_no_specs(
        self, monkeypatch
    ) -> None:
        """The short-circuit keeps ``allowed_tools`` byte-identical for every
        run that attaches nothing — which is nearly all of them."""
        instance, _ = self._orchestrator(monkeypatch, servers=None, specs=[])
        instance._tool_registry.list_specs.reset_mock()
        assert instance._session_mcp_tool_ids() == []
        instance._tool_registry.list_specs.assert_not_called()

    def test_plugin_servers_are_NOT_auto_admitted(self, monkeypatch) -> None:
        """The reason this parameter is not named ``extra_mcp_servers``.

        Both feed the same registry parameter, but they carry different grants: a
        plugin-contributed server's tools stay subject to ``allowed_tools`` like
        any other registry tool. Widening the existing parameter's meaning would
        have admitted every plugin server's tools into every scoped session in
        the deployment.
        """
        instance, _ = self._orchestrator(
            monkeypatch,
            servers={"attached": {"url": "https://a.example.com/mcp"}},
            specs=[
                self._spec("mcp__attached__go", "attached"),
                self._spec("mcp__plugin__go", "a-plugin-server"),
            ],
        )
        admitted = instance._session_mcp_tool_ids()
        assert admitted == ["mcp__attached__go"]
        assert "mcp__plugin__go" not in admitted

    def test_the_attached_servers_actually_reach_the_registry_build(
        self, monkeypatch, tmp_path
    ) -> None:
        """The other half — merged INTO the registry, not just read back out.

        The id-admission tests above construct the instance directly, so they
        would still pass if the constructor never merged the servers at all and
        the registry therefore held none of their tools. This drives the real
        ``__init__`` and reads what ``get_or_build_registry`` was handed.

        Plugin servers are merged too, and the attached ones win on a name
        collision — the caller naming a server for this specific run is the more
        specific intent.
        """
        import mewbo_core.loop.orchestrator as orch

        seen: dict = {}

        def _fake_registry(*, cwd=None, extra_mcp_servers=None):
            seen["extra"] = extra_mcp_servers
            registry = MagicMock()
            registry.list_specs.return_value = []
            return registry

        monkeypatch.setattr(orch, "get_or_build_registry", _fake_registry)
        monkeypatch.setattr(orch, "create_session_store", lambda: MagicMock())

        orch.Orchestrator(
            model_name="m",
            session_store=MagicMock(),
            cwd=str(tmp_path),
            session_mcp_servers=SERVERS,
        )
        assert seen["extra"] is not None
        for name, entry in SERVERS.items():
            assert seen["extra"][name] == entry

    def test_the_union_into_allowed_tools_is_what_the_filter_site_calls(
        self, monkeypatch
    ) -> None:
        """The rule has ONE home, and this is it.

        Dropping the union at the filter site is a mutation that left every
        other test in this file green — the id-resolution tests prove only that
        the ids can be COMPUTED, never that anything applies them. Naming the
        rule as a method is what makes the applying half assertable at all.
        """
        instance, _ = self._orchestrator(
            monkeypatch,
            servers=SERVERS,
            specs=[self._spec("mcp__schema_registry__lookup", "schema-registry")],
        )
        widened = instance._admit_attached_mcp_tools(["wiki_clone_repo"])
        assert widened == ["wiki_clone_repo", "mcp__schema_registry__lookup"]

    def test_an_empty_allowlist_still_admits_only_what_was_attached(
        self, monkeypatch
    ) -> None:
        """``[]`` grants NOTHING under the three-state contract, and attaching a
        server must widen it by exactly that server — never re-open the gate."""
        instance, _ = self._orchestrator(
            monkeypatch,
            servers=SERVERS,
            specs=[
                self._spec("mcp__schema_registry__lookup", "schema-registry"),
                self._spec("something_else", "unrelated"),
            ],
        )
        assert instance._admit_attached_mcp_tools([]) == ["mcp__schema_registry__lookup"]

    def test_nothing_attached_returns_the_allowlist_untouched(self, monkeypatch) -> None:
        """Byte-identical for every run that attaches nothing."""
        instance, _ = self._orchestrator(monkeypatch, servers=None, specs=[])
        original = ["wiki_clone_repo", "spawn_agent"]
        assert instance._admit_attached_mcp_tools(original) is original


class TestAttachedToolsSurviveTheRealFilterSite:
    """The integration half — the run body must APPLY the union, not just own it.

    Every other test here proves the ids can be computed and that the union
    method widens correctly. Removing the CALL at the filter site left all of
    them green: a rule with no caller is exactly as dead as no rule. This drives
    the real ``Orchestrator.run`` with the loop stubbed and reads the tool specs
    the loop was actually handed.
    """

    @staticmethod
    def _spec(tool_id: str, server: str):
        from mewbo_core.tooling.tool_registry import ToolSpec

        return ToolSpec(
            tool_id=tool_id,
            name=tool_id,
            description="attached",
            factory=lambda: MagicMock(),
            kind="mcp",
            metadata={"server": server},
        )

    def _run_capturing_specs(self, tmp_path, monkeypatch, *, servers):
        import mewbo_core.loop.orchestrator as orch
        from mewbo_core.classes import OrchestrationState, TaskQueue
        from mewbo_core.loop.tool_use_loop import ToolUseLoop
        from mewbo_core.session.session_store import SessionStore

        registry = MagicMock()
        registry.list_specs.return_value = [
            self._spec("mcp__schema_registry__lookup", "schema-registry"),
            self._spec("mcp__unrelated__thing", "some-other-server"),
        ]
        monkeypatch.setattr(
            orch, "get_or_build_registry", lambda **_kw: registry
        )

        store = SessionStore(root_dir=str(tmp_path))
        session_id = store.create_session()
        orchestrator = orch.Orchestrator(
            session_store=store, session_mcp_servers=servers
        )

        captured: list = []

        async def _capturing_run(_self, *args, tool_specs=None, **kwargs):
            if tool_specs is not None:
                captured.extend(tool_specs)
            tq = TaskQueue(action_steps=[])
            tq.task_result = "Done"
            state = OrchestrationState(goal="t", session_id=session_id)
            state.done = True
            state.done_reason = "completed"
            return tq, state

        with patch.object(ToolUseLoop, "run", _capturing_run):
            orchestrator.run(
                user_query="index it",
                session_id=session_id,
                allowed_tools=["wiki_clone_repo"],
                strict_tool_scope=True,
                max_iters=1,
            )
        return {s.tool_id for s in captured}

    def test_an_attached_servers_tool_is_bound_under_strict_scope(
        self, tmp_path, monkeypatch
    ) -> None:
        """The whole point: strict scope + an allowlist that never names the id.

        ``INDEXER_TOOLS`` is written long before anyone knows what an operator
        will attach, so the id reaches the loop only if the run body widens the
        allowlist itself.
        """
        bound = self._run_capturing_specs(tmp_path, monkeypatch, servers=SERVERS)
        assert "mcp__schema_registry__lookup" in bound

    def test_a_server_that_was_NOT_attached_stays_filtered_out(
        self, tmp_path, monkeypatch
    ) -> None:
        """The union admits what THIS run attached, never the registry at large.

        Without this the test above would also pass on a mutation that simply
        stopped filtering — which would be a far worse defect than the one the
        union fixes.
        """
        bound = self._run_capturing_specs(tmp_path, monkeypatch, servers=SERVERS)
        assert "mcp__unrelated__thing" not in bound

    def test_attaching_nothing_binds_neither(self, tmp_path, monkeypatch) -> None:
        """Strict scope stays strict for a run that attached no servers."""
        bound = self._run_capturing_specs(tmp_path, monkeypatch, servers=None)
        assert "mcp__schema_registry__lookup" not in bound
        assert "mcp__unrelated__thing" not in bound
