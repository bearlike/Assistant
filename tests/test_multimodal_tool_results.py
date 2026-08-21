#!/usr/bin/env python3
"""Multimodal tool results and image history, neither needing a device.

Two seams:

- an image reaching the model INSIDE a tool result, and the ordinary string
  path staying byte-identical to what it was before that seam existed — the
  regression half matters as much as the feature half, since every tool in the
  engine crosses this code;
- taking stale images back OUT at compaction time, leaving a placeholder that
  says the image can be requested again.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import AgentHypervisor
from mewbo_core.common import MockSpeaker
from mewbo_core.loop.multimodal import (
    IMAGE_STRIPPED_PLACEHOLDER,
    ImageHistoryStrip,
    ToolResultContent,
)
from mewbo_core.loop.tool_use_loop import ToolCallResult, ToolUseLoop, _tool_message_content
from mewbo_core.tooling.client_tools import ClientDeclaredTool, ClientToolSpec
from test_tool_use_loop import _allow_all_policy, _make_hook_manager, _make_registry


@pytest.fixture(autouse=True)
def _restore_dispatcher():
    """Snapshot/restore the process-wide dispatcher seam, as ``test_client_tools`` does.

    The tests below register a fake and ``reset()`` it in a ``finally``. A bare
    reset does not restore what was there BEFORE — and the api's startup wiring
    registers a real dispatcher at import, during collection — so it blanked that
    registration for every test collected afterward. The api's own
    "a dispatcher is registered at startup" assertion then failed in a full run
    and passed in isolation: the global-state leak `tests/CLAUDE.md` describes.
    """
    from mewbo_core.tooling.client_tools import DeviceToolDispatcher

    saved = DeviceToolDispatcher._impl
    yield
    DeviceToolDispatcher._impl = saved


def _image_part(data: str = "AAAA") -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


class _Message:
    """Stand-in for a langchain message — only ``content`` is read."""

    def __init__(self, content):
        self.content = content


class TestToolResultContent:
    def test_a_plain_string_carries_no_images(self):
        parsed = ToolResultContent.parse("ok")
        assert parsed.text == "ok"
        assert parsed.images == ()
        assert not parsed.has_images

    def test_parts_split_into_text_and_images(self):
        parsed = ToolResultContent.parse(
            [{"type": "text", "text": "1440x3120"}, _image_part()]
        )
        assert parsed.text == "1440x3120"
        assert len(parsed.images) == 1
        assert parsed.has_images

    def test_for_model_returns_a_bare_string_when_there_is_no_image(self):
        # The cache-prefix property: an ordinary result must not become a list
        # merely because the multimodal seam exists.
        assert ToolResultContent.parse("ok").for_model("ok") == "ok"

    def test_for_model_puts_text_first_then_the_image(self):
        parsed = ToolResultContent.parse([{"type": "text", "text": "x"}, _image_part()])
        parts = parsed.for_model("capped text")
        assert isinstance(parts, list)
        assert parts[0] == {"type": "text", "text": "capped text"}
        assert parts[1]["type"] == "image_url"


class TestToolMessageContent:
    def test_a_result_without_images_stays_a_string(self):
        result = ToolCallResult(
            tool_call_id="t1", tool_id="shell", content="hello", success=True
        )
        assert _tool_message_content(result) == "hello"

    def test_a_result_with_images_becomes_text_then_image_parts(self):
        result = ToolCallResult(
            tool_call_id="t1",
            tool_id="device_ui",
            content='{"width": 1440}',
            success=True,
            images=(_image_part(),),
        )
        content = _tool_message_content(result)
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": '{"width": 1440}'}
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


class TestClientDeclaredToolImages:
    """The client's base64 must be LIFTED out of the JSON, not left in it."""

    @staticmethod
    def _tool() -> ClientDeclaredTool:
        return ClientDeclaredTool(
            "s1",
            ClientToolSpec(
                tool_id="device_ui",
                description="Observe the screen.",
                parameters={"type": "object"},
            ),
        )

    def test_an_image_is_lifted_out_of_the_result_json(self):
        from mewbo_core.classes import ActionStep
        from mewbo_core.tooling import client_tools

        payload = {
            "status": "ok",
            "result": {
                "width": 1440,
                "image_base64": "QUJD",
                "image_media_type": "image/jpeg",
            },
        }

        class _Dispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                return payload

        client_tools.DeviceToolDispatcher.register(_Dispatcher())
        try:
            speaker = asyncio.run(
                self._tool().handle(
                    ActionStep(tool_id="device_ui", operation="get", tool_input={})
                )
            )
        finally:
            client_tools.DeviceToolDispatcher.reset()

        # The base64 is GONE from the text half — otherwise every screenshot is
        # paid for twice and lands in the persisted event snapshot.
        assert "QUJD" not in speaker.content
        assert json.loads(speaker.content)["result"] == {"width": 1440}
        assert len(speaker.images) == 1
        assert speaker.images[0]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"

    def test_a_result_with_no_image_is_unchanged_json_and_no_images(self):
        from mewbo_core.classes import ActionStep
        from mewbo_core.tooling import client_tools

        class _Dispatcher:
            async def dispatch(self, session_id, tool_id, tool_input):
                return {"status": "ok", "result": {"elements": []}}

        client_tools.DeviceToolDispatcher.register(_Dispatcher())
        try:
            speaker = asyncio.run(
                self._tool().handle(
                    ActionStep(tool_id="device_ui", operation="get", tool_input={})
                )
            )
        finally:
            client_tools.DeviceToolDispatcher.reset()

        assert speaker.images == ()
        assert json.loads(speaker.content)["result"] == {"elements": []}

    def test_the_bridge_declares_a_shell_class_result_cap(self):
        # Undeclared, a session tool falls to the 2000-char registry default —
        # which would bind on ordinary dumpsys output, each bind costing a
        # network round trip to the device.
        assert ClientDeclaredTool.max_result_chars == 30_000

    def test_the_bridge_declares_the_execute_capability_tier(self):
        # What keeps a device tool out of a read_only sub-agent.
        assert ClientDeclaredTool.capability == "execute"


