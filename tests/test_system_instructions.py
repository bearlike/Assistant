"""Tests for operator-authored custom system instructions.

The feature's whole contract is "an operator's Jinja template reaches the system
prompt of every agent, and NOTHING it can contain may break a run". So these
tests drive the real seams end to end — the sandbox, the store, the loop's
prompt assembly, and the orchestrator's once-per-run resolution — and stub only
the LLM boundary (``model.ainvoke``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import ChainableUndefined
from jinja2.sandbox import SandboxedEnvironment
from langchain_core.messages import SystemMessage
from mewbo_core.classes import OrchestrationState, TaskQueue
from mewbo_core.orchestrator import Orchestrator
from mewbo_core.prompt_registry import get_prompt_registry
from mewbo_core.session_provenance import KNOWN_SURFACES, SessionOrigin
from mewbo_core.session_store import SessionStore
from mewbo_core.session_tools import SessionToolFactory, SessionToolRegistry
from mewbo_core.spawn_agent import SpawnAgentTool
from mewbo_core.system_instructions import (
    INSTRUCTION_SANDBOX,
    KNOWN_PLATFORMS,
    MAX_RENDERED_BYTES,
    MAX_TEMPLATE_BYTES,
    InstructionContext,
    InstructionValueCatalog,
    InstructionVariable,
    JsonSystemInstructionsStore,
    SystemInstructionsDoc,
    SystemInstructionsStoreBase,
    ValuesKind,
)
from mewbo_core.tool_registry import ToolRegistry
from mewbo_core.tool_use_loop import ToolUseLoop
from pydantic import ValidationError
from test_tool_use_loop import (
    _allow_all_policy,
    _make_agent_context,
    _make_context,
    _make_hook_manager,
    _text_response,
)

# NB: the schedule_trigger provider global is reset per-test by an autouse
# fixture in ``tests/conftest.py`` (``_reset_schedule_trigger_provider``), so the
# exact-tool-set assertions below never inherit the backend-import leak.


# A template that branches on the invoking surface — the feature's headline use
# case (mobile/email/chat/CLI each get tailored extra instructions).
SURFACE_TEMPLATE = (
    "{% if surface == 'android' %}Answer in one sentence; the screen is small."
    "{% else %}Answer thoroughly with code blocks.{% endif %}"
)


class _FakeStore(SystemInstructionsStoreBase):
    """In-memory store — the DI seam that keeps these tests off Mongo/disk."""

    def __init__(self, doc: SystemInstructionsDoc | None = None) -> None:
        self.doc = doc
        self.put_calls: list[SystemInstructionsDoc] = []

    def get(self, doc_id: str = "global") -> SystemInstructionsDoc | None:
        return self.doc

    def put(self, doc: SystemInstructionsDoc) -> SystemInstructionsDoc:
        self.doc = doc
        self.put_calls.append(doc)
        return doc


class _ExplodingStore(SystemInstructionsStoreBase):
    """A store whose every read fails — stands in for an unreachable Mongo."""

    def get(self, doc_id: str = "global") -> SystemInstructionsDoc | None:
        raise ConnectionError("mongo is down")

    def put(self, doc: SystemInstructionsDoc) -> SystemInstructionsDoc:
        raise ConnectionError("mongo is down")


def _ctx(**overrides) -> InstructionContext:
    return InstructionContext(**overrides)


def _doc(template: str) -> SystemInstructionsDoc:
    return SystemInstructionsDoc(template=template)


def _row(rows: tuple[InstructionVariable, ...], name: str) -> InstructionVariable:
    """The one described variable named *name* (KeyErrors loudly if it vanished)."""
    return next(row for row in rows if row.name == name)


# ---------------------------------------------------------------------------
# The sandbox — a hostile or broken template must never execute or raise
# ---------------------------------------------------------------------------


class TestDocRenderSandbox:
    """``SystemInstructionsDoc.render`` is the security boundary for operator text."""

    def test_the_sandbox_is_configured_to_fail_closed(self) -> None:
        """The four settings the whole threat model rests on.

        Asserted directly because each one silently degrades the boundary if it
        is ever changed: a loader re-opens the filesystem, StrictUndefined turns
        a stale variable into a dead turn, and a non-sandboxed env evaluates the
        breakout below.
        """
        assert isinstance(INSTRUCTION_SANDBOX, SandboxedEnvironment)
        assert INSTRUCTION_SANDBOX.loader is None
        assert INSTRUCTION_SANDBOX.autoescape is False
        assert INSTRUCTION_SANDBOX.undefined is ChainableUndefined
        assert INSTRUCTION_SANDBOX.keep_trailing_newline is True

    def test_attribute_escape_does_not_execute(self) -> None:
        """The classic sandbox breakout must be refused, not evaluated."""
        rendered = _doc("{{ ''.__class__.__mro__[1].__subclasses__() }}").render(_ctx())

        assert rendered.text == ""
        assert rendered.error is not None
        assert "disallowed" in rendered.error
        # The payload's fingerprint must appear NOWHERE in the output.
        assert "subclasses" not in rendered.text
        assert "class" not in rendered.text

    @pytest.mark.parametrize(
        "source",
        [
            "{% include 'secrets.txt' %}",
            "{% import 'secrets.txt' as s %}",
            "{% extends 'base.txt' %}",
        ],
    )
    def test_template_inheritance_tags_fail_closed(self, source: str) -> None:
        """No loader means a template cannot pull in the filesystem."""
        rendered = _doc(source).render(_ctx())

        assert rendered.text == ""
        assert rendered.error is not None

    def test_unknown_variable_renders_blank_instead_of_raising(self) -> None:
        """Permissive undefined INVERTS the registry's StrictUndefined, on purpose.

        A template written against a variable we later remove must degrade to a
        blank, never raise — the render sits upstream of the first LLM call.
        """
        rendered = _doc("A{{ nope }}B{{ nope.deep.attr }}C").render(_ctx())

        assert rendered.text == "ABC"
        assert rendered.error is None

    def test_syntax_error_yields_no_injection(self) -> None:
        rendered = _doc("{% if surface %}never closed").render(_ctx())

        assert rendered.text == ""
        assert "syntax error" in (rendered.error or "").lower()

    def test_rendered_output_is_capped(self) -> None:
        """A small template can expand without bound; the OUTPUT is what we cap."""
        rendered = _doc("{% for i in range(5000) %}xxxxxxxxxx{% endfor %}").render(_ctx())

        assert len(rendered.text.encode("utf-8")) <= MAX_RENDERED_BYTES + len("\n[... truncated]")
        assert rendered.text.endswith("[... truncated]")

    def test_surface_conditional_produces_different_text_per_client(self) -> None:
        """The headline feature: one template, per-surface instructions."""
        doc = _doc(SURFACE_TEMPLATE)

        android = doc.render(_ctx(surface="android"))
        cli = doc.render(_ctx(surface="cli"))

        assert android.text == "Answer in one sentence; the screen is small."
        assert cli.text == "Answer thoroughly with code blocks."
        assert android.text != cli.text

    def test_render_does_not_mutate_the_document(self) -> None:
        """Render is a pure read of the doc — the error rides the RESULT.

        A failed render must not write ``last_error`` onto the model: persisting
        it is the caller-with-the-store's job, and a self-mutating model would
        make the doc unsafe to share and its validated fields lie.
        """
        doc = _doc("{% if surface %}unclosed")
        before = doc.model_dump()

        rendered = doc.render(_ctx())

        assert rendered.error is not None
        assert doc.last_error is None
        assert doc.model_dump() == before

    def test_render_exposes_the_full_context_contract(self) -> None:
        """Every declared field must actually reach the template."""
        doc = _doc(
            "{{ surface }}|{{ origin }}|{{ is_mobile }}|{{ session_id }}|{{ model }}|"
            "{{ cwd }}|{{ platform }}|{{ hostname }}|{{ mewbo_version }}|"
            "{{ capabilities|join(',') }}|{{ tools|join(',') }}|{{ project }}"
        )

        rendered = doc.render(
            _ctx(
                surface="android",
                origin="mobile",
                is_mobile=True,
                session_id="s1",
                model="test-model",
                cwd="/repo",
                platform="linux",
                hostname="box",
                mewbo_version="9.9.9",
                capabilities=("wiki", "scg"),
                tools=("read_file", "shell"),
                project="mewbo",
            )
        )

        assert rendered.text == (
            "android|mobile|True|s1|test-model|/repo|linux|box|9.9.9|"
            "wiki,scg|read_file,shell|mewbo"
        )

    def test_validate_template_reports_syntax_errors_without_rendering(self) -> None:
        assert _doc("{{ surface }}").validate_template() is None
        assert "syntax error" in (_doc("{% if x %}").validate_template() or "").lower()

    def test_a_stored_broken_template_still_loads(self) -> None:
        """Compilation is NOT a definition-time validator — deliberately.

        If it were, a stored template that no longer parses would be unloadable
        and the GET that exists to SHOW the operator their broken template would
        500 instead of rendering it for repair.
        """
        doc = SystemInstructionsDoc.model_validate({"template": "{% if x %}unclosed"})

        assert doc.template == "{% if x %}unclosed"
        assert doc.validate_template() is not None

    def test_empty_template_injects_nothing(self) -> None:
        assert _doc("   \n  ").render(_ctx()).text == ""


# ---------------------------------------------------------------------------
# origin — narrowed to SessionOrigin, projected to its plain string at the
# Jinja boundary via to_render_vars()'s model_dump(mode="json")
# ---------------------------------------------------------------------------


class TestOriginTyping:
    """``origin`` is a ``SessionOrigin`` str-enum, not a hand-listed ``str``.

    The trap this guards: ``SessionOrigin`` mixes in ``str``, so a *bare*
    ``model_dump()`` hands Jinja the enum MEMBER, not its value — ``{{ origin }}``
    would render ``SessionOrigin.WIKI`` and silently corrupt an operator's
    prompt. ``to_render_vars()`` fixes this with ``model_dump(mode="json")``.
    """

    def test_origin_enum_renders_as_its_plain_value(self) -> None:
        """The regression this refactor exists to prevent."""
        rendered = _doc("{{ origin }}").render(_ctx(origin=SessionOrigin.WIKI))

        assert rendered.text == "wiki"
        assert rendered.error is None

    def test_origin_comparison_against_a_plain_string_still_works(self) -> None:
        rendered = _doc('{% if origin == "wiki" %}HIT{% endif %}').render(
            _ctx(origin=SessionOrigin.WIKI)
        )

        assert rendered.text == "HIT"

    def test_origin_still_validates_from_a_raw_string(self) -> None:
        """The orchestrator and the API preview both pass a plain string;
        Pydantic must keep coercing it to the enum rather than rejecting it."""
        ctx = InstructionContext(origin="wiki")

        assert ctx.origin is SessionOrigin.WIKI

    def test_invalid_origin_string_is_rejected(self) -> None:
        """The whole point of narrowing the type: an unrecognised origin
        must fail at definition, not silently pass through as free text."""
        with pytest.raises(ValidationError):
            InstructionContext(origin="not-a-real-origin")

    def test_schema_still_exposes_the_allowed_origin_values(self) -> None:
        """The console renders ``model_json_schema()`` as the operator-facing
        variable reference, so the seven legal values must still be reachable
        even though they now live behind a ``$ref`` into ``$defs``."""
        schema = InstructionContext.model_json_schema()
        enum_values = {
            value
            for definition in schema.get("$defs", {}).values()
            for value in definition.get("enum", [])
        }

        assert enum_values >= {
            "user",
            "wiki",
            "search",
            "channel",
            "mobile",
            "structured",
            "draft",
        }


# ---------------------------------------------------------------------------
# The stored document + store
# ---------------------------------------------------------------------------


class TestSystemInstructionsDoc:
    """Validation happens AT DEFINITION, at the write boundary."""

    def test_oversized_template_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SystemInstructionsDoc(template="x" * (MAX_TEMPLATE_BYTES + 1))

    def test_template_at_the_cap_is_accepted(self) -> None:
        assert SystemInstructionsDoc(template="x" * MAX_TEMPLATE_BYTES).template

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SystemInstructionsDoc(template="hi", token="smuggled")  # type: ignore[call-arg]

    def test_blank_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SystemInstructionsDoc(id="  ", template="hi")


class TestJsonSystemInstructionsStore:
    """The JSON driver is the default; Mongo mirrors its contract."""

    def test_get_returns_none_before_anything_is_authored(self, tmp_path) -> None:
        store = JsonSystemInstructionsStore(data_file=tmp_path / "si.json")
        assert store.get() is None

    def test_put_then_get_round_trips(self, tmp_path) -> None:
        store = JsonSystemInstructionsStore(data_file=tmp_path / "si.json")

        store.put(SystemInstructionsDoc(template=SURFACE_TEMPLATE))
        loaded = store.get()

        assert loaded is not None
        assert loaded.template == SURFACE_TEMPLATE
        assert loaded.enabled is True
        assert loaded.updated_at

    def test_record_error_persists_and_clears(self, tmp_path) -> None:
        store = JsonSystemInstructionsStore(data_file=tmp_path / "si.json")
        store.put(SystemInstructionsDoc(template="{{ surface }}"))

        store.record_error("global", "boom")
        assert (store.get() or SystemInstructionsDoc()).last_error == "boom"

        store.record_error("global", None)
        assert (store.get() or SystemInstructionsDoc()).last_error is None

    def test_record_error_on_a_missing_doc_is_a_no_op(self, tmp_path) -> None:
        store = JsonSystemInstructionsStore(data_file=tmp_path / "si.json")
        store.record_error("global", "boom")  # must not raise
        assert store.get() is None

    def test_unreadable_file_is_treated_as_empty(self, tmp_path) -> None:
        path = tmp_path / "si.json"
        path.write_text("{ not json")
        assert JsonSystemInstructionsStore(data_file=path).get() is None


# ---------------------------------------------------------------------------
# ToolUseLoop — the prompt-assembly seam
# ---------------------------------------------------------------------------


def _make_loop(**kwargs) -> ToolUseLoop:
    return ToolUseLoop(
        agent_context=_make_agent_context(),
        tool_registry=ToolRegistry(),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
        **kwargs,
    )


class TestLoopPromptAssembly:
    """The rendered string is injected as its OWN section, never via skills."""

    def test_instructions_land_in_the_system_prompt(self) -> None:
        loop = _make_loop(user_instructions="Always cite the file path.")

        prompt = loop._render_system_prompt(None, None, "")

        assert "Custom instructions:" in prompt
        assert "Always cite the file path." in prompt

    def test_no_section_when_there_are_no_instructions(self) -> None:
        loop = _make_loop(user_instructions=None)

        assert "Custom instructions:" not in loop._render_system_prompt(None, None, "")

    def test_section_does_not_squat_the_skill_instructions_slot(self) -> None:
        """``skill_instructions`` is a last-write-wins slot with 5 writers.

        The custom instructions must coexist with an active skill, not clobber it.
        """
        loop = _make_loop(
            user_instructions="Operator says: be terse.",
            skill_instructions="Skill body here.",
        )

        prompt = loop._render_system_prompt(None, None, "")

        assert "Operator says: be terse." in prompt
        assert "Active skill instructions:" in prompt
        assert "Skill body here." in prompt

    def test_instructions_survive_a_model_escalation(self) -> None:
        """Model escalation re-renders messages[0] in place; the section must come back.

        This is why the value lives on the loop INSTANCE rather than arriving as
        a per-call argument — it would vanish on the first model fallback.
        """
        loop = _make_loop(user_instructions="Never force-push.")
        messages = [SystemMessage(content=loop._render_system_prompt(None, None, ""))]
        assert "Never force-push." in str(messages[0].content)
        # Minimal run()-set state the escalation path reads — same preconditions
        # the sibling escalation suite establishes (test_tool_use_loop_model_escalation).
        loop._tool_specs_full = []
        loop._tool_search_enabled = False
        loop._deferred_ids = set()
        loop._last_active_ids = set()

        with patch.object(loop, "_bind_model", return_value=object()):
            loop._apply_model_escalation(
                "escalated-model",
                messages,
                context=None,
                plan=None,
                agent_tree="",
                tool_schemas=[],
                model=object(),
            )

        assert loop._active_model == "escalated-model"
        assert "Custom instructions:" in str(messages[0].content)
        assert "Never force-push." in str(messages[0].content)

    def test_a_run_still_completes_when_instructions_are_absent(self) -> None:
        """The fail-soft end state: a broken template resolves to None upstream,
        and the turn runs exactly as it does today."""
        loop = _make_loop(user_instructions=None)
        bound = MagicMock()
        bound.ainvoke = AsyncMock(return_value=_text_response("hi"))

        with patch("mewbo_core.tool_use_loop.build_chat_model") as build:
            build.return_value = MagicMock()
            build.return_value.bind_tools.return_value = bound
            task_queue, state = asyncio.run(
                loop.run("hello", tool_specs=[], context=_make_context())
            )

        assert state.done is True
        assert state.done_reason == "completed"
        assert "hi" in (task_queue.task_result or "")


class TestChildAgentInheritance:
    """A sub-agent runs under the same operator rules as its parent."""

    def test_child_loop_inherits_the_instructions(self) -> None:
        parent_ctx = _make_agent_context()
        tool = SpawnAgentTool(
            agent_context=parent_ctx,
            tool_registry=ToolRegistry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            project_instructions="Project rules.",
            user_instructions="Operator rules.",
        )

        child = tool._build_child_loop(parent_ctx.child(), None)

        assert child._user_instructions == "Operator rules."
        assert "Operator rules." in child._render_system_prompt(None, None, "")

    def test_the_loop_hands_its_instructions_to_its_spawn_tool(self) -> None:
        loop = _make_loop(user_instructions="Operator rules.")

        assert loop._spawn_agent_tool is not None
        assert loop._spawn_agent_tool._user_instructions == "Operator rules."


# ---------------------------------------------------------------------------
# Orchestrator — the once-per-run resolution seam
# ---------------------------------------------------------------------------


class _LoopSpy:
    """Captures the kwargs the orchestrator hands ToolUseLoop, then no-ops the run."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        _LoopSpy.last_kwargs = kwargs

    async def run(self, *_args, **_kwargs):
        task_queue = TaskQueue(action_steps=[])
        task_queue.task_result = "done"
        state = OrchestrationState(goal="go")
        state.done = True
        state.done_reason = "completed"
        return task_queue, state


