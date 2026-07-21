"""Tests for the schedule_trigger SessionTool (mewbo_core.triggers.session_tool).

Covers each operation's happy path (arm per kind, list, cancel), policy
rejections surfaced through the tool, default-expiry stamping end to end,
list compaction, cancel idempotency, malformed-input error envelopes, and
the terminal-free contract.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.session_tool import ScheduleTriggerTool
from mewbo_core.triggers.store import JsonTriggerStore

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
SESSION_ID = "s1"


@pytest.fixture
def store(tmp_path):
    return JsonTriggerStore(data_file=tmp_path / "triggers.json")


def _tool(store, *, policy=None, event_logger=None, agent_id=None, session_id=SESSION_ID):
    return ScheduleTriggerTool(
        session_id=session_id,
        store=store,
        policy=policy or TriggerPolicy(),
        event_logger=event_logger,
        agent_id=agent_id,
    )


def _step(tool_input):
    return ActionStep(
        tool_id="schedule_trigger",
        operation="execute",
        tool_input=tool_input,
    )


def _run(tool, tool_input):
    return asyncio.run(tool.handle(_step(tool_input)))


def _payload(speaker) -> dict:
    """Parse a MockSpeaker's content back into its dict (ok or error envelope)."""
    return ast.literal_eval(speaker.content)


def _is_error(speaker) -> bool:
    return isinstance(_payload(speaker), dict) and "error" in _payload(speaker)


# ---------------------------------------------------------------------------
# should_terminate_run / terminal_reason contract
# ---------------------------------------------------------------------------


def test_never_terminates(store):
    tool = _tool(store)
    assert tool.should_terminate_run() is False
    _run(tool, {"operation": "list"})
    assert tool.should_terminate_run() is False


def test_terminal_reason_default(store):
    assert _tool(store).terminal_reason() == "awaiting_approval"


def test_tool_id_and_modes(store):
    assert ScheduleTriggerTool.tool_id == "schedule_trigger"
    assert ScheduleTriggerTool.modes == frozenset({"act"})


# ---------------------------------------------------------------------------
# arm — happy paths, one per kind
# ---------------------------------------------------------------------------


def test_arm_time_at_happy_path(store):
    events = []
    tool = _tool(store, event_logger=events.append, agent_id="agent-1")
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "time.at",
            "wake_prompt": "check the deploy",
            "at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    payload = _payload(result)
    assert payload["operation"] == "arm"
    assert payload["kind"] == "time.at"
    assert payload["status"] == "armed"
    assert payload["next_fire_at"] is not None

    persisted = store.get(payload["id"])
    assert persisted is not None
    assert persisted.session_id == SESSION_ID
    assert persisted.created_by == "agent"
    assert persisted.provenance.agent_id == "agent-1"
    assert persisted.provenance.step is None
    assert persisted.max_fires == 1  # time.at forces single-fire

    assert len(events) == 1
    assert events[0]["type"] == "trigger_armed"
    assert events[0]["payload"]["trigger_id"] == payload["id"]
    assert events[0]["payload"]["kind"] == "time.at"
    assert "check the deploy" in events[0]["payload"]["summary"]


def test_arm_time_cron_happy_path(store):
    tool = _tool(store)
    result = _run(
        tool,
        {"operation": "arm", "kind": "time.cron", "wake_prompt": "poll", "cron": "0 * * * *"},
    )
    payload = _payload(result)
    assert payload["kind"] == "time.cron"
    persisted = store.get(payload["id"])
    assert persisted.cron == "0 * * * *"


def test_arm_ci_workflow_happy_path(store):
    tool = _tool(store)
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "ci.workflow",
            "wake_prompt": "check the build",
            "repo": "acme/widget",
            "run_id": 42,
        },
    )
    payload = _payload(result)
    persisted = store.get(payload["id"])
    assert persisted.repo == "acme/widget"
    assert persisted.run_id == 42
    assert persisted.workflow is None


def test_arm_ci_workflow_conflicting_selectors_error(store):
    """Both run_id and workflow set — CiWorkflowTrigger's model_validator rejects."""
    tool = _tool(store)
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "ci.workflow",
            "wake_prompt": "x",
            "repo": "acme/widget",
            "run_id": 1,
            "workflow": "ci",
        },
    )
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "validation"
    assert store.list(session_id=SESSION_ID) == []


