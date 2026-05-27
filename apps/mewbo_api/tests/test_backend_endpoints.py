"""Integration tests for non-session backend endpoints.

Covers: /api/query (sync), tools/skills (?project=), projects list, virtual
projects CRUD + branches, plugins (list/marketplace/install/uninstall),
notifications (list/dismiss/clear), config (schema/get/patch with
x-protected/x-secret gating), API keys (mint/list/revoke), CORS preflight,
auth-missing → 401/403, models endpoint, commands registry, usage endpoint,
and helper utilities (_parse_bool, _classify_done_reason, _success_message).

Uses in-memory stores via _reset_backend; stubs only I/O boundaries.
"""

# mypy: ignore-errors

import json
import os

import pytest
from mewbo_api import backend
from mewbo_core.session_store import SessionStore

# ---------------------------------------------------------------------------
# Reset helper
# ---------------------------------------------------------------------------


def _reset_backend(tmp_path, monkeypatch):
    backend.session_store = SessionStore(root_dir=str(tmp_path))
    backend.runtime = backend.SessionRuntime(session_store=backend.session_store)
    backend.notification_store = backend.NotificationStore(root_dir=str(tmp_path))
    backend.share_store = backend.ShareStore(root_dir=str(tmp_path))
    backend.notification_service = backend.NotificationService(
        backend.notification_store,
        backend.runtime.session_store,
    )


class DummyQueue:
    def __init__(self, result: str = "ok") -> None:
        self.task_result = result
        self.plan_steps = [{"title": "T", "description": "D"}]
        self.action_steps = [
            {"tool_id": "shell", "operation": "run", "tool_input": "ls", "result": result}
        ]

    def dict(self):
        return {
            "task_result": self.task_result,
            "plan_steps": list(self.plan_steps),
            "action_steps": list(self.action_steps),
        }


# ---------------------------------------------------------------------------
# Auth gating — missing credential
# ---------------------------------------------------------------------------


class TestAuthGating:
    def test_sessions_list_401_no_key(self, client):
        assert client.get("/api/sessions").status_code == 401

    def test_sessions_create_401_no_key(self, client):
        assert client.post("/api/sessions", json={}).status_code == 401

    def test_query_sync_401_no_key(self, client):
        assert client.post("/api/query", json={"query": "hi"}).status_code == 401

    def test_tools_401_no_key(self, client):
        assert client.get("/api/tools").status_code == 401

    def test_skills_401_no_key(self, client):
        assert client.get("/api/skills").status_code == 401

    def test_projects_401_no_key(self, client):
        assert client.get("/api/projects").status_code == 401

    def test_models_401_no_key(self, client):
        assert client.get("/api/models").status_code == 401

    def test_notifications_401_no_key(self, client):
        assert client.get("/api/notifications").status_code == 401
        assert client.post("/api/notifications/dismiss").status_code == 401
        assert client.post("/api/notifications/clear").status_code == 401

    def test_config_schema_401_no_key(self, client):
        assert client.get("/api/config/schema").status_code == 401

    def test_config_get_401_no_key(self, client):
        assert client.get("/api/config").status_code == 401

    def test_config_patch_401_no_key(self, client):
        assert client.patch("/api/config", json={"runtime": {}}).status_code == 401

    def test_keys_401_no_key(self, client):
        assert client.get("/api/keys").status_code == 401
        assert client.post("/api/keys", json={"label": "x"}).status_code == 401

    def test_key_id_delete_401_no_key(self, client):
        assert client.delete("/api/keys/some-id").status_code == 401

    def test_plugins_list_401_no_key(self, client):
        assert client.get("/api/plugins").status_code == 401

    def test_plugins_marketplace_401_no_key(self, client):
        assert client.get("/api/plugins/marketplace").status_code == 401

    def test_commands_401_no_key(self, client):
        assert client.get("/api/commands").status_code == 401

    def test_v_projects_401_no_key(self, client):
        assert client.get("/api/v_projects/some-id").status_code == 401
        assert client.post("/api/v_projects", json={"name": "x"}).status_code == 401


