"""``WorkspaceMcpConfig`` — the DB-persisted virtual MCP config (#75).

Drives the real :class:`WorkspaceMcpConfig` façade over a real JSON
agentic_search store (no Mongo, no LLM), the ``CredentialStore`` test stance:

* round-trip: build from a merged MCP config → persist → load == equal selection;
* the encode seam keeps secret-bearing ``headers`` / ``env`` AT REST but
  :meth:`redacted` masks every value outward (the security invariant);
* resolve_servers maps source ids ∩ the configured servers (skips demo/unknown);
* ``attached_server_names`` is the run-grant seam — ``None`` when no config is
  persisted (fall back to global), the saved selection otherwise;
* delete removes the secret-bearing blob.

The merged MCP config is stubbed at the ONE seam (``get_merged_mcp_config`` as
imported into ``mcp_config``) so no real ``.mcp.json`` chain is read.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from mewbo_api.agentic_search import mcp_config as mcp_config_mod
from mewbo_api.agentic_search.mcp_config import WorkspaceMcpConfig
from mewbo_api.agentic_search.schemas import WorkspaceMcpConfigRecord
from mewbo_api.agentic_search.store import JsonAgenticSearchStore

# A representative merged config: an http server with a secret Authorization
# header + a stdio server with a secret env var, mirroring configs/mcp.json.
_MERGED = {
    "servers": {
        "gitea": {
            "transport": "streamable_http",
            "url": "http://mcp.hurricane.home/mcp/Gitea-Hurricane",
            "headers": {"Authorization": "Bearer sk-cloud-SECRET"},
        },
        "sidestage-postgres": {
            "command": "uvx",
            "args": ["postgres-mcp", "--access-mode=unrestricted"],
            "env": {"DATABASE_URI": "postgresql://u:p@postgres:5432/db"},
        },
        "internet-search": {
            "transport": "streamable_http",
            "url": "http://mcp.hurricane.home/mcp/Internet-Search",
            "headers": {"Authorization": "Bearer sk-cloud-SECRET"},
        },
    }
}


@pytest.fixture()
def store() -> JsonAgenticSearchStore:
    """A fresh JSON agentic_search store under a throwaway temp dir."""
    return JsonAgenticSearchStore(root_dir=Path(tempfile.mkdtemp(prefix="ws-mcp-")))


@pytest.fixture(autouse=True)
def _stub_merged_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the merged-MCP-config read at the single binding in ``mcp_config``."""
    monkeypatch.setattr(
        mcp_config_mod, "get_merged_mcp_config", lambda project=None: _MERGED
    )


# ── resolve_servers ─────────────────────────────────────────────────────────


def test_resolve_servers_maps_configured_only() -> None:
    """Only source ids that name a configured server resolve; others are skipped."""
    servers = WorkspaceMcpConfig.resolve_servers(["gitea", "demo-web", "internet-search"])
    assert [s.name for s in servers] == ["gitea", "internet-search"]


def test_resolve_servers_preserves_selection_order() -> None:
    """Selection order is preserved (and de-duplicated)."""
    servers = WorkspaceMcpConfig.resolve_servers(
        ["internet-search", "gitea", "internet-search"]
    )
    assert [s.name for s in servers] == ["internet-search", "gitea"]


def test_resolve_servers_lifts_transport_and_secrets() -> None:
    """The typed def carries transport/url + the secret-bearing fields."""
    [gitea] = WorkspaceMcpConfig.resolve_servers(["gitea"])
    assert gitea.transport == "streamable_http"
    assert gitea.url.endswith("/mcp/Gitea-Hurricane")
    assert gitea.headers == {"Authorization": "Bearer sk-cloud-SECRET"}
    [pg] = WorkspaceMcpConfig.resolve_servers(["sidestage-postgres"])
    assert pg.command == "uvx"
    assert pg.env == {"DATABASE_URI": "postgresql://u:p@postgres:5432/db"}


# ── persistence round-trip + the encode seam ────────────────────────────────


def test_save_load_round_trip(store: JsonAgenticSearchStore) -> None:
    """save → load returns the same selection, secrets intact AT REST."""
    saved = WorkspaceMcpConfig.save(store, "ws-1", ["gitea", "sidestage-postgres"])
    loaded = WorkspaceMcpConfig.load(store, "ws-1")
    assert loaded is not None
    assert loaded.server_names() == saved.server_names() == ["gitea", "sidestage-postgres"]
    # The credential survives the encode/decode seam at rest (so a run can use it).
    gitea = next(s for s in loaded.servers if s.name == "gitea")
    assert gitea.headers["Authorization"] == "Bearer sk-cloud-SECRET"


