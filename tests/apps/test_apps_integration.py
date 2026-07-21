"""E-wave integration seams for the Mewbo Apps sub-product.

Two cross-stream seams the individual stream tests can't cover on their own:

* **Capability build (e2)** — a maintainer session advertises the ``apps``
  capability (``lifecycle.py``); the plugin manifest gates its session tools on
  it (``plugin.json``). This proves the two meet: a ``build_for`` with only the
  ``apps`` capability (no explicit allowlist, exactly the maintainer's shape)
  surfaces the ``app_data`` tool, so a trigger re-woken maintainer can write.
* **SDK delivery (e3)** — the backend injects the agent SDK into a RENDERED
  app's ``frontend.files`` at the detail seam, while the STORED spec (and its
  version history) stays SDK-free.

Self-contained: builds a controller over real JSON stores with a fake session
backend (the one I/O boundary), mirroring ``test_apps_routes``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from mewbo_api.apps.lifecycle import AppLifecycle
from mewbo_api.apps.models import PipelineSpec
from mewbo_api.apps.plugin import PLUGIN_ROOT
from mewbo_api.apps.routes import AppsRoutesController
from mewbo_api.apps.store import JsonAppDataStore, JsonAppStore, JsonPipelineRunStore
from mewbo_api.apps.tokens import AppReadTokenSigner
from mewbo_core.session_tools import SessionToolRegistry
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.store import JsonTriggerStore

NOW = datetime(2026, 7, 17, 9, 0, 0, tzinfo=timezone.utc)
_SDK_SENTINEL = "# mewbo_app SDK sentinel\nAPP_TOKEN_ENV = '_app_context.json'\n"


class _FakeSessions:
    def __init__(self) -> None:
        self._n = 0

    def create_session(self) -> str:
        self._n += 1
        return f"session-{self._n}"

    def tag_session(self, session_id: str, tag: str) -> None:
        pass

    def append_context_event(self, session_id: str, context: dict) -> None:
        pass

    def append_event(self, session_id: str, event: dict) -> None:
        pass


def _make(tmp_path, *, sdk_files: dict | None = None) -> AppsRoutesController:
    """Build a controller over real JSON stores; ``sdk_files`` is the injected SDK."""
    app_store = JsonAppStore(root_dir=tmp_path / "apps")
    run_store = JsonPipelineRunStore(root_dir=tmp_path / "apps")
    data_store = JsonAppDataStore(root_dir=tmp_path / "apps")
    trigger_store = JsonTriggerStore(data_file=tmp_path / "triggers.json")
    lifecycle = AppLifecycle(
        app_store=app_store,
        trigger_store=trigger_store,
        trigger_policy=TriggerPolicy(),
        sessions=_FakeSessions(),
        now_fn=lambda: NOW,
    )
    return AppsRoutesController(
        lifecycle=lifecycle,
        app_store=app_store,
        run_store=run_store,
        data_store=data_store,
        trigger_store=trigger_store,
        token_signer=AppReadTokenSigner(secret="sekret"),
        require_api_key=lambda: None,
        require_master_token=lambda: None,
        now_fn=lambda: NOW,
        sdk_files=sdk_files,
    )


def _create_live_app(controller: AppsRoutesController) -> str:
    body, status = controller.create_app({"intent": "Digest my email"})
    assert status == 201
    draft = controller.app_store.get(body["app_id"])
    controller.lifecycle.submit(draft, builder_session_id=body["session_id"])
    return body["app_id"]


def _manifest() -> dict:
    return json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )


class TestCapabilityBuild:
    """e2 — the ``apps`` capability surfaces the plugin's session tools."""

    def test_manifest_gates_on_apps_capability(self):
        # The maintainer session stamps client_capabilities:["apps"]; the plugin
        # must gate its tools on the SAME id or they never surface on re-engagement.
        manifest = _manifest()
        assert manifest["requires-capabilities"] == ["apps"]
        ids = {e["tool_id"] for e in manifest["session_tools"]}
        assert {"submit_app", "app_data"} <= ids

    def test_maintainer_capability_build_exposes_app_data(self):
        # Register the manifest's tools exactly as the orchestrator does — stamping
        # the bundle's requires-capabilities onto each factory (load_entry).
        manifest = _manifest()
        caps = tuple(manifest["requires-capabilities"])
        registry = SessionToolRegistry()
        for entry in manifest["session_tools"]:
            registry.load_entry(entry, requires_capabilities=caps)

        # A maintainer session: capability "apps", NO explicit allowlist. The
        # capability gate (build_for) surfaces the app_data + get_app tools so a
        # pipeline re-engagement / repair can read the live app and write its data.
        built = registry.build_for(
            None,
            session_id="maintainer-1",
            event_logger=None,
            session_capabilities=("apps",),
        )
        built_ids = {t.tool_id for t in built}
        assert "app_data" in built_ids
        # get_app must surface through the SAME real plugin-manifest seam — a
        # maintainer/repair session reads its own app back through it.
        assert "get_app" in built_ids

    def test_without_apps_capability_nothing_surfaces(self):
        # A plain session (no "apps") never sees the app tools — the gate is real.
        manifest = _manifest()
        caps = tuple(manifest["requires-capabilities"])
        registry = SessionToolRegistry()
        for entry in manifest["session_tools"]:
            registry.load_entry(entry, requires_capabilities=caps)

        built = registry.build_for(
            None, session_id="plain-1", event_logger=None, session_capabilities=()
        )
        assert built == []


