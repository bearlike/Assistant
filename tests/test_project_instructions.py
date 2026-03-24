#!/usr/bin/env python3
"""Tests for runtime CLAUDE.md / AGENTS.md discovery."""

from __future__ import annotations

import os

from meeseeks_core.common import (
    _NOLOAD_MARKER,
    InstructionSource,
    discover_all_instructions,
    discover_project_instructions,
    get_git_context,
)


class TestDiscoverProjectInstructions:
    def test_no_files_returns_none(self, tmp_path):
        assert discover_project_instructions(str(tmp_path)) is None

    def test_claude_md_loaded(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Project\nUse DRY.", encoding="utf-8")
        result = discover_project_instructions(str(tmp_path))
        assert result is not None
        assert "# Project\nUse DRY." in result
        assert "Instructions (project: CLAUDE.md)" in result

    def test_claude_md_takes_priority_over_agents_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("claude content", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("agents content", encoding="utf-8")
        result = discover_project_instructions(str(tmp_path))
        assert result is not None
        assert "claude content" in result
        # AGENTS.md is not discovered by the hierarchical system
        assert "agents content" not in result

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


class TestDiscoverAllInstructions:
    """Tests for hierarchical instruction discovery."""

    def test_empty_dir_returns_empty(self, tmp_path):
        sources = discover_all_instructions(str(tmp_path))
        assert sources == []

    def test_project_claude_md_discovered(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("project instructions", encoding="utf-8")
        sources = discover_all_instructions(str(tmp_path))
        project_sources = [s for s in sources if s.level == "project"]
        assert len(project_sources) == 1
        assert project_sources[0].content == "project instructions"
        assert project_sources[0].priority == 20

    def test_dot_claude_subdir_discovered(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("dotclaude instructions", encoding="utf-8")
        sources = discover_all_instructions(str(tmp_path))
        project_sources = [s for s in sources if s.level == "project"]
        assert len(project_sources) == 1
        assert project_sources[0].content == "dotclaude instructions"

    def test_rules_directory_discovered(self, tmp_path):
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "style.md").write_text("use black formatter", encoding="utf-8")
        (rules_dir / "testing.md").write_text("always write tests", encoding="utf-8")
        sources = discover_all_instructions(str(tmp_path))
        rules_sources = [s for s in sources if s.level == "rules"]
        assert len(rules_sources) == 2
        assert rules_sources[0].content == "use black formatter"
        assert rules_sources[1].content == "always write tests"
        assert all(s.priority == 30 for s in rules_sources)

    def test_local_claude_md_discovered(self, tmp_path):
        (tmp_path / "CLAUDE.local.md").write_text("local overrides", encoding="utf-8")
        sources = discover_all_instructions(str(tmp_path))
        local_sources = [s for s in sources if s.level == "local"]
        assert len(local_sources) == 1
        assert local_sources[0].content == "local overrides"
        assert local_sources[0].priority == 40

    def test_priority_ordering(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("project", encoding="utf-8")
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "rule.md").write_text("rule", encoding="utf-8")
        (tmp_path / "CLAUDE.local.md").write_text("local", encoding="utf-8")
        sources = discover_all_instructions(str(tmp_path))
        levels = [s.level for s in sources]
        # Project (20) < rules (30) < local (40)
        assert levels.index("project") < levels.index("rules")
        assert levels.index("rules") < levels.index("local")

    def test_noload_marker_skips_source(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text(
            f"{_NOLOAD_MARKER}\nskip this", encoding="utf-8"
        )
        sources = discover_all_instructions(str(tmp_path))
        assert len([s for s in sources if s.level == "project"]) == 0

    def test_empty_files_skipped(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("", encoding="utf-8")
        (tmp_path / "CLAUDE.local.md").write_text("   \n  ", encoding="utf-8")
        sources = discover_all_instructions(str(tmp_path))
        assert sources == []

    def test_instruction_source_dataclass(self):
        src = InstructionSource(
            content="hello", path="/tmp/test.md", level="project", priority=20
        )
        assert src.content == "hello"
        assert src.level == "project"
        assert src.priority == 20

    def test_rules_non_md_files_ignored(self, tmp_path):
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "notes.txt").write_text("not markdown", encoding="utf-8")
        (rules_dir / "valid.md").write_text("markdown rule", encoding="utf-8")
        sources = discover_all_instructions(str(tmp_path))
        rules_sources = [s for s in sources if s.level == "rules"]
        assert len(rules_sources) == 1
        assert rules_sources[0].content == "markdown rule"

    def test_walk_up_to_git_root(self, tmp_path):
        # Create a fake git root with CLAUDE.md
        (tmp_path / ".git").mkdir()
        (tmp_path / "CLAUDE.md").write_text("root instructions", encoding="utf-8")
        # Create a subdirectory with its own CLAUDE.md
        subdir = tmp_path / "sub" / "deep"
        subdir.mkdir(parents=True)
        (subdir / "CLAUDE.md").write_text("subdir instructions", encoding="utf-8")
        sources = discover_all_instructions(str(subdir))
        project_sources = [s for s in sources if s.level == "project"]
        assert len(project_sources) == 2
        # Subdir has higher priority (20) than root (22)
        subdir_src = [s for s in project_sources if "subdir" in s.content][0]
        root_src = [s for s in project_sources if "root" in s.content][0]
        assert subdir_src.priority < root_src.priority


class TestGetGitContext:
    """Tests for git context gathering."""

    def test_not_a_git_repo(self, tmp_path):
        result = get_git_context(str(tmp_path))
        assert result is None

    def test_git_repo_returns_branch(self, tmp_path):
        # Initialize a git repo
        os.system(f"cd {tmp_path} && git init -q && git commit --allow-empty -m 'init' -q")
        result = get_git_context(str(tmp_path))
        assert result is not None
        assert "Current branch:" in result

    def test_git_repo_shows_status(self, tmp_path):
        os.system(f"cd {tmp_path} && git init -q && git commit --allow-empty -m 'init' -q")
        (tmp_path / "test.txt").write_text("hello", encoding="utf-8")
        result = get_git_context(str(tmp_path))
        assert result is not None
        assert "Status:" in result
        assert "test.txt" in result

    def test_clean_repo_shows_clean(self, tmp_path):
        repo = tmp_path / "clean_repo"
        repo.mkdir()
        os.system(f"cd {repo} && git init -q && git commit --allow-empty -m 'init' -q")
        result = get_git_context(str(repo))
        assert result is not None
        assert "Status: clean" in result

    def test_truncates_long_status(self, tmp_path):
        os.system(f"cd {tmp_path} && git init -q && git commit --allow-empty -m 'init' -q")
        # Create many files to generate a long status
        for i in range(200):
            (tmp_path / f"file_{i:04d}.txt").write_text(f"content {i}", encoding="utf-8")
        result = get_git_context(str(tmp_path), max_status_chars=100)
        assert result is not None
        assert "[truncated]" in result
