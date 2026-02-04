"""Tests for model configuration helpers."""

import sys
import types

from meeseeks_core import llm as llm_module
from meeseeks_core.llm import (
    allows_temperature,
    build_chat_model,
    model_supports_reasoning_effort,
    resolve_reasoning_effort,
)


def test_resolve_reasoning_effort_defaults(monkeypatch):
    """Default to medium for GPT-5 family models."""
    monkeypatch.delenv("MESEEKS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("MESEEKS_REASONING_EFFORT_MODELS", raising=False)
    assert resolve_reasoning_effort("gpt-5.2") == "medium"
    assert resolve_reasoning_effort("gpt-5.1") == "medium"


def test_resolve_reasoning_effort_gpt5_pro(monkeypatch):
    """Use high reasoning effort for GPT-5 pro."""
    monkeypatch.delenv("MESEEKS_REASONING_EFFORT", raising=False)
    assert resolve_reasoning_effort("gpt-5-pro") == "high"


def test_allows_temperature_gpt5_variants():
    """Respect GPT-5 temperature constraints."""
    assert allows_temperature("gpt-5.2", "none") is True
    assert allows_temperature("gpt-5.2", "medium") is False
    assert allows_temperature("gpt-5", "medium") is False


def test_build_chat_model_includes_reasoning_effort(monkeypatch):
    """Attach reasoning_effort to model kwargs when supported."""
    monkeypatch.delenv("MESEEKS_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("MESEEKS_REASONING_EFFORT_MODELS", raising=False)
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="gpt-5.2", temperature=0.2, openai_api_base=None)
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

    build_chat_model(model_name="gpt-4o", temperature=0.0, openai_api_base="http://host/v1")
    assert captured["model"] == "openai/gpt-4o"
    assert captured["api_base"] == "http://host/v1"


def test_parse_model_list_env(monkeypatch):
    """Parse model allowlists from JSON or CSV env values."""
    monkeypatch.setenv("MESEEKS_REASONING_EFFORT_MODELS", '["Foo","bar"]')
    assert llm_module._parse_model_list_env("MESEEKS_REASONING_EFFORT_MODELS") == [
        "foo",
        "bar",
    ]


def test_parse_model_list_env_empty(monkeypatch):
    """Return empty list for blank env values."""
    monkeypatch.setenv("MESEEKS_REASONING_EFFORT_MODELS", "   ")
    assert llm_module._parse_model_list_env("MESEEKS_REASONING_EFFORT_MODELS") == []
    monkeypatch.setenv("MESEEKS_REASONING_EFFORT_MODELS", "foo, Bar")
    assert llm_module._parse_model_list_env("MESEEKS_REASONING_EFFORT_MODELS") == [
        "foo",
        "bar",
    ]


def test_matches_model_list_wildcard():
    """Match model allowlist entries including wildcard suffixes."""
    assert llm_module._matches_model_list("gpt-4o", ["gpt-4*"]) is True
    assert llm_module._matches_model_list("gpt-4o", ["gpt-3*"]) is False
    assert llm_module._matches_model_list("gpt-4o", ["gpt-4o"]) is True


def test_model_supports_reasoning_effort_allowlist(monkeypatch):
    """Respect explicit allowlists for non-GPT-5 models."""
    monkeypatch.setenv("MESEEKS_REASONING_EFFORT_MODELS", '["custom*"]')
    assert llm_module.model_supports_reasoning_effort("custom-model") is True
    assert llm_module.model_supports_reasoning_effort("other") is False


def test_model_supports_reasoning_effort_without_name():
    """Return False when no model name is provided."""
    assert model_supports_reasoning_effort(None) is False


def test_resolve_reasoning_effort_env_override(monkeypatch):
    """Use explicit env override for reasoning effort."""
    monkeypatch.setenv("MESEEKS_REASONING_EFFORT", "LOW")
    assert resolve_reasoning_effort("gpt-5") == "low"


def test_allows_temperature_without_model_name():
    """Allow temperature when model name is missing."""
    assert allows_temperature(None, None) is True