def _run_orchestrator(tmp_path, store, *, source_platform: str | None = None) -> dict:
    """Drive a real ``arun`` with the LLM boundary replaced, return the loop kwargs."""
    orchestrator = Orchestrator(
        session_store=SessionStore(root_dir=str(tmp_path)),
        system_instructions_store=store,
    )
    with patch("mewbo_core.orchestrator.ToolUseLoop", _LoopSpy):
        asyncio.run(orchestrator.arun("hello", source_platform=source_platform))
    return _LoopSpy.last_kwargs


class TestOrchestratorResolution:
    """Resolution is once per run and fail-soft in every direction."""

    def test_enabled_template_reaches_the_loop(self, tmp_path) -> None:
        store = _FakeStore(SystemInstructionsDoc(template="Always run the tests."))

        kwargs = _run_orchestrator(tmp_path, store)

        assert kwargs["user_instructions"] == "Always run the tests."

    def test_the_surface_reaches_the_template(self, tmp_path) -> None:
        """``source_platform`` used to dead-end at the Langfuse seam; it must now
        reach the operator's template so a per-client branch actually branches."""
        store = _FakeStore(SystemInstructionsDoc(template=SURFACE_TEMPLATE))

        android = _run_orchestrator(tmp_path, store, source_platform="android")
        cli = _run_orchestrator(tmp_path, store, source_platform="cli")

        assert android["user_instructions"] == "Answer in one sentence; the screen is small."
        assert cli["user_instructions"] == "Answer thoroughly with code blocks."

    def test_disabled_doc_injects_nothing(self, tmp_path) -> None:
        store = _FakeStore(SystemInstructionsDoc(template="Be terse.", enabled=False))

        assert _run_orchestrator(tmp_path, store)["user_instructions"] is None

    def test_no_doc_injects_nothing(self, tmp_path) -> None:
        assert _run_orchestrator(tmp_path, _FakeStore(None))["user_instructions"] is None

    def test_broken_template_injects_nothing_and_the_run_proceeds(self, tmp_path) -> None:
        """The load-bearing guarantee: a broken template degrades, never kills."""
        store = _FakeStore(SystemInstructionsDoc(template="{% if surface %}unclosed"))

        kwargs = _run_orchestrator(tmp_path, store)

        assert kwargs["user_instructions"] is None
        # ...and the failure is persisted so the settings UI can surface it.
        assert store.doc is not None
        assert "syntax error" in (store.doc.last_error or "").lower()

    def test_a_sandbox_escape_injects_nothing_and_the_run_proceeds(self, tmp_path) -> None:
        store = _FakeStore(
            SystemInstructionsDoc(template="{{ ''.__class__.__mro__[1].__subclasses__() }}")
        )

        kwargs = _run_orchestrator(tmp_path, store)

        assert kwargs["user_instructions"] is None
        assert "disallowed" in ((store.doc.last_error if store.doc else "") or "")

    def test_an_unreachable_store_never_breaks_a_run(self, tmp_path) -> None:
        kwargs = _run_orchestrator(tmp_path, _ExplodingStore())

        assert kwargs["user_instructions"] is None

    def test_a_healthy_render_clears_a_stale_error(self, tmp_path) -> None:
        store = _FakeStore(
            SystemInstructionsDoc(template="Be terse.", last_error="an error from last time")
        )

        _run_orchestrator(tmp_path, store)

        assert store.doc is not None
        assert store.doc.last_error is None

    def test_a_healthy_render_does_not_rewrite_the_doc(self, tmp_path) -> None:
        """No write on the happy path — the store read is once per run, not a churn."""
        store = _FakeStore(SystemInstructionsDoc(template="Be terse."))

        _run_orchestrator(tmp_path, store)

        assert store.put_calls == []


