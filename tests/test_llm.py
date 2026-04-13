"""Tests for model configuration helpers."""

import sys
import types

from meeseeks_core import llm as llm_module
from meeseeks_core.config import LLMConfig, set_config_override
from meeseeks_core.llm import (
    build_chat_model,
    model_supports_prompt_caching,
    model_supports_reasoning_effort,
    register_proxy_model_capabilities,
    resolve_reasoning_effort,
)

_CACHE_INJECTION_POINTS = [
    {"location": "message", "role": "system", "control": {"type": "ephemeral"}}
]


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
    assert llm_module._resolve_litellm_model("z-ai/glm-5", base, "openai") == ("openai/z-ai/glm-5")
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


def test_model_supports_prompt_caching_anthropic_claude():
    """LiteLLM's bundled model_cost knows Claude 4.x supports prompt caching."""
    assert model_supports_prompt_caching("anthropic/claude-haiku-4-5") is True
    assert model_supports_prompt_caching("anthropic/claude-opus-4-7") is True
    assert model_supports_prompt_caching("anthropic/claude-sonnet-4-6") is True


def test_model_supports_prompt_caching_openai_gpt4o():
    """OpenAI auto-cache models also report supports_prompt_caching=True."""
    assert model_supports_prompt_caching("openai/gpt-4o") is True


def test_model_supports_prompt_caching_unknown_proxy_model():
    """Custom proxy model strings aren't in the bundled cost map.

    By design we don't try to be smart about provider/model splits — the proxy
    is responsible for caching when LiteLLM doesn't recognise the model name.
    """
    assert model_supports_prompt_caching("openai/claude-via-mycorp-proxy") is False


def test_model_supports_prompt_caching_handles_lookup_errors(monkeypatch):
    """Swallow any LiteLLM lookup error and return False."""

    def _boom(_model):
        raise ValueError("boom")

    monkeypatch.setattr(llm_module, "_litellm_supports_prompt_caching", _boom)
    assert model_supports_prompt_caching("anything") is False


def test_model_supports_prompt_caching_no_name():
    """Empty / None model name returns False without calling LiteLLM."""
    assert model_supports_prompt_caching(None) is False
    assert model_supports_prompt_caching("") is False


def test_build_chat_model_sets_cache_injection_points_for_supported(monkeypatch):
    """When LiteLLM reports supported, attach cache_control_injection_points."""
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="anthropic/claude-haiku-4-5")
    model_kwargs = captured.get("model_kwargs") or {}
    assert model_kwargs.get("cache_control_injection_points") == _CACHE_INJECTION_POINTS


def _reset_proxy_registration_cache():
    """Empty the module-level set so each test gets a clean slate."""
    llm_module._REGISTERED_PROXY_BASES.clear()


def test_register_proxy_model_capabilities_no_api_base():
    """Returns 0 when api_base is empty / None — nothing to fetch."""
    _reset_proxy_registration_cache()
    assert register_proxy_model_capabilities(None, "key") == 0
    assert register_proxy_model_capabilities("", "key") == 0


def test_register_proxy_model_capabilities_registers_from_fake_proxy(monkeypatch):
    """Fetch /v1/model/info from the proxy and register every advertised model."""
    _reset_proxy_registration_cache()
    import httpx
    import litellm

    captured_url: dict[str, str] = {}
    captured_registrations: list[dict[str, object]] = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": [
                    {
                        "model_name": "claude-sonnet-4-6",
                        "model_info": {
                            "supports_prompt_caching": True,
                            "input_cost_per_token": 3e-06,
                        },
                    },
                    {
                        "model_name": "claude-haiku-4-5",
                        "model_info": {"supports_prompt_caching": True},
                    },
                    # Skipped: empty model_info
                    {"model_name": "garbage", "model_info": {}},
                ]
            }

    def fake_get(url, headers=None, timeout=None):
        captured_url["url"] = url
        captured_url["auth"] = (headers or {}).get("Authorization", "")
        return FakeResp()

    def fake_register(payload):
        captured_registrations.append(payload)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(litellm, "register_model", fake_register)

    n = register_proxy_model_capabilities("https://proxy.example.com/v1/", "sk-test")
    assert n == 2  # garbage entry skipped
    assert captured_url["url"] == "https://proxy.example.com/v1/model/info"
    assert captured_url["auth"] == "Bearer sk-test"
    assert {next(iter(p.keys())) for p in captured_registrations} == {
        "openai/claude-sonnet-4-6",
        "openai/claude-haiku-4-5",
    }
    # Defaults are filled in for downstream consumers
    sonnet = next(p for p in captured_registrations if "openai/claude-sonnet-4-6" in p)
    info = sonnet["openai/claude-sonnet-4-6"]
    assert info["litellm_provider"] == "openai"
    assert info["mode"] == "chat"
    assert info["supports_prompt_caching"] is True


