"""Tests for config loading and preflight checks."""

from __future__ import annotations

import asyncio
import json

from meeseeks_core import config as config_module
from meeseeks_core.config import (
    AppConfig,
    ConfigCheck,
    LLMConfig,
    ProjectConfig,
    _discover_cwd_mcp_json,
    _discover_subtree_mcp_json,
    ensure_example_configs,
    get_config,
    get_config_section,
    get_config_value,
    get_merged_mcp_config,
    reset_config,
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


def test_llm_validate_models_skips_when_no_api_base():
    """Skip model listing gracefully when api_base is empty."""
    llm = LLMConfig(api_base="", api_key="key")
    result = llm.validate_models()
    assert result.ok is True
    assert "direct provider" in (result.reason or "")


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

    app_config = AppConfig.model_validate(
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
    assert "$schema" in payload
    assert payload["llm"]["default_model"]


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
        rt = config_module.RuntimeConfig.model_validate({})
        assert rt.session_dir == str(tmp_path / "sessions")

    def test_empty_cache_dir_resolves_to_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEESEEKS_HOME", str(tmp_path))
        config_module.reset_config()
        rt = config_module.RuntimeConfig.model_validate({})
        assert rt.cache_dir == str(tmp_path / "cache")

    def test_explicit_relative_path_preserved(self):
        rt = config_module.RuntimeConfig.model_validate(
            {"session_dir": "./data/sessions"},
        )
        assert rt.session_dir == "./data/sessions"

    def test_explicit_absolute_path_preserved(self, tmp_path):
        rt = config_module.RuntimeConfig.model_validate(
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
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "mhome"
        monkeypatch.setenv("MEESEEKS_HOME", str(home))
        app_p, mcp_p = ensure_example_configs()
        assert app_p.parent == home
        assert mcp_p.parent == home
        assert app_p.exists() and mcp_p.exists()

    def test_scaffolds_to_configs_when_present(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        app_p, mcp_p = ensure_example_configs()
        assert app_p.resolve().parent == (tmp_path / "configs").resolve()
        assert mcp_p.resolve().parent == (tmp_path / "configs").resolve()


# -- CWD .mcp.json discovery ---------------------------------------------------


class TestDiscoverCwdMcpJson:
    """Tests for _discover_cwd_mcp_json()."""

    def test_discover_cwd_mcp_json_found(self, tmp_path):
        """Discover a .mcp.json file in the given directory."""
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"servers": {"local": {"command": "echo"}}}),
            encoding="utf-8",
        )
        result = _discover_cwd_mcp_json(str(tmp_path))
        assert result is not None
        assert "servers" in result
        assert "local" in result["servers"]

    def test_discover_cwd_mcp_json_mcpservers_schema(self, tmp_path):
        """Normalize mcpServers key to servers (Claude Code style)."""
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"claude_srv": {"command": "test"}}}),
            encoding="utf-8",
        )
        result = _discover_cwd_mcp_json(str(tmp_path))
        assert result is not None
        assert "servers" in result
        assert "mcpServers" not in result
        assert "claude_srv" in result["servers"]

    def test_discover_cwd_mcp_json_missing(self, tmp_path):
        """Return None when no .mcp.json exists in the directory."""
        result = _discover_cwd_mcp_json(str(tmp_path))
        assert result is None

    def test_discover_cwd_mcp_json_invalid_json(self, tmp_path):
        """Return None for malformed JSON."""
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text("{bad json", encoding="utf-8")
        result = _discover_cwd_mcp_json(str(tmp_path))
        assert result is None

    def test_discover_cwd_mcp_json_non_dict(self, tmp_path):
        """Return None when JSON root is not a dict."""
        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text('"just a string"', encoding="utf-8")
        result = _discover_cwd_mcp_json(str(tmp_path))
        assert result is None