class TestDescribeTheVariableReference:
    """``InstructionContext.describe`` IS the operator-facing variable reference.

    It is generated from the model's own schema, so a row can never claim
    something the renderer does not actually expose.
    """

    def test_every_field_yields_exactly_one_row(self) -> None:
        rows = InstructionContext.describe(InstructionValueCatalog())

        assert [row.name for row in rows] == list(InstructionContext.model_fields)

    def test_origin_is_closed_and_carries_the_real_session_origin_members(self) -> None:
        """``origin`` is the ONE genuinely closed field, and its values come from
        ``SessionOrigin`` via ``$defs`` — not a hand-typed list that could drift
        from the enum (the "adding an origin is a three-site lockstep" trap)."""
        row = _row(InstructionContext.describe(InstructionValueCatalog()), "origin")

        assert row.candidates is not None
        assert row.candidates.kind is ValuesKind.CLOSED
        assert set(row.candidates.values) == {o.value for o in SessionOrigin}

    def test_an_empty_catalog_source_yields_no_candidates_at_all(self) -> None:
        """NOT an empty chip list. An operator shown "Known here: (nothing)" reads
        it as "this is always empty", when it means "the deployment said nothing"."""
        rows = InstructionContext.describe(InstructionValueCatalog())

        # NB the FIELD is ``model`` while its catalog SOURCE is ``models`` — the
        # two names are deliberately distinct (one value vs. the set it's drawn
        # from), which is exactly why the source is declared on the field.
        for name in ("tools", "capabilities", "model", "project"):
            assert _row(rows, name).candidates is None

    def test_a_populated_catalog_source_reaches_its_field(self) -> None:
        catalog = InstructionValueCatalog(tools=("read_file", "wiki_search_pages"))

        row = _row(InstructionContext.describe(catalog), "tools")

        assert row.candidates is not None
        assert row.candidates.kind is ValuesKind.KNOWN
        assert row.candidates.values == ("read_file", "wiki_search_pages")
        assert row.candidates.note

    def test_free_form_fields_get_no_candidates(self) -> None:
        """A list for ``session_id``/``cwd``/``hostname`` would simply be a lie."""
        rows = InstructionContext.describe(InstructionValueCatalog(tools=("read_file",)))

        for name in ("session_id", "cwd", "hostname", "mewbo_version", "is_mobile"):
            assert _row(rows, name).candidates is None

    def test_surface_and_platform_default_to_the_vocabularies_core_owns(self) -> None:
        """These two need no app injection — core knows them, so a bare catalog
        still documents them."""
        rows = InstructionContext.describe(InstructionValueCatalog())

        surface = _row(rows, "surface").candidates
        platform = _row(rows, "platform").candidates
        assert surface is not None and surface.values == KNOWN_SURFACES
        assert platform is not None and platform.values == KNOWN_PLATFORMS

    def test_home_assistant_is_a_real_surface_and_must_stay_branchable(self) -> None:
        """Regression guard, and it is NOT cosmetic.

        Home Assistant genuinely stamps its surface: the HA conversation agent
        sends ``X-Mewbo-Surface: home-assistant`` (``mewbo_ha_conversation/
        api.py``) and the sync ``POST /api/query`` route reads it back through
        ``_request_surface()``. So an operator's ``{% if surface ==
        "home-assistant" %}`` branch DOES fire, and dropping the value from the
        reference would tell them, falsely, that it never could.
        """
        assert "home-assistant" in KNOWN_SURFACES

    def test_the_type_of_an_enum_field_resolves_through_its_ref(self) -> None:
        """The ``$ref``-drop trap: an enum field is a ``$ref`` into ``$defs``, so a
        naive ``prop["type"]`` read reports nothing and the row degrades to a bare
        type word with no values."""
        rows = InstructionContext.describe(InstructionValueCatalog())

        assert _row(rows, "origin").type == "string"
        assert _row(rows, "project").type == "string"  # ``str | None`` -> anyOf
        assert _row(rows, "tools").type == "array"


