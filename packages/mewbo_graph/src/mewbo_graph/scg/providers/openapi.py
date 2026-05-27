"""OpenAPI / Swagger structure provider — pure dict parsing, never a network.

Parses an OpenAPI 3.x (or Swagger 2.0) document into the SCG fabric:

* one ``source`` node for the connector,
* one ``entity_type`` node per ``components.schemas`` entry, with a ``field``
  node per property (``HAS_ENTITY`` / ``HAS_FIELD`` edges),
* one ``capability`` node per operation (keyed by ``operationId``), with a
  :class:`CapabilityBinding` per parameter — ``required`` ⇒ ``bound``,
  optional ⇒ ``optional``; ``in: query`` parameters expose query operators —
  and a ``SUPPORTS_QUERY`` edge per bound/queryable field.

Binding patterns keep traversal honest (Florescu/Vassalos SIGMOD'99): a
capability "queryable by ``repo``, not free-text" emits ``mode="bound"`` so the
router only proposes executable plans. No token, credential, or any ``raw``
value beyond schema/parameter *names* and *docs* is ever copied into a node.
"""

from __future__ import annotations

from ..types import (
    CapabilityBinding,
    ScgEdge,
    ScgNode,
    SourceDescriptor,
    StructureGraph,
)

# Query operators a queryable (``in: query``) parameter is assumed to support.
# Coarse + connector-agnostic; the learned memory layer (#13) sharpens these.
_QUERY_OPERATORS = ["eq"]