class TestSdkInjection:
    """e3 — the SDK is injected into the rendered detail, never the stored spec."""

    def test_detail_carries_sdk_and_store_stays_clean(self, tmp_path):
        controller = _make(tmp_path, sdk_files={"mewbo_app.py": _SDK_SENTINEL})
        app_id = _create_live_app(controller)

        body, status = controller.get_app(app_id)
        assert status == 200
        files = body["spec"]["frontend"]["files"]
        # The rendered bundle can `import mewbo_app`.
        assert files["mewbo_app.py"] == _SDK_SENTINEL
        assert "app.py" in files  # the app's own entrypoint is untouched

        # The STORED spec is SDK-free — version history is never polluted.
        stored = controller.app_store.get(app_id)
        assert "mewbo_app.py" not in stored.frontend.files
        # ...and neither is the snapshot the detail echoes back.
        for version in body["versions"]:
            assert "mewbo_app.py" not in version["spec"]["frontend"]["files"]

    def test_rollback_detail_also_carries_sdk(self, tmp_path):
        controller = _make(tmp_path, sdk_files={"mewbo_app.py": _SDK_SENTINEL})
        app_id = _create_live_app(controller)
        body, status = controller.rollback_app(app_id, {"version": 1})
        assert status == 200
        assert body["spec"]["frontend"]["files"]["mewbo_app.py"] == _SDK_SENTINEL

    def test_no_sdk_files_leaves_frontend_untouched(self, tmp_path):
        # The default controller (empty sdk_files) injects nothing — the seam is
        # opt-in, so a deployment that fails to load the SDK degrades honestly.
        controller = _make(tmp_path)
        app_id = _create_live_app(controller)
        body, _ = controller.get_app(app_id)
        assert "mewbo_app.py" not in body["spec"]["frontend"]["files"]


def _create_live_app_with_pipeline_source(
    controller: AppsRoutesController, *, name: str = "report"
) -> str:
    """Create+submit a live app with a ``mode="code"`` pipeline whose source lives
    under ``pipelines/`` in the bundle — the server-only engine input the render
    seam must strip from the SERVED frontend.
    """
    body, status = controller.create_app({"intent": "Weekly report"})
    assert status == 201
    app_id = body["app_id"]
    entrypoint = f"pipelines/{name}.py"
    helper = "pipelines/_util.py"
    pipeline = PipelineSpec(
        name=name,
        wake_prompt="unused for code mode",
        on_demand=True,
        mode="code",
        entrypoint=entrypoint,
    )
    entry_source = (
        "def run(params, ctx):\n    from pipelines._util import SECRET\n    return SECRET\n"
    )
    draft = controller.app_store.get(app_id)
    frontend = draft.frontend.model_copy(
        update={
            "files": {
                **draft.frontend.files,
                entrypoint: entry_source,
                helper: "SECRET = 'server-only'\n",
            }
        }
    )
    draft = draft.model_copy(update={"pipelines": [pipeline], "frontend": frontend})
    controller.lifecycle.submit(draft, builder_session_id=body["session_id"])
    return app_id


