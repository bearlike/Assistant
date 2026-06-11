"""Tests for :class:`SourceDescriptorBuilder` + the map-route auto-build glue.

The builder composes ``mewbo_tools`` (the public
``list_server_tool_schemas`` introspection seam) with ``mewbo_graph`` (the
``SourceDescriptor`` type) at the app layer. The ONLY things stubbed are the
PUBLIC I/O boundaries — ``mewbo_core.config.get_merged_mcp_config`` (the
merged-config read) and ``mewbo_tools.integration.mcp_pool.get_mcp_pool``
(the pool connection) — so the config normalization, schema extraction,
descriptor validation, and route → builder wiring all run the real code
path. No private of another package is patched. NEVER hits a real server.
"""

from __future__ import annotations

import pytest
from mewbo_api import backend
from mewbo_api.agentic_search import store as store_mod
from mewbo_api.agentic_search.routes import ScgConfig
from mewbo_api.agentic_search.scg.descriptors import SourceDescriptorBuilder
from mewbo_api.agentic_search.schemas import MapJobRecord
from mewbo_graph.scg import store as scg_store_mod

_CONFIG = {
    "servers": {
        "gitea": {
            "transport": "streamable_http",
            "url": "http://gitea.test/mcp",
            "headers": {"Authorization": "Bearer SECRET-TOKEN"},
        }
    }
}


class _FakeTool:
    """The minimal langchain-tool surface the builder reads."""

    def __init__(self, name: str, description: str = "", args_schema=None) -> None:
        self.name = name
        self.description = description
        self.args_schema = args_schema


class _FakeState:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class _FakePool:
    """Pool double — connect returns the canned state (or raises)."""

    def __init__(self, state) -> None:
        self._state = state

    async def refresh_if_config_changed(self, config) -> bool:
        return False

    async def get_or_connect(self, server_name: str):
        if isinstance(self._state, Exception):
            raise self._state
        return self._state


def _patch_merged_config(monkeypatch, payload: dict) -> None:
    """Stub the public merged-config read the real ``_load_mcp_config`` does.

    The stub always carries at least one server so the loader never falls
    back to reading a real on-disk ``configs/mcp.json``.
    """
    import mewbo_core.config as core_config

    monkeypatch.setattr(
        core_config, "get_merged_mcp_config", lambda cwd=None: dict(payload)
    )


@pytest.fixture
def _mcp_env(monkeypatch):
    """Stub the config read + pool connect; real schema extraction beyond."""
    import mewbo_tools.integration.mcp_pool as pool_mod

    state = _FakeState(
        [
            _FakeTool(
                "list_issues",
                "List repo issues.",
                args_schema={
                    "type": "object",
                    "properties": {"repo": {"type": "string"}},
                    "required": ["repo"],
                },
            ),
            _FakeTool("get_file", "Read one file."),
        ]
    )
    _patch_merged_config(monkeypatch, _CONFIG)
    monkeypatch.setattr(pool_mod, "get_mcp_pool", lambda: _FakePool(state))
    return state


# ── SourceDescriptorBuilder ──────────────────────────────────────────────────


def test_build_produces_schema_only_descriptor(_mcp_env) -> None:
    """The descriptor carries name/description/inputSchema — and no secret."""
    descriptor = SourceDescriptorBuilder("gitea").build()
    assert descriptor.source_id == "gitea"
    assert descriptor.source_type == "mcp_tool_list"
    assert descriptor.raw["tools"] == [
        {"name": "get_file", "description": "Read one file."},
        {
            "name": "list_issues",
            "description": "List repo issues.",
            "inputSchema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}},
                "required": ["repo"],
            },
        },
    ]
    # No connection/auth material ever leaks into the schema-only payload.
    assert "SECRET-TOKEN" not in descriptor.model_dump_json()


def test_build_raises_lookup_error_for_unconfigured_source(monkeypatch) -> None:
    """No configured connector → LookupError (the route's 422)."""
    _patch_merged_config(monkeypatch, _CONFIG)  # has "gitea", not "nope"
    with pytest.raises(LookupError, match="no configured MCP connector"):
        SourceDescriptorBuilder("nope").build()


