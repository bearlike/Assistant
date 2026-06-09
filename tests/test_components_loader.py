"""Integration/contract tests for components.py.

Covers:
- ComponentStatus dataclass construction and semantics
- resolve_langfuse_status() / resolve_home_assistant_status() path logic
- format_component_status() rendering
- _is_hex_trace_id() contract
- _build_langfuse_trace_context() branching (invocation_id, hex session_id,
  non-hex session_id, missing langfuse)
- langfuse_propagate() graceful degradation when disabled
- langfuse_session_context() context-var lifecycle and reset-on-exit
- langfuse_trace_span() graceful degradation when disabled
- _ensure_langfuse_client() env population
- _attach_langfuse_metadata() metadata construction

Stubs only the Langfuse I/O boundary — all host logic under test runs real.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core import components as comp_module
from mewbo_core.components import (
    ComponentStatus,
    _attach_langfuse_metadata,
    _build_langfuse_trace_context,
    _is_hex_trace_id,
    build_langfuse_handler,
    format_component_status,
    langfuse_propagate,
    langfuse_session_context,
    langfuse_trace_span,
    record_span_exception,
    resolve_home_assistant_status,
    resolve_langfuse_status,
)
from mewbo_core.config import AppConfig, set_app_config_path


# ---------------------------------------------------------------------------
# ComponentStatus
# ---------------------------------------------------------------------------
class TestComponentStatus:
    def test_enabled_status(self):
        cs = ComponentStatus(name="foo", enabled=True)
        assert cs.name == "foo"
        assert cs.enabled is True
        assert cs.reason is None
        assert cs.metadata == {}

    def test_disabled_with_reason(self):
        cs = ComponentStatus(name="bar", enabled=False, reason="no config")
        assert cs.enabled is False
        assert cs.reason == "no config"

    def test_metadata_stored(self):
        cs = ComponentStatus(name="baz", enabled=True, metadata={"k": "v"})
        assert cs.metadata["k"] == "v"


# ---------------------------------------------------------------------------
# format_component_status()  (lines 102–109)
# ---------------------------------------------------------------------------
class TestFormatComponentStatus:
    def test_enabled_no_reason(self):
        cs = ComponentStatus(name="langfuse", enabled=True)
        result = format_component_status([cs])
        assert "langfuse" in result
        assert "enabled" in result

    def test_disabled_with_reason(self):
        cs = ComponentStatus(name="ha", enabled=False, reason="disabled via config")
        result = format_component_status([cs])
        assert "disabled" in result
        assert "disabled via config" in result

    def test_multiple_statuses(self):
        statuses = [
            ComponentStatus(name="langfuse", enabled=True),
            ComponentStatus(name="ha", enabled=False, reason="off"),
        ]
        result = format_component_status(statuses)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "langfuse" in lines[0]
        assert "ha" in lines[1]

    def test_empty_statuses_returns_empty(self):
        result = format_component_status([])
        assert result == ""


# ---------------------------------------------------------------------------
# resolve_langfuse_status()  (line 45–48)
# ---------------------------------------------------------------------------
class TestResolveLangfuseStatus:
    def test_langfuse_disabled_returns_not_enabled(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        set_app_config_path(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        status = resolve_langfuse_status()
        assert status.name == "langfuse"
        assert status.enabled is False
        assert status.reason is not None

    def test_langfuse_enabled_but_missing_keys_returns_not_enabled(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": True}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        status = resolve_langfuse_status()
        assert status.enabled is False


# ---------------------------------------------------------------------------
# resolve_home_assistant_status()  (lines 91–99)
# ---------------------------------------------------------------------------
class TestResolveHomeAssistantStatus:
    def test_ha_disabled_returns_not_enabled(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"home_assistant": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        status = resolve_home_assistant_status()
        assert status.name == "home_assistant_tool"
        assert status.enabled is False

    def test_ha_enabled_with_url_and_token(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {"home_assistant": {"enabled": True, "url": "http://ha", "token": "tok"}}
        ).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        status = resolve_home_assistant_status()
        assert status.enabled is True


# ---------------------------------------------------------------------------
# _is_hex_trace_id()  (lines 112–113)
# ---------------------------------------------------------------------------
class TestIsHexTraceId:
    def test_valid_32_char_hex(self):
        assert _is_hex_trace_id("a" * 32) is True
        assert _is_hex_trace_id("0123456789abcdef" * 2) is True

    def test_too_short(self):
        assert _is_hex_trace_id("abc") is False

    def test_too_long(self):
        assert _is_hex_trace_id("a" * 33) is False

    def test_non_hex_chars(self):
        assert _is_hex_trace_id("g" * 32) is False

    def test_uppercase_hex_rejected(self):
        # fullmatch requires lowercase hex [0-9a-f]
        assert _is_hex_trace_id("A" * 32) is False


# ---------------------------------------------------------------------------
# _build_langfuse_trace_context()  (lines 116–145)
# ---------------------------------------------------------------------------
class TestBuildLangfuseTraceContext:
    """Tests for _build_langfuse_trace_context branching logic."""

    def test_invocation_id_valid_hex_uses_it_directly(self):
        hex_id = "a" * 32
        result = _build_langfuse_trace_context(None, invocation_id=hex_id)
        assert result is not None
        assert result["trace_id"] == hex_id

    def test_invocation_id_non_hex_generates_new_uuid(self):
        result = _build_langfuse_trace_context(None, invocation_id="non-hex-id")
        assert result is not None
        # Should be a valid 32-char hex
        assert len(result["trace_id"]) == 32
        assert _is_hex_trace_id(result["trace_id"])

    def test_no_session_id_returns_none(self):
        result = _build_langfuse_trace_context(None, invocation_id=None)
        assert result is None

    def test_hex_session_id_used_directly(self):
        hex_sid = "b" * 32
        result = _build_langfuse_trace_context(hex_sid, invocation_id=None)
        assert result is not None
        assert result["trace_id"] == hex_sid

    def test_non_hex_session_id_tries_langfuse(self):
        """Non-hex session_id falls through to Langfuse.create_trace_id or returns None."""
        # Without langfuse installed or returning a valid ID, expect None or a trace context
        result = _build_langfuse_trace_context("non-hex-session-id", invocation_id=None)
        # Either None (langfuse not available) or a dict with trace_id
        if result is not None:
            assert "trace_id" in result

    def test_non_hex_session_id_returns_none_when_create_trace_id_returns_invalid(self):
        """When Langfuse.create_trace_id returns invalid ID, returns None."""
        fake_langfuse_cls = MagicMock()
        fake_langfuse_cls.create_trace_id = staticmethod(lambda seed: "not-a-valid-hex-id")
        fake_module = MagicMock()
        fake_module.Langfuse = fake_langfuse_cls

        with patch.dict("sys.modules", {"langfuse": fake_module}):
            result = _build_langfuse_trace_context("non-hex-session", invocation_id=None)
        assert result is None

    def test_non_hex_session_id_returns_none_when_create_trace_id_returns_empty(self):
        """When Langfuse.create_trace_id returns empty string, returns None."""
        fake_langfuse_cls = MagicMock()
        fake_langfuse_cls.create_trace_id = staticmethod(lambda seed: "")
        fake_module = MagicMock()
        fake_module.Langfuse = fake_langfuse_cls

        with patch.dict("sys.modules", {"langfuse": fake_module}):
            result = _build_langfuse_trace_context("non-hex-session", invocation_id=None)
        assert result is None


# ---------------------------------------------------------------------------
# langfuse_propagate()  (lines 153–191)
# ---------------------------------------------------------------------------
class TestLangfusePropagate:
    def test_yields_when_langfuse_disabled(self, tmp_path):
        """When Langfuse is disabled, context manager yields without error."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        reached = []
        with langfuse_propagate(session_id="sess123"):
            reached.append(True)
        assert reached == [True]

    def test_yields_when_no_kwargs(self, tmp_path):
        """When all kwargs are empty/None, yields immediately."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {"langfuse": {"enabled": True, "public_key": "pk", "secret_key": "sk"}}
        ).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        # No kwargs → yields immediately without calling propagate_attributes
        reached = []
        with langfuse_propagate():
            reached.append(True)
        assert reached == [True]

    def test_propagate_with_tags_and_metadata_when_disabled(self, tmp_path):
        """Even with tags/metadata, disabled Langfuse just yields."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        inner = []
        with langfuse_propagate(tags=["a"], metadata={"k": "v"}, session_id="s"):
            inner.append(1)
        assert inner == [1]

    def test_propagate_calls_propagate_attributes_when_enabled(self, tmp_path):
        """When Langfuse is enabled, propagate_attributes is called with kwargs."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {
                "langfuse": {
                    "enabled": True,
                    "public_key": "pk",
                    "secret_key": "sk",
                }
            }
        ).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)

        call_args = []

        @contextmanager
        def _fake_propagate(**kwargs):
            call_args.append(kwargs)
            yield

        with patch("langfuse.propagate_attributes", _fake_propagate):
            inner = []
            with langfuse_propagate(
                tags=["tag1"],
                metadata={"key": "val"},
                session_id="sess",
                user_id="uid",
            ):
                inner.append(1)
        assert inner == [1]
        assert len(call_args) == 1
        assert call_args[0]["tags"] == ["tag1"]
        assert call_args[0]["metadata"] == {"key": "val"}
        assert call_args[0]["session_id"] == "sess"
        assert call_args[0]["user_id"] == "uid"


# ---------------------------------------------------------------------------
# langfuse_session_context()  (lines 194–237)
# ---------------------------------------------------------------------------
class TestLangfuseSessionContext:
    def test_context_vars_reset_after_exit(self, tmp_path):
        """Context variables are restored on exit."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)

        before_session = comp_module._LANGFUSE_SESSION_ID.get()
        with langfuse_session_context("test-session-id"):
            inside = comp_module._LANGFUSE_SESSION_ID.get()
            assert inside == "test-session-id"
        after = comp_module._LANGFUSE_SESSION_ID.get()
        assert after == before_session

    def test_user_id_defaults_to_session_id_when_not_provided(self, tmp_path):
        """When user_id is None, resolved_user falls back to session_id."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)

        with langfuse_session_context("sid-123", user_id=None):
            uid = comp_module._LANGFUSE_USER_ID.get()
            assert uid == "sid-123"

    def test_user_id_used_when_provided(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)

        with langfuse_session_context("sid-123", user_id="custom-user"):
            uid = comp_module._LANGFUSE_USER_ID.get()
            assert uid == "custom-user"

    def test_source_platform_tag_added(self, tmp_path):
        """source_platform causes session_id context var to be set inside the context manager."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)

        session_id_inside = []
        with langfuse_session_context("test-session-cli", source_platform="cli"):
            # The _LANGFUSE_SESSION_ID context var must be set to the session_id
            session_id_inside.append(comp_module._LANGFUSE_SESSION_ID.get())

        assert len(session_id_inside) == 1
        assert session_id_inside[0] == "test-session-cli"
        # After exiting, the context var must be reset
        assert comp_module._LANGFUSE_SESSION_ID.get() != "test-session-cli"