def test_save_overwrites_prior_selection(store: JsonAgenticSearchStore) -> None:
    """A second save (a workspace update) replaces the prior config in place."""
    WorkspaceMcpConfig.save(store, "ws-1", ["gitea", "internet-search"])
    WorkspaceMcpConfig.save(store, "ws-1", ["sidestage-postgres"])
    loaded = WorkspaceMcpConfig.load(store, "ws-1")
    assert loaded is not None
    assert loaded.server_names() == ["sidestage-postgres"]


# ── redaction (the security invariant) ──────────────────────────────────────


def test_redacted_masks_every_secret_value(store: JsonAgenticSearchStore) -> None:
    """The outward projection masks header/env VALUES but keeps the key shape."""
    WorkspaceMcpConfig.save(store, "ws-1", ["gitea", "sidestage-postgres"])
    loaded = WorkspaceMcpConfig.load(store, "ws-1")
    assert loaded is not None
    red = loaded.redacted()
    serialized = repr(red)
    assert "sk-cloud-SECRET" not in serialized
    assert "postgresql://" not in serialized
    by_name = {s["name"]: s for s in red["servers"]}
    assert by_name["gitea"]["headers"] == {"Authorization": "***"}
    assert by_name["sidestage-postgres"]["env"] == {"DATABASE_URI": "***"}


def test_auth_scope_names_auth_without_revealing_it() -> None:
    """auth_scope surfaces WHICH auth a server carries, never the secret."""
    [gitea] = WorkspaceMcpConfig.resolve_servers(["gitea"])
    assert gitea.auth_scope() == "header:Authorization"
    [pg] = WorkspaceMcpConfig.resolve_servers(["sidestage-postgres"])
    assert pg.auth_scope() == "env:DATABASE_URI"


# ── run-grant seam: attached_server_names ───────────────────────────────────


def test_attached_server_names_none_without_config(store: JsonAgenticSearchStore) -> None:
    """No persisted config → None (the run grant falls back to global)."""
    assert WorkspaceMcpConfig.attached_server_names(store, "ws-absent") is None


def test_attached_server_names_returns_saved_selection(
    store: JsonAgenticSearchStore,
) -> None:
    """A persisted config → its server names (the authoritative grant)."""
    WorkspaceMcpConfig.save(store, "ws-1", ["gitea", "internet-search"])
    assert WorkspaceMcpConfig.attached_server_names(store, "ws-1") == [
        "gitea",
        "internet-search",
    ]


def test_attached_server_names_empty_selection_is_authoritative(
    store: JsonAgenticSearchStore,
) -> None:
    """An empty saved selection is [] (authoritative), NOT None (fall back).

    A workspace that explicitly cleared its sources grants nothing — that must be
    distinguishable from "no config persisted yet".
    """
    WorkspaceMcpConfig.save(store, "ws-1", [])
    assert WorkspaceMcpConfig.attached_server_names(store, "ws-1") == []


# ── delete ──────────────────────────────────────────────────────────────────


def test_delete_removes_secret_blob(store: JsonAgenticSearchStore) -> None:
    """delete drops the config; a second delete is False (idempotent)."""
    WorkspaceMcpConfig.save(store, "ws-1", ["gitea"])
    assert WorkspaceMcpConfig.delete(store, "ws-1") is True
    assert WorkspaceMcpConfig.load(store, "ws-1") is None
    assert WorkspaceMcpConfig.delete(store, "ws-1") is False


# ── malformed blob tolerance ────────────────────────────────────────────────


def test_load_skips_malformed_blob(store: JsonAgenticSearchStore) -> None:
    """A malformed at-rest blob decodes to None, never raises (lenient load)."""
    store.save_workspace_mcp_config("ws-bad", {"not": "a valid record"})
    assert WorkspaceMcpConfig.load(store, "ws-bad") is None


def test_decode_accepts_a_well_formed_blob() -> None:
    """The decode seam round-trips a hand-built record blob."""
    rec = WorkspaceMcpConfigRecord(workspace_id="ws-9", servers=[])
    blob = WorkspaceMcpConfig._encode(rec)
    back = WorkspaceMcpConfig._decode(blob)
    assert back is not None
    assert back.workspace_id == "ws-9"
