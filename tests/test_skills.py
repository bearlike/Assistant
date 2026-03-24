#!/usr/bin/env python3
"""Tests for the skills discovery, parsing, registry, and activation."""

from __future__ import annotations

import time

from meeseeks_core.skills import (
    ACTIVATE_SKILL_SCHEMA,
    SkillRegistry,
    SkillSpec,
    _preprocess_shell,
    activate_skill,
    discover_skills,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _write_skill(base, name, body, *, source="project", **meta_overrides):
    """Write a SKILL.md file into the expected directory structure."""
    import yaml

    meta = {
        "name": name,
        "description": f"Test skill {name}",
        **meta_overrides,
    }
    frontmatter = yaml.dump(meta, default_flow_style=False).strip()
    content = f"---\n{frontmatter}\n---\n{body}"
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


# ------------------------------------------------------------------
# discover_skills
# ------------------------------------------------------------------


class TestDiscoverSkills:
    def test_no_skills_returns_empty(self, tmp_path):
        result = discover_skills(str(tmp_path))
        assert result == []

    def test_project_skill_discovered(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "review-pr", "Review the PR.")
        result = discover_skills(str(tmp_path))
        assert len(result) == 1
        assert result[0].name == "review-pr"
        assert result[0].source == "project"
        assert "Review the PR." in result[0].body

    def test_personal_skill_discovered(self, tmp_path, monkeypatch):
        personal_dir = tmp_path / "home" / ".claude" / "skills"
        _write_skill(personal_dir, "commit", "Do a commit.")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
        result = discover_skills(str(tmp_path))
        assert len(result) == 1
        assert result[0].name == "commit"
        assert result[0].source == "personal"

    def test_project_overrides_personal(self, tmp_path, monkeypatch):
        personal_dir = tmp_path / "home" / ".claude" / "skills"
        _write_skill(personal_dir, "deploy", "Personal deploy.")
        project_dir = tmp_path / ".claude" / "skills"
        _write_skill(project_dir, "deploy", "Project deploy.")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
        result = discover_skills(str(tmp_path))
        assert len(result) == 1
        assert result[0].source == "project"
        assert "Project deploy." in result[0].body

    def test_malformed_frontmatter_skipped(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills" / "bad-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
        result = discover_skills(str(tmp_path))
        assert result == []

    def test_missing_name_skipped(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills" / "no-name"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\ndescription: has desc\n---\nbody\n",
            encoding="utf-8",
        )
        result = discover_skills(str(tmp_path))
        assert result == []

    def test_missing_description_skipped(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills" / "no-desc"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: no-desc\n---\nbody\n",
            encoding="utf-8",
        )
        result = discover_skills(str(tmp_path))
        assert result == []

    def test_invalid_name_skipped(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills" / "Bad_Name"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: Bad_Name\ndescription: test\n---\nbody\n",
            encoding="utf-8",
        )
        result = discover_skills(str(tmp_path))
        assert result == []


# ------------------------------------------------------------------
# SkillSpec parsing
# ------------------------------------------------------------------


class TestSkillSpecParsing:
    def test_allowed_tools_parsed(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(
            skills_dir,
            "scoped",
            "body",
            **{"allowed-tools": "Read Grep Bash"},
        )
        result = discover_skills(str(tmp_path))
        assert result[0].allowed_tools == ["Read", "Grep", "Bash"]

    def test_disable_model_invocation(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(
            skills_dir,
            "manual-only",
            "body",
            **{"disable-model-invocation": "true"},
        )
        result = discover_skills(str(tmp_path))
        assert result[0].disable_model_invocation is True

    def test_user_invocable_false(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(
            skills_dir,
            "llm-only",
            "body",
            **{"user-invocable": False},
        )
        result = discover_skills(str(tmp_path))
        assert result[0].user_invocable is False

    def test_context_fork(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "forked", "body", context="fork")
        result = discover_skills(str(tmp_path))
        assert result[0].context == "fork"

    def test_model_override(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "custom-model", "body", model="gpt-4o")
        result = discover_skills(str(tmp_path))
        assert result[0].model == "gpt-4o"


# ------------------------------------------------------------------
# SkillRegistry
# ------------------------------------------------------------------


class TestSkillRegistry:
    def test_load_and_list(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "skill-a", "A body")
        _write_skill(skills_dir, "skill-b", "B body")

        registry = SkillRegistry()
        registry.load(str(tmp_path))
        assert len(registry.list_all()) == 2

    def test_get_by_name(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "my-skill", "body text")

        registry = SkillRegistry()
        registry.load(str(tmp_path))
        skill = registry.get("my-skill")
        assert skill is not None
        assert skill.name == "my-skill"
        assert registry.get("nonexistent") is None

    def test_list_user_invocable(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "visible", "body")
        _write_skill(
            skills_dir,
            "hidden",
            "body",
            **{"user-invocable": False},
        )

        registry = SkillRegistry()
        registry.load(str(tmp_path))
        invocable = registry.list_user_invocable()
        assert len(invocable) == 1
        assert invocable[0].name == "visible"

    def test_list_auto_invocable(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "auto", "body")
        _write_skill(
            skills_dir,
            "manual",
            "body",
            **{"disable-model-invocation": "true"},
        )

        registry = SkillRegistry()
        registry.load(str(tmp_path))
        auto = registry.list_auto_invocable()
        assert len(auto) == 1
        assert auto[0].name == "auto"

    def test_render_catalog_empty(self, tmp_path):
        registry = SkillRegistry()
        registry.load(str(tmp_path))
        assert registry.render_catalog() == ""

    def test_render_catalog_format(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "review-pr", "body")

        registry = SkillRegistry()
        registry.load(str(tmp_path))
        catalog = registry.render_catalog()
        assert "review-pr" in catalog
        assert "activate_skill" in catalog

    def test_maybe_reload_detects_change(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "mutable", "original body")

        registry = SkillRegistry()
        registry.load(str(tmp_path))
        assert "original body" in registry.get("mutable").body

        # Modify the file.
        time.sleep(0.05)  # ensure mtime changes
        skill_file = skills_dir / "mutable" / "SKILL.md"
        content = skill_file.read_text()
        skill_file.write_text(content.replace("original body", "updated body"))

        changed = registry.maybe_reload()
        assert changed is True
        assert "updated body" in registry.get("mutable").body

    def test_maybe_reload_detects_new_skill(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "existing", "body")

        registry = SkillRegistry()
        registry.load(str(tmp_path))
        assert len(registry.list_all()) == 1

        _write_skill(skills_dir, "brand-new", "new body")
        changed = registry.maybe_reload()
        assert changed is True
        assert len(registry.list_all()) == 2

    def test_maybe_reload_detects_deletion(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        _write_skill(skills_dir, "deletable", "body")

        registry = SkillRegistry()
        registry.load(str(tmp_path))
        assert len(registry.list_all()) == 1

        import shutil
        shutil.rmtree(skills_dir / "deletable")
        changed = registry.maybe_reload()
        assert changed is True
        assert len(registry.list_all()) == 0


# ------------------------------------------------------------------
# Shell preprocessing
# ------------------------------------------------------------------


class TestPreprocessShell:
    def test_simple_command(self):
        body = "Git log: !`echo hello-world`"
        result = _preprocess_shell(body)
        assert "hello-world" in result
        assert "!`" not in result

    def test_no_shell_patterns(self):
        body = "Plain text with no commands."
        result = _preprocess_shell(body)
        assert result == body

    def test_command_failure(self):
        body = "Result: !`exit 1`"
        result = _preprocess_shell(body)
        assert "[ERROR:" in result

    def test_multiple_commands(self):
        body = "A: !`echo aaa` and B: !`echo bbb`"
        result = _preprocess_shell(body)
        assert "aaa" in result
        assert "bbb" in result


# ------------------------------------------------------------------
# activate_skill
# ------------------------------------------------------------------


class TestActivateSkill:
    def _make_skill(self, body="body", **kwargs):
        defaults = {
            "name": "test-skill",
            "description": "test",
            "source_path": "/fake/SKILL.md",
            "source": "project",
        }
        defaults.update(kwargs)
        return SkillSpec(body=body, **defaults)

    def test_argument_substitution(self):
        skill = self._make_skill(body="Deploy $ARGUMENTS to $0 env")
        instructions, _ = activate_skill(skill, "staging --force")
        assert "Deploy staging --force to staging env" in instructions

    def test_no_tool_scoping_without_allowed_tools(self):
        skill = self._make_skill()
        instructions, specs = activate_skill(skill, "")
        assert specs is None

    def test_tool_scoping_filters_specs(self):
        from meeseeks_core.tool_registry import ToolSpec

        specs = [
            ToolSpec(tool_id="read", name="read", description="", factory=lambda: None),
            ToolSpec(tool_id="write", name="write", description="", factory=lambda: None),
            ToolSpec(tool_id="shell", name="shell", description="", factory=lambda: None),
        ]
        skill = self._make_skill(allowed_tools=["read", "write"])
        _, scoped = activate_skill(skill, "", specs)
        assert scoped is not None
        scoped_ids = {s.tool_id for s in scoped}
        assert scoped_ids == {"read", "write"}


# ------------------------------------------------------------------
# ACTIVATE_SKILL_SCHEMA
# ------------------------------------------------------------------


class TestActivateSkillSchema:
    def test_schema_structure(self):
        assert ACTIVATE_SKILL_SCHEMA["type"] == "function"
        func = ACTIVATE_SKILL_SCHEMA["function"]
        assert func["name"] == "activate_skill"
        params = func["parameters"]
        assert "skill_name" in params["properties"]
        assert "skill_name" in params["required"]
