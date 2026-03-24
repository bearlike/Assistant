#!/usr/bin/env python3
"""Tests for runtime CLAUDE.md / AGENTS.md discovery."""

from __future__ import annotations

from meeseeks_core.common import _NOLOAD_MARKER, discover_project_instructions


class TestDiscoverProjectInstructions:
    def test_no_files_returns_none(self, tmp_path):
        assert discover_project_instructions(str(tmp_path)) is None

    def test_claude_md_loaded(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Project\nUse DRY.", encoding="utf-8")
        result = discover_project_instructions(str(tmp_path))
        assert result == "# Project\nUse DRY."

    def test_claude_md_takes_priority_over_agents_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude content", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("agents content", encoding="utf-8")
        result = discover_project_instructions(str(tmp_path))
        assert result == "claude content"

    def test_agents_md_fallback_when_no_claude_md(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Instructions\nDo things.", encoding="utf-8")
        result = discover_project_instructions(str(tmp_path))
        assert result == "# Instructions\nDo things."

    def test_empty_claude_md_falls_through_to_agents(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("   \n  ", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("fallback content", encoding="utf-8")
        result = discover_project_instructions(str(tmp_path))
        assert result == "fallback content"

    def test_empty_both_returns_none(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("", encoding="utf-8")
        assert discover_project_instructions(str(tmp_path)) is None

    def test_noload_marker_skips_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text(
            f"{_NOLOAD_MARKER}\n# Skip this.", encoding="utf-8"
        )
        assert discover_project_instructions(str(tmp_path)) is None

    def test_noload_marker_skips_claude_md_falls_through(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text(
            f"{_NOLOAD_MARKER}\n# Skip this.", encoding="utf-8"
        )
        (tmp_path / "AGENTS.md").write_text("agents content", encoding="utf-8")
        result = discover_project_instructions(str(tmp_path))
        assert result == "agents content"

    def test_noload_marker_skips_agents_md(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text(
            f"{_NOLOAD_MARKER}\nSkip.", encoding="utf-8"
        )
        assert discover_project_instructions(str(tmp_path)) is None
