"""Shared fixtures for the SCG eval harness — fake connectors + fake embedder.

``wiki_store`` is the genuinely shared fixture (consumed by ``test_memory_bridge``,
``test_plugin_tools``, and ``test_eval_harness``). The eval-only fakes
(``EvalEmbedder``, ``entity_type``, the descriptor builders) currently have a
single consumer — ``test_eval_harness`` — and live here as the SCG eval catalog.
NO network, NO real LLM, NO MongoDB.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mewbo_graph.scg.types import (
    CapabilityBinding,
    ScgNode,
    SourceDescriptor,
)

# ── Deterministic keyword-bag embedder ───────────────────────────────────────


class EvalEmbedder:
    """A token-presence-count embedder over a closed vocabulary — fully offline.

    Satisfies every embedder surface the SCG touches with one object:

    * ``embed_nodes(items)`` → wiki ``Embedding`` rows (what ``ScgParser`` and
      the memory bridge's ``InsightIngestor`` persist),
    * ``embed_query(text)`` → a raw vector (what ``ScgRouter`` and the memory
      bridge's ``read_insights`` consume),
    * ``model`` attribute (read off by the parser onto each ``ScgEmbedding``).

    Each text becomes a fixed-width vector of per-token counts over ``_VOCAB``,
    so cosine similarity is meaningful and reproducible (the same projection
    embeds nodes and queries). Vocabulary spans the three fake connectors so a
    query lexically favours one source's capability text (name + doc + example
    queries).
    """

    model = "eval-embed"

    _VOCAB = [
        "jira",
        "linear",
        "pagerduty",
        "issue",
        "ticket",
        "incident",
        "search",
        "list",
        "find",
        "acknowledge",
        "project",
        "team",
        "status",
        "user",
        "assignee",
        "triggered",
        "oncall",
    ]

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        return [float(low.count(tok)) for tok in self._VOCAB]

    def embed_nodes(self, items: list[tuple[str, str]], *, slug: str = "") -> list:
        from mewbo_graph.wiki.types import Embedding

        return [
            Embedding(
                slug=slug,
                node_id=nid,
                vector=self._vec(text),
                model=self.model,
                dim=len(self._VOCAB),
            )
            for nid, text in items
        ]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


# ── Fake connector descriptors (two OpenAPI + one MCP-tool-list) ─────────────


def _jira_descriptor() -> SourceDescriptor:
    """A Jira-like OpenAPI source: an Issue entity + two search capabilities."""
    return SourceDescriptor(
        source_id="jira",
        source_type="openapi",
        raw={
            "openapi": "3.1.0",
            "info": {"title": "Jira"},
            "components": {
                "schemas": {
                    "Issue": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "status": {"type": "string"},
                            "assignee": {"type": "string"},
                        },
                    }
                }
            },
            "paths": {
                "/search": {
                    "get": {
                        "operationId": "search_jira_issue",
                        "summary": "Search jira issue by project and status.",
                        "parameters": [
                            {"name": "project", "in": "query", "required": True,
                             "schema": {"type": "string"}},
                            {"name": "status", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                        ],
                    }
                },
                "/issue/{id}": {
                    "get": {
                        "operationId": "get_jira_issue",
                        "summary": "Get a jira issue by id with its status.",
                        "parameters": [
                            {"name": "id", "in": "path", "required": True,
                             "schema": {"type": "string"}},
                        ],
                    }
                },
            },
        },
    )


def _linear_descriptor() -> SourceDescriptor:
    """A Linear-like OpenAPI source: an Issue entity (overlaps Jira) + searches."""
    return SourceDescriptor(
        source_id="linear",
        source_type="openapi",
        raw={
            "openapi": "3.1.0",
            "info": {"title": "Linear"},
            "components": {
                "schemas": {
                    "Issue": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "status": {"type": "string"},
                            "assignee": {"type": "string"},
                        },
                    }
                }
            },
            "paths": {
                "/tickets": {
                    "get": {
                        "operationId": "list_linear_ticket",
                        "summary": "List linear ticket for a team or assignee user.",
                        "parameters": [
                            {"name": "team", "in": "query", "required": True,
                             "schema": {"type": "string"}},
                            {"name": "assignee", "in": "query", "required": False,
                             "schema": {"type": "string"}},
                        ],
                    }
                },
                "/tickets/find": {
                    "get": {
                        "operationId": "find_linear_ticket",
                        "summary": "Find a linear ticket assigned to a user.",
                        "parameters": [
                            {"name": "user", "in": "query", "required": True,
                             "schema": {"type": "string"}},
                        ],
                    }
                },
            },
        },
    )


def _pagerduty_descriptor() -> SourceDescriptor:
    """A PagerDuty-like MCP tool-list source: incident tools (dissimilar type)."""
    return SourceDescriptor(
        source_id="pagerduty",
        source_type="mcp_tool_list",
        raw={
            "tools": [
                {
                    "name": "list_pagerduty_incident",
                    "description": "List pagerduty incidents that are triggered.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "service": {"type": "string"},
                        },
                        "required": ["service"],
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {"incident_id": {"type": "string"}},
                    },
                },
                {
                    "name": "acknowledge_pagerduty_incident",
                    "description": "Acknowledge the oncall pagerduty incident.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"incident_id": {"type": "string"}},
                        "required": ["incident_id"],
                    },
                },
            ]
        },
    )


# ── Alignable entity types (binding-bearing) ─────────────────────────────────
#
# The OpenAPI/MCP providers attach a capability's queryable fields to the
# *capability* node and split entity fields into separate ``field`` nodes; they
# do not populate ``entity_type.bindings``. ``TypeAligner`` aligns on
# ``entity_type.bindings`` (its field-overlap heuristic), so a faithful
# cross-source-link eval seeds binding-bearing entity types alongside the parsed
# catalog — the same shape the spec/§6 contract and ``entity_resolution`` expect.


def entity_type(source_id: str, name: str, fields: list[str]) -> ScgNode:
    """A binding-bearing ``entity_type`` node — the aligner's input shape."""
    return ScgNode(
        source_key=f"{source_id}#{name}",
        kind="entity_type",
        source_id=source_id,
        name=name,
        bindings=[
            CapabilityBinding(field_key=f"{source_id}#{name}.{f}", mode="optional")
            for f in fields
        ],
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def eval_descriptors() -> list[SourceDescriptor]:
    """The fake multi-connector catalog: Jira + Linear (OpenAPI) + PagerDuty (MCP)."""
    return [_jira_descriptor(), _linear_descriptor(), _pagerduty_descriptor()]


@pytest.fixture()
def wiki_store(tmp_path: Path):
    """A fresh wiki JSON store under a throwaway temp dir (no Mongo)."""
    from mewbo_graph.wiki.store import JsonWikiStore

    return JsonWikiStore(root_dir=tmp_path / "wiki")