class TestDiscoverSubtreeMcpJson:
    """Tests for _discover_subtree_mcp_json()."""

    def test_discovers_nested_mcp_json(self, tmp_path):
        """Find .mcp.json files in subdirectories."""
        (tmp_path / "pkg_a").mkdir()
        (tmp_path / "pkg_a" / ".mcp.json").write_text(
            json.dumps({"servers": {"srv_a": {"command": "a"}}}),
            encoding="utf-8",
        )
        (tmp_path / "pkg_b" / "sub").mkdir(parents=True)
        (tmp_path / "pkg_b" / "sub" / ".mcp.json").write_text(
            json.dumps({"servers": {"srv_b": {"command": "b"}}}),
            encoding="utf-8",
        )
        result = _discover_subtree_mcp_json(str(tmp_path))
        assert len(result) == 2
        # Deepest first
        assert "srv_b" in result[0]["servers"]
        assert "srv_a" in result[1]["servers"]

    def test_skips_cwd_itself(self, tmp_path):
        """CWD's own .mcp.json is NOT included (handled by _discover_cwd_mcp_json)."""
        (tmp_path / ".mcp.json").write_text(json.dumps({"servers": {"root": {}}}), encoding="utf-8")
        result = _discover_subtree_mcp_json(str(tmp_path))
        assert result == []

    def test_prunes_hidden_dirs(self, tmp_path):
        """Hidden directories are skipped."""
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / ".mcp.json").write_text(
            json.dumps({"servers": {"hidden": {}}}), encoding="utf-8"
        )
        result = _discover_subtree_mcp_json(str(tmp_path))
        assert result == []

    def test_respects_max_depth(self, tmp_path):
        """Files beyond max_depth are not discovered."""
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / ".mcp.json").write_text(json.dumps({"servers": {"deep": {}}}), encoding="utf-8")
        assert _discover_subtree_mcp_json(str(tmp_path), max_depth=2) == []
        assert len(_discover_subtree_mcp_json(str(tmp_path), max_depth=3)) == 1

    def test_normalizes_mcpservers_key(self, tmp_path):
        """Claude Code schema (mcpServers) is normalized to servers."""
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"claude_srv": {}}}), encoding="utf-8"
        )
        result = _discover_subtree_mcp_json(str(tmp_path))
        assert "servers" in result[0]
        assert "claude_srv" in result[0]["servers"]

    def test_skips_invalid_json(self, tmp_path):
        """Malformed files are skipped without crashing."""
        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / ".mcp.json").write_text("{bad", encoding="utf-8")
        result = _discover_subtree_mcp_json(str(tmp_path))
        assert result == []