class TestKnownIsASupersetClaimNotAnExhaustiveOne:
    """The invariant the whole feature rests on.

    A KNOWN candidate list describes the DEPLOYMENT, never a SESSION. A session's
    tools are a SUBSET: the catalog is everything installed here, while a session
    is scoped by its project, the plugins that loaded, and the caller's allowlist.
    An operator who reads the list as exhaustive writes ``{% if tools ==
    [...] %}`` and it silently never fires.
    """

    def test_a_sessions_tools_are_a_strict_subset_of_the_catalogs(self, tmp_path) -> None:
        """Driven through the REAL orchestrator, against a catalog built from the
        SAME registries the app would build one from at its edge."""
        orchestrator = Orchestrator(
            session_store=SessionStore(root_dir=str(tmp_path)),
            system_instructions_store=_FakeStore(
                SystemInstructionsDoc(template="{{ tools|join(',') }}")
            ),
        )
        # Installed on this deployment, but gated on a capability the session below
        # never advertises — so the catalog lists it and the session does NOT hold it.
        # The id is SYNTHETIC on purpose. ``register`` is a setdefault and
        # ``Orchestrator.__init__`` has already loaded every plugin manifest, so
        # naming a real manifest tool here registers nothing at all: the manifest's
        # own entry wins and IT, not this test, decides the gating. This line used
        # to name ``wiki_search_pages`` and was therefore a silent no-op that
        # happened to pass while that entry was capability-gated.
        orchestrator._session_tool_registry.register(
            SessionToolFactory(
                tool_id="deployment_only_tool",
                build=lambda session_id, event_logger: MagicMock(),
                requires_capabilities=("wiki",),
            )
        )

        with patch("mewbo_core.orchestrator.ToolUseLoop", _LoopSpy):
            asyncio.run(orchestrator.arun("hello"))

        rendered = _LoopSpy.last_kwargs["user_instructions"] or ""
        session_tools = {t for t in rendered.split(",") if t}
        catalog = InstructionValueCatalog(
            tools=tuple(
                sorted(
                    {spec.tool_id for spec in orchestrator._tool_registry.list_specs()}
                    # BOTH halves, exactly as the app's catalog builds them
                    # (``InstructionValueSources._tools`` = every scope's registry
                    # UNION the plugin manifests' session-tool entries). Omitting
                    # the session half made this fixture — not the product — the
                    # thing that broke when a manifest first shipped a default-on
                    # session tool: the deployment declared it, only the fixture
                    # did not.
                    | set(orchestrator._session_tool_registry._factories)
                )
            )
        )

        assert session_tools
        assert session_tools <= set(catalog.tools)
        # STRICTLY a subset — the deployment has a tool this session does not, which
        # is the whole reason a KNOWN list may not be read as exhaustive.
        assert "deployment_only_tool" in catalog.tools
        assert "deployment_only_tool" not in session_tools

    def test_a_session_can_hold_a_capability_the_catalog_never_listed(self) -> None:
        """The other half of "not exhaustive": the catalog is what this deployment
        DECLARES, and a client can advertise something outside it. The reference
        must not imply otherwise, which is exactly why ``kind`` exists."""
        catalog = InstructionValueCatalog(capabilities=("wiki",))
        ctx = InstructionContext(capabilities=("wiki", "scg"))

        row = _row(InstructionContext.describe(catalog), "capabilities")
        assert row.candidates is not None
        assert row.candidates.kind is ValuesKind.KNOWN  # NOT closed
        assert not set(ctx.capabilities) <= set(catalog.capabilities)

    def test_only_a_real_enum_is_ever_closed(self) -> None:
        """A CLOSED list is a promise an ``else`` branch is unreachable. Only
        ``origin`` may make it; everything else is KNOWN, however full it looks.

        The cross-layer half of this invariant — that the APP's catalog really is
        drawn from the same registries a session is scoped from — lives with the
        app that resolves it (``apps/mewbo_api`` builds the catalog at its edge
        and injects it). Core asserts the strongest thing it owns: the KIND.
        """
        catalog = InstructionValueCatalog(
            tools=("read_file",),
            capabilities=("wiki",),
            projects=("mewbo",),
            models=("openai/gpt-5",),
        )

        closed = {
            row.name
            for row in InstructionContext.describe(catalog)
            if row.candidates and row.candidates.kind is ValuesKind.CLOSED
        }

        assert closed == {"origin"}


