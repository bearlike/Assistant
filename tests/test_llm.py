"""Tests for model configuration helpers."""

import sys
import types

from meeseeks_core import llm as llm_module
from meeseeks_core.config import LLMConfig, set_config_override
from meeseeks_core.llm import (
    build_chat_model,
    model_supports_reasoning_effort,
    resolve_reasoning_effort,
)


def test_resolve_reasoning_effort_none_when_unconfigured(monkeypatch):
    """Return None for supported models when no reasoning_effort is configured."""
    set_config_override({"llm": {"reasoning_effort": "", "reasoning_effort_models": []}})
    assert resolve_reasoning_effort("gpt-5.2") is None
    assert resolve_reasoning_effort("openai/claude-sonnet-4-6") is None
    assert resolve_reasoning_effort("gemini/gemini-2.5-pro") is None
    assert resolve_reasoning_effort("unknown-model") is None


def test_build_chat_model_includes_reasoning_effort(monkeypatch):
    """Attach reasoning_effort to model kwargs when explicitly configured."""
    set_config_override({"llm": {"reasoning_effort": "high", "reasoning_effort_models": []}})
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="gpt-5.2", openai_api_base=None)
    model_kwargs = captured.get("model_kwargs") or {}
    assert model_kwargs.get("reasoning_effort") == "high"
    assert "temperature" not in captured
    # drop_params must be inside model_kwargs to reach litellm.acompletion();
    # ChatLiteLLM has no drop_params field so top-level kwarg is silently ignored.
    assert model_kwargs.get("drop_params") is True


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


def test_build_chat_model_defaults_api_base_and_key_from_config(monkeypatch):
    """Resolve llm.api_base and llm.api_key from config when caller omits them."""
    set_config_override(
        {
            "llm": {
                "api_base": "https://proxy.example/v1",
                "api_key": "sk-from-config",
                "reasoning_effort": "",
                "reasoning_effort_models": [],
            }
        }
    )
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="gpt-4o")
    # Config-resolved values are used when caller passes nothing.
    assert captured["api_base"] == "https://proxy.example/v1"
    assert captured["api_key"] == "sk-from-config"
    # And the model is prefixed because api_base is set.
    assert captured["model"] == "openai/gpt-4o"


def test_build_chat_model_explicit_kwargs_override_config(monkeypatch):
    """Explicit kwargs win over config-resolved defaults."""
    set_config_override(
        {
            "llm": {
                "api_base": "https://should-be-ignored.example/v1",
                "api_key": "sk-should-be-ignored",
                "reasoning_effort": "",
                "reasoning_effort_models": [],
            }
        }
    )
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(
        model_name="gpt-4o",
        openai_api_base="https://override.example/v1",
        api_key="sk-override",
    )
    assert captured["api_base"] == "https://override.example/v1"
    assert captured["api_key"] == "sk-override"


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


def test_model_supports_reasoning_effort_claude():
    """Claude models support reasoning_effort via LiteLLM."""
    set_config_override({"llm": {"reasoning_effort_models": []}})
    assert model_supports_reasoning_effort("openai/claude-sonnet-4-6") is True
    assert model_supports_reasoning_effort("claude-opus-4-6") is True
    assert model_supports_reasoning_effort("anthropic/claude-3.5-sonnet") is True


def test_model_supports_reasoning_effort_gemini():
    """Gemini models support reasoning_effort via LiteLLM."""
    set_config_override({"llm": {"reasoning_effort_models": []}})
    assert model_supports_reasoning_effort("gemini/gemini-2.5-pro") is True
    assert model_supports_reasoning_effort("vertex_ai/gemini-2.5-flash") is True


def test_model_supports_reasoning_effort_o3():
    """O3 models support reasoning_effort."""
    set_config_override({"llm": {"reasoning_effort_models": []}})
    assert model_supports_reasoning_effort("openai/o3") is True
    assert model_supports_reasoning_effort("o3-mini") is True


def test_resolve_litellm_model_proxy_prepends_configured_prefix():
    """Prepend the configured proxy prefix when routing through a proxy.

    LiteLLM strips exactly the leading 'prefix/' before sending the model
    name in the HTTP request, so the proxy receives its own model ID intact.
    """
    base = "http://host/v1"
    # Default openai prefix
    assert llm_module._resolve_litellm_model("z-ai/glm-5", base, "openai") == (
        "openai/z-ai/glm-5"
    )
    assert llm_module._resolve_litellm_model("gpt-4o", base, "openai") == "openai/gpt-4o"
    # Idempotent when already prefixed
    assert llm_module._resolve_litellm_model("openai/claude-sonnet-4-6", base, "openai") == (
        "openai/claude-sonnet-4-6"
    )
    # Custom prefix (e.g. azure)
    assert llm_module._resolve_litellm_model("gpt-4o", base, "azure") == "azure/gpt-4o"
    assert llm_module._resolve_litellm_model("azure/gpt-4o", base, "azure") == "azure/gpt-4o"
    # No proxy: pass through unchanged regardless of prefix
    assert llm_module._resolve_litellm_model("z-ai/glm-5", None, "openai") == "z-ai/glm-5"
    assert llm_module._resolve_litellm_model("anthropic/claude-3-5-sonnet", None, "openai") == (
        "anthropic/claude-3-5-sonnet"
    )


def test_build_chat_model_reads_proxy_prefix_from_config(monkeypatch):
    """build_chat_model reads proxy_model_prefix from config."""
    set_config_override(
        {
            "llm": {
                "api_base": "https://proxy.example/v1",
                "api_key": "sk-test",
                "proxy_model_prefix": "azure",
                "reasoning_effort": "",
                "reasoning_effort_models": [],
            }
        }
    )
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="gpt-4o")
    assert captured["model"] == "azure/gpt-4o"


def test_llm_config_proxy_model_prefix_default():
    """proxy_model_prefix defaults to 'openai'."""
    cfg = LLMConfig()
    assert cfg.proxy_model_prefix == "openai"


def test_llm_config_proxy_model_prefix_strips_slash():
    """Trailing/leading slashes are stripped from proxy_model_prefix."""
    cfg = LLMConfig(proxy_model_prefix="openai/")
    assert cfg.proxy_model_prefix == "openai"
    cfg2 = LLMConfig(proxy_model_prefix="/azure/")
    assert cfg2.proxy_model_prefix == "azure"


def test_llm_config_proxy_model_prefix_empty_falls_back_to_default():
    """Empty or whitespace proxy_model_prefix falls back to 'openai'."""
    assert LLMConfig(proxy_model_prefix="").proxy_model_prefix == "openai"
    assert LLMConfig(proxy_model_prefix="   ").proxy_model_prefix == "openai"
    assert LLMConfig(proxy_model_prefix="///").proxy_model_prefix == "openai"
