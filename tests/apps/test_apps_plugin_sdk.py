"""Tests for the injected SDK (apps/plugin/sdk/mewbo_app.py) — pure logic only.

The SDK is bundled into an app's stlite kernel and imported as ``mewbo_app``; its
HTTP transport is browser-only (pyodide ``js``). Most tests exercise the pure
parts — context loading, the ``data.query`` envelope unwrap, and the consolidated
``/system`` read — by faking the ``_get`` transport, so no browser is needed. The
transport-seam tests further down drive the real ``_send`` by planting a fake
``js`` module in ``sys.modules`` (pyodide's `from js import ...` resolves through
the module cache regardless of who imports it), so the origin-aware token
placement and error wrapping are covered without a browser either. The module is
loaded from its file path (it is data in the wheel, not an importable package
module).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_SDK_PATH = (
    Path(__file__).resolve().parents[2]
    / "apps/mewbo_api/src/mewbo_api/apps/plugin/sdk/mewbo_app.py"
)


def _load_sdk():
    spec = importlib.util.spec_from_file_location("mewbo_app_under_test", _SDK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sdk = _load_sdk()


class FakeApp:
    """Stands in for :class:`MewboApp` — records ``_get``/``_post`` calls, returns canned JSON.

    ``scope`` mirrors the render token's scope the real ``MewboApp`` reads from its
    context, gating the ``pipelines.submit`` write path.
    """

    def __init__(self, responses: dict[str, Any], *, scope: str = "read") -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []
        self.posts: list[tuple[str, Any]] = []
        self._scope = scope

    def _get(self, path: str, params: dict | None = None) -> Any:
        self.calls.append((path, params))
        return self.responses.get(path)

    def _post(self, path: str, body: Any) -> Any:
        self.posts.append((path, body))
        return self.responses.get(path)


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------


def test_context_missing_raises(tmp_path):
    with pytest.raises(sdk.AppContextMissing):
        sdk.MewboApp(context_file=str(tmp_path / "nope.json"))


def test_context_loads_token_base_and_id(tmp_path):
    ctx = tmp_path / "_app_context.json"
    ctx.write_text('{"token": "tok", "api_base": "https://h/", "app_id": "a1"}', encoding="utf-8")
    app = sdk.MewboApp(context_file=str(ctx))
    assert app.app_id == "a1"


# ---------------------------------------------------------------------------
# data.query — unwrap the AppDataDoc envelope to the bare doc body
# ---------------------------------------------------------------------------


def test_data_query_unwraps_appdatadoc_envelope():
    app = FakeApp(
        {
            "data/task_groups": {
                "documents": [
                    {"app_id": "a", "collection": "task_groups", "key": "k",
                     "doc": {"title": "T", "count": 2}, "updated_at": "2026"}
                ]
            }
        }
    )
    rows = sdk._AppData(app).query("task_groups")
    assert rows == [{"title": "T", "count": 2}]
    assert app.calls[0][0] == "data/task_groups"


def test_data_query_passes_filter_and_limit():
    app = FakeApp({"data/emails": {"documents": []}})
    sdk._AppData(app).query("emails", filter={"x": 1}, limit=5)
    path, params = app.calls[0]
    assert path == "data/emails"
    assert params is not None
    assert params["limit"] == "5"
    assert "filter" in params


# ---------------------------------------------------------------------------
# system.* — one consolidated GET /system feeds triggers/runs/freshness
# ---------------------------------------------------------------------------


def test_system_reads_the_consolidated_endpoint():
    health = {
        "freshness": {"seconds": 10},
        "triggers": [{"id": "t1"}],
        "runs": [{"status": "succeeded"}, {"status": "failed"}],
    }
    app = FakeApp({"system": health})
    system = sdk._AppSystem(app)

    assert system.triggers() == [{"id": "t1"}]
    assert system.runs(limit=1) == [{"status": "succeeded"}]
    assert system.freshness() == {"seconds": 10}
    # every accessor reads the ONE /system endpoint (not /system/triggers etc.)
    assert {call[0] for call in app.calls} == {"system"}


# ---------------------------------------------------------------------------
# pipelines.* (Phase 2) — read-scoped list/invoke over the injected SDK
# ---------------------------------------------------------------------------


def test_pipelines_list_unwraps_envelope():
    app = FakeApp({"pipelines": {"pipelines": [{"name": "ingest", "mode": "code"}]}})
    rows = sdk._AppPipelines(app).list()
    assert rows == [{"name": "ingest", "mode": "code"}]
    assert app.calls[0][0] == "pipelines"


def test_pipelines_list_accepts_bare_array():
    app = FakeApp({"pipelines": [{"name": "ingest"}]})
    assert sdk._AppPipelines(app).list() == [{"name": "ingest"}]


def test_pipelines_run_without_params_sends_no_query():
    app = FakeApp({"pipelines/ingest": {"rows": 5}})
    result = sdk._AppPipelines(app).run("ingest")
    assert result == {"rows": 5}
    path, params = app.calls[0]
    assert path == "pipelines/ingest"
    assert params is None


def test_pipelines_run_sends_flat_query_keys_not_a_params_blob():
    """Each params entry is its OWN query key — the server reads request.args flat
    and coerces per-property against params_schema; a wrapping `params=` key 400s.
    """
    app = FakeApp({"pipelines/ingest": {"rows": 1}})
    sdk._AppPipelines(app).run("ingest", params={"since": "2026-01-01", "limit": 5})
    path, params = app.calls[0]
    assert path == "pipelines/ingest"
    assert params == {"since": "2026-01-01", "limit": "5"}
    assert "params" not in params


def test_pipelines_run_omits_none_valued_params():
    app = FakeApp({"pipelines/ingest": {"rows": 1}})
    sdk._AppPipelines(app).run("ingest", params={"since": "2026-01-01", "category": None})
    _, params = app.calls[0]
    assert params == {"since": "2026-01-01"}


def test_pipelines_run_rejects_object_valued_param_client_side():
    """A dict/list param value can't be sent over the read-only GET path (v1 gap) —
    reject it HERE, before any network call, rather than sending a str()-repr the
    server would then reject with a much less legible schema error.
    """
    app = FakeApp({"pipelines/ingest": {"rows": 1}})
    with pytest.raises(sdk.AppRequestError, match="filters"):
        sdk._AppPipelines(app).run("ingest", params={"filters": {"status": "open"}})
    assert app.calls == []  # rejected before any request was made


def test_pipelines_run_rejects_array_valued_param_client_side():
    app = FakeApp({"pipelines/ingest": {"rows": 1}})
    with pytest.raises(sdk.AppRequestError, match="tags"):
        sdk._AppPipelines(app).run("ingest", params={"tags": ["a", "b"]})
    assert app.calls == []


def test_mewbo_app_exposes_pipelines_namespace(tmp_path):
    ctx = tmp_path / "_app_context.json"
    ctx.write_text('{"token": "tok", "api_base": "https://h/", "app_id": "a1"}', encoding="utf-8")
    app = sdk.MewboApp(context_file=str(ctx))
    assert isinstance(app.pipelines, sdk._AppPipelines)


# ---------------------------------------------------------------------------
# pipelines.submit (wave 5) — the write-back path (POST, write-scoped)
# ---------------------------------------------------------------------------


def test_pipelines_submit_posts_body_on_write_scoped_app():
    """submit POSTs its params as the JSON body (the write path), not the GET run path."""
    app = FakeApp({"pipelines/add-note": {"saved": "note:1"}}, scope="write")
    result = sdk._AppPipelines(app).submit("add-note", {"text": "hi", "pinned": True})
    assert result == {"saved": "note:1"}
    assert app.posts == [("pipelines/add-note", {"text": "hi", "pinned": True})]
    assert app.calls == []  # a POST, never the read-only GET path


def test_pipelines_submit_supports_object_and_array_params():
    """The write path takes a JSON body, so object/array params are sent verbatim —
    exactly the case run() (the GET path) rejects client-side.
    """
    app = FakeApp({"pipelines/save": {"ok": 1}}, scope="write")
    params = {"filters": {"status": "open"}, "tags": ["a", "b"]}
    sdk._AppPipelines(app).submit("save", params)
    assert app.posts == [("pipelines/save", params)]


def test_pipelines_submit_none_params_sends_empty_object():
    app = FakeApp({"pipelines/save": {"ok": 1}}, scope="write")
    sdk._AppPipelines(app).submit("save")
    assert app.posts == [("pipelines/save", {})]


def test_pipelines_submit_read_scoped_app_raises_client_side():
    """A read-token page (this app declares no user_writable pipeline) can't write —
    raise BEFORE any network call with an actionable message, never a silent 403.
    """
    app = FakeApp({}, scope="read")
    with pytest.raises(sdk.AppRequestError, match="write-scoped"):
        sdk._AppPipelines(app).submit("add-note", {"text": "hi"})
    assert app.posts == [] and app.calls == []  # rejected before any request


# ---------------------------------------------------------------------------
# pipelines.refresh — on-demand fire (POST /fire, read-auth, both modes)
# ---------------------------------------------------------------------------


def test_pipelines_refresh_posts_to_the_fire_seam():
    """refresh POSTs to .../pipelines/<name>/fire (an empty body) and returns the result.

    Read-scoped — unlike submit it needs no write token, so a plain read-token page
    (the default) can trigger an on-demand refresh.
    """
    app = FakeApp(
        {"pipelines/report/fire": {"pipeline": "report", "mode": "code", "status": "succeeded"}}
    )
    result = sdk._AppPipelines(app).refresh("report")
    assert result["mode"] == "code" and result["status"] == "succeeded"
    assert app.posts == [("pipelines/report/fire", {})]
    assert app.calls == []  # a POST to /fire, never the GET run path


def test_pipelines_refresh_needs_no_write_scope():
    # A read-scoped app can refresh (submit could not) — no client-side scope gate.
    app = FakeApp({"pipelines/wake/fire": {"pipeline": "wake", "mode": "agentic"}}, scope="read")
    assert sdk._AppPipelines(app).refresh("wake") == {"pipeline": "wake", "mode": "agentic"}


def test_pipelines_refresh_non_dict_response_is_empty_dict():
    app = FakeApp({"pipelines/report/fire": None})
    assert sdk._AppPipelines(app).refresh("report") == {}


def test_context_defaults_scope_read_when_absent(tmp_path):
    ctx = tmp_path / "_app_context.json"
    ctx.write_text('{"token": "tok", "api_base": "https://h/", "app_id": "a1"}', encoding="utf-8")
    app = sdk.MewboApp(context_file=str(ctx))
    assert app._scope == "read"


def test_context_reads_write_scope(tmp_path):
    ctx = tmp_path / "_app_context.json"
    ctx.write_text(
        '{"token": "tok", "api_base": "https://h/", "app_id": "a1", "scope": "write"}',
        encoding="utf-8",
    )
    app = sdk.MewboApp(context_file=str(ctx))
    assert app._scope == "write"


# ---------------------------------------------------------------------------
# Transport (_send) — origin-aware token placement + error wrapping
# ---------------------------------------------------------------------------
#
# A fake `js` module stands in for pyodide's bridge: `XMLHttpRequest.new()`
# returns a recording double, and `window.location.origin` (when present)
# stands in for the embedding page's origin. `_send`'s `from js import ...`
# statements resolve through `sys.modules` regardless of which module does the
# importing, so planting the fake there is enough — no browser needed.


class _FakeXhrInstance:
    """Records `open`/`setRequestHeader`/`send`; replays a canned status + body."""

    def __init__(
        self,
        *,
        status: int = 200,
        response_text: str = "{}",
        raise_on_send: Exception | None = None,
    ) -> None:
        self._status = status
        self._response_text = response_text
        self._raise_on_send = raise_on_send
        self.opened: tuple[str, str] | None = None
        self.headers: dict[str, str] = {}
        self.sent_payload: Any = None

    def open(self, method: str, url: str, _async: bool) -> None:
        self.opened = (method, url)

    def setRequestHeader(self, name: str, value: str) -> None:
        self.headers[name] = value

    def send(self, payload: Any) -> None:
        self.sent_payload = payload
        if self._raise_on_send is not None:
            raise self._raise_on_send

    @property
    def status(self) -> int:
        return self._status

    @property
    def responseText(self) -> str:
        return self._response_text


def _install_fake_js(monkeypatch, *, xhr: _FakeXhrInstance, page_origin: str | None) -> None:
    """Plant a fake `js` module. `page_origin=None` simulates no browser `window`
    at all (this test suite's own default) — `_is_cross_origin` must treat that as
    indeterminate, not as cross-origin.
    """
    fake_js = types.ModuleType("js")
    fake_js.XMLHttpRequest = types.SimpleNamespace(new=lambda: xhr)
    if page_origin is not None:
        fake_js.window = types.SimpleNamespace(
            location=types.SimpleNamespace(origin=page_origin)
        )
    monkeypatch.setitem(sys.modules, "js", fake_js)


def _make_app(
    tmp_path,
    *,
    api_base: str = "https://api.example.com",
    token: str = "tok-secret",
    scope: str = "read",
):
    ctx = tmp_path / "_app_context.json"
    ctx.write_text(
        json.dumps({"token": token, "api_base": api_base, "app_id": "a1", "scope": scope}),
        encoding="utf-8",
    )
    return sdk.MewboApp(context_file=str(ctx))


def test_same_origin_get_sends_header_not_query_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, api_base="https://api.example.com", token="tok-secret")
    xhr = _FakeXhrInstance()
    _install_fake_js(monkeypatch, xhr=xhr, page_origin="https://api.example.com")

    app._get("system")

    assert xhr.headers.get("X-Mewbo-App-Token") == "tok-secret"
    assert "token=" not in xhr.opened[1]


def test_indeterminate_origin_falls_back_to_header(tmp_path, monkeypatch):
    app = _make_app(tmp_path, token="tok-secret")
    xhr = _FakeXhrInstance()
    _install_fake_js(monkeypatch, xhr=xhr, page_origin=None)

    app._get("system")

    assert xhr.headers.get("X-Mewbo-App-Token") == "tok-secret"
    assert "token=" not in xhr.opened[1]


def test_cross_origin_get_sends_query_token_not_header(tmp_path, monkeypatch):
    app = _make_app(tmp_path, api_base="https://api.example.com", token="tok-secret")
    xhr = _FakeXhrInstance()
    _install_fake_js(monkeypatch, xhr=xhr, page_origin="https://aura.embed.local")

    app._get("system")

    assert "X-Mewbo-App-Token" not in xhr.headers
    method, url = xhr.opened
    assert method == "GET"
    assert "token=tok-secret" in url


def test_cross_origin_post_still_sets_content_type_and_query_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, token="tok-secret", scope="write")
    xhr = _FakeXhrInstance()
    _install_fake_js(monkeypatch, xhr=xhr, page_origin="https://aura.embed.local")

    app.pipelines.submit("add-note", {"text": "hi"})

    assert xhr.headers.get("Content-Type") == "application/json"
    assert "X-Mewbo-App-Token" not in xhr.headers
    _, url = xhr.opened
    assert "token=tok-secret" in url


def test_network_failure_wraps_into_app_request_error_without_leaking_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, api_base="https://api.example.com", token="super-secret-token")
    xhr = _FakeXhrInstance(raise_on_send=RuntimeError("NetworkError: failed to fetch"))
    _install_fake_js(monkeypatch, xhr=xhr, page_origin="https://aura.embed.local")

    with pytest.raises(sdk.AppRequestError) as exc_info:
        app._get("system")

    message = str(exc_info.value)
    assert "super-secret-token" not in message
    assert "https://api.example.com" in message
    assert "cross-origin" in message
    assert "CORS" in message
    assert "TLS" in message


def test_same_origin_network_failure_names_same_origin(tmp_path, monkeypatch):
    app = _make_app(tmp_path, api_base="https://api.example.com", token="super-secret-token")
    xhr = _FakeXhrInstance(raise_on_send=RuntimeError("boom"))
    _install_fake_js(monkeypatch, xhr=xhr, page_origin="https://api.example.com")

    with pytest.raises(sdk.AppRequestError) as exc_info:
        app._get("system")

    message = str(exc_info.value)
    assert "super-secret-token" not in message
    assert "same-origin" in message


def test_non_2xx_error_never_leaks_token(tmp_path, monkeypatch):
    app = _make_app(tmp_path, token="super-secret-token")
    xhr = _FakeXhrInstance(status=500, response_text="boom")
    _install_fake_js(monkeypatch, xhr=xhr, page_origin="https://api.example.com")

    with pytest.raises(sdk.AppRequestError) as exc_info:
        app._get("system")

    assert "super-secret-token" not in str(exc_info.value)