class TestInstructionToolsAreHonest:
    """``InstructionContext.tools`` must not lie about what the agent holds."""

    def test_the_catalog_hides_an_empty_source_rather_than_showing_nothing(self) -> None:
        assert InstructionValueCatalog().candidates_for("tools") is None
        assert InstructionValueCatalog(tools=("read_file",)).candidates_for("tools") is not None

    def test_an_unknown_source_key_degrades_instead_of_raising(self) -> None:
        """A typo'd or since-removed ``x-values`` must not 500 the settings page."""
        assert InstructionValueCatalog(tools=("read_file",)).candidates_for("nope") is None
        assert InstructionValueCatalog().candidates_for("model_config") is None

    def test_session_tools_reach_the_template(self, tmp_path) -> None:
        """The bug this task exists to kill.

        ``tools`` used to carry the ``ToolRegistry`` specs ONLY, so every session
        tool the agent genuinely had (``wiki_*``, ``scg_*``, ``submit_widget``,
        ``schedule_trigger``) was missing and an operator's
        ``{% if 'wiki_search_pages' in tools %}`` branch silently never fired.

        Driven through the REAL orchestrator seam with a template that dumps
        ``tools``, so it asserts on what an operator's template actually receives.
        """
        orchestrator = Orchestrator(
            session_store=SessionStore(root_dir=str(tmp_path)),
            system_instructions_store=_FakeStore(
                SystemInstructionsDoc(template="{{ tools|join(',') }}")
            ),
        )
        orchestrator._session_tool_registry.register(
            SessionToolFactory(
                tool_id="wiki_search_pages",
                build=lambda session_id, event_logger: MagicMock(),
                requires_capabilities=("wiki",),
            )
        )

        with patch("mewbo_core.orchestrator.ToolUseLoop", _LoopSpy):
            asyncio.run(orchestrator.arun("hello", allowed_tools=["wiki_search_pages"]))

        rendered = _LoopSpy.last_kwargs["user_instructions"] or ""
        assert "wiki_search_pages" in rendered.split(",")

    def test_the_loop_injected_internals_are_absent_but_tool_search_is_not(
        self, tmp_path
    ) -> None:
        """The documented omission, and the ONE that is not an omission.

        The five internals below are decided INSIDE ``ToolUseLoop``, downstream of
        resolution, and are mode-dependent — so the field omits them and its
        description SAYS so. ``tool_search`` LOOKS like a sixth, but it is a real
        ``ToolRegistry`` spec (``always_load``) and genuinely reaches the agent, so
        it IS listed. Calling it omitted would have been a fresh lie in the very
        description written to stop the field lying — this test pins both halves.
        """
        orchestrator = Orchestrator(
            session_store=SessionStore(root_dir=str(tmp_path)),
            system_instructions_store=_FakeStore(
                SystemInstructionsDoc(template="{{ tools|join(',') }}")
            ),
        )

        with patch("mewbo_core.orchestrator.ToolUseLoop", _LoopSpy):
            asyncio.run(orchestrator.arun("hello"))

        tools = {t for t in (_LoopSpy.last_kwargs["user_instructions"] or "").split(",") if t}
        injected = {
            "spawn_agent",
            "spawn_agents",
            "update_todos",
            "exit_plan_mode",
            "activate_skill",
        }
        assert not tools & injected
        assert "tool_search" in tools

        # The description must name exactly what it omits, or it drifts back into
        # the lie this whole change removes.
        description = InstructionContext.model_fields["tools"].description or ""
        for omitted in sorted(injected):
            assert omitted in description

    def test_ids_for_matches_what_build_for_actually_builds(self) -> None:
        """The two must not drift — that is the entire reason ``tools`` can be
        trusted. One selection algorithm, two callers (name it vs. build it)."""
        registry = SessionToolRegistry()
        registry.register(
            SessionToolFactory(
                tool_id="scg_map",
                build=lambda session_id, event_logger: MagicMock(tool_id="scg_map"),
                requires_capabilities=("scg",),
            )
        )
        registry.register(
            SessionToolFactory(
                tool_id="wiki_search_pages",
                build=lambda session_id, event_logger: MagicMock(tool_id="wiki_search_pages"),
                requires_capabilities=("wiki",),
            )
        )
        # The UNCONDITIONAL axis needs covering here too: it is a third gate with
        # its own (df875) ceiling, so a drift between the two callers could hide
        # in it exactly as it could in the capability gate.
        registry.register(
            SessionToolFactory(
                tool_id="schedule_trigger",
                build=lambda session_id, event_logger: MagicMock(tool_id="schedule_trigger"),
                unconditional=True,
            )
        )

        for allowed, caps, strict in (
            (None, ("scg",), False),
            (None, ("wiki", "scg"), False),
            (["wiki_search_pages"], (), False),
            (None, (), False),
            # Both sides of the unconditional ceiling: a PERMISSIVE allowlist
            # surfaces it, a STRICT one caps it.
            (["wiki_search_pages"], (), True),
            (None, (), True),
        ):
            built = registry.build_for(
                allowed,
                session_id="s1",
                event_logger=None,
                session_capabilities=caps,
                strict_tool_scope=strict,
            )
            named = registry.ids_for(
                allowed, session_capabilities=caps, strict_tool_scope=strict
            )

            assert named == [tool.tool_id for tool in built]


class TestPromptRegistryStillValid:
    """A new entry is additive — the CI gate must stay green."""

    def test_validate_all_passes(self) -> None:
        get_prompt_registry().validate_all()

    def test_the_new_entry_substitutes_rather_than_re_renders(self) -> None:
        """The SSTI boundary: operator text is a VARIABLE, never re-parsed.

        The registry env is NON-sandboxed and StrictUndefined, so if the
        operator's rendered text were re-rendered here, `{{ ... }}` inside it
        would execute against it (or raise and kill the turn). It must survive
        as literal text.
        """
        hostile = "{{ ''.__class__ }} and {{ undefined_name }}"

        rendered = get_prompt_registry().render(
            "loop.section.user_instructions", user_instructions=hostile
        )

        assert rendered == f"Custom instructions:\n{hostile}"
