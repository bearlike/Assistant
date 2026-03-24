"""Tests for config loading and preflight checks."""

from __future__ import annotations

import asyncio
import json

from meeseeks_core import config as config_module
from meeseeks_core.config import (
    AppConfig,
    ConfigCheck,
    LLMConfig,
    ensure_example_configs,
    get_config,
    get_config_section,
    get_config_value,
    set_app_config_path,
    set_config_override,
    set_mcp_config_path,
)


def test_app_config_write_and_load_roundtrip(tmp_path):
    """Persist config to disk and load it back."""
    target = tmp_path / "app.json"
    AppConfig().write(target)
    loaded = AppConfig.load(target)
    assert loaded.runtime.envmode
    assert loaded.llm.default_model == "gpt-5.2"


def test_get_config_merges_file_and_override(tmp_path):
    """Merge file payload with in-memory overrides."""
    target = tmp_path / "app.json"
    payload = {"llm": {"api_base": "http://example"}}
    target.write_text(json.dumps(payload), encoding="utf-8")
    set_app_config_path(target)
    set_config_override({"llm": {"api_key": "key"}})

    assert get_config_value("llm", "api_base") == "http://example"
    assert get_config_value("llm", "api_key") == "key"
    section = get_config_section("llm")
    assert section.get("api_base") == "http://example"
    assert section.get("api_key") == "key"


def test_llm_validate_models_requires_api_base():
    """Fail validation when api_base is missing."""
    llm = LLMConfig(api_base="", api_key="key")
    result = llm.validate_models()
    assert result.ok is False
    assert "api_base" in (result.reason or "")


def test_llm_validate_models_requires_api_key():
    """Fail validation when api_key is missing."""
    llm = LLMConfig(api_base="http://example", api_key="")
    result = llm.validate_models()
    assert result.ok is False
    assert "api_key" in (result.reason or "")


def test_llm_validate_models_reports_missing_models(monkeypatch):
    """Report missing configured models when listing succeeds."""
    llm = LLMConfig(api_base="http://example", api_key="key", default_model="gpt-5.2")
    monkeypatch.setattr(LLMConfig, "list_models", lambda *_a, **_k: ["gpt-4o"])
    result = llm.validate_models()
    assert result.ok is False
    assert result.metadata.get("missing_models") == ["gpt-5.2"]


