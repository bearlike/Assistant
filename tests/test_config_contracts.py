"""Integration/contract tests for config.py.

Covers Pydantic field_validators, ConfigDict constraints, schema markers,
write/load round-trips, effective_fallback_models(), reset/set path helpers,
env-var overrides, nested sub-config defaults, and validation error paths.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError

import pytest
from mewbo_core import config as config_module
from mewbo_core.config import (
    AgentConfig,
    AppConfig,
    ChatConfig,
    CLIConfig,
    CompactionConfig,
    ContextConfig,
    HomeAssistantConfig,
    HookEntry,
    LangfuseConfig,
    LLMConfig,
    PermissionsConfig,
    PluginsConfig,
    ProjectConfig,
    ReflectionConfig,
    RuntimeConfig,
    StorageConfig,
    TokenBudgetConfig,
    WebIdeConfig,
    effective_fallback_models,
    get_config,
    get_config_value,
    reset_config,
    set_app_config_path,
    set_config_override,
    set_mcp_config_path,
)
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# _coerce_bool  (lines 87–98)
# ---------------------------------------------------------------------------
class TestCoerceBool:
    """_coerce_bool covers all branches."""

    def _cb(self, v, *, default=False):
        return config_module._coerce_bool(v, default=default)

    def test_true_passthrough(self):
        assert self._cb(True) is True

    def test_false_passthrough(self):
        assert self._cb(False) is False

    def test_none_returns_default(self):
        assert self._cb(None, default=True) is True
        assert self._cb(None, default=False) is False

    def test_int_truthy(self):
        assert self._cb(1) is True
        assert self._cb(0) is False

    def test_float_truthy(self):
        assert self._cb(1.5) is True
        assert self._cb(0.0) is False

    @pytest.mark.parametrize("v", ["1", "true", "yes", "on", "True", "YES", "ON"])
    def test_truthy_strings(self, v):
        assert self._cb(v) is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off", "False", "OFF"])
    def test_falsy_strings(self, v):
        assert self._cb(v) is False

    def test_unknown_string_returns_default(self):
        assert self._cb("maybe", default=True) is True
        assert self._cb("maybe", default=False) is False


# ---------------------------------------------------------------------------
# _coerce_list  (lines 101–111)
# ---------------------------------------------------------------------------
class TestCoerceList:
    def _cl(self, v):
        return config_module._coerce_list(v)

    def test_none_returns_empty(self):
        assert self._cl(None) == []

    def test_list_strips_and_filters_blanks(self):
        assert self._cl(["a", " b ", "", "  "]) == ["a", "b"]

    def test_comma_string(self):
        assert self._cl("foo, bar,baz") == ["foo", "bar", "baz"]

    def test_empty_string(self):
        assert self._cl("") == []

    def test_other_type_returns_empty(self):
        assert self._cl(42) == []


# ---------------------------------------------------------------------------
# RuntimeConfig validators
# ---------------------------------------------------------------------------
class TestRuntimeConfigValidators:
    def test_log_level_normalized_to_upper(self):
        rt = RuntimeConfig.model_validate({"log_level": "info"})
        assert rt.log_level == "INFO"

    def test_log_level_empty_defaults_to_debug(self):
        rt = RuntimeConfig.model_validate({"log_level": ""})
        assert rt.log_level == "DEBUG"

    def test_log_level_none_defaults_to_debug(self):
        rt = RuntimeConfig.model_validate({"log_level": None})
        assert rt.log_level == "DEBUG"

    def test_projects_home_expands_tilde(self):
        rt = RuntimeConfig.model_validate({"projects_home": "~/myprojects"})
        assert "~" not in rt.projects_home
        assert "myprojects" in rt.projects_home

    def test_projects_home_empty_defaults_to_mewbo_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEWBO_HOME", str(tmp_path))
        reset_config()
        rt = RuntimeConfig.model_validate({"projects_home": ""})
        assert rt.projects_home == str(tmp_path / "projects")

    def test_preflight_enabled_bool_coercion(self):
        rt = RuntimeConfig.model_validate({"preflight_enabled": "yes"})
        assert rt.preflight_enabled is True
        rt2 = RuntimeConfig.model_validate({"preflight_enabled": "no"})
        assert rt2.preflight_enabled is False


# ---------------------------------------------------------------------------
# LLMConfig validators and helpers
# ---------------------------------------------------------------------------
class TestLLMConfigValidators:
    def test_reasoning_effort_valid_values(self):
        for val in ("low", "medium", "high", "none"):
            assert LLMConfig.model_validate({"reasoning_effort": val}).reasoning_effort == val

    def test_reasoning_effort_invalid_normalizes_to_empty(self):
        assert LLMConfig.model_validate({"reasoning_effort": "extreme"}).reasoning_effort == ""

    def test_reasoning_effort_none_normalizes_to_empty(self):
        assert LLMConfig.model_validate({"reasoning_effort": None}).reasoning_effort == ""

    def test_reasoning_effort_models_lowercased(self):
        llm = LLMConfig.model_validate({"reasoning_effort_models": ["GPT-5.2", "Gemini"]})
        assert llm.reasoning_effort_models == ["gpt-5.2", "gemini"]

    def test_structured_patch_models_lowercased(self):
        llm = LLMConfig.model_validate({"structured_patch_models": ["GPT-5.4", "Codex"]})
        assert llm.structured_patch_models == ["gpt-5.4", "codex"]

    def test_proxy_model_prefix_strips_slashes(self):
        llm = LLMConfig.model_validate({"proxy_model_prefix": "/openai/"})
        assert llm.proxy_model_prefix == "openai"

    def test_proxy_model_prefix_empty_defaults_to_openai(self):
        llm = LLMConfig.model_validate({"proxy_model_prefix": ""})
        assert llm.proxy_model_prefix == "openai"

    def test_proxy_model_prefix_none_defaults_to_openai(self):
        llm = LLMConfig.model_validate({"proxy_model_prefix": None})
        assert llm.proxy_model_prefix == "openai"

    def test_models_endpoint_with_v1_suffix(self):
        llm = LLMConfig.model_validate({"api_base": "http://proxy/v1", "api_key": "k"})
        assert llm._models_endpoint() == "http://proxy/v1/models"

    def test_models_endpoint_without_v1_suffix(self):
        llm = LLMConfig.model_validate({"api_base": "http://proxy", "api_key": "k"})
        assert llm._models_endpoint() == "http://proxy/v1/models"

    def test_models_endpoint_raises_when_no_api_base(self):
        llm = LLMConfig.model_validate({"api_base": "", "api_key": "k"})
        with pytest.raises(ValueError, match="api_base is not set"):
            llm._models_endpoint()

    def test_list_models_raises_when_no_api_key(self):
        llm = LLMConfig.model_validate({"api_base": "http://proxy", "api_key": ""})
        with pytest.raises(ValueError, match="api_key is not set"):
            llm.list_models()

    def test_list_models_raises_on_http_error(self, monkeypatch):
        """HTTPError from urlopen is wrapped as ValueError."""
        llm = LLMConfig.model_validate({"api_base": "http://proxy", "api_key": "key"})

        def _fake_urlopen(req, timeout=None):
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(config_module, "urlopen", _fake_urlopen)
        with pytest.raises(ValueError, match="HTTP 401"):
            llm.list_models()

    def test_list_models_raises_on_url_error(self, monkeypatch):
        """URLError from urlopen is wrapped as ValueError."""
        llm = LLMConfig.model_validate({"api_base": "http://proxy", "api_key": "key"})

        def _fake_urlopen(req, timeout=None):
            raise URLError("connection refused")

        monkeypatch.setattr(config_module, "urlopen", _fake_urlopen)
        with pytest.raises(ValueError, match="connection refused"):
            llm.list_models()

    def test_list_models_returns_sorted_ids(self, monkeypatch):
        """list_models returns sorted model IDs from the response."""
        llm = LLMConfig.model_validate({"api_base": "http://proxy", "api_key": "key"})

        raw_payload = json.dumps({"data": [{"id": "z-model"}, {"id": "a-model"}]}).encode()

        class _FakeResp:
            def read(self):
                return raw_payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr(config_module, "urlopen", lambda *a, **k: _FakeResp())
        result = llm.list_models()
        assert result == ["a-model", "z-model"]

    def test_validate_models_ok_when_all_models_present(self, monkeypatch):
        llm = LLMConfig.model_validate(
            {
                "api_base": "http://proxy",
                "api_key": "key",
                "default_model": "m1",
            }
        )
        monkeypatch.setattr(LLMConfig, "list_models", lambda *a, **k: ["m1", "m2"])
        result = llm.validate_models()
        assert result.ok is True
        assert "m1" in result.metadata.get("available_models", [])

    def test_validate_models_list_error_returns_failure(self, monkeypatch):
        llm = LLMConfig.model_validate({"api_base": "http://proxy", "api_key": "key"})
        monkeypatch.setattr(
            LLMConfig, "list_models", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        )
        result = llm.validate_models()
        assert result.ok is False
        assert "boom" in (result.reason or "")


# ---------------------------------------------------------------------------
# ContextConfig validators
# ---------------------------------------------------------------------------
class TestContextConfigValidators:
    def test_recent_event_limit_clamped_to_at_least_one(self):
        ctx = ContextConfig.model_validate({"recent_event_limit": 0})
        assert ctx.recent_event_limit == 1

    def test_recent_event_limit_invalid_type_defaults(self):
        ctx = ContextConfig.model_validate({"recent_event_limit": "bad"})
        assert ctx.recent_event_limit == 8

    def test_selection_threshold_clamped(self):
        assert (
            ContextConfig.model_validate({"selection_threshold": -1.0}).selection_threshold == 0.0
        )
        assert ContextConfig.model_validate({"selection_threshold": 2.0}).selection_threshold == 1.0

    def test_selection_threshold_invalid_type_defaults(self):
        ctx = ContextConfig.model_validate({"selection_threshold": "not-a-float"})
        assert ctx.selection_threshold == 0.8

    def test_selection_enabled_coercion(self):
        assert ContextConfig.model_validate({"selection_enabled": "yes"}).selection_enabled is True
        assert ContextConfig.model_validate({"selection_enabled": "no"}).selection_enabled is False


# ---------------------------------------------------------------------------
# TokenBudgetConfig validators
# ---------------------------------------------------------------------------
class TestTokenBudgetConfigValidators:
    def test_context_window_clamped_to_at_least_one(self):
        tb = TokenBudgetConfig.model_validate({"default_context_window": 0})
        assert tb.default_context_window == 1

    def test_context_window_invalid_type_defaults(self):
        tb = TokenBudgetConfig.model_validate({"default_context_window": "bad"})
        assert tb.default_context_window == 128000

    def test_compact_threshold_clamped(self):
        assert (
            TokenBudgetConfig.model_validate(
                {"auto_compact_threshold": -1.0}
            ).auto_compact_threshold
            == 0.0
        )
        assert (
            TokenBudgetConfig.model_validate({"auto_compact_threshold": 5.0}).auto_compact_threshold
            == 1.0
        )

    def test_compact_threshold_invalid_type_defaults(self):
        tb = TokenBudgetConfig.model_validate({"auto_compact_threshold": "bad"})
        assert tb.auto_compact_threshold == 0.8

    def test_model_context_windows_non_dict_returns_empty(self):
        tb = TokenBudgetConfig.model_validate({"model_context_windows": "not-a-dict"})
        assert tb.model_context_windows == {}

    def test_model_context_windows_skips_invalid_values(self):
        tb = TokenBudgetConfig.model_validate(
            {"model_context_windows": {"gpt-5": 4096, "bad": "nope"}}
        )
        assert tb.model_context_windows.get("gpt-5") == 4096
        assert "bad" not in tb.model_context_windows

    def test_model_context_windows_clamps_to_at_least_one(self):
        tb = TokenBudgetConfig.model_validate({"model_context_windows": {"m": 0}})
        assert tb.model_context_windows["m"] == 1


# ---------------------------------------------------------------------------
# LangfuseConfig.evaluate()  (lines 668–675)
# ---------------------------------------------------------------------------
class TestLangfuseConfigEvaluate:
    def test_disabled_returns_false_reason(self):
        cfg = LangfuseConfig.model_validate({"enabled": False})
        enabled, reason, _ = cfg.evaluate()
        assert enabled is False
        assert reason == "disabled via config"

    def test_missing_keys_returns_false(self):
        cfg = LangfuseConfig.model_validate({"enabled": True})
        enabled, reason, meta = cfg.evaluate()
        assert enabled is False
        assert "required_config" in meta

    def test_missing_public_key_only(self):
        cfg = LangfuseConfig.model_validate({"enabled": True, "secret_key": "sk"})
        enabled, reason, meta = cfg.evaluate()
        assert enabled is False
        assert "langfuse.public_key" in meta["required_config"]

    def test_missing_secret_key_only(self):
        cfg = LangfuseConfig.model_validate({"enabled": True, "public_key": "pk"})
        enabled, reason, meta = cfg.evaluate()
        assert enabled is False
        assert "langfuse.secret_key" in meta["required_config"]


# ---------------------------------------------------------------------------
# HomeAssistantConfig.evaluate()
# ---------------------------------------------------------------------------
class TestHomeAssistantConfigEvaluate:
    def test_disabled_returns_false_with_reason(self):
        cfg = HomeAssistantConfig.model_validate({"enabled": False})
        enabled, reason, _ = cfg.evaluate()
        assert enabled is False
        assert "disabled" in reason

    def test_missing_url_returns_false(self):
        cfg = HomeAssistantConfig.model_validate({"enabled": True, "token": "tok"})
        enabled, reason, meta = cfg.evaluate()
        assert enabled is False
        assert "home_assistant.url" in meta["required_config"]

    def test_missing_token_returns_false(self):
        cfg = HomeAssistantConfig.model_validate({"enabled": True, "url": "http://ha"})
        enabled, reason, meta = cfg.evaluate()
        assert enabled is False
        assert "home_assistant.token" in meta["required_config"]

    def test_both_set_returns_true(self):
        cfg = HomeAssistantConfig.model_validate(
            {
                "enabled": True,
                "url": "http://ha",
                "token": "tok",
            }
        )
        enabled, reason, _ = cfg.evaluate()
        assert enabled is True
        assert reason is None


# ---------------------------------------------------------------------------
# PermissionsConfig.approval_mode validator
# ---------------------------------------------------------------------------
class TestPermissionsConfigApprovalMode:
    @pytest.mark.parametrize(
        "val,expected",
        [
            ("allow", "allow"),
            ("auto", "allow"),
            ("approve", "allow"),
            ("yes", "allow"),
            ("deny", "deny"),
            ("never", "deny"),
            ("no", "deny"),
            ("ask", "ask"),
            ("unknown", "ask"),
            (None, "ask"),
        ],
    )
    def test_approval_mode_normalization(self, val, expected):
        pc = PermissionsConfig.model_validate({"approval_mode": val})
        assert pc.approval_mode == expected


# ---------------------------------------------------------------------------
# CLIConfig validators
# ---------------------------------------------------------------------------
class TestCLIConfigValidators:
    def test_disable_textual_coercion(self):
        assert CLIConfig.model_validate({"disable_textual": "yes"}).disable_textual is True
        assert CLIConfig.model_validate({"disable_textual": "no"}).disable_textual is False

    @pytest.mark.parametrize(
        "val,expected",
        [
            ("inline", "inline"),
            ("textual", "textual"),
            ("aider", "aider"),
            ("bogus", "inline"),
            (None, "inline"),
        ],
    )
    def test_approval_style_normalization(self, val, expected):
        assert CLIConfig.model_validate({"approval_style": val}).approval_style == expected


# ---------------------------------------------------------------------------
# ChatConfig validators
# ---------------------------------------------------------------------------
class TestChatConfigValidators:
    def test_port_clamped_to_at_least_one(self):
        assert ChatConfig.model_validate({"port": 0}).port == 1

    def test_port_invalid_type_defaults(self):
        assert ChatConfig.model_validate({"port": "bad"}).port == 8501

    def test_port_valid(self):
        assert ChatConfig.model_validate({"port": 9000}).port == 9000


# ---------------------------------------------------------------------------
# HookEntry model_validator
# ---------------------------------------------------------------------------
class TestHookEntryValidation:
    def test_http_hook_requires_url(self):
        with pytest.raises(ValidationError, match="requires a non-empty 'url'"):
            HookEntry(type="http", command="", url="")

    def test_command_with_url_only_rejected(self):
        with pytest.raises(ValidationError, match="use type='http'"):
            HookEntry(type="command", command="", url="http://example.com")

    def test_valid_http_hook(self):
        h = HookEntry(type="http", url="http://example.com")
        assert h.url == "http://example.com"

    def test_valid_command_hook(self):
        h = HookEntry(type="command", command="echo hello")
        assert h.command == "echo hello"


# ---------------------------------------------------------------------------
# StorageConfig.driver validator and env override
# ---------------------------------------------------------------------------
class TestStorageConfigDriver:
    def test_invalid_driver_raises(self):
        with pytest.raises(ValidationError, match="Unknown storage driver"):
            StorageConfig.model_validate({"driver": "redis"})

    def test_env_overrides_driver(self, monkeypatch):
        monkeypatch.setenv("MEWBO_STORAGE_DRIVER", "mongodb")
        sc = StorageConfig.model_validate({"driver": "json"})
        assert sc.driver == "mongodb"

    def test_driver_mongodb_accepted(self):
        sc = StorageConfig.model_validate({"driver": "mongodb"})
        assert sc.driver == "mongodb"


# ---------------------------------------------------------------------------
# MongoDBConfig env override
# ---------------------------------------------------------------------------
class TestMongoDBConfigEnvOverride:
    def test_uri_env_override(self, monkeypatch):
        """Env var overrides an explicitly-provided URI value."""
        from mewbo_core.config import MongoDBConfig

        monkeypatch.setenv("MEWBO_MONGODB_URI", "mongodb://testhost:1234")
        # Env override only fires when validator is invoked (i.e. a value is provided)
        mc = MongoDBConfig.model_validate({"uri": "mongodb://placeholder"})
        assert mc.uri == "mongodb://testhost:1234"

    def test_database_env_override(self, monkeypatch):
        """Env var overrides an explicitly-provided database name."""
        from mewbo_core.config import MongoDBConfig

        monkeypatch.setenv("MEWBO_MONGODB_DATABASE", "mydb")
        mc = MongoDBConfig.model_validate({"database": "placeholder"})
        assert mc.database == "mydb"

    def test_uri_default_used_when_no_env(self):
        """Without env var, default URI is used."""
        from mewbo_core.config import MongoDBConfig

        mc = MongoDBConfig.model_validate({"uri": "mongodb://custom:9999"})
        assert mc.uri == "mongodb://custom:9999"


# ---------------------------------------------------------------------------
# AgentConfig validators
# ---------------------------------------------------------------------------
class TestAgentConfigValidators:
    def test_edit_tool_valid_values(self):
        for val in ("", "search_replace_block", "structured_patch"):
            ac = AgentConfig.model_validate({"edit_tool": val})
            assert ac.edit_tool == val

    def test_edit_tool_invalid_normalizes_to_empty(self):
        ac = AgentConfig.model_validate({"edit_tool": "custom_tool"})
        assert ac.edit_tool == ""

    def test_edit_tool_none_normalizes_to_empty(self):
        ac = AgentConfig.model_validate({"edit_tool": None})
        assert ac.edit_tool == ""

    def test_max_depth_clamped_to_at_least_one(self):
        assert AgentConfig.model_validate({"max_depth": 0}).max_depth == 1

    def test_max_depth_invalid_defaults(self):
        assert AgentConfig.model_validate({"max_depth": "bad"}).max_depth == 5

    def test_max_concurrent_clamped_to_at_least_one(self):
        assert AgentConfig.model_validate({"max_concurrent": 0}).max_concurrent == 1

    def test_max_concurrent_invalid_defaults(self):
        assert AgentConfig.model_validate({"max_concurrent": "bad"}).max_concurrent == 20

    def test_max_iters_clamped_to_at_least_one(self):
        assert AgentConfig.model_validate({"max_iters": 0}).max_iters == 1

    def test_max_iters_invalid_defaults(self):
        assert AgentConfig.model_validate({"max_iters": "oops"}).max_iters == 30

    def test_sub_agent_max_steps_clamped(self):
        assert AgentConfig.model_validate({"sub_agent_max_steps": 0}).sub_agent_max_steps == 1

    def test_sub_agent_max_steps_invalid_defaults(self):
        assert AgentConfig.model_validate({"sub_agent_max_steps": "bad"}).sub_agent_max_steps == 10

    def test_allowed_models_coercion_from_csv(self):
        ac = AgentConfig.model_validate({"allowed_models": "gpt-5,claude-4"})
        assert "gpt-5" in ac.allowed_models

    def test_plan_mode_allow_mcp_coercion(self):
        assert (
            AgentConfig.model_validate({"plan_mode_allow_mcp": "yes"}).plan_mode_allow_mcp is True
        )
        assert (
            AgentConfig.model_validate({"plan_mode_allow_mcp": "no"}).plan_mode_allow_mcp is False
        )


# ---------------------------------------------------------------------------
# WebIdeConfig extra="forbid"
# ---------------------------------------------------------------------------
class TestWebIdeConfigExtraForbid:
    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            WebIdeConfig(enabled=True, unknown_field="x")


# ---------------------------------------------------------------------------
# PluginsConfig validators
# ---------------------------------------------------------------------------
class TestPluginsConfigValidators:
    def test_install_path_expands_and_resolves(self, tmp_path):
        pc = PluginsConfig.model_validate({"install_path": str(tmp_path)})
        assert pc.install_path == str(tmp_path.resolve())

    def test_install_path_empty_returns_empty(self):
        pc = PluginsConfig.model_validate({"install_path": ""})
        assert pc.install_path == ""

    def test_marketplace_default_host_fallback(self):
        pc = PluginsConfig.model_validate({"marketplace_default_host": ""})
        assert pc.marketplace_default_host == "github.com"

    def test_enabled_coercion(self):
        assert PluginsConfig.model_validate({"enabled": "no"}).enabled is False

    def test_resolve_install_dir_uses_install_path_when_set(self, tmp_path):
        pc = PluginsConfig.model_validate({"install_path": str(tmp_path)})
        assert pc.resolve_install_dir() == tmp_path.resolve()

    def test_resolve_install_dir_falls_back_to_mewbo_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEWBO_HOME", str(tmp_path))
        reset_config()
        pc = PluginsConfig.model_validate({"install_path": ""})
        assert pc.resolve_install_dir() == tmp_path / "plugins"


# ---------------------------------------------------------------------------
# ProjectConfig path validator
# ---------------------------------------------------------------------------
class TestProjectConfigPathValidator:
    def test_path_expands_tilde(self):
        pc = ProjectConfig.model_validate({"path": "~/myproject"})
        assert "~" not in pc.path
        assert "myproject" in pc.path

    def test_empty_path_remains_empty(self):
        pc = ProjectConfig.model_validate({"path": ""})
        assert pc.path == ""


# ---------------------------------------------------------------------------
# AppConfig._normalize_projects validator
# ---------------------------------------------------------------------------
class TestAppConfigNormalizeProjects:
    def test_dict_projects_are_parsed(self, tmp_path):
        app = AppConfig.model_validate(
            {"projects": {"work": {"path": str(tmp_path), "description": "Work dir"}}}
        )
        assert "work" in app.projects
        assert app.projects["work"].path == str(tmp_path.resolve())

    def test_non_dict_projects_becomes_empty(self):
        app = AppConfig.model_validate({"projects": "invalid"})
        assert app.projects == {}

    def test_project_config_objects_pass_through(self, tmp_path):
        pc = ProjectConfig.model_validate({"path": str(tmp_path)})
        app = AppConfig.model_validate({"projects": {"p": pc}})
        assert app.projects["p"].path == str(tmp_path.resolve())


# ---------------------------------------------------------------------------
# AppConfig.write() / load round-trip  (lines 1766–1770)
# ---------------------------------------------------------------------------
class TestAppConfigWriteLoad:
    def test_write_creates_parent_dirs(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "app.json"
        AppConfig().write(deep)
        assert deep.exists()

    def test_roundtrip_preserves_llm_key(self, tmp_path):
        target = tmp_path / "app.json"
        cfg = AppConfig.model_validate({"llm": {"api_key": "sk-test", "default_model": "my-model"}})
        cfg.write(target)
        loaded = AppConfig.load(target)
        assert loaded.llm.api_key == "sk-test"
        assert loaded.llm.default_model == "my-model"

    def test_roundtrip_preserves_nested_agent_config(self, tmp_path):
        target = tmp_path / "app.json"
        cfg = AppConfig.model_validate({"agent": {"max_depth": 3, "max_concurrent": 5}})
        cfg.write(target)
        loaded = AppConfig.load(target)
        assert loaded.agent.max_depth == 3
        assert loaded.agent.max_concurrent == 5


# ---------------------------------------------------------------------------
# AppConfig.extra="ignore" — extra fields silently dropped
# ---------------------------------------------------------------------------
class TestAppConfigExtraIgnored:
    def test_extra_fields_ignored(self):
        app = AppConfig.model_validate({"unknown_top_level": "value"})
        assert not hasattr(app, "unknown_top_level")


# ---------------------------------------------------------------------------
# x-secret / x-protected schema markers in JSON schema
# ---------------------------------------------------------------------------
class TestSchemaMarkers:
    def test_x_secret_on_llm_api_key(self):
        schema = LLMConfig.model_json_schema()
        props = schema.get("properties", {})
        assert props["api_key"].get("x-secret") is True

    def test_x_secret_on_langfuse_keys(self):
        schema = LangfuseConfig.model_json_schema()
        props = schema.get("properties", {})
        assert props["public_key"].get("x-secret") is True
        assert props["secret_key"].get("x-secret") is True

    def test_x_protected_on_runtime_paths(self):
        schema = RuntimeConfig.model_json_schema()
        props = schema.get("properties", {})
        for field_name in ("cache_dir", "session_dir", "config_dir"):
            assert props[field_name].get("x-protected") is True, (
                f"{field_name} should be x-protected"
            )

    def test_x_group_on_llm_config(self):
        schema = LLMConfig.model_json_schema()
        assert schema.get("x-group") == "models"

    def test_x_advanced_on_runtime_config(self):
        schema = RuntimeConfig.model_json_schema()
        assert schema.get("x-advanced") is True


# ---------------------------------------------------------------------------
# effective_fallback_models()  (lines 2034–2047)
# ---------------------------------------------------------------------------
class TestEffectiveFallbackModels:
    def test_no_fallback_configured_returns_empty(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig().write(cfg_path)
        set_app_config_path(cfg_path)
        models = effective_fallback_models()
        assert models == []

    def test_legacy_fallback_models_honored_when_fallback_disabled(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        cfg = AppConfig.model_validate(
            {"llm": {"fallback_models": ["gpt-5.4", "gemini-pro"], "fallback": {"enabled": False}}}
        )
        cfg.write(cfg_path)
        reset_config()
        set_app_config_path(cfg_path)
        models = effective_fallback_models()
        assert models == ["gpt-5.4", "gemini-pro"]

    def test_typed_fallback_enabled_uses_typed_list(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        cfg = AppConfig.model_validate(
            {
                "llm": {
                    "fallback": {"enabled": True, "models": ["model-a", "model-b"]},
                    "fallback_models": ["legacy-model"],
                }
            }
        )
        cfg.write(cfg_path)
        reset_config()
        set_app_config_path(cfg_path)
        models = effective_fallback_models()
        assert models == ["model-a", "model-b"]

    def test_typed_fallback_enabled_empty_models_falls_back_to_legacy(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        cfg = AppConfig.model_validate(
            {
                "llm": {
                    "fallback": {"enabled": True, "models": []},
                    "fallback_models": ["legacy-model"],
                }
            }
        )
        cfg.write(cfg_path)
        reset_config()
        set_app_config_path(cfg_path)
        models = effective_fallback_models()
        assert models == ["legacy-model"]


# ---------------------------------------------------------------------------
# reset_config / set_*_path / set_config_override
# ---------------------------------------------------------------------------
class TestConfigHelpers:
    def test_reset_config_clears_cache_and_overrides(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate({"llm": {"api_key": "x"}}).write(cfg_path)
        set_app_config_path(cfg_path)
        set_config_override({"llm": {"api_key": "override"}})
        assert get_config_value("llm", "api_key") == "override"
        reset_config()
        # After reset there's no cache, override is gone
        assert config_module._APP_CONFIG_OVERRIDE == {}
        assert config_module._CONFIG_CACHE is None

    def test_set_config_override_replace_true(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig().write(cfg_path)
        set_app_config_path(cfg_path)
        set_config_override({"llm": {"api_key": "a"}})
        set_config_override({"agent": {"max_depth": 2}}, replace=True)
        # replace=True wipes previous override
        assert config_module._APP_CONFIG_OVERRIDE == {"agent": {"max_depth": 2}}

    def test_set_mcp_config_path_none_disables(self):
        set_mcp_config_path(None)
        assert config_module._MCP_CONFIG_DISABLED is True
        assert config_module._MCP_CONFIG_PATH_OVERRIDE is None

    def test_set_mcp_config_path_empty_string_disables(self):
        set_mcp_config_path("")
        assert config_module._MCP_CONFIG_DISABLED is True

    def test_set_mcp_config_path_re_enables(self, tmp_path):
        set_mcp_config_path(None)
        mcp_path = tmp_path / "mcp.json"
        set_mcp_config_path(mcp_path)
        assert config_module._MCP_CONFIG_DISABLED is False
        assert config_module._MCP_CONFIG_PATH_OVERRIDE == mcp_path

    def test_get_config_value_returns_default_for_missing_key(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig().write(cfg_path)
        set_app_config_path(cfg_path)
        val = get_config_value("llm", "nonexistent_key", default="sentinel")
        assert val == "sentinel"

    def test_get_config_caches_after_first_call(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig().write(cfg_path)
        set_app_config_path(cfg_path)
        c1 = get_config()
        c2 = get_config()
        assert c1 is c2


# ---------------------------------------------------------------------------
# CompactionConfig / ReflectionConfig validators
# ---------------------------------------------------------------------------
class TestCompactionConfigValidators:
    def test_caveman_mode_coercion(self):
        assert CompactionConfig.model_validate({"caveman_mode": "yes"}).caveman_mode is True
        assert CompactionConfig.model_validate({"caveman_mode": "no"}).caveman_mode is False


class TestReflectionConfigValidators:
    def test_enabled_coercion(self):
        assert ReflectionConfig.model_validate({"enabled": "no"}).enabled is False
        assert ReflectionConfig.model_validate({"enabled": "yes"}).enabled is True


# ---------------------------------------------------------------------------
# get_version() (lines 78–83) — covers the fallback path
# ---------------------------------------------------------------------------
class TestGetVersion:
    def test_returns_version_string(self):
        """get_version() always returns a non-empty string."""
        v = config_module.get_version()
        assert isinstance(v, str)
        assert len(v) > 0

    def test_fallback_to_000_when_packages_missing(self, monkeypatch):
        """When all packages are missing, return '0.0.0'."""
        from importlib.metadata import PackageNotFoundError

        monkeypatch.setattr(
            config_module, "_pkg_version", lambda _: (_ for _ in ()).throw(PackageNotFoundError())
        )
        result = config_module.get_version()
        assert result == "0.0.0"


# ---------------------------------------------------------------------------
# ensure_app_config  (line 2063-2065)
# ---------------------------------------------------------------------------
class TestEnsureAppConfig:
    def test_writes_default_config_when_missing(self, tmp_path):
        target = tmp_path / "app.json"
        config_module.ensure_app_config(target)
        assert target.exists()
        payload = json.loads(target.read_text())
        assert "runtime" in payload

    def test_does_not_overwrite_existing(self, tmp_path):
        target = tmp_path / "app.json"
        target.write_text('{"runtime": {"envmode": "MINE"}}', encoding="utf-8")
        config_module.ensure_app_config(target)
        payload = json.loads(target.read_text())
        assert payload["runtime"]["envmode"] == "MINE"


# ---------------------------------------------------------------------------
# _deep_merge (lines 1933–1939) — deep merge semantics
# ---------------------------------------------------------------------------
class TestDeepMerge:
    def test_nested_dicts_merged(self):
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": {"c": 99, "d": 3}}
        result = config_module._deep_merge(base, override)
        assert result == {"a": {"b": 1, "c": 99, "d": 3}}

    def test_non_dict_values_overridden(self):
        base = {"x": [1, 2, 3]}
        override = {"x": [4, 5]}
        result = config_module._deep_merge(base, override)
        assert result["x"] == [4, 5]


# ---------------------------------------------------------------------------
# CompactionConfig / ReflectionConfig defaults  (sub-config defaults)
# ---------------------------------------------------------------------------
class TestNestedSubConfigDefaults:
    def test_agent_retry_config_has_defaults(self):
        ac = AgentConfig.model_validate({})
        assert ac.retry.backoff_base > 0
        assert ac.retry.doom_loop_threshold >= 0

    def test_agent_lsp_config_enabled_by_default(self):
        ac = AgentConfig.model_validate({})
        assert ac.lsp.enabled is True

    def test_agent_tool_search_off_by_default(self):
        ac = AgentConfig.model_validate({})
        assert ac.tool_search.mode == "off"