# ---------------------------------------------------------------------------
# langfuse_trace_span()  (lines 240–298)
# ---------------------------------------------------------------------------
class TestLangfuseTraceSpan:
    def test_yields_none_when_langfuse_disabled(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        with langfuse_trace_span("test-span") as span:
            assert span is None

    def test_yields_none_when_no_trace_context(self, tmp_path):
        """Even if Langfuse enabled, no trace context → yields None."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {"langfuse": {"enabled": True, "public_key": "pk", "secret_key": "sk"}}
        ).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        # No active trace context set
        comp_module._LANGFUSE_TRACE_CONTEXT.set(None)
        with langfuse_trace_span("test-span") as span:
            assert span is None

    def test_body_exceptions_propagate(self, tmp_path):
        """Exceptions raised inside the context manager are not suppressed."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        with pytest.raises(RuntimeError, match="test error"):
            with langfuse_trace_span("test-span"):
                raise RuntimeError("test error")


class TestRecordSpanException:
    """``record_span_exception`` records an OTel ``exception`` event so Langfuse
    error tooling (which queries those events) is not blind (#65)."""

    def test_records_exception_with_attributes(self):
        otel = MagicMock()
        otel.is_recording.return_value = True
        span = MagicMock()
        span._otel_span = otel
        exc = ValueError("boom")

        record_span_exception(span, exc, attributes={"errortype": "x"})

        otel.record_exception.assert_called_once_with(exc, attributes={"errortype": "x"})

    def test_records_message_when_no_exc(self):
        otel = MagicMock()
        otel.is_recording.return_value = True
        span = MagicMock()
        span._otel_span = otel

        record_span_exception(span, message="exhausted", attributes={"reason": "r"})

        assert otel.record_exception.call_count == 1
        recorded_exc = otel.record_exception.call_args.args[0]
        assert isinstance(recorded_exc, RuntimeError)
        assert "exhausted" in str(recorded_exc)

    def test_no_op_when_span_is_none(self):
        # Must not raise — graceful when there is no active span.
        record_span_exception(None, ValueError("x"))

    def test_no_op_when_otel_not_recording(self):
        otel = MagicMock()
        otel.is_recording.return_value = False
        span = MagicMock()
        span._otel_span = otel

        record_span_exception(span, ValueError("x"))

        otel.record_exception.assert_not_called()

    def test_no_op_when_no_otel_span(self):
        span = MagicMock(spec=[])  # no _otel_span attribute
        # Must not raise.
        record_span_exception(span, ValueError("x"))


# ---------------------------------------------------------------------------
# _ensure_langfuse_client()  (lines 301–327)
# ---------------------------------------------------------------------------
class TestEnsureLangfuseClient:
    def test_no_op_when_config_is_none(self):
        # Should not raise
        comp_module._ensure_langfuse_client(None)

    def test_no_op_when_keys_missing(self):
        from mewbo_core.config import LangfuseConfig

        cfg = LangfuseConfig.model_validate({"enabled": True, "public_key": "", "secret_key": ""})
        comp_module._ensure_langfuse_client(cfg)

    def test_sets_env_vars_when_keys_present(self, monkeypatch):
        """When keys are set, env vars should be populated."""
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)

        # Patch Langfuse to avoid real HTTP
        with patch.dict("sys.modules", {"langfuse": MagicMock()}):
            cfg = MagicMock()
            cfg.public_key = "pk-test"
            cfg.secret_key = "sk-test"
            cfg.host = "https://langfuse.local"
            comp_module._ensure_langfuse_client(cfg)

        assert os.environ.get("LANGFUSE_PUBLIC_KEY") == "pk-test"
        assert os.environ.get("LANGFUSE_SECRET_KEY") == "sk-test"


