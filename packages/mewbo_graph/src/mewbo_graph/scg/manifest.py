"""ManifestHash — a stable fingerprint of a source's tool/operation surface.

A connector's *manifest* is the set of operations it exposes plus each
operation's input/output shape. When that surface drifts — a tool is added or
removed, an argument's name/requiredness changes — a previously-mapped Source
Capability Graph is stale: the router proposes pathways through capabilities the
connector no longer offers (or misses new ones). Detecting drift cheaply needs a
deterministic digest of the manifest that two independent observations of the
SAME surface always agree on, and that any real surface change perturbs.

:class:`ManifestHash` is that one digest. It is **order-independent** (a server
that lists its tools in a different order is NOT a drift) and **schema-aware**
(it folds in each tool's input/output property names + required set, so a renamed
or newly-required argument IS a drift). It is stored on
:attr:`SourceDescriptor.schema_version` at map time and recomputed from the live
tool list on workspace save to gate an idempotent re-map (#81-C).

Security invariant (spec §6): the hash is computed over schema field *names* and
docs only — never an argument *value*, token, or credential.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# The digest length kept on the descriptor — 16 hex chars (64 bits) is ample to
# make an accidental collision between two distinct manifests negligible while
# staying compact in the persisted ``schema_version`` field. Matches the
# ``ScgNode.make_id`` truncation so the two id schemes read alike.
_DIGEST_CHARS = 16


class ManifestHash:
    """Deterministic, order-independent fingerprint of an MCP tool-list manifest.

    Stateless — :meth:`of_tool_list` and :meth:`of_descriptor_raw` are pure
    functions over the raw descriptor shape the
    :class:`~mewbo_graph.scg.providers.mcp_tool_list.McpToolListStructureProvider`
    parses (``{"tools": [{name, description?, inputSchema?, outputSchema?}, …]}``).
    """

    @classmethod
    def of_descriptor_raw(cls, raw: Mapping[str, object]) -> str:
        """Hash the ``tools`` list of a descriptor's ``raw`` payload.

        A ``raw`` with no ``tools`` list (a non-tool-list source) hashes the
        empty manifest — a stable sentinel, never a raise — so the caller can
        compare uniformly across source types.
        """
        tools = raw.get("tools")
        return cls.of_tool_list(tools if isinstance(tools, list) else [])

    @classmethod
    def of_tool_list(cls, tools: Iterable[object]) -> str:
        """Hash a list of tool dicts, order-independently.

        Each tool contributes its ``name``, ``description``, and the property
        names + required set of its input/output schemas. Tools are sorted by
        name before hashing, so reordering the server's advertised list is not a
        drift. A non-dict / unnamed entry is skipped (it produced no SCG node, so
        it can't anchor a recipe either).
        """
        fingerprints = sorted(
            fp for tool in tools if (fp := cls._tool_fingerprint(tool)) is not None
        )
        blob = json.dumps(fingerprints, separators=(",", ":"), sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]

    @classmethod
    def _tool_fingerprint(cls, tool: object) -> list[object] | None:
        """The canonical, comparable fingerprint of one tool, or None to skip.

        ``[name, description, input_shape, output_shape]`` where each shape is
        ``[sorted_property_names, sorted_required_names]`` — every component
        deterministically ordered so equal manifests hash equal.
        """
        if not isinstance(tool, dict):
            return None
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            return None
        description = tool.get("description")
        return [
            name,
            description if isinstance(description, str) else "",
            cls._schema_shape(tool.get("inputSchema")),
            cls._schema_shape(tool.get("outputSchema")),
        ]

    @staticmethod
    def _schema_shape(schema: object) -> list[list[str]]:
        """``[sorted property names, sorted required names]`` for a JSON schema.

        Folds in BOTH the field set and the required set so a field rename, a
        new field, or a field becoming required all perturb the hash. Never
        reads a field *value* — only the schema's structural keys (spec §6).
        """
        if not isinstance(schema, dict):
            return [[], []]
        props = schema.get("properties")
        names = sorted(str(k) for k in props) if isinstance(props, dict) else []
        req = schema.get("required")
        required = sorted(str(r) for r in req) if isinstance(req, list) else []
        return [names, required]


__all__ = ["ManifestHash"]