class TestImageHistoryStrip:
    """Compaction-time only, newest survives, older become a re-request hint."""

    def test_the_newest_image_survives_and_older_ones_become_a_placeholder(self):
        messages = [
            _Message([{"type": "text", "text": "step 1"}, _image_part("one")]),
            _Message([{"type": "text", "text": "step 2"}, _image_part("two")]),
            _Message([{"type": "text", "text": "step 3"}, _image_part("three")]),
        ]

        stripped = ImageHistoryStrip().strip(messages)

        assert stripped == 2
        assert messages[0].content[1] == {
            "type": "text",
            "text": IMAGE_STRIPPED_PLACEHOLDER,
        }
        assert messages[1].content[1]["type"] == "text"
        # The newest is what a run still driving the screen needs.
        assert messages[2].content[1]["type"] == "image_url"

    def test_the_placeholder_tells_the_model_the_image_is_re_requestable(self):
        # A bare "[Image Omitted]" says the image is gone; it does not say the
        # model may ask again. For a screen that has since moved on, asking
        # again is the ONLY correct recovery.
        assert "again" in IMAGE_STRIPPED_PLACEHOLDER.lower()

    def test_surrounding_text_and_string_messages_are_untouched(self):
        messages = [
            _Message([{"type": "text", "text": "keep me"}, _image_part("a")]),
            _Message("plain string, no image"),
            _Message([{"type": "text", "text": "newest"}, _image_part("b")]),
        ]

        ImageHistoryStrip().strip(messages)

        assert messages[0].content[0] == {"type": "text", "text": "keep me"}
        assert messages[1].content == "plain string, no image"

    def test_a_single_image_is_never_stripped(self):
        messages = [_Message([{"type": "text", "text": "x"}, _image_part()])]
        assert ImageHistoryStrip().strip(messages) == 0
        assert messages[0].content[1]["type"] == "image_url"

    def test_a_string_only_transcript_is_a_no_op(self):
        messages = [_Message("one"), _Message("two")]
        assert ImageHistoryStrip().strip(messages) == 0


class TestMockSpeakerRemainsBackwardCompatible:
    def test_content_only_construction_still_works_and_has_no_images(self):
        # ~100 call sites construct it positionally with a bare string.
        speaker = MockSpeaker(content="done")
        assert speaker.content == "done"
        assert speaker.images == ()


class TestDeviceToolsRespectCapabilityMode:
    """``extra_session_tools`` is appended AFTER ``build_for``'s gates.

    That append was unconditional, so every privilege ceiling — ``allowed_tools``,
    ``session_capabilities``, ``strict_tool_scope`` and ``capability_mode`` —
    missed the one tool population a client controls. Tolerable while the
    surface was ``setAlarm``; not once it includes a shell at shell UID, which
    a ``read_only`` sub-agent would have been handed.
    """

    @staticmethod
    def _device_tool() -> ClientDeclaredTool:
        return ClientDeclaredTool(
            "s1",
            ClientToolSpec(
                tool_id="device_shell",
                description="Run a shell command at shell UID.",
                parameters={"type": "object"},
            ),
        )

    def _loop_with_mode(self, mode: str) -> ToolUseLoop:
        # Built through the real seam: AgentContext is frozen, and
        # ``capability_mode`` narrows monotonically from the root.
        ctx = AgentContext.root(
            model_name="test-model",
            registry=AgentHypervisor(max_concurrent=4),
            capability_mode=mode,
        )
        return ToolUseLoop(
            agent_context=ctx,
            tool_registry=_make_registry(),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            extra_session_tools=[self._device_tool()],
        )

    def test_a_read_only_agent_is_not_handed_a_device_tool(self):
        loop = self._loop_with_mode("read_only")
        assert [t.tool_id for t in loop._session_tools] == []

    def test_an_execute_agent_still_gets_it(self):
        # The paired positive: without this, the test above would pass just as
        # well against a build_for that returned nothing at all.
        loop = self._loop_with_mode("execute")
        assert [t.tool_id for t in loop._session_tools] == ["device_shell"]

    def test_the_root_default_all_is_unaffected(self):
        loop = self._loop_with_mode("all")
        assert [t.tool_id for t in loop._session_tools] == ["device_shell"]
