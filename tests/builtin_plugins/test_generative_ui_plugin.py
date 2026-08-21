"""The bundled ``generative-ui`` plugin — manifest, gating, and the tool itself.

Deliberately integration-shaped against the REAL plugin tree, like
``test_widget_builder_plugin.py``: the discovery pipeline (manifest parse →
``load_entry`` → capability gate → instantiation) is most of what can break
here, and none of it is exercised by constructing the tool class directly.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
from pathlib import Path

import pytest
from mewbo_core.builtin_plugins.generative_ui.nodes import GenerativeUISpec
from mewbo_core.builtin_plugins.generative_ui.present_ui import (
    GENERATIVE_UI_CAPABILITY,
    GENERATIVE_UI_EVENT,
    PRESENT_UI_TOOL_ID,
    GenerativeUIPayload,
    PresentUiArgs,
    PresentUiTool,
)
from mewbo_core.classes import ActionStep
from mewbo_core.tooling.session_tools import (
    DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS,
    SessionToolFactory,
    SessionToolRegistry,
)
from pydantic import ValidationError

CAPS = (GENERATIVE_UI_CAPABILITY,)

SIMPLE_TREE = {
    "root": [
        {"component": "Heading", "value": "Status"},
        {"component": "Badge", "label": "green", "status": "success"},
    ]
}


def _plugin_root() -> Path:
    traversable = (
        importlib.resources.files("mewbo_core") / "builtin_plugins" / "generative_ui"
    )
    return Path(str(traversable))


def _manifest_entry() -> dict:
    raw = json.loads(
        (_plugin_root() / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    return raw["session_tools"][0]


def _registry() -> SessionToolRegistry:
    """A registry loaded exactly the way the orchestrator loads this plugin."""
    reg = SessionToolRegistry()
    reg.load_entry(_manifest_entry(), requires_capabilities=CAPS)
    return reg


def _step(**tool_input) -> ActionStep:
    return ActionStep(
        tool_id=PRESENT_UI_TOOL_ID, operation="run", tool_input=tool_input
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_parses_with_the_generative_ui_capability(self):
        from mewbo_core.tooling.plugins import parse_plugin_manifest

        manifest = parse_plugin_manifest(_plugin_root())
        assert manifest is not None
        assert manifest.name == "generative-ui"
        assert manifest.requires_capabilities == (GENERATIVE_UI_CAPABILITY,)

    def test_manifest_declares_present_ui_as_a_default_on_session_tool(self):
        entry = _manifest_entry()
        assert entry["tool_id"] == PRESENT_UI_TOOL_ID
        assert entry["module"].endswith("generative_ui.present_ui")
        assert entry["class"] == "PresentUiTool"
        # Load-bearing: without it the capability gate is structurally
        # unreachable for a console session (any ``mcp_tools`` list, ``[]``
        # included, caps that gate) and the tool never reaches a root agent.
        assert entry["unconditional"] is True

    def test_the_plugin_contributes_a_skill_but_no_agentdef(self):
        """Capability gating has two enforcement surfaces, and this bundle now
        uses both.

        The catalog surface (``filter_by_capabilities`` over AgentDefs and
        skills) used to be vacuous here because the bundle shipped neither. It
        ships a skill now, so that surface is live: the skill inherits the
        manifest's ``generative_ui`` gate and stays out of the catalogue on a
        surface that can render nothing. There is still no AgentDef, because
        ``present_ui`` is called by the root rather than delegated — that is
        the distinction from the widget bundle, which ships one."""
        assert not (_plugin_root() / "agents").exists()
        skills = sorted(p.name for p in (_plugin_root() / "skills").iterdir())
        assert skills == ["generative-ui"]
        assert (_plugin_root() / "skills/generative-ui/SKILL.md").is_file()

    def test_the_plugin_is_discovered_from_the_shipped_builtin_root(self):
        from mewbo_core.tooling.plugins import discover_builtin_plugins

        root = Path(str(importlib.resources.files("mewbo_core") / "builtin_plugins"))
        names = [
            pc.manifest.name for pc in discover_builtin_plugins(root) if pc.manifest
        ]
        assert "generative-ui" in names


# ---------------------------------------------------------------------------
# Capability gating — the session-tool build surface
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    """Six scenarios covering every route a session can take to this tool.

    The pair that matters most sits at the top: a console/Aura root ALWAYS
    sends some ``context.mcp_tools`` (``useSessions`` creates with ``[]``), so
    it is a permissive session with an explicit allowlist — and the capability
    gate alone can never fire for it.
    """

    def test_permissive_session_with_the_capability_gets_the_tool(self):
        ids = _registry().ids_for(
            [], session_capabilities=CAPS, strict_tool_scope=False
        )
        assert ids == [PRESENT_UI_TOOL_ID]

    def test_permissive_session_without_the_capability_does_not(self):
        assert _registry().ids_for([], session_capabilities=(), strict_tool_scope=False) == []

    def test_unscoped_session_without_the_capability_does_not(self):
        """The headless drives (wiki indexing, triggers, channels) reach here.

        They can render nothing, so binding eleven component schemas on every
        one of their LLM calls would be pure cost. This is what the capability
        buys once ``unconditional`` has relaxed the allowlist ceiling.
        """
        assert _registry().ids_for(None, session_capabilities=()) == []

    def test_unscoped_session_with_the_capability_gets_the_tool(self):
        assert _registry().ids_for(None, session_capabilities=CAPS) == [PRESENT_UI_TOOL_ID]

    def test_strict_scope_that_omits_the_tool_does_not_get_it(self):
        """An authoritative AgentDef ``tools:`` is the last word, capability or not."""
        ids = _registry().ids_for(
            ["shell"], session_capabilities=CAPS, strict_tool_scope=True
        )
        assert ids == []

    def test_strict_scope_that_names_the_tool_gets_it(self):
        ids = _registry().ids_for(
            [PRESENT_UI_TOOL_ID], session_capabilities=CAPS, strict_tool_scope=True
        )
        assert ids == [PRESENT_UI_TOOL_ID]

    def test_read_only_delegation_still_drops_it(self):
        """A session tool is an action; ``read_only`` admits none by default."""
        ids = _registry().ids_for(
            None, session_capabilities=CAPS, capability_mode="read_only"
        )
        assert ids == []

    def test_build_for_agrees_with_ids_for(self):
        """The catalog an operator reads and the tools an agent holds resolve
        through one algorithm — a drift between them is a lie in the operator's
        variable reference."""
        reg = _registry()
        for allowed, caps, strict in [
            ([], CAPS, False),
            ([], (), False),
            (None, CAPS, False),
            (None, (), False),
            ([PRESENT_UI_TOOL_ID], CAPS, True),
            (["shell"], CAPS, True),
        ]:
            built = reg.build_for(
                allowed,
                session_id="s1",
                event_logger=None,
                session_capabilities=caps,
                strict_tool_scope=strict,
            )
            named = reg.ids_for(
                allowed, session_capabilities=caps, strict_tool_scope=strict
            )
            assert [t.tool_id for t in built] == named

    def test_naming_the_tool_unlocks_its_capability(self):
        """``capabilities_for`` is the read-side mirror: selecting the tool by id
        grants the capability that gates it for that request."""
        assert _registry().capabilities_for([PRESENT_UI_TOOL_ID]) == CAPS

    def test_a_capability_free_unconditional_factory_is_unaffected(self):
        """``schedule_trigger``'s shape — unconditional with NO capabilities —
        must stay byte-identical now that the two flags compose."""
        reg = SessionToolRegistry()
        reg.register(
            SessionToolFactory(
                tool_id="schedule_trigger", build=lambda *_: object(), unconditional=True
            )
        )
        assert reg.ids_for(None, session_capabilities=()) == ["schedule_trigger"]
        assert reg.ids_for([], session_capabilities=(), strict_tool_scope=False) == [
            "schedule_trigger"
        ]
        assert reg.ids_for(["x"], session_capabilities=(), strict_tool_scope=True) == []


# ---------------------------------------------------------------------------
# The tool contract
# ---------------------------------------------------------------------------


class TestToolContract:
    def test_load_entry_builds_the_real_tool_class(self):
        tools = _registry().build_for(
            [PRESENT_UI_TOOL_ID], session_id="s1", event_logger=None
        )
        assert len(tools) == 1
        tool = tools[0]
        assert isinstance(tool, PresentUiTool)
        assert tool.tool_id == PRESENT_UI_TOOL_ID
        assert tool.schema["function"]["name"] == PRESENT_UI_TOOL_ID
        assert tool.modes == frozenset({"act"})

    def test_presenting_never_terminates_the_run(self):
        """Verbatim replica of the loop's terminal poll.

        ``SessionTool`` is a structural Protocol, so a tool that omits
        ``terminal_reason`` crashes this poll with an ``AttributeError`` AFTER
        its side effect has already landed — how ``submit_widget`` broke every
        call it served. The panel renders off the event, so this tool never
        terminates and still defines the method.
        """
        tool = PresentUiTool(session_id="s1", event_logger=None)
        terminating = [t for t in [tool] if t.should_terminate_run()]
        done_reason = terminating[0].terminal_reason() if terminating else "completed"
        assert terminating == []
        assert done_reason == "completed"
        assert tool.terminal_reason() == "awaiting_approval"

    def test_result_cap_is_declared_not_inherited(self):
        """A session tool has no ``ToolSpec``, so an undeclared cap silently
        falls through to the registry's 2000 — which would truncate the
        validation error the model needs in order to fix its tree."""
        tool = PresentUiTool(session_id="s1", event_logger=None)
        declared = getattr(tool, "max_result_chars", None)
        assert isinstance(declared, int) and not isinstance(declared, bool)
        assert declared == 12_000
        assert declared != DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS


# ---------------------------------------------------------------------------
# handle() — the event it emits
# ---------------------------------------------------------------------------


class TestHandle:
    FROZEN_KEYS = {"ui_id", "session_id", "spec", "alt_text", "summary"}

    def test_a_successful_call_emits_one_frozen_generative_ui_event(self):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(
            tool.handle(_step(**SIMPLE_TREE, summary="build status"))
        )

        assert len(events) == 1
        assert events[0]["type"] == GENERATIVE_UI_EVENT
        payload = events[0]["payload"]
        assert set(payload) == self.FROZEN_KEYS
        assert payload["session_id"] == "s1"
        assert payload["summary"] == "build status"
        assert payload["spec"] == GenerativeUISpec.model_validate(SIMPLE_TREE).to_wire()
        assert payload["ui_id"] in result.content

    def test_alt_text_is_computed_server_side_from_the_tree(self):
        """Every non-visual client renders this, so it must be derived from the
        same tree the console gets — never a second thing the model authored."""
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        asyncio.run(tool.handle(_step(**SIMPLE_TREE, summary="s")))
        assert events[0]["payload"]["alt_text"] == (
            GenerativeUISpec.model_validate(SIMPLE_TREE).to_text()
        )
        # Default heading level is 2, which renders one rank below the page title.
        assert events[0]["payload"]["alt_text"] == "### Status\n[green]"

    def test_the_result_is_a_receipt_not_an_echo(self):
        """The model just authored the tree; handing it back would spend context
        on what it already knows. What the receipt DOES carry is the panel's
        measured state and the composition affordances — the facts the next
        call needs and the model does not otherwise have."""
        tool = PresentUiTool(session_id="s1", event_logger=None)
        result = asyncio.run(tool.handle(_step(**SIMPLE_TREE, summary="s")))
        assert "Status" not in result.content
        assert len(result.content) < 500
        assert "2 nodes" in result.content
        assert "depth 1" in result.content
        assert "append" in result.content

    def test_a_minted_ui_id_matches_the_frozen_pattern(self):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        asyncio.run(tool.handle(_step(**SIMPLE_TREE, summary="s")))
        ui_id = events[0]["payload"]["ui_id"]
        assert len(ui_id) == 12
        assert ui_id.startswith("gui-")
        assert all(c in "0123456789abcdef" for c in ui_id[4:])

    def test_two_calls_mint_distinct_ids(self):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        asyncio.run(tool.handle(_step(**SIMPLE_TREE, summary="s")))
        asyncio.run(tool.handle(_step(**SIMPLE_TREE, summary="s")))
        assert events[0]["payload"]["ui_id"] != events[1]["payload"]["ui_id"]

    def test_a_supplied_ui_id_is_reused_so_the_panel_upserts(self):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        asyncio.run(
            tool.handle(_step(**SIMPLE_TREE, summary="s", ui_id="gui-0123abcd"))
        )
        assert events[0]["payload"]["ui_id"] == "gui-0123abcd"

    @pytest.mark.parametrize(
        "ui_id", ["0123abcd", "ab", "has space", "-leading-dash", "x" * 65]
    )
    def test_a_malformed_ui_id_is_refused(self, ui_id):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(
            tool.handle(_step(**SIMPLE_TREE, summary="s", ui_id=ui_id))
        )
        assert result.content.startswith("ERROR: invalid present_ui args")
        assert events == []

    @pytest.mark.parametrize(
        # The ids real models AUTHORED and were refused for under the old
        # ``gui-`` hex pattern — the pattern never prevented collision (models
        # fabricated matching hex), it only prevented readable ids.
        "ui_id",
        ["vscode-cheatsheet", "gui-team-dir", "gui-demo-001", "team-directory"],
    )
    def test_a_model_authored_readable_ui_id_is_accepted(self, ui_id):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(
            tool.handle(_step(**SIMPLE_TREE, summary="s", ui_id=ui_id))
        )
        assert events[0]["payload"]["ui_id"] == ui_id
        assert ui_id in result.content

    @pytest.mark.parametrize(
        "bad_input",
        [
            {},
            {"summary": "s"},
            SIMPLE_TREE,
            {**SIMPLE_TREE, "summary": ""},
            {"root": [], "summary": "s"},
            {**SIMPLE_TREE, "summary": "s", "theme": "dark"},
            {"root": [{"component": "Nope"}], "summary": "s"},
            # The wrapper that is GONE. A caller still sending the old shape
            # must be REFUSED, not silently accepted through some leftover
            # tolerance — `extra="forbid"` is what makes the removal real.
            {"spec": SIMPLE_TREE, "summary": "s"},
        ],
    )
    def test_invalid_args_return_a_correctable_error_and_emit_nothing(self, bad_input):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(tool.handle(_step(**bad_input)))
        assert result.content.startswith("ERROR: invalid present_ui args")
        assert events == []

    def test_a_pathologically_deep_tree_is_refused_not_crashed(self):
        """The tree is recursive, so ``handle`` catching only ``ValidationError``
        rests on pydantic-core's own recursion guard reporting a stack overflow
        AS a validation error. Pin that rather than assume it — if it ever
        changes, this arm stops covering the input it was reasoned about."""
        node: dict[str, object] = {"component": "Text", "value": "leaf"}
        for _ in range(3_000):
            node = {"component": "Stack", "children": [node]}

        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(tool.handle(_step(root=[node], summary="deep")))
        assert result.content.startswith("ERROR: invalid present_ui args")
        assert events == []

    def test_a_non_dict_tool_input_is_handled(self):
        tool = PresentUiTool(session_id="s1", event_logger=None)
        step = ActionStep(
            tool_id=PRESENT_UI_TOOL_ID, operation="run", tool_input="not a dict"
        )
        result = asyncio.run(tool.handle(step))
        assert result.content.startswith("ERROR: invalid present_ui args")

    def test_a_failing_event_logger_never_breaks_the_call(self):
        def boom(_event):
            raise RuntimeError("store is down")

        tool = PresentUiTool(session_id="s1", event_logger=boom)
        result = asyncio.run(tool.handle(_step(**SIMPLE_TREE, summary="s")))
        assert result.content.startswith("Presented UI gui-")

    def test_no_event_logger_degrades_silently(self):
        tool = PresentUiTool(session_id="s1", event_logger=None)
        result = asyncio.run(tool.handle(_step(**SIMPLE_TREE, summary="s")))
        assert result.content.startswith("Presented UI gui-")


# ---------------------------------------------------------------------------
# The payload's own contract
# ---------------------------------------------------------------------------


class TestPayload:
    def _valid(self, **overrides):
        base = dict(
            ui_id="gui-0123abcd",
            session_id="s1",
            spec={"root": []},
            alt_text="x",
            summary="y",
        )
        base.update(overrides)
        return base

    def test_round_trip_matches_the_frozen_wire_shape(self):
        dumped = GenerativeUIPayload(**self._valid()).model_dump()
        assert set(dumped) == {"ui_id", "session_id", "spec", "alt_text", "summary"}

    def test_rejects_an_extra_key(self):
        with pytest.raises(ValidationError):
            GenerativeUIPayload(**self._valid(), rendered=True)

    @pytest.mark.parametrize(
        "spec",
        [{}, {"nodes": []}, {"root": {}}, {"root": [], "theme": "dark"}],
    )
    def test_rejects_a_spec_that_is_not_the_frozen_shape(self, spec):
        with pytest.raises(ValidationError):
            GenerativeUIPayload(**self._valid(spec=spec))

    def test_rejects_a_malformed_ui_id(self):
        with pytest.raises(ValidationError):
            GenerativeUIPayload(**self._valid(ui_id="1-starts-with-digit"))


class TestArgs:
    def test_summary_is_required_and_capped(self):
        with pytest.raises(ValidationError):
            PresentUiArgs(**SIMPLE_TREE, summary="x" * 201)
        assert PresentUiArgs(**SIMPLE_TREE, summary=" x " * 1).summary == "x"


# ---------------------------------------------------------------------------
# The regression corpus: payloads two real small models actually sent
# ---------------------------------------------------------------------------


# Verbatim shapes lifted from two traced sessions on two unrelated small models
# (a 26B MoE and a 9B), both of which had already retrieved the FULL untruncated
# tool schema. Held here rather than described in prose because a rejection is
# only useful if it is useful against what models really send — a hand-invented
# bad payload is a guess about the failure, and these are the failure.
#
# 13 calls, 8 rejected. Neither model repeated a byte-identical payload, so the
# doom-loop guard (identical input AND identical result) could never fire: each
# retry varied the guess. That is why the CORRECTION has to arrive with the
# rejection — nothing upstream was going to stop the loop.
REAL_REJECTED_PAYLOADS = {
    # Both models reached for `$defs` KEY names after failing to dereference
    # `$ref`. One of them searched `tool_search select:AlertNode` first.
    "defs_class_name_as_key": {
        "AlertNode": {"body": "x", "title": "t", "variant": "info"},
        "summary": "s",
    },
    "root_as_an_object": {
        "root": {"body": "x", "component": "Alert", "variant": "info"},
        "summary": "s",
    },
    "root_as_an_object_with_children": {
        "root": {"children": [{"component": "Text", "value": "x"}]},
        "summary": "s",
    },
    "a_node_nested_inside_a_leaf": {
        "root": [{"component": "Alert", "body": "x", "badge": {"label": "T"}}],
        "summary": "s",
    },
    "node_fields_spread_onto_the_wrapper": {
        "component": "Stack",
        "gap": "lg",
        "root": [{"component": "Text", "value": "x"}],
        "summary": "s",
    },
    # A nested node with no resolvable `component`, which is what the
    # discriminator reports as `union_tag_not_found` — distinct from naming a
    # tag that does not exist, and the arm a model reaches by copying a sibling
    # and dropping the one field that identifies it.
    "a_child_node_with_no_component_key": {
        "root": [{"component": "Card", "children": [{"title": "Alert Variants"}]}],
        "summary": "s",
    },
}


class TestRealModelFailures:
    """Every shape a traced model sent is still refused, and refused USEFULLY.

    Two properties, and the second is the one that was missing. Refusal alone
    was already true and did not help: a model handed only a pydantic path
    infers the contract from a sequence of refusals, which one of these sessions
    did out loud and got WRONG — it announced a shape it had already disproved
    and sent it. So the correct shape has to travel with the refusal.
    """

    @pytest.mark.parametrize("name", sorted(REAL_REJECTED_PAYLOADS))
    def test_the_shape_is_still_refused_and_emits_nothing(self, name):
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        result = asyncio.run(tool.handle(_step(**REAL_REJECTED_PAYLOADS[name])))
        assert result.content.startswith("ERROR: invalid present_ui args")
        assert events == []

    @pytest.mark.parametrize("name", sorted(REAL_REJECTED_PAYLOADS))
    def test_the_refusal_carries_the_correct_shape_and_the_vocabulary(self, name):
        tool = PresentUiTool(session_id="s1", event_logger=None)
        content = asyncio.run(tool.handle(_step(**REAL_REJECTED_PAYLOADS[name]))).content
        # The canonical call, so the caller need not derive it.
        assert '{"root": [{"component": "Alert", "body": "..."}]' in content
        # And every component, so picking the next one needs no second attempt.
        assert content.endswith(GenerativeUISpec.component_guide())
        assert len(content) <= PresentUiTool.max_result_chars, (
            "The rejection is the one large result this tool produces; if it "
            "exceeds the declared cap the correction is truncated mid-sentence, "
            "which is the failure the cap was sized to prevent."
        )

    def test_a_rejection_never_teaches_a_python_class_name(self):
        """The names a rejection offers must be the names validation accepts.

        ``AlertNode`` is what the schema's ``$defs`` used to be keyed by, and it
        is precisely what one traced model copied into a payload. A correction
        that reintroduces it would teach the original mistake.
        """
        tool = PresentUiTool(session_id="s1", event_logger=None)
        content = asyncio.run(tool.handle(_step(root="nonsense", summary="s"))).content
        assert "- Alert:" in content
        assert "AlertNode" not in content


# ---------------------------------------------------------------------------
# The rejection closes the gap: every error, and the offending node rewritten
# ---------------------------------------------------------------------------


class TestRejectionShowsTheCorrectedNode:
    def _content(self, payload: dict) -> str:
        tool = PresentUiTool(session_id="s1", event_logger=None)
        return asyncio.run(tool.handle(_step(**payload))).content

    def test_every_error_is_reported_with_its_path_not_just_the_first(self):
        content = self._content(
            {
                "summary": "s",
                "root": [
                    {"component": "Text", "valu": "typo"},
                    {"component": "Badge"},
                ],
            }
        )
        assert "Every problem, not just the first" in content
        # Both nodes' failures surface together, each with a path a model can
        # follow into its own payload.
        assert "root[0]" in content
        assert "root[1]" in content
        assert "label" in content  # Badge's missing required field, named

    def test_the_offending_node_is_rewritten_to_the_declared_shape(self):
        content = self._content(
            {
                "summary": "s",
                "root": [
                    {
                        "component": "KeyValue",
                        "items": [["only-one-element"]],
                        "invented": True,
                    }
                ],
            }
        )
        assert "Your node at root[0], rewritten to the declared shape" in content
        assert "unknown key(s) invented dropped" in content
        # The corrected node is real JSON in the declared shape.
        assert '{"component": "KeyValue", "items": [{"label": "...", "value": "..."}]}' in content

    def test_a_correct_field_survives_the_rewrite_verbatim(self):
        """The model's OWN values are kept wherever they validate — the rewrite
        fixes the one wrong thing rather than blanking the node."""
        content = self._content(
            {
                "summary": "s",
                "root": [{"component": "Alert", "body": "Deploy is blocked.", "variant": "nope"}],
            }
        )
        assert '"body": "Deploy is blocked."' in content
        assert '"variant": "info"' in content  # skeleton = first legal literal


# ---------------------------------------------------------------------------
# Observed near-miss aliases, end to end through handle()
# ---------------------------------------------------------------------------


class TestObservedNearMissAliases:
    """Each alias is justified by a rejected call ON RECORD, never invented.

    The tree stays strict everywhere else — TestRealModelFailures above pins
    that the genuinely-wrong shapes are still refused.
    """

    def _emit(self, payload: dict) -> tuple[str, list[dict]]:
        events: list[dict] = []
        tool = PresentUiTool(session_id="s1", event_logger=events.append)
        content = asyncio.run(tool.handle(_step(**payload))).content
        return content, events

    def test_text_text_lands_as_value(self):
        content, events = self._emit(
            {"summary": "s", "root": [{"component": "Text", "text": "hello"}]}
        )
        assert not content.startswith("ERROR"), content
        assert events[0]["payload"]["spec"]["root"][0]["props"]["value"] == "hello"

    def test_table_singular_row_column_land_as_plurals(self):
        content, events = self._emit(
            {
                "summary": "s",
                "root": [{"component": "Table", "column": ["A"], "row": [["1"]]}],
            }
        )
        assert not content.startswith("ERROR"), content
        props = events[0]["payload"]["spec"]["root"][0]["props"]
        assert props["columns"] == ["A"]
        assert props["rows"] == [["1"]]

    def test_a_two_element_list_lands_as_a_keyvalue_item(self):
        """The observed `[["Server Load", "34%"]]` shape — order is display
        order, so the pair carries exactly the declared content."""
        content, events = self._emit(
            {
                "summary": "s",
                "root": [{"component": "KeyValue", "items": [["Server Load", "34%"]]}],
            }
        )
        assert not content.startswith("ERROR"), content
        items = events[0]["payload"]["spec"]["root"][0]["props"]["items"]
        assert items == [{"label": "Server Load", "value": "34%"}]

    def test_a_number_lands_as_a_cell_string(self):
        content, events = self._emit(
            {
                "summary": "s",
                "root": [{"component": "Table", "columns": ["load"], "rows": [[34.5]]}],
            }
        )
        assert not content.startswith("ERROR"), content
        assert events[0]["payload"]["spec"]["root"][0]["props"]["rows"] == [["34.5"]]

    def test_a_boolean_cell_is_still_refused(self):
        """`bool` is an `int` subclass; rendering it as "True" would be an
        invented cell, not a recovery — the coercion must not admit it."""
        content, events = self._emit(
            {
                "summary": "s",
                "root": [{"component": "Table", "columns": ["ok"], "rows": [[True]]}],
            }
        )
        assert content.startswith("ERROR")
        assert events == []

    def test_both_spellings_at_once_are_still_refused(self):
        """An alias fires only when the canonical key is ABSENT — a call
        carrying both must fail extra="forbid", never have one silently win."""
        content, events = self._emit(
            {
                "summary": "s",
                "root": [{"component": "Text", "text": "a", "value": "b"}],
            }
        )
        assert content.startswith("ERROR")
        assert "text" in content
        assert events == []


# ---------------------------------------------------------------------------
# Incremental composition: append / update on ONE panel
# ---------------------------------------------------------------------------


SKELETON = {
    "summary": "team directory",
    "ui_id": "team-directory",
    "root": [
        {"component": "Heading", "value": "Team"},
        {"component": "Card", "id": "members", "title": "Members", "children": []},
    ],
}


class TestIncrementalComposition:
    def _tool(self) -> tuple[PresentUiTool, list[dict]]:
        events: list[dict] = []
        return PresentUiTool(session_id="s1", event_logger=events.append), events

    def _call(self, tool: PresentUiTool, payload: dict) -> str:
        return asyncio.run(tool.handle(_step(**payload))).content

    def test_append_into_a_named_container_emits_the_full_merged_tree(self):
        """The wire contract is UNCHANGED: every event carries the complete
        `{"root": [...]}` tree, so replay and both timeline builders see an
        ordinary replace-by-ui_id event and `to_text` degrades the whole
        panel. Only the model's per-call payload got small."""
        tool, events = self._tool()
        self._call(tool, SKELETON)
        content = self._call(
            tool,
            {
                "summary": "team directory",
                "ui_id": "team-directory",
                "operation": "append",
                "target": "members",
                "root": [{"component": "Badge", "label": "Alice"}],
            },
        )
        assert not content.startswith("ERROR"), content
        assert len(events) == 2
        merged = events[1]["payload"]["spec"]["root"]
        assert merged[1]["children"][0]["props"]["label"] == "Alice"
        # alt_text is the WHOLE merged panel, not the delta.
        assert "Team" in events[1]["payload"]["alt_text"]
        assert "[Alice]" in events[1]["payload"]["alt_text"]

    def test_append_without_a_target_lands_after_the_roots(self):
        tool, events = self._tool()
        self._call(tool, SKELETON)
        self._call(
            tool,
            {
                "summary": "team directory",
                "ui_id": "team-directory",
                "operation": "append",
                "root": [{"component": "Divider"}],
            },
        )
        assert events[1]["payload"]["spec"]["root"][2]["component"] == "Divider"

    def test_update_replaces_the_addressed_container(self):
        tool, events = self._tool()
        self._call(tool, SKELETON)
        self._call(
            tool,
            {
                "summary": "team directory",
                "ui_id": "team-directory",
                "operation": "update",
                "target": "members",
                "root": [
                    {
                        "component": "Stack",
                        "id": "members",
                        "children": [{"component": "Badge", "label": "Bob"}],
                    }
                ],
            },
        )
        merged = events[1]["payload"]["spec"]["root"]
        assert merged[1]["component"] == "Stack"
        assert merged[1]["children"][0]["props"]["label"] == "Bob"

    def test_the_receipt_reports_size_depth_and_addressable_containers(self):
        tool, _ = self._tool()
        content = self._call(tool, SKELETON)
        assert "2 nodes" in content
        assert "depth 1" in content
        assert "Addressable containers: members." in content
        assert 'operation="append"' in content

    def test_append_to_an_unknown_panel_refuses_and_teaches_replace(self):
        tool, events = self._tool()
        content = self._call(
            tool,
            {
                "summary": "s",
                "ui_id": "ghost-panel",
                "operation": "append",
                "root": [{"component": "Divider"}],
            },
        )
        assert content.startswith("ERROR")
        assert 'operation="replace"' in content
        assert events == []

    def test_append_to_an_unknown_target_names_the_addressable_ids(self):
        tool, events = self._tool()
        self._call(tool, SKELETON)
        content = self._call(
            tool,
            {
                "summary": "s",
                "ui_id": "team-directory",
                "operation": "append",
                "target": "nope",
                "root": [{"component": "Divider"}],
            },
        )
        assert content.startswith("ERROR")
        assert "members" in content
        assert "unchanged" in content
        assert len(events) == 1  # nothing new emitted

    def test_update_demands_exactly_one_node_and_a_target(self):
        tool, _ = self._tool()
        self._call(tool, SKELETON)
        two = self._call(
            tool,
            {
                "summary": "s",
                "ui_id": "team-directory",
                "operation": "update",
                "target": "members",
                "root": [{"component": "Divider"}, {"component": "Divider"}],
            },
        )
        assert two.startswith("ERROR") and "exactly ONE node" in two
        untargeted = self._call(
            tool,
            {
                "summary": "s",
                "ui_id": "team-directory",
                "operation": "update",
                "root": [{"component": "Divider"}],
            },
        )
        assert untargeted.startswith("ERROR") and "target" in untargeted

    def test_append_without_a_ui_id_is_refused(self):
        tool, events = self._tool()
        content = self._call(
            tool,
            {"summary": "s", "operation": "append", "root": [{"component": "Divider"}]},
        )
        assert content.startswith("ERROR")
        assert "ui_id" in content
        assert events == []

    def test_a_duplicate_container_id_is_refused(self):
        """Addressing must be unambiguous — a duplicated id would land an
        append on whichever copy the walk meets first."""
        tool, events = self._tool()
        content = self._call(
            tool,
            {
                "summary": "s",
                "root": [
                    {"component": "Card", "id": "twin", "children": []},
                    {"component": "Stack", "id": "twin", "children": []},
                ],
            },
        )
        assert content.startswith("ERROR")
        assert "twin" in content
        assert events == []

    def test_a_merge_that_breaks_a_tree_limit_leaves_the_panel_unchanged(self):
        from mewbo_core.builtin_plugins.generative_ui.nodes import MAX_TREE_DEPTH

        deep: dict = {"component": "Stack", "id": "lvl0", "children": []}
        cursor = deep
        for level in range(1, MAX_TREE_DEPTH - 1):
            child: dict = {"component": "Stack", "id": f"lvl{level}", "children": []}
            cursor["children"].append(child)
            cursor = child
        tool, events = self._tool()
        self._call(tool, {"summary": "s", "ui_id": "deep-panel", "root": [deep]})
        content = self._call(
            tool,
            {
                "summary": "s",
                "ui_id": "deep-panel",
                "operation": "append",
                "target": f"lvl{MAX_TREE_DEPTH - 2}",
                "root": [{"component": "Card", "children": [{"component": "Divider"}]}],
            },
        )
        assert content.startswith("ERROR") and "unchanged" in content
        assert len(events) == 1
        # The panel state really is unchanged: a legal append still lands.
        follow_up = self._call(
            tool,
            {
                "summary": "s",
                "ui_id": "deep-panel",
                "operation": "append",
                "target": "lvl0",
                "root": [{"component": "Divider"}],
            },
        )
        assert not follow_up.startswith("ERROR"), follow_up
        assert len(events) == 2