class TestPipelineSourceStrip:
    """Wave 5 — a code pipeline's source is stripped from the SERVED frontend
    (the browser's file map) while the STORED spec keeps it (the runner's input)."""

    def test_detail_omits_pipeline_source_store_retains_it(self, tmp_path):
        controller = _make(tmp_path, sdk_files={"mewbo_app.py": _SDK_SENTINEL})
        app_id = _create_live_app_with_pipeline_source(controller)

        body, status = controller.get_app(app_id)
        assert status == 200
        files = body["spec"]["frontend"]["files"]
        # No pipeline source reaches the browser...
        assert not any(name.startswith("pipelines/") for name in files)
        # ...while the app's own frontend + the injected SDK ARE served.
        assert "app.py" in files
        assert files["mewbo_app.py"] == _SDK_SENTINEL

        # The STORED spec keeps the pipeline source — the runner reads it.
        stored = controller.app_store.get(app_id)
        assert "pipelines/report.py" in stored.frontend.files
        assert "pipelines/_util.py" in stored.frontend.files
        # ...but the version snapshot ON THE WIRE is stripped too (w5): history
        # must not leak the exact source the live strip withholds.
        for version in body["versions"]:
            assert not any(
                name.startswith("pipelines/")
                for name in version["spec"]["frontend"]["files"]
            )
        # The STORE's version history still retains it (the runner's input) — the
        # STORE keeps every file, only the WIRE strips.
        stored_versions = controller.app_store.list_versions(app_id)
        assert any("pipelines/report.py" in v.spec.frontend.files for v in stored_versions)

    def test_strip_happens_without_sdk_injection_too(self, tmp_path):
        # Stripping is a security concern independent of the SDK: a controller with
        # no sdk_files still omits pipeline source from the served frontend.
        controller = _make(tmp_path)
        app_id = _create_live_app_with_pipeline_source(controller)
        body, _ = controller.get_app(app_id)
        files = body["spec"]["frontend"]["files"]
        assert not any(name.startswith("pipelines/") for name in files)
        assert "app.py" in files

    def test_rollback_detail_also_strips_pipeline_source(self, tmp_path):
        controller = _make(tmp_path, sdk_files={"mewbo_app.py": _SDK_SENTINEL})
        app_id = _create_live_app_with_pipeline_source(controller)
        body, status = controller.rollback_app(app_id, {"version": 1})
        assert status == 200
        files = body["spec"]["frontend"]["files"]
        assert not any(name.startswith("pipelines/") for name in files)
        assert files["mewbo_app.py"] == _SDK_SENTINEL

    def test_strips_code_entrypoint_outside_pipelines_dir(self, tmp_path):
        # The strip keys off the declared code-entrypoint set too, not only the
        # `pipelines/` prefix — an entrypoint at the bundle root is still server-only.
        controller = _make(tmp_path)
        body, _ = controller.create_app({"intent": "Calc"})
        app_id = body["app_id"]
        pipeline = PipelineSpec(
            name="calc", wake_prompt="unused", on_demand=True, mode="code", entrypoint="calc.py"
        )
        draft = controller.app_store.get(app_id)
        calc_source = "def run(params, ctx):\n    return 1\n"
        frontend = draft.frontend.model_copy(
            update={"files": {**draft.frontend.files, "calc.py": calc_source}}
        )
        draft = draft.model_copy(update={"pipelines": [pipeline], "frontend": frontend})
        controller.lifecycle.submit(draft, builder_session_id=body["session_id"])

        detail, _ = controller.get_app(app_id)
        assert "calc.py" not in detail["spec"]["frontend"]["files"]
        assert "calc.py" in controller.app_store.get(app_id).frontend.files
