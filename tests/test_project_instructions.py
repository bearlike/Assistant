#!/usr/bin/env python3
"""Tests for runtime CLAUDE.md / AGENTS.md discovery."""

from __future__ import annotations

import os

from meeseeks_core.common import (
    _NOLOAD_MARKER,
    InstructionSource,
    discover_all_instructions,
    discover_project_instructions,
    discover_subtree_instructions,
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

    @staticmethod
    def _git_init(path) -> None:
        """Initialize a git repo with identity (required in CI)."""
        os.system(
            f"cd {path} && git init -q"
            f" && git config user.email 'test@test.com'"
            f" && git config user.name 'Test'"
            f" && git commit --allow-empty -m 'init' -q"
        )

    def test_git_repo_returns_branch(self, tmp_path):
        self._git_init(tmp_path)
        result = get_git_context(str(tmp_path))
        assert result is not None
        assert "Current branch:" in result

    def test_git_repo_shows_status(self, tmp_path):
        self._git_init(tmp_path)
        (tmp_path / "test.txt").write_text("hello", encoding="utf-8")
        result = get_git_context(str(tmp_path))
        assert result is not None
        assert "Status:" in result
        assert "test.txt" in result

    def test_clean_repo_shows_clean(self, tmp_path):
        repo = tmp_path / "clean_repo"
        repo.mkdir()
        self._git_init(repo)
        result = get_git_context(str(repo))
        assert result is not None
        assert "Status: clean" in result

    def test_truncates_long_status(self, tmp_path):
        self._git_init(tmp_path)
        for i in range(200):
            (tmp_path / f"file_{i:04d}.txt").write_text(f"content {i}", encoding="utf-8")
        result = get_git_context(str(tmp_path), max_status_chars=100)
        assert result is not None
        assert "[truncated]" in result


class TestDiscoverSubtreeInstructions:
    """Tests for downward subtree instruction discovery."""

    def test_finds_claude_md_in_subdirs(self, tmp_path):
        sub = tmp_path / "apps" / "api"
        sub.mkdir(parents=True)
        (sub / "CLAUDE.md").write_text("API guidance", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 1
        assert result[0].level == "subtree"
        assert result[0].content == ""  # Index only, no content
        assert "api" in result[0].path and "CLAUDE.md" in result[0].path

    def test_finds_agents_md_in_subdirs(self, tmp_path):
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("Agent guidance", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 1
        assert "AGENTS.md" in result[0].path

    def test_finds_dot_claude_subdir(self, tmp_path):
        sub = tmp_path / "pkg" / ".claude"
        sub.mkdir(parents=True)
        (sub / "CLAUDE.md").write_text("Dot claude", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 1
        assert ".claude/CLAUDE.md" in result[0].path

    def test_respects_noload_marker(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "CLAUDE.md").write_text(
            f"{_NOLOAD_MARKER}\nSkip me", encoding="utf-8"
        )
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 0

    def test_respects_max_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f"  # depth 6
        deep.mkdir(parents=True)
        (deep / "CLAUDE.md").write_text("Too deep", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path), max_depth=5)
        assert len(result) == 0

    def test_within_max_depth(self, tmp_path):
        sub = tmp_path / "a" / "b" / "c" / "d" / "e"  # depth 5
        sub.mkdir(parents=True)
        (sub / "CLAUDE.md").write_text("Just right", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path), max_depth=5)
        assert len(result) == 1

    def test_skips_cwd_itself(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Root", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 0

    def test_skips_hidden_dirs(self, tmp_path):
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "CLAUDE.md").write_text("Hidden", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 0

    def test_skips_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "CLAUDE.md").write_text("Dep", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 0

    def test_skips_pycache(self, tmp_path):
        pc = tmp_path / "__pycache__"
        pc.mkdir()
        (pc / "CLAUDE.md").write_text("Cache", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 0

    def test_multiple_files_sorted_by_path(self, tmp_path):
        for name in ("beta", "alpha"):
            sub = tmp_path / name
            sub.mkdir()
            (sub / "CLAUDE.md").write_text(f"{name} guide", encoding="utf-8")
        result = discover_subtree_instructions(str(tmp_path))
        assert len(result) == 2
        assert "alpha" in result[0].path
        assert "beta" in result[1].path

    def test_integrated_in_discover_project_instructions(self, tmp_path):
        """Subtree files appear as index in composed output."""
        (tmp_path / "CLAUDE.md").write_text("Root instructions", encoding="utf-8")
        sub = tmp_path / "apps" / "api"
        sub.mkdir(parents=True)
        (sub / "CLAUDE.md").write_text("API guidance", encoding="utf-8")
        result = discover_project_instructions(str(tmp_path))
        assert result is not None
        assert "Root instructions" in result
        assert "Sub-package instruction files" in result
        assert "apps/api/CLAUDE.md" in result or "apps\\api\\CLAUDE.md" in result