def test_register_proxy_model_capabilities_idempotent_per_base(monkeypatch):
    """Same api_base on a second call short-circuits; no second HTTP fetch."""
    _reset_proxy_registration_cache()
    import httpx
    import litellm

    call_count = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": []}

    def fake_get(*args, **kwargs):
        call_count["n"] += 1
        return FakeResp()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(litellm, "register_model", lambda _: None)

    register_proxy_model_capabilities("https://proxy.example.com/v1", "key")
    register_proxy_model_capabilities("https://proxy.example.com/v1", "key")
    assert call_count["n"] == 1


def test_register_proxy_model_capabilities_swallows_http_errors(monkeypatch):
    """A failing fetch returns 0, logs at info, and does not raise."""
    _reset_proxy_registration_cache()
    import httpx

    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "get", fake_get)
    n = register_proxy_model_capabilities("https://offline.example/v1", "key")
    assert n == 0


def test_build_chat_model_triggers_proxy_registration_when_api_base_set(monkeypatch):
    """build_chat_model invokes the proxy bridge exactly once per distinct base."""
    _reset_proxy_registration_cache()
    invocations: list[tuple[str, str | None]] = []

    def fake_register(api_base, api_key, *, timeout=5.0):
        invocations.append((api_base, api_key))
        return 0

    monkeypatch.setattr(llm_module, "register_proxy_model_capabilities", fake_register)

    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(
        "openai/claude-sonnet-4-6",
        openai_api_base="https://proxy.example.com/v1",
        api_key="sk-test",
    )
    build_chat_model(
        "openai/claude-haiku-4-5", openai_api_base="https://proxy.example.com/v1", api_key="sk-test"
    )
    # Bridge is called per build_chat_model call (the dedup happens *inside*
    # register_proxy_model_capabilities via _REGISTERED_PROXY_BASES); both
    # invocations should be passed through with the same base.
    assert invocations == [
        ("https://proxy.example.com/v1", "sk-test"),
        ("https://proxy.example.com/v1", "sk-test"),
    ]


def test_build_chat_model_skips_proxy_registration_when_no_api_base(monkeypatch):
    """No api_base → no bridge call (direct provider deployments)."""
    _reset_proxy_registration_cache()
    set_config_override({"llm": {"api_base": "", "api_key": ""}})

    invocations: list[tuple[str, str | None]] = []

    def fake_register(api_base, api_key, *, timeout=5.0):
        invocations.append((api_base, api_key))
        return 0

    monkeypatch.setattr(llm_module, "register_proxy_model_capabilities", fake_register)

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            pass

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model("anthropic/claude-haiku-4-5", openai_api_base=None)
    assert invocations == []


def test_build_chat_model_omits_cache_injection_points_for_unsupported(monkeypatch):
    """When LiteLLM reports unsupported, leave the kwarg unset."""
    captured: dict[str, object] = {}

    class DummyChatLiteLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("langchain_litellm")
    module.ChatLiteLLM = DummyChatLiteLLM
    monkeypatch.setitem(sys.modules, "langchain_litellm", module)

    build_chat_model(model_name="openai/claude-via-mycorp-proxy")
    model_kwargs = captured.get("model_kwargs") or {}
    assert "cache_control_injection_points" not in model_kwargs
