"""LLM-fallback structure provider — schemaless sources.

For a source with no machine-readable schema (a free-text connector blurb, a
legacy API with only prose docs), there is nothing to parse deterministically.
This provider asks an injected LLM for a single coarse *capability label* and
emits one ``capability`` node so the source is at least reachable by the router
— the RML pattern's escape hatch (#19: "LLM fallback for schemaless sources").

The LLM is **dependency-injected** (``llm`` constructor arg). The default is
``None`` so an accidental use raises loudly rather than silently no-opping — and
so tests inject a fake and never spawn a real model. The callable contract is a
minimal ``Callable[[str], str]``: ``str (prompt) -> str (label)``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from ..types import (
    ScgEdge,
    ScgNode,
    SourceDescriptor,
    StructureGraph,
)


class LlmStructureProvider:
    """Coarse single-capability provider for schemaless sources (DI LLM)."""

    source_type = "text"

    # Slugify a free-text label into a capability name token.
    _SLUG_RE = re.compile(r"[^a-z0-9]+")

    def __init__(self, *, llm: Callable[[str], str] | None = None) -> None:
        """Inject the text LLM callable; ``None`` (default) raises on use."""
        self._llm = llm

    def build_structure(self, descriptor: SourceDescriptor) -> StructureGraph:
        """Emit a source node + one coarse capability node from a description."""
        if self._llm is None:
            raise RuntimeError(
                "LlmStructureProvider requires an injected `llm` callable; "
                f"cannot parse schemaless source {descriptor.source_id!r}."
            )
        source_id = descriptor.source_id
        label = self._slug(self._llm(self._prompt(descriptor)))
        cap_key = f"{source_id}#{label}"

        return StructureGraph(
            nodes=[
                ScgNode(
                    source_key=source_id,
                    kind="source",
                    source_id=source_id,
                    name=source_id,
                    doc=self._description(descriptor),
                ),
                ScgNode(
                    source_key=cap_key,
                    kind="capability",
                    source_id=source_id,
                    name=label,
                    doc=self._description(descriptor),
                ),
            ],
            edges=[ScgEdge(source=source_id, target=cap_key, kind="HAS_ENTITY")],
        )

    # -- helpers ------------------------------------------------------------

    @classmethod
    def _slug(cls, label: str) -> str:
        """Normalize an LLM label to a capability name token; fallback ``search``."""
        slug = cls._SLUG_RE.sub("_", label.strip().lower()).strip("_")
        return slug or "search"

    @staticmethod
    def _description(descriptor: SourceDescriptor) -> str:
        """Pull a human description from the raw descriptor, or ''."""
        for key in ("description", "desc", "summary"):
            value = descriptor.raw.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @classmethod
    def _prompt(cls, descriptor: SourceDescriptor) -> str:
        """Build the capability-labeling prompt for the injected LLM."""
        return (
            "Name the single primary capability of this data source as a short "
            "snake_case verb phrase (e.g. search_crm). Respond with only the "
            f"label.\n\nSource id: {descriptor.source_id}\n"
            f"Description: {cls._description(descriptor) or '(none)'}"
        )


__all__ = ["LlmStructureProvider"]
