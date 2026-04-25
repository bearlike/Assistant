"""Tests for the config API endpoints (GET/PATCH /api/config, GET /api/config/schema)."""

# mypy: ignore-errors
import json
import tempfile
from pathlib import Path

from mewbo_api import backend
from mewbo_core.config import reset_config, set_app_config_path


def _setup_temp_config(monkeypatch, payload: dict | None = None):
    """Write a temp config file and point the backend at it."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    json.dump(payload or {}, tmp)
    tmp.flush()
    tmp.close()
    set_app_config_path(tmp.name)
    monkeypatch.setattr(backend, "MASTER_API_TOKEN", "test-token")
    return tmp.name


def _teardown(path: str):
    reset_config()
    Path(path).unlink(missing_ok=True)


# ---------- GET /api/config/schema ----------


def test_config_schema_requires_auth():
    """GET /api/config/schema without API key returns 401."""
    client = backend.app.test_client()
    resp = client.get("/api/config/schema")
    assert resp.status_code == 401


def test_config_schema_strips_protected(monkeypatch):
    """GET /api/config/schema omits x-protected fields."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.get(
            "/api/config/schema",
            headers={"X-API-Key": "test-token"},
        )
        assert resp.status_code == 200
        schema = resp.get_json()
        # APIConfig should not have master_token
        api_def = schema.get("$defs", {}).get("APIConfig", {})
        api_props = api_def.get("properties", {})
        assert "master_token" not in api_props
        # LLMConfig should not have api_key
        llm_def = schema.get("$defs", {}).get("LLMConfig", {})
        llm_props = llm_def.get("properties", {})
        assert "api_key" not in llm_props
        # LangfuseConfig should not have secret_key or public_key
        lf_def = schema.get("$defs", {}).get("LangfuseConfig", {})
        lf_props = lf_def.get("properties", {})
        assert "secret_key" not in lf_props
        assert "public_key" not in lf_props
    finally:
        _teardown(path)


# ---------- GET /api/config ----------


def test_config_get_omits_protected(monkeypatch):
    """GET /api/config omits protected field values."""
    path = _setup_temp_config(monkeypatch, {"api": {"master_token": "secret"}})
    try:
        client = backend.app.test_client()
        resp = client.get(
            "/api/config",
            headers={"X-API-Key": "test-token"},
        )
        assert resp.status_code == 200
        data = resp.get_json()["config"]
        # master_token should be stripped
        assert "master_token" not in data.get("api", {})
        # api_key should be stripped
        assert "api_key" not in data.get("llm", {})
        # Non-protected fields should still be present
        assert "default_model" in data.get("llm", {})
    finally:
        _teardown(path)


# ---------- PATCH /api/config ----------


def test_config_patch_rejects_protected(monkeypatch):
    """PATCH /api/config with protected field returns 403."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"api": {"master_token": "hacked"}},
        )
        assert resp.status_code == 403
        assert "protected" in resp.get_json()["message"].lower()
    finally:
        _teardown(path)


def test_config_patch_validates_input(monkeypatch):
    """PATCH /api/config with invalid type returns 422."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        # llm expects an object, sending a string should fail validation
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": "not-an-object"},
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert "errors" in body
    finally:
        _teardown(path)


def test_config_patch_success(monkeypatch):
    """PATCH /api/config with valid data persists and returns updated config."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={"llm": {"default_model": "anthropic/claude-sonnet-4-6"}},
        )
        assert resp.status_code == 200
        data = resp.get_json()["config"]
        assert data["llm"]["default_model"] == "anthropic/claude-sonnet-4-6"

        # Verify it persisted to disk
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["llm"]["default_model"] == "anthropic/claude-sonnet-4-6"
    finally:
        _teardown(path)


def test_config_patch_empty_payload(monkeypatch):
    """PATCH /api/config with empty body returns 400."""
    path = _setup_temp_config(monkeypatch)
    try:
        client = backend.app.test_client()
        resp = client.patch(
            "/api/config",
            headers={"X-API-Key": "test-token"},
            json={},
        )
        assert resp.status_code == 400
    finally:
        _teardown(path)


# ---------- Project CWD validation ----------


def test_projects_endpoint_includes_available(monkeypatch, tmp_path):
    """GET /api/projects includes available flag per project."""
    real_dir = str(tmp_path / "real")
    Path(real_dir).mkdir()
    fake_dir = str(tmp_path / "nonexistent")
    config = {
        "projects": {
            "real": {"path": real_dir, "description": "exists"},
            "fake": {"path": fake_dir, "description": "missing"},
        }
    }
    path = _setup_temp_config(monkeypatch, config)
    try:
        client = backend.app.test_client()
        resp = client.get(
            "/api/projects",
            headers={"X-API-Key": "test-token"},
        )
        assert resp.status_code == 200
        projects = {p["name"]: p for p in resp.get_json()["projects"]}
        assert projects["real"]["available"] is True
        assert projects["fake"]["available"] is False
    finally:
        _teardown(path)


def test_resolve_project_cwd_rejects_missing_dir(monkeypatch, tmp_path):
    """_resolve_project_cwd raises ValueError for nonexistent project path."""
    config = {
        "projects": {
            "phantom": {"path": str(tmp_path / "nonexistent"), "description": "gone"},
        }
    }
    path = _setup_temp_config(monkeypatch, config)
    try:
        import pytest

        with pytest.raises(ValueError, match="not found"):
            backend._resolve_project_cwd({"project": "phantom"})
    finally:
        _teardown(path)
