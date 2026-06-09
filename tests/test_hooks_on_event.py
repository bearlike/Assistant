"""Tests for the #48 ``on_event`` hook surface.

``on_event`` hooks run on the event-append hot path (via the SessionEventBus
observer), so they must be fire-and-forget / non-blocking and failure-isolated.
"""

from __future__ import annotations

from unittest.mock import patch

from mewbo_core.config import HookEntry, HooksConfig
from mewbo_core.hooks import (
    HookManager,
    _make_event_command_hook,
    _make_http_event_hook,
)
from mewbo_core.types import EventRecord


def _event(etype: str = "tool_result", text: str = "ok") -> EventRecord:
    return {"ts": "2026-06-07T00:00:00Z", "type": etype, "payload": {"text": text}}


# -- run_on_event firing + isolation ----------------------------------------


class TestRunOnEvent:
    def test_fires_every_hook(self):
        seen: list[tuple[str, str]] = []
        mgr = HookManager(
            on_event=[
                lambda sid, ev: seen.append((sid, ev["type"])),
                lambda sid, ev: seen.append((sid, "second")),
            ]
        )
        mgr.run_on_event("s1", _event())
        assert ("s1", "tool_result") in seen
        assert ("s1", "second") in seen

    def test_failing_hook_isolated(self):
        good: list[str] = []

        def bad(sid: str, ev: EventRecord) -> None:
            raise RuntimeError("on_event boom")

        mgr = HookManager(on_event=[bad, lambda sid, ev: good.append(sid)])
        # Must not raise; the surviving hook still fires.
        mgr.run_on_event("s1", _event())
        assert good == ["s1"]

    def test_no_hooks_is_noop(self):
        HookManager().run_on_event("s1", _event())  # must not raise


# -- Command factory (subprocess on a daemon thread, stdin JSON) ------------


class TestCommandEventHook:
    def test_matcher_filters_by_event_type(self):
        entry = HookEntry(type="command", command="true", matcher="tool_*")
        hook = _make_event_command_hook(entry)
        with patch("mewbo_core.hooks._run_event_command") as runner:
            hook("s1", _event("tool_result"))
            hook("s1", _event("user"))
        # Only the matching event type dispatches.
        assert runner.call_count == 1
        sid, ev, passed_entry = runner.call_args[0]
        assert sid == "s1"
        assert ev["type"] == "tool_result"
        assert passed_entry is entry

    def test_runs_on_daemon_thread(self):
        entry = HookEntry(type="command", command="true")
        hook = _make_event_command_hook(entry)
        with patch("mewbo_core.hooks.threading.Thread") as thread_cls:
            hook("s1", _event())
        thread_cls.assert_called_once()
        assert thread_cls.call_args.kwargs.get("daemon") is True


# -- HTTP factory (fire-and-forget POST) ------------------------------------


class TestHttpEventHook:
    def test_payload_shape(self):
        entry = HookEntry(type="http", url="http://hook.local/event")
        hook = _make_http_event_hook(entry)
        with patch("mewbo_core.hooks._fire_http") as fire:
            hook("s1", _event("compaction"))
        fire.assert_called_once()
        url, payload, headers, timeout = fire.call_args[0]
        assert url == "http://hook.local/event"
        assert payload == {
            "event": "session_event",
            "session_id": "s1",
            "record": _event("compaction"),
        }

    def test_matcher_filters(self):
        entry = HookEntry(type="http", url="http://hook.local/event", matcher="user")
        hook = _make_http_event_hook(entry)
        with patch("mewbo_core.hooks._fire_http") as fire:
            hook("s1", _event("tool_result"))
        fire.assert_not_called()


# -- load_from_config wires on_event by type --------------------------------


class TestLoadFromConfig:
    def test_wires_command_and_http(self):
        cfg = HooksConfig(
            on_event=[
                HookEntry(type="command", command="echo hi"),
                HookEntry(type="http", url="http://hook.local/event"),
            ]
        )
        mgr = HookManager.load_from_config(cfg)
        assert len(mgr.on_event) == 2
        # The wired callables fire-and-forget without raising.
        with (
            patch("mewbo_core.hooks._run_event_command"),
            patch("mewbo_core.hooks._fire_http"),
        ):
            for hook in mgr.on_event:
                hook("s1", _event())
