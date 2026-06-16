"""Tests for the anyio/ExceptionGroup unwrap + classify helpers (Gitea #132)."""

from __future__ import annotations

import socket
import sys

import pytest
from mewbo_tools.integration.exception_unwrap import (
    REASON_AUTH,
    REASON_CONFIG,
    REASON_DNS,
    REASON_OTHER,
    REASON_REFUSED,
    REASON_TIMEOUT,
    classify_connect_failure,
    describe_exception_group,
    unwrap_exception_group,
)


class _DuckGroup(Exception):
    """Minimal ExceptionGroup-like: carries an ``exceptions`` tuple.

    Mirrors anyio's group duck-typing so the helpers are exercised without
    depending on the builtin ``ExceptionGroup`` (absent on Python 3.10).
    """

    def __init__(self, message: str, exceptions: list[Exception]) -> None:
        super().__init__(message)
        self.exceptions = tuple(exceptions)


def _make_group(message: str, excs: list[Exception]) -> Exception:
    """Build a real ``ExceptionGroup`` on 3.11+, else the duck-typed stand-in."""
    if sys.version_info >= (3, 11):
        return ExceptionGroup(message, excs)  # noqa: F821 - 3.11+ builtin
    return _DuckGroup(message, excs)


class TestUnwrapExceptionGroup:
    def test_plain_exception_returned_as_is(self):
        exc = ValueError("boom")
        assert unwrap_exception_group(exc) is exc

    def test_single_child_group_peeled(self):
        inner = ConnectionRefusedError("[Errno 111] Connection refused")
        assert unwrap_exception_group(_make_group("tg", [inner])) is inner

    def test_nested_single_child_groups_peeled_to_innermost(self):
        inner = OSError("[Errno -2] failed to resolve host 'postgres'")
        nested = _make_group("outer", [_make_group("inner", [inner])])
        assert unwrap_exception_group(nested) is inner

    def test_multi_child_group_returned_unchanged(self):
        group = _make_group("tg", [ValueError("a"), KeyError("b")])
        assert unwrap_exception_group(group) is group

    def test_duck_typed_group_is_unwrapped(self):
        inner = RuntimeError("real cause")
        assert unwrap_exception_group(_DuckGroup("tg", [inner])) is inner

    def test_self_referential_group_does_not_loop(self):
        group = _DuckGroup("loop", [])
        group.exceptions = (group,)
        # Should terminate (cycle guard) and return the group itself.
        assert unwrap_exception_group(group) is group


class TestDescribeExceptionGroup:
    def test_single_child_uses_inner_message(self):
        inner = OSError("[Errno -2] failed to resolve host 'postgres'")
        msg = describe_exception_group(_make_group("tg (1 sub-exception)", [inner]))
        assert msg == "[Errno -2] failed to resolve host 'postgres'"
        assert "sub-exception" not in msg

    def test_multi_child_joins_reprs(self):
        group = _make_group("tg", [ValueError("a"), KeyError("b")])
        msg = describe_exception_group(group)
        assert "ValueError('a')" in msg
        assert "KeyError('b')" in msg
        assert ";" in msg

    def test_message_less_exception_falls_back_to_repr(self):
        msg = describe_exception_group(_make_group("tg", [ValueError()]))
        assert msg == "ValueError()"

    def test_plain_exception_str(self):
        assert describe_exception_group(RuntimeError("nope")) == "nope"


class TestClassifyFailureReason:
    def test_config_kwarg_error(self):
        exc = TypeError("__init__() got an unexpected keyword argument 'oauth'")
        assert classify_connect_failure(exc) == REASON_CONFIG

    def test_dns_via_gaierror_type(self):
        exc = socket.gaierror(-2, "Name or service not known")
        assert classify_connect_failure(exc) == REASON_DNS

    def test_dns_via_message_through_group(self):
        inner = OSError("[Errno -2] failed to resolve host 'postgres'")
        assert classify_connect_failure(_make_group("tg", [inner])) == REASON_DNS

    def test_connection_refused(self):
        exc = ConnectionRefusedError("[Errno 111] Connection refused")
        assert classify_connect_failure(exc) == REASON_REFUSED

    def test_timeout_type(self):
        assert classify_connect_failure(TimeoutError()) == REASON_TIMEOUT

    def test_timeout_message(self):
        assert classify_connect_failure(RuntimeError("operation timed out")) == REASON_TIMEOUT

    @pytest.mark.parametrize(
        "message",
        ["401 Unauthorized", "403 Forbidden", "invalid api key", "permission denied"],
    )
    def test_auth(self, message):
        assert classify_connect_failure(RuntimeError(message)) == REASON_AUTH

    def test_other_fallback(self):
        assert classify_connect_failure(ValueError("something weird")) == REASON_OTHER

    def test_refused_wins_over_generic_connection_error(self):
        # ConnectionRefusedError is a ConnectionError subclass; refused must win.
        exc = ConnectionRefusedError("Connection refused")
        assert classify_connect_failure(exc) == REASON_REFUSED