def test_arm_forge_pr_happy_path(store):
    tool = _tool(store)
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "forge.pr",
            "wake_prompt": "check the review",
            "repo": "acme/widget",
            "number": 7,
            "events": ["merged", "review"],
        },
    )
    payload = _payload(result)
    persisted = store.get(payload["id"])
    assert persisted.number == 7
    assert persisted.events == ["merged", "review"]


def test_arm_forge_pr_empty_events_error(store):
    tool = _tool(store)
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "forge.pr",
            "wake_prompt": "x",
            "repo": "acme/widget",
            "number": 1,
            "events": [],
        },
    )
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "validation"


def test_arm_webhook_happy_path(store):
    tool = _tool(store)
    result = _run(tool, {"operation": "arm", "kind": "webhook", "wake_prompt": "notify me"})
    payload = _payload(result)
    assert payload["kind"] == "webhook"
    persisted = store.get(payload["id"])
    assert persisted.secret  # auto-generated
    assert "secret" not in payload  # never echoed back in the confirmation


# ---------------------------------------------------------------------------
# arm — default expiry, from the policy
# ---------------------------------------------------------------------------


def test_arm_stamps_policy_default_expiry(store):
    policy = TriggerPolicy(default_expiry=timedelta(days=2))
    tool = _tool(store, policy=policy)
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "time.at",
            "wake_prompt": "x",
            "at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    payload = _payload(result)
    persisted = store.get(payload["id"])
    assert persisted.expires_at is not None
    assert persisted.expires_at - persisted.created_at == timedelta(days=2)


# ---------------------------------------------------------------------------
# arm — policy rejections surfaced through the tool
# ---------------------------------------------------------------------------


def test_arm_rejected_at_armed_ceiling(store):
    policy = TriggerPolicy(max_armed_per_session=1)
    tool = _tool(store, policy=policy)
    first = _run(
        tool,
        {
            "operation": "arm",
            "kind": "time.at",
            "wake_prompt": "one",
            "at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    assert not _is_error(first)

    second = _run(
        tool,
        {
            "operation": "arm",
            "kind": "time.at",
            "wake_prompt": "two",
            "at": (NOW + timedelta(hours=2)).isoformat(),
        },
    )
    assert _is_error(second)
    assert _payload(second)["error"]["code"] == "policy"
    # Only the first trigger was ever persisted.
    assert len(store.list(session_id=SESSION_ID)) == 1


def test_arm_rejected_max_fires_over_cap(store):
    policy = TriggerPolicy(max_fires_cap=3)
    tool = _tool(store, policy=policy)
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "time.cron",
            "wake_prompt": "x",
            "cron": "*/10 * * * *",
            "max_fires": 10,
        },
    )
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "policy"
    assert store.list(session_id=SESSION_ID) == []


def test_arm_rejected_cron_below_min_interval(store):
    policy = TriggerPolicy(cron_min_interval_seconds=3600)
    tool = _tool(store, policy=policy)
    result = _run(
        tool,
        {"operation": "arm", "kind": "time.cron", "wake_prompt": "x", "cron": "* * * * *"},
    )
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "policy"
    assert store.list(session_id=SESSION_ID) == []


# ---------------------------------------------------------------------------
# arm — malformed input
# ---------------------------------------------------------------------------


def test_arm_missing_kind_error(store):
    result = _run(_tool(store), {"operation": "arm", "wake_prompt": "x"})
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "validation"


def test_arm_missing_wake_prompt_error(store):
    result = _run(
        _tool(store),
        {"operation": "arm", "kind": "time.at", "at": (NOW + timedelta(hours=1)).isoformat()},
    )
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "validation"


def test_missing_operation_error(store):
    result = _run(_tool(store), {"kind": "time.at"})
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "validation"


def test_string_tool_input_error(store):
    """A raw-string tool_input (ToolInput = str | dict) fails validation cleanly."""
    tool = _tool(store)
    step = ActionStep(tool_id="schedule_trigger", operation="execute", tool_input="not-json")
    result = asyncio.run(tool.handle(step))
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "validation"


def test_unknown_operation_rejected_by_schema(store):
    """`operation` is a closed Literal — an unrecognised value fails validation."""
    result = _run(_tool(store), {"operation": "delete", "trigger_id": "x"})
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "validation"


# ---------------------------------------------------------------------------
# list — compaction
# ---------------------------------------------------------------------------


def test_list_empty(store):
    result = _run(_tool(store), {"operation": "list"})
    payload = _payload(result)
    assert payload == {"operation": "list", "count": 0, "triggers": []}


