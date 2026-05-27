"""The information→graph parser SEAM — one provider per source *type*.

There is no tree-sitter to lean on here: connectors aren't code (that is #13's
AST trick). The SCG instead parses each source's *self-description* — an OpenAPI
doc, an MCP tool list, a GraphQL SDL, a SQL schema — into the shared
:class:`~mewbo_graph.scg.types.StructureGraph`. The seam is an
RML-style declarative shell (R2RML/RML, LDOW'14): one
:class:`SourceStructureProvider` per source type, dispatched by
``descriptor.source_type``. **A new source type = one class + one register
call, zero core edits.**

NOTE — distinct from #13's ``StructureProvider``. That Protocol (wiki memory
layer) resolves *between* an ``entity_key`` and a code node. This
``SourceStructureProvider`` *builds* a connector's capability subgraph from a
raw descriptor. They share a name root and nothing else.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types import SourceDescriptor, StructureGraph
from .mcp_tool_list import McpToolListStructureProvider
from .openapi import OpenApiStructureProvider


@runtime_checkable
class SourceStructureProvider(Protocol):
    """Parse one source *type*'s descriptor into a normalized structure graph."""

    #: The ``SourceDescriptor.source_type`` this provider handles (dispatch key).
    source_type: str

    def build_structure(self, descriptor: SourceDescriptor) -> StructureGraph:
        """Parse ``descriptor.raw`` into a :class:`StructureGraph`. No network."""
        ...


class StructureProviderRegistry:
    """Dispatches a :class:`SourceDescriptor` to the provider for its type.

    The declarative shell of the RML pattern: providers register by
    ``source_type`` and the registry routes ``build(descriptor)`` to the match.
    Construct via :meth:`with_defaults` for the built-in OpenAPI + MCP set.
    """

    def __init__(
        self, providers: list[SourceStructureProvider] | None = None
    ) -> None:
        """Build a registry, optionally seeded with *providers*."""
        self._providers: dict[str, SourceStructureProvider] = {}
        for provider in providers or []:
            self.register(provider)

    @classmethod
    def with_defaults(cls) -> StructureProviderRegistry:
        """Registry seeded with the schema-bearing built-in providers.

        Schemaless sources (``LlmStructureProvider``) are *not* auto-registered:
        they need an injected ``llm`` callable, so the caller wires them
        explicitly via :meth:`register`.
        """
        return cls([OpenApiStructureProvider(), McpToolListStructureProvider()])

    def register(self, provider: SourceStructureProvider) -> None:
        """Add or replace the provider for its ``source_type``."""
        self._providers[provider.source_type] = provider

    def providers(self) -> list[SourceStructureProvider]:
        """Return the registered providers (the parser's seam — public accessor)."""
        return list(self._providers.values())

    def for_type(self, source_type: str) -> SourceStructureProvider:
        """Return the provider for *source_type*, or raise ``KeyError``."""
        try:
            return self._providers[source_type]
        except KeyError:
            raise KeyError(f"No SCG structure provider for source_type={source_type!r}")

    def build(self, descriptor: SourceDescriptor) -> StructureGraph:
        """Dispatch *descriptor* to its provider and return the parsed graph."""
        return self.for_type(descriptor.source_type).build_structure(descriptor)


__all__ = ["SourceStructureProvider", "StructureProviderRegistry"]
