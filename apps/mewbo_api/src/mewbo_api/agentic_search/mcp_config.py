"""``WorkspaceMcpConfig`` — the DB-persisted virtual MCP config for a workspace.

Atomic class (the :class:`~mewbo_graph.wiki.credentials.CredentialStore` sibling):
all durable state lives in the injected agentic_search store; this class is the
single read/write/build chokepoint with one ``_encode``/``_decode`` seam so
encryption-at-rest is a one-line swap later. Keyed by **workspace id**.

What it owns (#75): the resolved selection of MCP servers a workspace's runs may
reach — server name → :class:`McpServerDef` (transport / url / command, headers
+ env behind the encode seam). It is **the source of truth for what a run may
reach**: built from ``Workspace.sources`` ∩ the merged ``configs/mcp.json`` chain
at save/attach time and refreshed on every workspace update, so a run grant
resolves against the persisted virtual config first (with the live global catalog
as the fallback).

SECURITY: ``headers`` / ``env`` carry secrets (Bearer tokens, ``DATABASE_URI``).
They are plaintext-at-rest in the isolated config store (mode 0600 JSON /
dedicated Mongo collection) but MUST be redacted in-flight — never logged, never
echoed into an SCG node, a run event, or any wire payload. Use :meth:`redacted`
(or :meth:`attached_servers` → :meth:`McpServerDef.redacted`) for anything
outward-facing; only the run-grant resolution reads the live values, and only to
hand them to the connector pool — never into a transcript.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewbo_core.common import get_logger
from mewbo_core.config import get_merged_mcp_config

from .schemas import McpServerDef, WorkspaceMcpConfigRecord

if TYPE_CHECKING:
    from .store import AgenticSearchStoreBase

logging = get_logger(name="api.agentic_search.mcp_config")


class WorkspaceMcpConfig:
    """Static façade over the workspace's persisted virtual MCP config."""

    @staticmethod
    def _encode(record: WorkspaceMcpConfigRecord) -> dict[str, Any]:
        """Serialise a config record for at-rest storage. Identity today.

        The ONE place a future cipher lands: encrypt the secret-bearing blob here
        and decrypt in :meth:`_decode`; nothing else in the codebase changes.
        """
        return record.model_dump(mode="json")

    @staticmethod
    def _decode(blob: dict[str, Any]) -> WorkspaceMcpConfigRecord | None:
        """Deserialise an at-rest blob back into a record (None if malformed)."""
        try:
            return WorkspaceMcpConfigRecord.model_validate(blob)
        except Exception:
            logging.warning("skipping malformed workspace MCP config blob")
            return None

    # -- build from the live catalog ---------------------------------------

    @staticmethod
    def resolve_servers(
        source_ids: list[str], *, project: str | None = None
    ) -> list[McpServerDef]:
        """Resolve *source_ids* against the merged MCP config → typed server defs.

        Each enabled source id that names a configured MCP server resolves to its
        full server def (transport/url/command + the secret-bearing headers/env);
        a source id with no matching configured server (a demo fixture, or an
        unconfigured id) is skipped — the virtual config holds only servers a run
        can actually reach. Selection order is preserved; a config-read failure
        degrades to an empty list, never an error (mirrors ``SourceCatalog``).
        """
        try:
            merged = get_merged_mcp_config(project)
        except Exception:
            return []
        servers = merged.get("servers") or merged.get("mcpServers") or {}
        if not isinstance(servers, dict):
            return []
        out: list[McpServerDef] = []
        seen: set[str] = set()
        for sid in source_ids:
            if sid in seen or sid not in servers:
                continue
            raw = servers[sid]
            if not isinstance(raw, dict):
                continue
            seen.add(sid)
            out.append(McpServerDef.model_validate({"name": sid, **raw}))
        return out

    @classmethod
    def build(
        cls,
        workspace_id: str,
        source_ids: list[str],
        *,
        project: str | None = None,
        nl_fingerprint: str = "",
    ) -> WorkspaceMcpConfigRecord:
        """Build (not persist) the virtual config for *source_ids*.

        ``nl_fingerprint`` stamps the workspace-prose digest that last drove a
        map-time enrich (server-internal bookkeeping, #83); default empty keeps
        the legacy shape for callers that don't track it.
        """
        return WorkspaceMcpConfigRecord(
            workspace_id=workspace_id,
            servers=cls.resolve_servers(source_ids, project=project),
            nl_fingerprint=nl_fingerprint,
        )

    # -- persistence (the encode seam) -------------------------------------

    @classmethod
    def save(
        cls,
        store: AgenticSearchStoreBase,
        workspace_id: str,
        source_ids: list[str],
        *,
        project: str | None = None,
        nl_fingerprint: str = "",
    ) -> WorkspaceMcpConfigRecord:
        """Resolve + persist the virtual config for *source_ids*; return it.

        The save/attach refresh point: re-resolves the selection against the live
        merged config and overwrites any prior config, so a workspace update keeps
        the virtual config in lockstep with the (possibly changed) selection.
        ``nl_fingerprint`` stamps the workspace-prose digest driving the current
        map-time enrich (#83) — the caller reads the prior value via
        :meth:`nl_fingerprint_of` BEFORE this overwrite to detect a prose change.
        """
        record = cls.build(
            workspace_id, source_ids, project=project, nl_fingerprint=nl_fingerprint
        )
        store.save_workspace_mcp_config(workspace_id, cls._encode(record))
        return record

    @classmethod
    def nl_fingerprint_of(
        cls, store: AgenticSearchStoreBase, workspace_id: str
    ) -> str:
        """Return the NL-context fingerprint stamped on the persisted config.

        ``""`` when no config is persisted yet (a fresh workspace) or it predates
        #83 — both read as "no prior enrich prose", so the first prose-bearing
        save always counts as a change. The seam the re-enrich gate compares
        against (#83).
        """
        record = cls.load(store, workspace_id)
        return record.nl_fingerprint if record is not None else ""

    @classmethod
    def load(
        cls, store: AgenticSearchStoreBase, workspace_id: str
    ) -> WorkspaceMcpConfigRecord | None:
        """Return the persisted virtual config for *workspace_id*, or None."""
        blob = store.get_workspace_mcp_config(workspace_id)
        if blob is None:
            return None
        return cls._decode(blob)

    @staticmethod
    def delete(store: AgenticSearchStoreBase, workspace_id: str) -> bool:
        """Delete *workspace_id*'s virtual config; True if one was removed."""
        return store.delete_workspace_mcp_config(workspace_id)

    # -- run-grant resolution ----------------------------------------------

    @classmethod
    def attached_server_names(
        cls, store: AgenticSearchStoreBase, workspace_id: str
    ) -> list[str] | None:
        """The workspace's attached MCP server names, or None if no config saved.

        The seam a run-grant resolution reads first: ``None`` means "no virtual
        config persisted — fall back to the global catalog / the workspace's raw
        ``sources``" (current behavior); a list (possibly empty) is the
        authoritative, persisted selection.
        """
        record = cls.load(store, workspace_id)
        return None if record is None else record.server_names()


__all__ = ["WorkspaceMcpConfig"]