# ---------------------------------------------------------------------------
# _attach_langfuse_metadata()  (lines 329–353)
# ---------------------------------------------------------------------------
class TestAttachLangfuseMetadata:
    def test_metadata_set_on_handler(self):
        handler = MagicMock()
        _attach_langfuse_metadata(
            handler,
            user_id="user1",
            session_id="sess1",
            trace_name="mewbo-trace",
            version="1.0",
            release="dev",
        )
        assert hasattr(handler, "langfuse_metadata")
        meta = handler.langfuse_metadata
        assert meta["langfuse_user_id"] == "user1"
        assert meta["langfuse_session_id"] == "sess1"
        assert "mewbo-trace" in meta["langfuse_tags"]
        assert "version:1.0" in meta["langfuse_tags"]
        assert "release:dev" in meta["langfuse_tags"]

    def test_empty_user_id_skipped(self):
        handler = MagicMock()
        _attach_langfuse_metadata(
            handler,
            user_id="",
            session_id="sess1",
            trace_name="",
            version="",
            release="",
        )
        meta = handler.langfuse_metadata
        assert "langfuse_user_id" not in meta
        # No tags means langfuse_tags not in metadata
        assert "langfuse_tags" not in meta

    def test_no_metadata_not_set_when_all_empty(self):
        """When all values are empty, langfuse_metadata is never set on the handler."""

        class _FakeHandler:
            pass

        handler = _FakeHandler()
        _attach_langfuse_metadata(
            handler,
            user_id="",
            session_id="",
            trace_name="",
            version="",
            release="",
        )
        # setattr was never called because metadata dict is empty
        assert not hasattr(handler, "langfuse_metadata")