def test_list_compacts_and_truncates(store):
    tool = _tool(store)
    long_prompt = "x" * 500
    armed = _run(
        tool,
        {
            "operation": "arm",
            "kind": "time.at",
            "wake_prompt": long_prompt,
            "at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    trigger_id = _payload(armed)["id"]

    listed = _payload(_run(tool, {"operation": "list"}))
    assert listed["count"] == 1
    item = listed["triggers"][0]
    assert item["id"] == trigger_id
    assert item["kind"] == "time.at"
    assert item["status"] == "armed"
    assert len(item["wake_prompt"]) == 160
    assert item["fires"] == 0
    assert item["max_fires"] == 1
    assert "next_fire_at" in item


def test_list_scoped_to_session(store):
    tool_a = _tool(store, session_id="session-a")
    tool_b = _tool(store, session_id="session-b")
    _run(
        tool_a,
        {
            "operation": "arm",
            "kind": "time.at",
            "wake_prompt": "a's trigger",
            "at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    listed_b = _payload(_run(tool_b, {"operation": "list"}))
    assert listed_b["count"] == 0


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_transitions_armed_to_cancelled(store):
    tool = _tool(store)
    armed = _payload(
        _run(
            tool,
            {
                "operation": "arm",
                "kind": "time.at",
                "wake_prompt": "x",
                "at": (NOW + timedelta(hours=1)).isoformat(),
            },
        )
    )
    result = _payload(_run(tool, {"operation": "cancel", "trigger_id": armed["id"]}))
    assert result == {"operation": "cancel", "id": armed["id"], "status": "cancelled"}
    assert store.get(armed["id"]).status == "cancelled"


def test_cancel_idempotent_on_terminal(store):
    tool = _tool(store)
    armed = _payload(
        _run(
            tool,
            {
                "operation": "arm",
                "kind": "time.at",
                "wake_prompt": "x",
                "at": (NOW + timedelta(hours=1)).isoformat(),
            },
        )
    )
    first = _payload(_run(tool, {"operation": "cancel", "trigger_id": armed["id"]}))
    assert first["status"] == "cancelled"

    second = _run(tool, {"operation": "cancel", "trigger_id": armed["id"]})
    assert not _is_error(second)
    second_payload = _payload(second)
    assert second_payload["status"] == "cancelled"
    assert second_payload["already_terminal"] is True


def test_cancel_unknown_trigger_id_error(store):
    result = _run(_tool(store), {"operation": "cancel", "trigger_id": "does-not-exist"})
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "not_found"


def test_cancel_missing_trigger_id_error(store):
    result = _run(_tool(store), {"operation": "cancel"})
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "validation"


def test_cancel_scoped_to_owning_session(store):
    """A trigger from another session reads as not_found — no cross-session leak."""
    tool_a = _tool(store, session_id="session-a")
    tool_b = _tool(store, session_id="session-b")
    armed = _payload(
        _run(
            tool_a,
            {
                "operation": "arm",
                "kind": "time.at",
                "wake_prompt": "x",
                "at": (NOW + timedelta(hours=1)).isoformat(),
            },
        )
    )
    result = _run(tool_b, {"operation": "cancel", "trigger_id": armed["id"]})
    assert _is_error(result)
    assert _payload(result)["error"]["code"] == "not_found"
    # The trigger is untouched.
    assert store.get(armed["id"]).status == "armed"


# ---------------------------------------------------------------------------
# event_logger resilience
# ---------------------------------------------------------------------------


def test_arm_succeeds_with_no_event_logger(store):
    tool = _tool(store, event_logger=None)
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "time.at",
            "wake_prompt": "x",
            "at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    assert not _is_error(result)


def test_arm_survives_raising_event_logger(store):
    def _boom(event):
        raise RuntimeError("sink down")

    tool = _tool(store, event_logger=_boom)
    result = _run(
        tool,
        {
            "operation": "arm",
            "kind": "time.at",
            "wake_prompt": "x",
            "at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    # A failing event sink must never fail the step itself.
    assert not _is_error(result)
    payload = _payload(result)
    assert store.get(payload["id"]) is not None


# ---------------------------------------------------------------------------
# Down-only registration seam: schedule_trigger via SessionToolRegistry
# ---------------------------------------------------------------------------


class TestScheduleTriggerFactory:
    """The down-only push that lets a SPAWNED sub-agent hold schedule_trigger.

    schedule_trigger needs a store+policy the generic manifest path can't feed,
    so the app pushes them once and core builds an ``unconditional``
    ``SessionToolFactory`` closing over them (mirrors ``register_app_submitter``).
    That is what finally reaches a spawned child: it rides the shared
    ``SessionToolRegistry`` a child already receives, so the app-builder
    AgentDef's ``tools:`` list delivers it — impossible over the old root-only
    ``extra_session_tools`` seam.
    """

    @pytest.fixture(autouse=True)
    def _restore_provider(self):
        """Save/restore the module global so the push never leaks across tests."""
        from mewbo_core.triggers import session_tool as st

        saved = st._TRIGGER_TOOL_PROVIDER
        st._TRIGGER_TOOL_PROVIDER = None
        try:
            yield
        finally:
            st._TRIGGER_TOOL_PROVIDER = saved

    def test_factory_is_none_before_push(self):
        from mewbo_core.triggers.session_tool import schedule_trigger_factory

        assert schedule_trigger_factory() is None

    def test_factory_after_push_is_unconditional(self, store):
        from mewbo_core.triggers.session_tool import (
            register_schedule_trigger_provider,
            schedule_trigger_factory,
        )

        register_schedule_trigger_provider(store, TriggerPolicy())
        factory = schedule_trigger_factory()
        assert factory is not None
        assert factory.tool_id == "schedule_trigger"
        assert factory.unconditional is True
        assert factory.requires_capabilities == ()

    def test_built_tool_arms_through_the_real_store(self, store):
        """The factory's build closes over the pushed store+policy end to end."""
        from mewbo_core.session_tools import SessionToolRegistry
        from mewbo_core.triggers.session_tool import (
            register_schedule_trigger_provider,
            schedule_trigger_factory,
        )

        register_schedule_trigger_provider(store, TriggerPolicy())
        reg = SessionToolRegistry()
        reg.register(schedule_trigger_factory())

        # Root (no explicit scope) gets it — the always-on shape it had before.
        root_tools = reg.build_for(None, session_id=SESSION_ID, event_logger=None)
        assert [t.tool_id for t in root_tools] == ["schedule_trigger"]

        # It actually arms against the pushed store.
        result = asyncio.run(
            root_tools[0].handle(
                _step(
                    {
                        "operation": "arm",
                        "kind": "time.at",
                        "wake_prompt": "wake me",
                        "at": (NOW + timedelta(hours=2)).isoformat(),
                    }
                )
            )
        )
        assert not _is_error(result)
        assert store.get(_payload(result)["id"]) is not None

    def test_spawned_child_scope_gates_delivery(self, store):
        """A STRICT child that names it binds it; a differently-scoped one does not.

        The load-bearing regression: before, NO spawned child could ever
        hold schedule_trigger. Under a STRICT AgentDef scope (the app-builder
        shape) the tool must be NAMED to appear.
        """
        from mewbo_core.session_tools import SessionToolRegistry
        from mewbo_core.triggers.session_tool import (
            register_schedule_trigger_provider,
            schedule_trigger_factory,
        )

        register_schedule_trigger_provider(store, TriggerPolicy())
        reg = SessionToolRegistry()
        reg.register(schedule_trigger_factory())

        admits = reg.build_for(
            ["schedule_trigger", "read_file"],
            session_id="child",
            event_logger=None,
            strict_tool_scope=True,
        )
        assert [t.tool_id for t in admits] == ["schedule_trigger"]

        excludes = reg.build_for(
            ["read_file"],
            session_id="child",
            event_logger=None,
            strict_tool_scope=True,
        )
        assert [t.tool_id for t in excludes] == []

    def test_permissive_fe_root_still_arms(self, store):
        """A permissive FE root (mcp_tools, no schedule_trigger) still holds it.

        df875 regression guard: the Aura alarm flow arms a time trigger
        on a permissive root whose ``context.mcp_tools`` never lists built-ins.
        """
        from mewbo_core.session_tools import SessionToolRegistry
        from mewbo_core.triggers.session_tool import (
            register_schedule_trigger_provider,
            schedule_trigger_factory,
        )

        register_schedule_trigger_provider(store, TriggerPolicy())
        reg = SessionToolRegistry()
        reg.register(schedule_trigger_factory())

        tools = reg.build_for(
            [f"mcp__srv__t{i}" for i in range(139)],
            session_id="aura-root",
            event_logger=None,
            strict_tool_scope=False,
        )
        assert [t.tool_id for t in tools] == ["schedule_trigger"]