class TestGetMergedMcpConfig:
    """Tests for get_merged_mcp_config()."""

    def test_get_merged_mcp_config_merges(self, tmp_path):
        """Global + CWD configs merge; CWD overrides global servers."""
        # Create a global MCP config
        global_config = tmp_path / "global_mcp.json"
        global_config.write_text(
            json.dumps(
                {
                    "servers": {
                        "global_srv": {"command": "global_cmd"},
                        "shared_srv": {"command": "global_shared"},
                    },
                }
            ),
            encoding="utf-8",
        )
        set_mcp_config_path(str(global_config))

        # Create CWD .mcp.json that overrides shared_srv and adds local_srv
        cwd_dir = tmp_path / "project"
        cwd_dir.mkdir()
        cwd_mcp = cwd_dir / ".mcp.json"
        cwd_mcp.write_text(
            json.dumps(
                {
                    "servers": {
                        "shared_srv": {"command": "cwd_override"},
                        "local_srv": {"command": "local_cmd"},
                    },
                }
            ),
            encoding="utf-8",
        )

        try:
            result = get_merged_mcp_config(cwd=str(cwd_dir))
            servers = result.get("servers", {})
            # Global server preserved
            assert "global_srv" in servers
            assert servers["global_srv"]["command"] == "global_cmd"
            # CWD overrides shared server
            assert servers["shared_srv"]["command"] == "cwd_override"
            # CWD-only server present
            assert "local_srv" in servers
            assert servers["local_srv"]["command"] == "local_cmd"
        finally:
            reset_config()

    def test_get_merged_mcp_config_disabled(self):
        """Return empty dict when MCP is disabled."""
        set_mcp_config_path(None)
        try:
            result = get_merged_mcp_config()
            assert result == {}
        finally:
            reset_config()

    def test_get_merged_mcp_config_no_cwd_file(self, tmp_path):
        """Return global config when no CWD .mcp.json exists."""
        global_config = tmp_path / "mcp.json"
        global_config.write_text(
            json.dumps({"servers": {"srv": {"command": "x"}}}),
            encoding="utf-8",
        )
        set_mcp_config_path(str(global_config))
        try:
            result = get_merged_mcp_config(cwd=str(tmp_path / "empty"))
            assert "srv" in result.get("servers", {})
        finally:
            reset_config()

    def test_get_merged_mcp_config_with_subtree(self, tmp_path):
        """Subtree .mcp.json servers are merged; CWD overrides subtree."""
        # Global config
        global_cfg = tmp_path / "mcp.json"
        global_cfg.write_text(
            json.dumps({"servers": {"global_srv": {"command": "g"}}}),
            encoding="utf-8",
        )
        # CWD with its own .mcp.json
        cwd_dir = tmp_path / "project"
        cwd_dir.mkdir()
        (cwd_dir / ".mcp.json").write_text(
            json.dumps({"servers": {"cwd_srv": {"command": "c"}}}),
            encoding="utf-8",
        )
        # Subtree .mcp.json
        (cwd_dir / "packages" / "api").mkdir(parents=True)
        (cwd_dir / "packages" / "api" / ".mcp.json").write_text(
            json.dumps({"servers": {"sub_srv": {"command": "s"}}}),
            encoding="utf-8",
        )

        set_mcp_config_path(str(global_cfg))
        try:
            result = get_merged_mcp_config(cwd=str(cwd_dir))
            servers = result.get("servers", {})
            assert "global_srv" in servers
            assert "cwd_srv" in servers
            assert "sub_srv" in servers  # subtree server discovered
        finally:
            reset_config()

    def test_get_merged_mcp_config_cwd_overrides_subtree(self, tmp_path):
        """CWD .mcp.json overrides a subtree server with the same name."""
        cwd_dir = tmp_path / "project"
        cwd_dir.mkdir()
        (cwd_dir / ".mcp.json").write_text(
            json.dumps({"servers": {"shared": {"command": "cwd_wins"}}}),
            encoding="utf-8",
        )
        (cwd_dir / "sub").mkdir()
        (cwd_dir / "sub" / ".mcp.json").write_text(
            json.dumps({"servers": {"shared": {"command": "subtree_loses"}}}),
            encoding="utf-8",
        )

        # Point global to a non-existent file (not empty string, which disables MCP)
        set_mcp_config_path(str(tmp_path / "no_such_global.json"))
        try:
            result = get_merged_mcp_config(cwd=str(cwd_dir))
            assert result["servers"]["shared"]["command"] == "cwd_wins"
        finally:
            reset_config()


# -- ProjectConfig in AppConfig -----------------------------------------------


class TestProjectConfigInAppConfig:
    """Tests for projects dict in AppConfig."""

    def test_project_config_in_appconfig(self, tmp_path):
        """Parse projects dict from JSON into AppConfig."""
        target = tmp_path / "app.json"
        payload = {
            "projects": {
                "myapp": {"path": "/home/user/myapp", "description": "My app"},
                "other": {"path": "/tmp/other"},
            },
        }
        target.write_text(json.dumps(payload), encoding="utf-8")
        config = AppConfig.load(target)
        assert "myapp" in config.projects
        assert config.projects["myapp"].description == "My app"
        assert "other" in config.projects
        assert config.projects["other"].description == ""

    def test_project_config_path_normalization(self):
        """Verify path expansion (~ expansion and resolution)."""
        proj = ProjectConfig.model_validate({"path": "~/my-project"})
        # ~ should be expanded to an absolute path
        assert "~" not in proj.path
        assert proj.path.startswith("/")

    def test_project_config_empty_path(self):
        """Empty path stays empty after normalization."""
        proj = ProjectConfig.model_validate({"path": ""})
        assert proj.path == ""

    def test_project_config_non_dict_ignored(self):
        """Non-dict projects value results in empty dict."""
        config = AppConfig.model_validate({"projects": "bad"})
        assert config.projects == {}

    def test_project_config_nested_non_dict_skipped(self):
        """Non-dict project entries are skipped."""
        config = AppConfig.model_validate({"projects": {"a": "not_a_dict", "b": {"path": "/tmp"}}})
        assert "a" not in config.projects
        assert "b" in config.projects