def test_build_raises_runtime_error_when_connect_fails(monkeypatch) -> None:
    """A live introspection failure settles as RuntimeError (the route's 503)."""
    import mewbo_tools.integration.mcp_pool as pool_mod

    _patch_merged_config(monkeypatch, _CONFIG)
    monkeypatch.setattr(
        pool_mod, "get_mcp_pool", lambda: _FakePool(ValueError("connection refused"))
    )
    with pytest.raises(RuntimeError, match="failed to introspect"):
        SourceDescriptorBuilder("gitea").build()


# ── Map-route auto-build wiring ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_stores():
    store_mod.reset_for_tests()
    scg_store_mod.reset_for_tests()
    yield
    store_mod.reset_for_tests()
    scg_store_mod.reset_for_tests()


@pytest.fixture
def _scg_on(monkeypatch):
    monkeypatch.setattr(ScgConfig, "enabled", staticmethod(lambda: True))


def _auth():
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


def _mock_map_start(monkeypatch, captured: dict) -> None:
    import mewbo_api.agentic_search.scg.map_job as map_job_mod

    def _fake_start(source, *, store, runtime, model=None, **_):
        captured["descriptor"] = source.descriptor
        captured["source_type"] = source.source_type
        rec = MapJobRecord(
            job_id="map-desc-1",
            source_id=source.source_id,
            source_type=source.source_type,
            status="queued",
        )
        store.create_map_job(rec)
        return rec

    monkeypatch.setattr(map_job_mod.MapSourceJob, "start", staticmethod(_fake_start))


def test_map_route_autobuilds_mcp_descriptor(monkeypatch, _scg_on, _mcp_env) -> None:
    """POST /map without a descriptor builds one from the live tool list."""
    captured: dict = {}
    _mock_map_start(monkeypatch, captured)

    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/sources/gitea/map",
        json={"source_type": "mcp_tool_list"},
        headers=_auth(),
    )
    assert resp.status_code == 202, resp.get_data(as_text=True)
    assert captured["source_type"] == "mcp_tool_list"
    names = [t["name"] for t in captured["descriptor"]["tools"]]
    assert names == ["get_file", "list_issues"]


def test_map_route_422_when_no_connector_and_no_descriptor(
    monkeypatch, _scg_on
) -> None:
    """An MCP source without a configured connector 422s with a clear message."""
    _patch_merged_config(monkeypatch, _CONFIG)  # has "gitea", not "ghost"
    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/sources/ghost/map",
        json={"source_type": "mcp_tool_list"},
        headers=_auth(),
    )
    assert resp.status_code == 422
    assert "no configured MCP connector" in resp.get_json()["message"]


def test_map_route_keeps_explicit_descriptor_verbatim(
    monkeypatch, _scg_on
) -> None:
    """A caller-supplied descriptor is never overwritten by the auto-build."""
    captured: dict = {}
    _mock_map_start(monkeypatch, captured)

    client = backend.app.test_client()
    explicit = {"tools": [{"name": "hand_written"}]}
    resp = client.post(
        "/api/agentic_search/sources/gitea/map",
        json={"source_type": "mcp_tool_list", "descriptor": explicit},
        headers=_auth(),
    )
    assert resp.status_code == 202, resp.get_data(as_text=True)
    assert captured["descriptor"] == explicit


def test_map_route_skips_autobuild_for_non_mcp_source_types(
    monkeypatch, _scg_on
) -> None:
    """A descriptor-less openapi map keeps the mapper's native-fetch contract."""
    captured: dict = {}
    _mock_map_start(monkeypatch, captured)

    client = backend.app.test_client()
    resp = client.post(
        "/api/agentic_search/sources/gitea/map",
        json={"source_type": "openapi"},
        headers=_auth(),
    )
    assert resp.status_code == 202, resp.get_data(as_text=True)
    assert captured["descriptor"] is None
