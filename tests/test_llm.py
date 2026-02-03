"""Tests for model configuration helpers."""
import sys
import types

from core.llm import allows_temperature, build_chat_model, resolve_reasoning_effort


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

    class DummyChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_openai")
    module.ChatOpenAI = DummyChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", module)

    build_chat_model(model_name="gpt-5.2", temperature=0.2, openai_api_base=None)
    model_kwargs = captured.get("model_kwargs") or {}
    assert model_kwargs.get("reasoning_effort") == "medium"
    assert "temperature" not in captured
