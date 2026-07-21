"""A traced operation that fails must leave its cause on the Langfuse span.

The defect these cover: across a multi-day corpus every failure exported as an
ERROR-level span with an EMPTY ``status_message`` and no exception event at all,
so root cause below the client deadline was unrecoverable from a trace. The span
recorded THAT something failed, never WHAT.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core import components as comp_module
from mewbo_core.components import langfuse_trace_span, record_span_exception
from mewbo_core.config import AppConfig, reset_config, set_app_config_path


@pytest.fixture()
def enabled_langfuse(tmp_path):
    """Enable Langfuse and pin a trace context so spans are actually created."""
    cfg_path = tmp_path / "app.json"
    AppConfig.model_validate(
        {"langfuse": {"enabled": True, "public_key": "pk", "secret_key": "sk"}}
    ).write(cfg_path)
    reset_config()
    set_app_config_path(cfg_path)
    token = comp_module._LANGFUSE_TRACE_CONTEXT.set({"trace_id": "c" * 32})
    try:
        yield
    finally:
        comp_module._LANGFUSE_TRACE_CONTEXT.reset(token)
        reset_config()


def _fake_span() -> MagicMock:
    otel = MagicMock()
    otel.is_recording.return_value = True
    span = MagicMock()
    span._otel_span = otel
    return span


def _patched_client(span: MagicMock):
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=False)
    client = MagicMock()
    client.start_as_current_observation = MagicMock(return_value=cm)
    return patch("langfuse.get_client", return_value=client)


def _status_messages(span: MagicMock) -> list[str]:
    return [
        call.kwargs["status_message"]
        for call in span.update.call_args_list
        if "status_message" in call.kwargs
    ]


def test_failing_traced_operation_records_the_exception(enabled_langfuse):
    """The span carries ERROR level, a named cause, and an OTel exception event."""
    span = _fake_span()
    boom = RuntimeError("upstream returned 500")

    with _patched_client(span), pytest.raises(RuntimeError, match="upstream returned 500"):
        with langfuse_trace_span("step:0"):
            raise boom

    levels = [c.kwargs.get("level") for c in span.update.call_args_list]
    assert "ERROR" in levels
    assert _status_messages(span) == ["RuntimeError: upstream returned 500"]
    span._otel_span.record_exception.assert_called_once()
    assert span._otel_span.record_exception.call_args.args[0] is boom


def test_successful_traced_operation_records_nothing(enabled_langfuse):
    """The seam must stay silent when the body completes — no false ERROR spans."""
    span = _fake_span()

    with _patched_client(span):
        with langfuse_trace_span("step:0"):
            pass

    assert [c.kwargs.get("level") for c in span.update.call_args_list] == []
    span._otel_span.record_exception.assert_not_called()


def test_cancelled_operation_is_recorded(enabled_langfuse):
    """A deadline kill is a ``BaseException``; the wedged-call case must survive it."""
    span = _fake_span()

    async def _drive() -> None:
        with langfuse_trace_span("llm_call"):
            raise asyncio.CancelledError()

    with _patched_client(span), pytest.raises(asyncio.CancelledError):
        asyncio.run(_drive())

    assert _status_messages(span) == ["CancelledError"]
    span._otel_span.record_exception.assert_called_once()


def test_empty_exception_message_falls_back_to_the_type_name():
    """``str(TimeoutError())`` is empty; the status must still name something."""
    span = _fake_span()

    record_span_exception(span, TimeoutError())

    assert _status_messages(span) == ["TimeoutError"]


def test_status_message_is_bounded():
    """A provider that embeds a whole page in its message cannot flood the span."""
    span = _fake_span()

    record_span_exception(span, ValueError("x" * 5000))

    assert _status_messages(span) == [f"ValueError: {'x' * 5000}"[:500]]


def test_recording_is_graceful_when_the_span_rejects_updates():
    """A span object without ``update`` must not turn a failure into a crash."""
    span = MagicMock(spec=["_otel_span"])
    otel = MagicMock()
    otel.is_recording.return_value = True
    span._otel_span = otel

    record_span_exception(span, ValueError("boom"))

    otel.record_exception.assert_called_once()
