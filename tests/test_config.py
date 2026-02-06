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
    assert loaded.runtime.version
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