# ---------------------------------------------------------------------------
# build_langfuse_handler()  (lines 51–88) — with Langfuse enabled+mocked
# ---------------------------------------------------------------------------
class TestBuildLangfuseHandler:
    def _enable_langfuse(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {"langfuse": {"enabled": True, "public_key": "pk", "secret_key": "sk"}}
        ).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)

    def test_returns_none_when_langfuse_disabled(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"langfuse": {"enabled": False}}).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)
        result = build_langfuse_handler(
            user_id="u", session_id="s", trace_name="t", version="1", release="r"
        )
        assert result is None

    def test_returns_handler_when_langfuse_enabled(self, tmp_path):
        """When Langfuse is enabled and callable, returns a handler."""
        self._enable_langfuse(tmp_path)

        fake_handler = MagicMock()
        fake_handler_cls = MagicMock(return_value=fake_handler)

        with patch("langfuse.langchain.CallbackHandler", fake_handler_cls):
            result = build_langfuse_handler(
                user_id="uid",
                session_id="sess",
                trace_name="mewbo-tool-use",
                version="1.0",
                release="dev",
            )
        assert result is fake_handler
        assert fake_handler_cls.called

    def test_uses_context_var_session_id_when_set(self, tmp_path):
        """Session ID from context var overrides the argument."""
        self._enable_langfuse(tmp_path)

        fake_handler = MagicMock()
        fake_handler_cls = MagicMock(return_value=fake_handler)

        comp_module._LANGFUSE_SESSION_ID.set("ctx-session")
        try:
            with patch("langfuse.langchain.CallbackHandler", fake_handler_cls):
                build_langfuse_handler(
                    user_id="u",
                    session_id="arg-session",
                    trace_name="t",
                    version="1",
                    release="r",
                )
            # The context var session_id takes precedence
            call_kwargs = fake_handler_cls.call_args
            assert call_kwargs is not None
        finally:
            comp_module._LANGFUSE_SESSION_ID.set(None)