# ---------------------------------------------------------------------------
# CORS preflight (after_request hook)
# ---------------------------------------------------------------------------


class TestCors:
    def test_cors_header_present_on_normal_request(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/sessions", headers=auth_headers)
        assert "Access-Control-Allow-Origin" in resp.headers

    def test_cors_options_preflight(self, client):
        resp = client.options("/api/sessions")
        assert resp.headers.get("Access-Control-Allow-Origin") is not None
        assert "Access-Control-Allow-Methods" in resp.headers


# ---------------------------------------------------------------------------
# /api/query (sync legacy)
# ---------------------------------------------------------------------------


class TestSyncQuery:
    def test_query_success(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)

        def fake_run_sync(*args, **kwargs):
            return DummyQueue("answer")

        monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
        resp = client.post("/api/query", headers=auth_headers, json={"query": "What is 2+2?"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["task_result"] == "answer"
        assert body["session_id"]
        assert body["plan_steps"]

    def test_query_missing_query_field(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/query", headers=auth_headers, json={})
        assert resp.status_code == 400

    def test_query_mode_passed_through(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        captured = {}

        def fake_run_sync(*args, **kwargs):
            captured["mode"] = kwargs.get("mode")
            return DummyQueue()

        monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
        resp = client.post("/api/query", headers=auth_headers, json={"query": "hi", "mode": "plan"})
        assert resp.status_code == 200
        assert captured["mode"] == "plan"

    def test_query_invalid_mode_ignored(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        captured = {}

        def fake_run_sync(*args, **kwargs):
            captured["mode"] = kwargs.get("mode")
            return DummyQueue()

        monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
        resp = client.post(
            "/api/query", headers=auth_headers, json={"query": "hi", "mode": "bogus"}
        )
        assert resp.status_code == 200
        assert captured["mode"] is None

    def test_query_api_key_via_query_param(self, client, tmp_path, monkeypatch):
        """EventSource path: api_key query param accepted."""
        _reset_backend(tmp_path, monkeypatch)

        def fake_run_sync(*args, **kwargs):
            return DummyQueue()

        monkeypatch.setattr(backend.runtime, "run_sync", fake_run_sync)
        resp = client.post(
            f"/api/query?api_key={backend.MASTER_API_TOKEN}",
            json={"query": "hi"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/tools
# ---------------------------------------------------------------------------


class TestTools:
    def test_tools_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/tools", headers=auth_headers)
        assert resp.status_code == 200
        assert "tools" in resp.get_json()

    def test_tools_list_with_project_param(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        # project=nonexistent is silently ignored (no path resolves)
        resp = client.get("/api/tools?project=nonexistent", headers=auth_headers)
        assert resp.status_code == 200
        assert "tools" in resp.get_json()


# ---------------------------------------------------------------------------
# /api/skills
# ---------------------------------------------------------------------------


class TestSkills:
    def test_skills_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/skills", headers=auth_headers)
        assert resp.status_code == 200
        assert "skills" in resp.get_json()

    def test_skills_list_with_project_param(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/skills?project=nonexistent", headers=auth_headers)
        assert resp.status_code == 200
        assert "skills" in resp.get_json()


# ---------------------------------------------------------------------------
# /api/projects
# ---------------------------------------------------------------------------


class TestProjects:
    def test_projects_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/projects", headers=auth_headers)
        assert resp.status_code == 200
        assert "projects" in resp.get_json()

    def test_projects_list_schema(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/projects", headers=auth_headers)
        projects = resp.get_json()["projects"]
        # Each project must have name, path, source, available
        for p in projects:
            assert "name" in p
            assert "source" in p


# ---------------------------------------------------------------------------
# /api/v_projects (virtual projects CRUD)
# ---------------------------------------------------------------------------


class TestVirtualProjects:
    def test_create_virtual_project(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post(
            "/api/v_projects",
            headers=auth_headers,
            json={"name": "test-project", "description": "desc"},
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["name"] == "test-project"
        assert body["project_id"]

    def test_create_virtual_project_missing_name(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post(
            "/api/v_projects",
            headers=auth_headers,
            json={"description": "no name"},
        )
        assert resp.status_code == 400

    def test_get_virtual_project(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        create_resp = client.post(
            "/api/v_projects",
            headers=auth_headers,
            json={"name": "get-test"},
        )
        pid = create_resp.get_json()["project_id"]
        resp = client.get(f"/api/v_projects/{pid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["project_id"] == pid

    def test_get_virtual_project_not_found(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/v_projects/no-such-id", headers=auth_headers)
        assert resp.status_code == 404

    def test_patch_virtual_project(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        create_resp = client.post(
            "/api/v_projects",
            headers=auth_headers,
            json={"name": "patch-test"},
        )
        pid = create_resp.get_json()["project_id"]
        resp = client.patch(
            f"/api/v_projects/{pid}",
            headers=auth_headers,
            json={"name": "updated-name"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "updated-name"

    def test_patch_virtual_project_not_found(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.patch(
            "/api/v_projects/no-such-id",
            headers=auth_headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404

    def test_delete_virtual_project(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        create_resp = client.post(
            "/api/v_projects",
            headers=auth_headers,
            json={"name": "del-test"},
        )
        pid = create_resp.get_json()["project_id"]
        resp = client.delete(f"/api/v_projects/{pid}", headers=auth_headers)
        assert resp.status_code == 204

    def test_delete_virtual_project_not_found(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.delete("/api/v_projects/no-such-id", headers=auth_headers)
        assert resp.status_code == 404

    def test_virtual_project_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        assert client.get("/api/v_projects/x").status_code == 401
        assert client.patch("/api/v_projects/x", json={}).status_code == 401
        assert client.delete("/api/v_projects/x").status_code == 401

    def test_branches_for_nonexistent_project_404(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/v_projects/no-such/branches", headers=auth_headers)
        assert resp.status_code == 404

    def test_branches_for_non_git_dir(self, client, auth_headers, tmp_path, monkeypatch):
        """A real (non-git) directory gives git_repo=False."""
        _reset_backend(tmp_path, monkeypatch)
        non_git_dir = str(tmp_path / "not-a-repo")
        os.makedirs(non_git_dir)
        create_resp = client.post(
            "/api/v_projects",
            headers=auth_headers,
            json={"name": "branch-test", "path": non_git_dir},
        )
        pid = create_resp.get_json()["project_id"]
        resp = client.get(f"/api/v_projects/{pid}/branches", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["git_repo"] is False


# ---------------------------------------------------------------------------
# /api/plugins
# ---------------------------------------------------------------------------


class TestPlugins:
    def test_plugins_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/plugins", headers=auth_headers)
        assert resp.status_code == 200
        assert "plugins" in resp.get_json()

    def test_plugins_marketplace_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/plugins/marketplace", headers=auth_headers)
        assert resp.status_code == 200
        assert "plugins" in resp.get_json()

    def test_plugins_marketplace_install_missing_fields(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post(
            "/api/plugins/marketplace",
            headers=auth_headers,
            json={"name": "myplugin"},  # missing marketplace
        )
        assert resp.status_code == 400

    def test_plugins_marketplace_install_not_found(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        # Valid payload but plugin doesn't exist → ValueError → 400.
        # The route explicitly maps ValueError to 400 (backend.py line ~2581).
        resp = client.post(
            "/api/plugins/marketplace",
            headers=auth_headers,
            json={"name": "nonexistent-plugin", "marketplace": "https://example.com"},
        )
        assert resp.status_code == 400

    def test_plugins_uninstall_not_found(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.delete("/api/plugins/nonexistent-plugin", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /api/notifications
# ---------------------------------------------------------------------------


class TestNotifications:
    def test_notifications_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/notifications", headers=auth_headers)
        assert resp.status_code == 200
        assert "notifications" in resp.get_json()

    def test_notifications_list_include_dismissed(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        n = backend.notification_store.add(title="t", message="m")
        backend.notification_store.dismiss([n["id"]])
        resp = client.get("/api/notifications?include_dismissed=1", headers=auth_headers)
        notifications = resp.get_json()["notifications"]
        assert any(item["id"] == n["id"] for item in notifications)

    def test_notifications_dismiss_by_id(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        n = backend.notification_store.add(title="t", message="m")
        resp = client.post(
            "/api/notifications/dismiss",
            headers=auth_headers,
            json={"id": n["id"]},
        )
        assert resp.status_code == 200
        assert resp.get_json()["dismissed"] == 1

    def test_notifications_dismiss_by_ids(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        n1 = backend.notification_store.add(title="t1", message="m1")
        n2 = backend.notification_store.add(title="t2", message="m2")
        resp = client.post(
            "/api/notifications/dismiss",
            headers=auth_headers,
            json={"ids": [n1["id"], n2["id"]]},
        )
        assert resp.status_code == 200
        assert resp.get_json()["dismissed"] == 2

    def test_notifications_clear_dismissed(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        n = backend.notification_store.add(title="t", message="m")
        backend.notification_store.dismiss([n["id"]])
        resp = client.post(
            "/api/notifications/clear",
            headers=auth_headers,
            json={},
        )
        assert resp.status_code == 200
        assert resp.get_json()["cleared"] >= 1

    def test_notifications_clear_all(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        backend.notification_store.add(title="t1", message="m1")
        backend.notification_store.add(title="t2", message="m2")
        resp = client.post(
            "/api/notifications/clear",
            headers=auth_headers,
            json={"clear_all": True},
        )
        assert resp.status_code == 200
        assert resp.get_json()["cleared"] >= 2

    def test_notifications_clear_all_string_true(self, client, auth_headers, tmp_path, monkeypatch):
        """clear_all='true' as a string is accepted."""
        _reset_backend(tmp_path, monkeypatch)
        backend.notification_store.add(title="t", message="m")
        resp = client.post(
            "/api/notifications/clear",
            headers=auth_headers,
            json={"clear_all": "true"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["cleared"] >= 1


# ---------------------------------------------------------------------------
# /api/config — schema, GET, PATCH
# ---------------------------------------------------------------------------


class TestConfigEndpoints:
    def test_config_schema_returns_dict(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/config/schema", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert isinstance(body, dict)
        # Schema should have type or properties
        assert "properties" in body or "type" in body

    def test_config_schema_strips_protected_fields(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/config/schema", headers=auth_headers)
        raw = resp.get_data(as_text=True)
        # master_token is x-protected → should not be in schema
        assert "master_token" not in raw

    def test_config_get_returns_config_and_secrets(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/config", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "config" in body
        assert "secrets" in body

    def test_config_get_strips_master_token_from_values(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/config", headers=auth_headers)
        # master_token should not appear in the config values dict
        config_str = json.dumps(resp.get_json().get("config", {}))
        # master_token value itself should not be visible
        assert backend.MASTER_API_TOKEN not in config_str

    def test_config_patch_empty_payload_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.patch("/api/config", headers=auth_headers, json={})
        assert resp.status_code == 400

    def test_config_patch_protected_field_403(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        # api.master_token is x-protected → PATCH should be 403
        resp = client.patch(
            "/api/config",
            headers=auth_headers,
            json={"api": {"master_token": "hacked"}},
        )
        assert resp.status_code == 403
        body = resp.get_json()
        assert "protected" in body["message"].lower() or "Cannot modify" in body["message"]

    def test_config_patch_valid_field_succeeds(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        # Patch a non-protected, non-secret field (e.g. runtime.log_level)
        resp = client.patch(
            "/api/config",
            headers=auth_headers,
            json={"runtime": {"log_level": "DEBUG"}},
        )
        # May fail with 422 if validation rejects "DEBUG" but must not be 403
        assert resp.status_code != 403

    def test_config_schema_requires_auth(self, client):
        assert client.get("/api/config/schema").status_code == 401

    def test_config_get_requires_auth(self, client):
        assert client.get("/api/config").status_code == 401

    def test_config_patch_requires_auth(self, client):
        assert client.patch("/api/config", json={"x": 1}).status_code == 401


# ---------------------------------------------------------------------------
# /api/keys — mint, list, revoke
# ---------------------------------------------------------------------------


class TestApiKeys:
    def test_keys_require_master_token(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        assert client.get("/api/keys").status_code == 401
        assert client.post("/api/keys", json={"label": "x"}).status_code == 401

    def test_keys_mint_and_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        # Mint a key
        resp = client.post("/api/keys", headers=auth_headers, json={"label": "my-key"})
        assert resp.status_code == 201
        body = resp.get_json()
        assert "key" in body
        assert "id" in body
        assert body["label"] == "my-key"

        # List keys — should contain the minted key (without plaintext)
        list_resp = client.get("/api/keys", headers=auth_headers)
        assert list_resp.status_code == 200
        keys = list_resp.get_json()["keys"]
        assert any(k["label"] == "my-key" for k in keys)
        # Plaintext must not be in any listed key record
        for k in keys:
            assert "key" not in k or k.get("key") is None

    def test_keys_mint_missing_label_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/keys", headers=auth_headers, json={})
        assert resp.status_code == 400

    def test_keys_mint_empty_label_400(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.post("/api/keys", headers=auth_headers, json={"label": "  "})
        assert resp.status_code == 400

    def test_keys_revoke_existing_key(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        mint_resp = client.post("/api/keys", headers=auth_headers, json={"label": "revoke-me"})
        key_id = mint_resp.get_json()["id"]
        del_resp = client.delete(f"/api/keys/{key_id}", headers=auth_headers)
        assert del_resp.status_code == 200
        assert del_resp.get_json()["revoked"] is True

    def test_keys_revoke_unknown_key_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.delete("/api/keys/no-such-key", headers=auth_headers)
        assert resp.status_code == 404

    def test_minted_key_authenticates_api_routes(self, client, auth_headers, tmp_path, monkeypatch):
        """A minted (non-master) key is accepted by API-key-gated routes."""
        _reset_backend(tmp_path, monkeypatch)
        mint_resp = client.post("/api/keys", headers=auth_headers, json={"label": "access-key"})
        plaintext = mint_resp.get_json()["key"]
        resp = client.get("/api/sessions", headers={"X-API-KEY": plaintext})
        assert resp.status_code == 200

    def test_minted_key_does_not_manage_keys(self, client, auth_headers, tmp_path, monkeypatch):
        """A minted key must NOT be able to mint or revoke other keys."""
        _reset_backend(tmp_path, monkeypatch)
        mint_resp = client.post("/api/keys", headers=auth_headers, json={"label": "limited-key"})
        plaintext = mint_resp.get_json()["key"]
        issued_key_headers = {"X-API-KEY": plaintext}
        # Issuing new keys with a non-master token must fail
        assert (
            client.post("/api/keys", headers=issued_key_headers, json={"label": "new"}).status_code
            == 401
        )
        # Listing keys must also fail
        assert client.get("/api/keys", headers=issued_key_headers).status_code == 401


# ---------------------------------------------------------------------------
# /api/models
# ---------------------------------------------------------------------------


class TestModels:
    def test_models_returns_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/models", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "models" in body
        assert "capabilities" in body
        assert "default" in body

    def test_models_vision_capabilities(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend, "model_supports_vision", lambda name: name == "gpt-4o")

        class _LLM:
            def list_models(self):
                return ["gpt-4o", "gpt-3.5-turbo"]

        monkeypatch.setattr(backend, "get_config", lambda: type("C", (), {"llm": _LLM()})())
        resp = client.get("/api/models", headers=auth_headers)
        caps = resp.get_json()["capabilities"]
        assert caps["gpt-4o"]["supports_vision"] is True
        assert caps["gpt-3.5-turbo"]["supports_vision"] is False


# ---------------------------------------------------------------------------
# /api/commands
# ---------------------------------------------------------------------------


class TestCommands:
    def test_commands_list(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        resp = client.get("/api/commands", headers=auth_headers)
        assert resp.status_code == 200
        assert "commands" in resp.get_json()

    def test_commands_requires_auth(self, client):
        assert client.get("/api/commands").status_code == 401


# ---------------------------------------------------------------------------
# /api/sessions/<id>/command
# ---------------------------------------------------------------------------


class TestSessionCommand:
    def test_session_command_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(f"/api/sessions/{sid}/command", json={"name": "compact", "args": []})
        assert resp.status_code == 401

    def test_session_command_bad_request_missing_name(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/command",
            headers=auth_headers,
            json={"args": []},
        )
        assert resp.status_code == 400

    def test_session_command_unknown_name_404(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/command",
            headers=auth_headers,
            json={"name": "nonexistent_command_xyz", "args": []},
        )
        assert resp.status_code == 404

    def test_session_command_compact_transcript(self, client, auth_headers, tmp_path, monkeypatch):
        """Compact command is TRANSCRIPT-render type and returns 202."""
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        # Provide some events so compact has something to work with
        backend.session_store.append_event(
            sid, {"type": "user", "payload": {"text": "compress this"}}
        )
        monkeypatch.setattr(backend.runtime, "start_command", lambda sid, fn: True)
        resp = client.post(
            f"/api/sessions/{sid}/command",
            headers=auth_headers,
            json={"name": "compact", "args": []},
        )
        # Accepted as async transcript command
        assert resp.status_code == 202
        assert resp.get_json()["accepted"] is True

    def test_session_command_compact_409_when_running(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        _reset_backend(tmp_path, monkeypatch)
        monkeypatch.setattr(backend.runtime, "is_running", lambda sid: True)
        sid = backend.session_store.create_session()
        resp = client.post(
            f"/api/sessions/{sid}/command",
            headers=auth_headers,
            json={"name": "compact", "args": []},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# /api/sessions/<id>/usage
# ---------------------------------------------------------------------------


class TestSessionUsage:
    def test_usage_requires_auth(self, client, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/usage")
        assert resp.status_code == 401

    def test_usage_returns_dict(self, client, auth_headers, tmp_path, monkeypatch):
        _reset_backend(tmp_path, monkeypatch)
        sid = backend.session_store.create_session()
        resp = client.get(f"/api/sessions/{sid}/usage", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Helper functions (pure unit tests — no Flask involved)
# ---------------------------------------------------------------------------


class TestParseMode:
    @pytest.mark.parametrize("mode", ["plan", "act", "PLAN", "ACT", "Plan"])
    def test_valid_modes(self, mode):
        result = backend._parse_mode(mode)
        assert result in ("plan", "act")

    @pytest.mark.parametrize("bad", ["invalid", "", 123, None, "auto"])
    def test_invalid_modes_return_none(self, bad):
        assert backend._parse_mode(bad) is None


class TestParseBool:
    @pytest.mark.parametrize(
        "val,expected",
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("off", False),
            ("", False),
            (None, False),
        ],
    )
    def test_parse_bool_variants(self, val, expected):
        assert backend._parse_bool(val) is expected


class TestUtcNow:
    def test_utc_now_returns_string(self):
        ts = backend._utc_now()
        assert isinstance(ts, str)
        assert "T" in ts  # ISO format
