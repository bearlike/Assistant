"""Unit tests for ``ConfigSchemaView`` against the real ``AppConfig`` schema.

Pure tests — no Flask client, no I/O. They pin the protected/secret contract
the ``/config`` endpoints and the faceted Settings frontend depend on.
"""

from mewbo_api.config_view import ConfigSchemaView
from mewbo_core.config import AppConfig

# The canonical classification, mirrored from config.py's annotation contract.
EXPECTED_SECRET = {
    "llm.api_key",
    "langfuse.public_key",
    "langfuse.secret_key",
    "home_assistant.token",
}
EXPECTED_PROTECTED = {
    "api.master_token",
    "runtime.cache_dir",
    "runtime.session_dir",
    "runtime.config_dir",
    "runtime.projects_home",
}


def _view() -> ConfigSchemaView:
    return ConfigSchemaView.from_model(AppConfig)


# ---------- classification ----------


def test_protected_paths():
    assert _view().protected_paths() == EXPECTED_PROTECTED


def test_secret_paths():
    assert _view().secret_paths() == EXPECTED_SECRET


# ---------- public_schema ----------


def test_public_schema_removes_protected_keeps_secret_writeonly():
    schema = _view().public_schema()
    defs = schema["$defs"]

    # Protected: master_token + runtime paths are gone entirely.
    assert "master_token" not in defs["APIConfig"]["properties"]
    runtime_props = defs["RuntimeConfig"]["properties"]
    for field in ("cache_dir", "session_dir", "config_dir", "projects_home"):
        assert field not in runtime_props

    # Secret: api_key kept and marked writeOnly so the console can set it.
    api_key = defs["LLMConfig"]["properties"]["api_key"]
    assert api_key.get("writeOnly") is True
    # Other secrets likewise kept + writeOnly.
    assert defs["LangfuseConfig"]["properties"]["secret_key"].get("writeOnly") is True
    assert defs["HomeAssistantConfig"]["properties"]["token"].get("writeOnly") is True


def test_public_schema_drops_protected_from_required():
    # Build a synthetic schema where a protected field is required, to prove
    # the def's `required` list is pruned.
    schema = {
        "$defs": {
            "Sec": {
                "type": "object",
                "properties": {
                    "keep": {"type": "string"},
                    "gone": {"type": "string", "x-protected": True},
                },
                "required": ["keep", "gone"],
            }
        },
        "properties": {"sec": {"$ref": "#/$defs/Sec"}},
        "type": "object",
    }
    out = ConfigSchemaView(schema).public_schema()
    sec = out["$defs"]["Sec"]
    assert "gone" not in sec["properties"]
    assert sec["required"] == ["keep"]


def test_public_schema_does_not_mutate_source():
    view = _view()
    before = view.protected_paths()
    view.public_schema()
    # Re-deriving from a fresh view yields identical classification (no mutation).
    assert view.protected_paths() == before


# ---------- strip_values ----------


def test_strip_values_drops_protected_and_secret():
    data = {
        "api": {"master_token": "msk-x"},
        "llm": {"api_key": "sk-x", "default_model": "gpt-5.2"},
        "langfuse": {"public_key": "pk", "secret_key": "sk", "host": "h"},
        "home_assistant": {"token": "t", "url": "u"},
        "runtime": {"cache_dir": "/c", "log_level": "INFO"},
    }
    out = _view().strip_values(data)

    # Protected values gone.
    assert "master_token" not in out["api"]
    assert "cache_dir" not in out["runtime"]
    # Secret values gone.
    assert "api_key" not in out["llm"]
    assert "public_key" not in out["langfuse"]
    assert "secret_key" not in out["langfuse"]
    assert "token" not in out["home_assistant"]
    # Non-sensitive siblings retained.
    assert out["llm"]["default_model"] == "gpt-5.2"
    assert out["langfuse"]["host"] == "h"
    assert out["home_assistant"]["url"] == "u"
    assert out["runtime"]["log_level"] == "INFO"
    # Source untouched (deep copy).
    assert data["llm"]["api_key"] == "sk-x"


# ---------- secret_status ----------


def test_secret_status_reports_is_set_bools():
    cfg = {
        "llm": {"api_key": "sk-set"},
        "langfuse": {"public_key": "", "secret_key": "sk"},
        # home_assistant.token absent entirely.
    }
    status = _view().secret_status(cfg)
    assert status == {
        "llm.api_key": True,
        "langfuse.public_key": False,  # empty string -> not set
        "langfuse.secret_key": True,
        "home_assistant.token": False,  # missing -> not set
    }


# ---------- reject_protected ----------


def test_reject_protected_flags_protected_allows_secret():
    view = _view()
    # Protected path present -> flagged.
    assert view.reject_protected({"api": {"master_token": "x"}}) == ["api.master_token"]
    # Secret path present -> allowed (no violation).
    assert view.reject_protected({"llm": {"api_key": "sk"}}) == []
    # Mixed payload: only protected paths returned.
    violations = view.reject_protected(
        {"llm": {"api_key": "sk", "default_model": "m"}, "runtime": {"cache_dir": "/c"}}
    )
    assert violations == ["runtime.cache_dir"]


def test_reject_protected_empty_for_clean_patch():
    assert _view().reject_protected({"llm": {"default_model": "anthropic/claude"}}) == []