class OpenApiStructureProvider:
    """Parse an OpenAPI/Swagger ``descriptor.raw`` dict into a StructureGraph."""

    source_type = "openapi"

    def build_structure(self, descriptor: SourceDescriptor) -> StructureGraph:
        """Build the source/entity/capability subgraph for one OpenAPI source."""
        source_id = descriptor.source_id
        raw = descriptor.raw

        nodes: list[ScgNode] = [self._source_node(descriptor)]
        edges: list[ScgEdge] = []

        for entity_nodes, entity_edges in self._iter_entities(source_id, raw):
            nodes.extend(entity_nodes)
            edges.extend(entity_edges)

        for cap_node, cap_edges in self._iter_capabilities(source_id, raw):
            nodes.append(cap_node)
            edges.extend(cap_edges)

        return StructureGraph(nodes=nodes, edges=edges)

    # -- nodes --------------------------------------------------------------

    @staticmethod
    def _source_node(descriptor: SourceDescriptor) -> ScgNode:
        """The root ``source`` node — carries an auth_scope descriptor, no secret."""
        info = descriptor.raw.get("info")
        doc = ""
        if isinstance(info, dict):
            title = info.get("title")
            if isinstance(title, str):
                doc = title
        return ScgNode(
            source_key=descriptor.source_id,
            kind="source",
            source_id=descriptor.source_id,
            name=descriptor.source_id,
            doc=doc,
        )

    @classmethod
    def _iter_entities(
        cls, source_id: str, raw: dict[str, object]
    ) -> list[tuple[list[ScgNode], list[ScgEdge]]]:
        """Yield (nodes, edges) per ``components.schemas`` entry."""
        out: list[tuple[list[ScgNode], list[ScgEdge]]] = []
        for name, schema in cls._schemas(raw).items():
            entity_key = f"{source_id}#{name}"
            nodes: list[ScgNode] = [
                ScgNode(
                    source_key=entity_key,
                    kind="entity_type",
                    source_id=source_id,
                    name=name,
                )
            ]
            edges: list[ScgEdge] = [
                ScgEdge(source=source_id, target=entity_key, kind="HAS_ENTITY")
            ]
            for field_name in cls._properties(schema):
                field_key = f"{entity_key}.{field_name}"
                nodes.append(
                    ScgNode(
                        source_key=field_key,
                        kind="field",
                        source_id=source_id,
                        name=field_name,
                    )
                )
                edges.append(
                    ScgEdge(source=entity_key, target=field_key, kind="HAS_FIELD")
                )
            out.append((nodes, edges))
        return out

    @classmethod
    def _iter_capabilities(
        cls, source_id: str, raw: dict[str, object]
    ) -> list[tuple[ScgNode, list[ScgEdge]]]:
        """Yield (capability node, edges) per operation across all paths."""
        out: list[tuple[ScgNode, list[ScgEdge]]] = []
        for op_name, operation in cls._operations(raw):
            cap_key = f"{source_id}#{op_name}"
            bindings: list[CapabilityBinding] = []
            edges: list[ScgEdge] = []
            for param in cls._parameters(operation):
                binding = cls._binding(cap_key, param)
                if binding is None:
                    continue
                bindings.append(binding)
                edges.append(
                    ScgEdge(
                        source=cap_key, target=binding.field_key, kind="SUPPORTS_QUERY"
                    )
                )
            out.append(
                (
                    ScgNode(
                        source_key=cap_key,
                        kind="capability",
                        source_id=source_id,
                        name=op_name,
                        doc=cls._operation_doc(operation),
                        bindings=bindings,
                    ),
                    edges,
                )
            )
        return out

    # -- binding ------------------------------------------------------------

    @staticmethod
    def _binding(
        cap_key: str, param: dict[str, object]
    ) -> CapabilityBinding | None:
        """Build a binding from one OpenAPI parameter, or None if unnamed."""
        name = param.get("name")
        if not isinstance(name, str) or not name:
            return None
        required = bool(param.get("required", False))
        location = param.get("in")
        operators = (
            list(_QUERY_OPERATORS) if location == "query" else []
        )
        return CapabilityBinding(
            field_key=f"{cap_key}.{name}",
            mode="bound" if required else "optional",
            operators=operators,
        )

    # -- raw-dict accessors (defensive: descriptors are untrusted shape) ----

    @staticmethod
    def _schemas(raw: dict[str, object]) -> dict[str, dict[str, object]]:
        """Return ``components.schemas`` (OpenAPI 3) merged with ``definitions`` (2.0)."""
        out: dict[str, dict[str, object]] = {}
        components = raw.get("components")
        if isinstance(components, dict):
            schemas = components.get("schemas")
            if isinstance(schemas, dict):
                for k, v in schemas.items():
                    if isinstance(v, dict):
                        out[str(k)] = v
        definitions = raw.get("definitions")  # Swagger 2.0
        if isinstance(definitions, dict):
            for k, v in definitions.items():
                if isinstance(v, dict):
                    out.setdefault(str(k), v)
        return out

    @staticmethod
    def _properties(schema: dict[str, object]) -> list[str]:
        """Return the property names of an object schema, in declared order."""
        props = schema.get("properties")
        if isinstance(props, dict):
            return [str(k) for k in props]
        return []

    @classmethod
    def _operations(
        cls, raw: dict[str, object]
    ) -> list[tuple[str, dict[str, object]]]:
        """Return (operation_id, operation) for every HTTP method under paths."""
        methods = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
        out: list[tuple[str, dict[str, object]]] = []
        paths = raw.get("paths")
        if not isinstance(paths, dict):
            return out
        for path, item in paths.items():
            if not isinstance(item, dict):
                continue
            for method in methods:
                operation = item.get(method)
                if not isinstance(operation, dict):
                    continue
                op_id = operation.get("operationId")
                name = (
                    str(op_id)
                    if isinstance(op_id, str) and op_id
                    else f"{method}_{path}"
                )
                out.append((name, operation))
        return out

    @staticmethod
    def _parameters(operation: dict[str, object]) -> list[dict[str, object]]:
        """Return the operation's parameter dicts (skips malformed entries)."""
        params = operation.get("parameters")
        if not isinstance(params, list):
            return []
        return [p for p in params if isinstance(p, dict)]

    @staticmethod
    def _operation_doc(operation: dict[str, object]) -> str:
        """Prefer ``summary`` then ``description`` for the capability doc."""
        for key in ("summary", "description"):
            value = operation.get(key)
            if isinstance(value, str) and value:
                return value
        return ""


__all__ = ["OpenApiStructureProvider"]
