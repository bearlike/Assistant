"""SCG structure providers — the information→graph parser seam.

One :class:`SourceStructureProvider` per source *type* (RML declarative shell):
OpenAPI, MCP tool list, and an LLM fallback for schemaless sources. A
:class:`StructureProviderRegistry` dispatches a
:class:`~mewbo_graph.scg.types.SourceDescriptor` to the matching
provider by ``source_type``. New source type = one class + one register call.
"""

from __future__ import annotations

from .base import SourceStructureProvider, StructureProviderRegistry
from .llm_fallback import LlmStructureProvider
from .mcp_tool_list import McpToolListStructureProvider
from .openapi import OpenApiStructureProvider

__all__ = [
    "SourceStructureProvider",
    "StructureProviderRegistry",
    "OpenApiStructureProvider",
    "McpToolListStructureProvider",
    "LlmStructureProvider",
]
