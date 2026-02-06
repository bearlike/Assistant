"""Tests for model configuration helpers."""

import sys
import types

from meeseeks_core import llm as llm_module
from meeseeks_core.config import set_config_override
from meeseeks_core.llm import (
    build_chat_model,
    model_supports_reasoning_effort,
    resolve_reasoning_effort,
)


def test_resolve_reasoning_effort_defaults(monkeypatch):
    """Default to medium for GPT-5 family models."""
    set_config_override({"llm": {"reasoning_effort": "", "reasoning_effort_models": []}})
    assert resolve_reasoning_effort("gpt-5.2") == "medium"
    assert resolve_reasoning_effort("gpt-5.1") == "medium"


def test_resolve_reasoning_effort_gpt5_pro(monkeypatch):
    """Use high reasoning effort for GPT-5 pro."""
    set_config_override({"llm": {"reasoning_effort": ""}})
    assert resolve_reasoning_effort("gpt-5-pro") == "high"


def test_build_chat_model_includes_reasoning_effort(monkeypatch):
    """Attach reasoning_effort to model kwargs when supported."""
    set_config_override({"llm": {"reasoning_effort": "", "reasoning_effort_models": []}})
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="gpt-5.2", openai_api_base=None)
    model_kwargs = captured.get("model_kwargs") or {}
    assert model_kwargs.get("reasoning_effort") == "medium"
    assert "temperature" not in captured


def test_build_chat_model_prefixes_openai_model(monkeypatch):
    """Prefix OpenAI-compatible models when a base URL is provided."""
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="gpt-4o", openai_api_base="http://host/v1")
    assert captured["model"] == "openai/gpt-4o"
    assert captured["api_base"] == "http://host/v1"


def test_build_chat_model_passes_api_key(monkeypatch):
    """Pass api_key through when provided."""
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="gpt-4o", openai_api_base=None, api_key="key")
    assert captured["api_key"] == "key"


def test_parse_model_list_config_list():
    """Parse model allowlists from list values."""
    assert llm_module._normalize_model_list(["Foo", "bar"]) == [
        "foo",
        "bar",
    ]


def test_parse_model_list_empty():
    """Return empty list for blank values."""
    assert llm_module._normalize_model_list("   ") == []
    assert llm_module._normalize_model_list("foo, Bar") == [
        "foo",
        "bar",
    ]


def test_parse_model_list_none():
    """Handle None values gracefully."""
    assert llm_module._normalize_model_list(None) == []
    assert llm_module._normalize_model_list({"model": "gpt-5"}) == []


def test_strip_provider_handles_none():
    """Return empty string when model name is missing."""
    assert llm_module._strip_provider(None) == ""


def test_matches_model_list_wildcard():
    """Match model allowlist entries including wildcard suffixes."""
    assert llm_module._matches_model_list("gpt-4o", ["gpt-4*"]) is True
    assert llm_module._matches_model_list("gpt-4o", ["gpt-3*"]) is False
    assert llm_module._matches_model_list("gpt-4o", ["gpt-4o"]) is True


def test_model_supports_reasoning_effort_allowlist(monkeypatch):
    """Respect explicit allowlists for non-GPT-5 models."""
    set_config_override({"llm": {"reasoning_effort_models": ["custom*"]}})
    assert llm_module.model_supports_reasoning_effort("custom-model") is True
    assert llm_module.model_supports_reasoning_effort("other") is False


def test_model_supports_reasoning_effort_without_name():
    """Return False when no model name is provided."""
    assert model_supports_reasoning_effort(None) is False


def test_resolve_reasoning_effort_env_override(monkeypatch):
    """Use explicit env override for reasoning effort."""
    set_config_override({"llm": {"reasoning_effort": "LOW"}})
    assert resolve_reasoning_effort("gpt-5") == "low"


def test_model_supports_reasoning_effort_with_provider_prefix():
    """Treat provider-prefixed model names as GPT-5 family."""
    set_config_override({"llm": {"reasoning_effort_models": []}})
    assert model_supports_reasoning_effort("openai/gpt-5.2") is True


def test_resolve_litellm_model_keeps_prefixed_name():
    """Avoid prefixing models that already include a provider."""
    assert llm_module._resolve_litellm_model("openai/gpt-4o", "http://host/v1") == "openai/gpt-4o"