def test_preflight_disables_failed_integrations(monkeypatch):
    """Disable optional integrations when preflight checks fail."""
    set_mcp_config_path("")

    app_config = AppConfig.parse_obj(
        {
            "langfuse": {
                "enabled": True,
                "host": "http://langfuse",
                "public_key": "pk",
                "secret_key": "sk",
            },
            "home_assistant": {
                "enabled": True,
                "url": "http://ha",
                "token": "token",
            },
        }
    )

    monkeypatch.setattr(
        config_module.LLMConfig,
        "validate_models",
        lambda *_a, **_k: ConfigCheck(name="llm", enabled=True, ok=True),
    )
    monkeypatch.setattr(
        config_module.LangfuseConfig,
        "evaluate",
        lambda *_a, **_k: (True, None, {}),
    )
    monkeypatch.setattr(
        config_module.HomeAssistantConfig,
        "evaluate",
        lambda *_a, **_k: (True, None, {}),
    )

    def _raise_probe(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(config_module, "_probe_http", _raise_probe)

    results = asyncio.run(app_config.preflight(disable_on_failure=True))
    assert results["langfuse"]["ok"] is False
    assert results["home_assistant"]["ok"] is False
    assert app_config.langfuse.enabled is False
    assert app_config.home_assistant.enabled is False


def test_ensure_example_configs_writes_files(tmp_path):
    """Write example config payloads when targets are missing."""
    app_path = tmp_path / "app.example.json"
    mcp_path = tmp_path / "mcp.example.json"

    ensure_example_configs(app_path=app_path, mcp_path=mcp_path)

    assert app_path.exists()
    assert mcp_path.exists()
    payload = json.loads(app_path.read_text(encoding="utf-8"))
    assert payload["llm"]["api_base"]


def test_get_config_warns_once_for_missing_file(tmp_path, monkeypatch):
    """Log a single warning when config file is missing."""
    missing = tmp_path / "missing.json"
    set_app_config_path(missing)
    captured: list[str] = []

    monkeypatch.setattr(config_module._logger, "warning", lambda msg, *_a: captured.append(msg))
    _ = get_config()
    _ = get_config()

    assert len(captured) == 1


# -- MEESEEKS_HOME resolution chain ------------------------------------------


class TestResolveMeeseeksHome:
    """Tests for resolve_meeseeks_home()."""

    def test_env_var_takes_precedence(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEESEEKS_HOME", str(tmp_path / "custom"))
        result = config_module.resolve_meeseeks_home()
        assert result == (tmp_path / "custom").resolve()

    def test_default_is_home_dot_meeseeks(self, monkeypatch):
        monkeypatch.delenv("MEESEEKS_HOME", raising=False)
        from pathlib import Path
        result = config_module.resolve_meeseeks_home()
        assert result == Path.home() / ".meeseeks"

    def test_tilde_in_env_expanded(self, monkeypatch):
        monkeypatch.setenv("MEESEEKS_HOME", "~/my-meeseeks")
        result = config_module.resolve_meeseeks_home()
        assert "~" not in str(result)
        assert "my-meeseeks" in str(result)


class TestResolveConfigPath:
    """Tests for _resolve_config_path() priority chain."""

    def test_cwd_configs_dir_wins(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "app.json").write_text("{}", encoding="utf-8")
        result = config_module._resolve_config_path("app.json")
        assert result.resolve() == (configs_dir / "app.json").resolve()

    def test_falls_back_to_home(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MEESEEKS_HOME", str(tmp_path / "home"))
        # No configs/ directory in CWD
        result = config_module._resolve_config_path("app.json")
        assert result == tmp_path / "home" / "app.json"

    def test_home_file_found(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        (home / "app.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("MEESEEKS_HOME", str(home))
        result = config_module._resolve_config_path("app.json")
        assert result == home / "app.json"
        assert result.exists()


class TestRuntimeConfigDefaults:
    """RuntimeConfig resolves empty defaults to MEESEEKS_HOME paths."""

    def test_empty_session_dir_resolves_to_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEESEEKS_HOME", str(tmp_path))
        config_module.reset_config()
        rt = config_module.RuntimeConfig.parse_obj({})
        assert rt.session_dir == str(tmp_path / "sessions")

    def test_empty_cache_dir_resolves_to_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEESEEKS_HOME", str(tmp_path))
        config_module.reset_config()
        rt = config_module.RuntimeConfig.parse_obj({})
        assert rt.cache_dir == str(tmp_path / "cache")

    def test_explicit_relative_path_preserved(self):
        rt = config_module.RuntimeConfig.parse_obj(
            {"session_dir": "./data/sessions"},
        )
        assert rt.session_dir == "./data/sessions"

    def test_explicit_absolute_path_preserved(self, tmp_path):
        rt = config_module.RuntimeConfig.parse_obj(
            {"cache_dir": str(tmp_path / "my_cache")},
        )
        assert rt.cache_dir == str(tmp_path / "my_cache")


class TestGetConfigPathIntegration:
    """get_app_config_path / get_mcp_config_path use the resolution chain."""

    def test_app_config_cwd_first(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        config_module.reset_config()
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "app.json").write_text("{}", encoding="utf-8")
        path = config_module.get_app_config_path()
        assert "configs" in path and "app.json" in path

    def test_app_config_falls_back_to_home(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MEESEEKS_HOME", str(tmp_path / "mhome"))
        config_module.reset_config()
        path = config_module.get_app_config_path()
        assert str(tmp_path / "mhome" / "app.json") == path

    def test_override_takes_priority(self, monkeypatch, tmp_path):
        config_module.reset_config()
        override = tmp_path / "custom.json"
        config_module.set_app_config_path(override)
        assert config_module.get_app_config_path() == str(override)
        config_module.reset_config()


class TestEnsureExampleConfigs:
    """ensure_example_configs respects MEESEEKS_HOME when outside project."""

    def test_scaffolds_to_home_when_no_configs_dir(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "mhome"
        monkeypatch.setenv("MEESEEKS_HOME", str(home))
        app_p, mcp_p = ensure_example_configs()
        assert app_p.parent == home
        assert mcp_p.parent == home
        assert app_p.exists() and mcp_p.exists()

    def test_scaffolds_to_configs_when_present(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        app_p, mcp_p = ensure_example_configs()
        assert app_p.resolve().parent == (tmp_path / "configs").resolve()
        assert mcp_p.resolve().parent == (tmp_path / "configs").resolve()
