#!/usr/bin/env python3
"""Unit tests for ``mewbo_core.client_tools`` (Gitea #179, Phase 1).

Covers ``ClientToolSpec`` validation (tool_id pattern, extra-forbid,
non-object parameters), the ``DeviceToolDispatcher`` registration seam, and
``ClientDeclaredTool.handle`` with no / a fake registered dispatcher —
including verbatim schema construction and JSON passthrough of the
dispatcher's result (success and error status alike).
"""

from __future__ import annotations

import asyncio

import pytest
from mewbo_core.classes import ActionStep
from mewbo_core.client_tools import (
    ClientDeclaredTool,
    ClientToolSpec,
    DeviceToolDispatcher,
)
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    """Test isolation: snapshot/restore the process-wide dispatcher seam.

    ``DeviceToolDispatcher`` is a process-wide singleton (mirrors
    ``SearchLauncher``): another suite module (the api's startup wiring) may
    already have registered a real dispatcher before this module runs. A bare
    ``reset()`` with no restore would permanently blank that registration for
    every test collected afterward — the exact "global-state leak" full-suite
    ordering failure documented in ``tests/CLAUDE.md``. Save/restore instead.
    """
    previous = DeviceToolDispatcher._impl
    DeviceToolDispatcher.reset()
    yield
    DeviceToolDispatcher._impl = previous


def _spec(**overrides) -> ClientToolSpec:
    fields = {
        "tool_id": "device_send_sms",
        "description": "Send an SMS message.",
        "parameters": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "body"],
        },
    }
    fields.update(overrides)
    return ClientToolSpec(**fields)


class TestClientToolSpec:
    def test_valid_spec_round_trips(self):
        spec = _spec()
        assert spec.tool_id == "device_send_sms"
        assert spec.description == "Send an SMS message."
        assert spec.parameters["type"] == "object"

    @pytest.mark.parametrize(
        "bad_id",
        [
            "send_sms",  # missing device_ prefix
            "device_",  # empty suffix
            "device_Send",  # uppercase not allowed
            "device_" + "a" * 49,  # exceeds 48-char suffix cap
            "device-send-sms",  # hyphens not allowed
            "device_send sms",  # space not allowed
        ],
    )
    def test_rejects_bad_tool_id(self, bad_id):
        with pytest.raises(ValidationError):
            _spec(tool_id=bad_id)

    def test_accepts_max_length_tool_id(self):
        spec = _spec(tool_id="device_" + "a" * 48)
        assert spec.tool_id == "device_" + "a" * 48

    def test_rejects_empty_description(self):
        with pytest.raises(ValidationError):
            _spec(description="   ")

    def test_rejects_non_object_parameters_type(self):
        with pytest.raises(ValidationError):
            _spec(parameters={"type": "string"})

    def test_parameters_without_type_key_allowed(self):
        spec = _spec(parameters={"properties": {}})
        assert spec.parameters == {"properties": {}}

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            ClientToolSpec(
                tool_id="device_foo",
                description="foo",
                parameters={"type": "object"},
                extra_field="nope",
            )


class TestDeviceToolDispatcherSeam:
    def test_unregistered_by_default(self):
        assert DeviceToolDispatcher.available() is False

    def test_register_flips_available(self):
        class _FakeDispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                return {"status": "ok", "result": None}

        DeviceToolDispatcher.register(_FakeDispatcher())
        assert DeviceToolDispatcher.available() is True

    def test_reset_clears_registration(self):
        class _FakeDispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                return {"status": "ok", "result": None}

        DeviceToolDispatcher.register(_FakeDispatcher())
        DeviceToolDispatcher.reset()
        assert DeviceToolDispatcher.available() is False

    def test_dispatch_with_no_impl_returns_none(self):
        assert asyncio.run(DeviceToolDispatcher.dispatch("s1", "device_x", {})) is None


