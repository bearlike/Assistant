"""Flask-level integration smoke for the Mewbo Apps routes.

The controller-direct tests (``test_apps_routes.py``) exercise the domain logic
but CANNOT catch a wiring bug — whether the controller actually reaches the
request path. This file hits the routes through the shared backend's Flask test
client, which is how it catches the class of bug where a Resource is registered
WITHOUT its ``resource_class_kwargs`` controller (the double `@ns.route` +
`add_resource` registration that 500'd every request). It does NOT re-register
the namespace (forbidden on the shared app); it points the ONE registered
controller at fresh temp stores (the documented pattern) and restores them.
"""

from __future__ import annotations

import mewbo_api.backend as backend
import pytest
from mewbo_api.apps import routes as apps_routes
from mewbo_api.apps.store import JsonAppDataStore, JsonAppStore, JsonPipelineRunStore


@pytest.fixture
def client_and_key(tmp_path):
    """Point the registered apps controller at fresh temp stores; yield (client, key)."""
    ctrl = apps_routes._controller
    assert ctrl is not None, "apps controller must be wired at backend import"
    saved = (ctrl.app_store, ctrl.run_store, ctrl.data_store)
    ctrl.app_store = JsonAppStore(root_dir=tmp_path / "apps")
    ctrl.run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
    ctrl.data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
    try:
        yield backend.app.test_client(), backend.MASTER_API_TOKEN
    finally:
        ctrl.app_store, ctrl.run_store, ctrl.data_store = saved


def test_list_reaches_the_controller(client_and_key):
    client, key = client_and_key
    # A 200 (not 500) proves the controller IS injected into the Resource — the
    # regression guard for the `@ns.route` + `add_resource` double-registration
    # that left the controller un-injected and 500'd every apps request.
    resp = client.get("/api/apps", headers={"X-API-Key": key})
    assert resp.status_code == 200
    assert resp.get_json() == {"apps": []}


def test_unknown_app_404_carries_message(client_and_key):
    client, key = client_and_key
    resp = client.get("/api/apps/nope", headers={"X-API-Key": key})
    assert resp.status_code == 404
    # Error bodies carry a top-level `message` — the console's readJson reads it.
    assert "message" in resp.get_json()


def test_missing_api_key_is_401(client_and_key):
    client, _ = client_and_key
    assert client.get("/api/apps").status_code == 401


def test_data_route_write_verb_is_405(client_and_key):
    client, key = client_and_key
    # The data plane is read-only at REST: POST to a GET-only route -> 405.
    resp = client.post(
        "/api/apps/app-x/data/tasks", headers={"X-API-Key": key}, json={"x": 1}
    )
    assert resp.status_code == 405


def test_submitter_wired_at_startup():
    # e1: init_apps() pushes the concrete lifecycle to the plugin's submitter seam,
    # so submit_app resolves it instead of degrading to "runtime not configured".
    from mewbo_api.apps.plugin.runtime import current_app_submitter

    assert current_app_submitter() is apps_routes._controller.lifecycle


def test_sdk_loaded_into_controller_at_startup():
    # e3: _load_app_sdk_files() read the real SDK off the plugin dir at startup and
    # cached it on the controller for server-side injection into rendered apps.
    sdk_files = apps_routes._controller.sdk_files
    assert "mewbo_app.py" in sdk_files
    assert "MewboApp" in sdk_files["mewbo_app.py"]  # the real SDK class is present


def test_pipeline_runner_wired_at_startup():
    # Phase 2: init_apps hands the SAME AppPipelineRunner the fire seam
    # and the run_pipeline tool use to the controller, so GET/POST
    # .../pipelines/<name> executes for real instead of degrading to the
    # unwired 503 — a regression guard mirroring test_submitter_wired_at_startup.
    from mewbo_api.apps.pipeline_runner import AppPipelineRunner

    assert isinstance(apps_routes._controller.runner, AppPipelineRunner)
