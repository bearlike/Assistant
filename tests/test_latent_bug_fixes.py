"""Regression tests for latent bugs fixed during the 2026-06 hardening pass.

Each test was confirmed to FAIL against the pre-fix code:
- ``MongoDBConfig`` env overrides silently no-op'd on the ``model_validate({})``
  default path (missing ``validate_default``).
- ``HomeAssistantCall`` carried a non-JSON-schemable ``cache`` field, so
  ``PydanticOutputParser`` raised and broke the entire HA "set" action.
- ``clean_entities`` mutated the list it iterated, skipping adjacent matches.
- The wiki source/graph tools checked ``isinstance(view, dict)`` while their
  error sentinel is a ``MockSpeaker`` → ``AttributeError`` on the error path.
- ``find_similar_lines`` referenced an unbound local for ``threshold <= 0``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from mewbo_core.common import MockSpeaker


class TestMongoDBEnvOverride:
    """MEWBO_MONGODB_* must apply even when the field uses its default."""

    def test_env_override_applies_under_model_validate_empty(self, monkeypatch):
        from mewbo_core.config import MongoDBConfig

        monkeypatch.setenv("MEWBO_MONGODB_URI", "mongodb://envhost:9999")
        monkeypatch.setenv("MEWBO_MONGODB_DATABASE", "envdb")
        cfg = MongoDBConfig.model_validate({})
        assert cfg.uri == "mongodb://envhost:9999"
        assert cfg.database == "envdb"

    def test_default_when_no_env(self, monkeypatch):
        from mewbo_core.config import MongoDBConfig

        monkeypatch.delenv("MEWBO_MONGODB_URI", raising=False)
        monkeypatch.delenv("MEWBO_MONGODB_DATABASE", raising=False)
        cfg = MongoDBConfig.model_validate({})
        assert cfg.uri == "mongodb://localhost:27017"
        assert cfg.database == "mewbo"


class TestHomeAssistantCallSchema:
    """The ``set`` action's PydanticOutputParser must build successfully."""

    def test_output_parser_builds_format_instructions(self):
        from langchain_core.output_parsers import PydanticOutputParser
        from mewbo_tools.integration.homeassistant import HomeAssistantCall

        parser = PydanticOutputParser(pydantic_object=HomeAssistantCall)
        instructions = parser.get_format_instructions()
        assert "domain" in instructions
        assert "entity_id" in instructions
        # The injected cache field must NOT leak into the LLM-facing schema.
        assert "_ha_cache" not in instructions

    def test_cache_remains_a_real_field(self):
        # SkipJsonSchema only hides it from the schema; validators still use it.
        from mewbo_tools.integration.homeassistant import HomeAssistantCall

        assert "cache" in HomeAssistantCall.model_fields


class TestCleanEntitiesNoSkip:
    """cache_monitor's clean_entities must not skip adjacent matches."""

    @staticmethod
    def _holder(entities):
        class _H:
            cache = {
                "entities": [dict(e) for e in entities],
                "sensors": [],
                "services": [],
                "entity_ids": [],
                "sensor_ids": [],
                "allowed_domains": [],
            }

        return _H()

    @staticmethod
    def _run(holder):
        from mewbo_tools.integration.homeassistant import cache_monitor

        @cache_monitor
        def _noop(self):
            return None

        _noop(holder)

    def test_adjacent_forbidden_entities_all_removed(self):
        # "switch." is a hardcoded forbidden prefix; two ADJACENT switches must
        # BOTH be dropped (the old in-place .remove() skipped the second).
        holder = self._holder(
            [{"entity_id": "switch.a"}, {"entity_id": "switch.b"}, {"entity_id": "light.keep"}]
        )
        self._run(holder)
        assert [e["entity_id"] for e in holder.cache["entities"]] == ["light.keep"]

    def test_adjacent_sensors_all_moved(self):
        holder = self._holder(
            [
                {"entity_id": "sensor.temp1"},
                {"entity_id": "sensor.temp2"},
                {"entity_id": "light.keep"},
            ]
        )
        self._run(holder)
        assert [e["entity_id"] for e in holder.cache["entities"]] == ["light.keep"]
        assert [e["entity_id"] for e in holder.cache["sensors"]] == [
            "sensor.temp1",
            "sensor.temp2",
        ]


class TestWikiToolErrorPathReturnsSentinel:
    """handle() must return the error MockSpeaker, not AttributeError, on a
    failed ctx resolution (the sentinel is a MockSpeaker, never a dict)."""

    @staticmethod
    def _step():
        step = MagicMock()
        step.tool_input = {}
        return step

    def test_source_tool_handle_returns_error_sentinel(self):
        from mewbo_graph.plugins.wiki.source_tools import WikiReadFileTool, WikiSourceAccess

        tool = WikiReadFileTool(session_id="sess-x")
        with patch.object(WikiSourceAccess, "_resolve_runtime", return_value=None):
            result = asyncio.run(tool.handle(self._step()))
        assert isinstance(result, MockSpeaker)
        assert "wiki QA ctx not found" in result.content

    def test_graph_neighbors_handle_returns_error_sentinel(self):
        from mewbo_graph.plugins.wiki.graph_neighbors import (
            WikiGraphNeighbors,
            WikiGraphNeighborsTool,
        )

        tool = WikiGraphNeighborsTool(session_id="sess-x")
        with patch.object(WikiGraphNeighbors, "_resolve_runtime", return_value=None):
            result = asyncio.run(tool.handle(self._step()))
        assert isinstance(result, MockSpeaker)
        assert "runtime not available" in result.content


class TestFindSimilarLinesRobust:
    """find_similar_lines must not reference an unbound local."""

    def test_no_match_with_nonpositive_threshold_returns_empty(self):
        from mewbo_tools.aider_bridge.edit_blocks import find_similar_lines

        # threshold<=0 defeats the early-return guard; empty content used to hit
        # an unbound best_match_i. Must return "" cleanly.
        assert find_similar_lines("a\nb", "", threshold=0) == ""

    def test_real_match_still_returned(self):
        from mewbo_tools.aider_bridge.edit_blocks import find_similar_lines

        content = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        out = find_similar_lines("def foo():\n    return 1", content, threshold=0.6)
        assert "def foo():" in out