class TestClientDeclaredTool:
    def test_schema_built_verbatim(self):
        spec = _spec()
        tool = ClientDeclaredTool("sess-1", spec)
        assert tool.tool_id == "device_send_sms"
        assert tool.schema == {
            "type": "function",
            "function": {
                "name": "device_send_sms",
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }

    def test_never_terminates_run(self):
        tool = ClientDeclaredTool("sess-1", _spec())
        assert tool.should_terminate_run() is False

    def test_handle_without_dispatcher_returns_unavailable_envelope(self):
        tool = ClientDeclaredTool("sess-1", _spec())
        step = ActionStep(
            tool_id="device_send_sms",
            operation="execute",
            tool_input={"to": "1", "body": "hi"},
        )
        result = asyncio.run(tool.handle(step))
        # Matches the exact envelope shape `_session_tool_error_envelope`
        # (tool_use_loop.py) reclassifies as a failed step: str({"error": {...}}).
        assert result.content.startswith("{'error'")
        assert "device_tool_unavailable" in result.content

    def test_handle_with_registered_dispatcher_returns_result_json(self):
        calls = []

        class _FakeDispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                calls.append((session_id, tool_id, tool_input))
                return {"status": "ok", "result": {"sent": True}}

        DeviceToolDispatcher.register(_FakeDispatcher())
        tool = ClientDeclaredTool("sess-1", _spec())
        step = ActionStep(
            tool_id="device_send_sms",
            operation="execute",
            tool_input={"to": "1", "body": "hi"},
        )
        result = asyncio.run(tool.handle(step))
        assert result.content == '{"status": "ok", "result": {"sent": true}}'
        assert calls == [("sess-1", "device_send_sms", {"to": "1", "body": "hi"})]

    def test_handle_reshapes_dispatcher_error_status_into_error_envelope(self):
        """A dispatcher-reported error must fail the SAME way an unavailable
        dispatcher does — the shared ``{'error': {...}}`` envelope
        ``tool_use_loop._session_tool_error_envelope`` recognises — not ride
        through as opaque ``{"status": "error", ...}`` JSON the loop cannot
        see as a failure (it would record ``success=True``, losing the
        per-step failure nudge and doom-loop tracking)."""
        class _FakeErrDispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                return {"status": "error", "error": {"code": "denied", "message": "no"}}

        DeviceToolDispatcher.register(_FakeErrDispatcher())
        tool = ClientDeclaredTool("sess-1", _spec())
        step = ActionStep(tool_id="device_send_sms", operation="execute", tool_input={})
        result = asyncio.run(tool.handle(step))
        assert result.content.startswith("{'error'")
        assert result.content == str({"error": {"code": "denied", "message": "no"}})

    def test_handle_error_status_is_recognized_by_the_loop_envelope_detector(self):
        """Same assertion mechanism as ``test_tool_use_loop.py``'s envelope
        test: feed ``handle()``'s output straight into the loop's real
        detector and assert it recognizes a FAILED step (not just that our
        own string check thinks it looks right)."""
        from mewbo_core.tool_use_loop import _session_tool_error_envelope

        class _FakeTimeoutDispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                # The exact shape ApiDeviceToolDispatcher.dispatch returns on
                # timeout (mewbo_api/device_tools.py).
                return {
                    "status": "error",
                    "error": {
                        "code": "device_timeout",
                        "message": (
                            "No result received for device tool "
                            "'device_send_sms' within 30.0s."
                        ),
                    },
                }

        DeviceToolDispatcher.register(_FakeTimeoutDispatcher())
        tool = ClientDeclaredTool("sess-1", _spec())
        step = ActionStep(tool_id="device_send_sms", operation="execute", tool_input={})
        result = asyncio.run(tool.handle(step))
        assert _session_tool_error_envelope(result) == (
            "device_timeout: No result received for device tool 'device_send_sms' within 30.0s."
        )

    def test_handle_success_status_stays_verbatim_json(self):
        """Success results must NOT be reshaped — only ``status: "error"`` is."""
        from mewbo_core.tool_use_loop import _session_tool_error_envelope

        class _FakeOkDispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                return {"status": "ok", "result": {"sent": True}}

        DeviceToolDispatcher.register(_FakeOkDispatcher())
        tool = ClientDeclaredTool("sess-1", _spec())
        step = ActionStep(tool_id="device_send_sms", operation="execute", tool_input={})
        result = asyncio.run(tool.handle(step))
        assert result.content == '{"status": "ok", "result": {"sent": true}}'
        assert _session_tool_error_envelope(result) is None

    def test_handle_blank_error_fields_still_recognized_by_the_loop(self):
        """F3: an error status with EMPTY code and message must still be
        recognized as a failure — ``_session_tool_error_envelope`` treats a
        blank ``{'code': '', 'message': ''}`` pair as "not an error envelope"
        (both empty ⇒ None), so a naive passthrough would silently record
        success=True for a genuine dispatcher-reported failure. Reachable:
        the result route accepts ``{"status":"error","error":{}}``."""
        from mewbo_core.tool_use_loop import _session_tool_error_envelope

        class _FakeBlankErrDispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                return {"status": "error", "error": {}}

        DeviceToolDispatcher.register(_FakeBlankErrDispatcher())
        tool = ClientDeclaredTool("sess-1", _spec())
        step = ActionStep(tool_id="device_send_sms", operation="execute", tool_input={})
        result = asyncio.run(tool.handle(step))
        detected = _session_tool_error_envelope(result)
        assert detected is not None
        assert detected == "device_error: Device reported an unspecified error."

    def test_handle_coerces_non_dict_tool_input_to_empty_dict(self):
        class _FakeDispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                return {"status": "ok", "result": tool_input}

        DeviceToolDispatcher.register(_FakeDispatcher())
        tool = ClientDeclaredTool("sess-1", _spec())
        step = ActionStep(tool_id="device_send_sms", operation="execute", tool_input="not-a-dict")
        result = asyncio.run(tool.handle(step))
        assert result.content == '{"status": "ok", "result": {}}'