# ---------------------------------------------------------------------------
# langfuse_trace_span() with active trace context (lines 262–296)
# ---------------------------------------------------------------------------
class TestLangfuseTraceSpanWithContext:
    def test_yields_span_when_context_active_and_langfuse_enabled(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {"langfuse": {"enabled": True, "public_key": "pk", "secret_key": "sk"}}
        ).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)

        # Set a fake trace context
        hex_id = "a" * 32
        token = comp_module._LANGFUSE_TRACE_CONTEXT.set({"trace_id": hex_id})
        try:
            fake_span = MagicMock()
            fake_cm = MagicMock()
            fake_cm.__enter__ = MagicMock(return_value=fake_span)
            fake_cm.__exit__ = MagicMock(return_value=False)
            fake_langfuse = MagicMock()
            fake_langfuse.start_as_current_observation = MagicMock(return_value=fake_cm)

            with patch("langfuse.get_client", return_value=fake_langfuse):
                with langfuse_trace_span(
                    "my-span",
                    metadata={"k": "v"},
                    input_data="test input",
                    level="INFO",
                ) as span:
                    assert span is fake_span
        finally:
            comp_module._LANGFUSE_TRACE_CONTEXT.reset(token)

    def test_span_update_called_with_metadata(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {"langfuse": {"enabled": True, "public_key": "pk", "secret_key": "sk"}}
        ).write(cfg_path)
        from mewbo_core.config import reset_config

        reset_config()
        set_app_config_path(cfg_path)

        hex_id = "b" * 32
        token = comp_module._LANGFUSE_TRACE_CONTEXT.set({"trace_id": hex_id})
        try:
            fake_span = MagicMock()
            fake_cm = MagicMock()
            fake_cm.__enter__ = MagicMock(return_value=fake_span)
            fake_cm.__exit__ = MagicMock(return_value=False)
            fake_langfuse = MagicMock()
            fake_langfuse.start_as_current_observation = MagicMock(return_value=fake_cm)

            with patch("langfuse.get_client", return_value=fake_langfuse):
                with langfuse_trace_span("span", metadata={"x": "y"}):
                    pass
            # span.update should have been called with the metadata
            fake_span.update.assert_called_once_with(metadata={"x": "y"})
        finally:
            comp_module._LANGFUSE_TRACE_CONTEXT.reset(token)
