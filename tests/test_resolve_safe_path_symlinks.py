"""Tests for ``resolve_safe_path`` symlink handling.

A symlink living inside an allowed project root must be honored even when
its target sits outside every root. The previous implementation called
``Path.resolve()`` which follows symlinks, so a workspace symlink like
``<project>/homelab -> /mnt/external`` was rejected with
"resolves outside all allowed project roots". ``../`` escape attempts
must still be rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from meeseeks_tools.core import resolve_safe_path


def test_accepts_symlink_inside_root_pointing_outside(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (external / "config.yml").write_text("hello\n", encoding="utf-8")

    (project / "homelab").symlink_to(external)

    resolved = resolve_safe_path("homelab/config.yml", root=str(project))

    assert resolved.read_text(encoding="utf-8") == "hello\n"
    # Returned path is under the allowed root (logical view preserved).
    assert str(resolved).startswith(str(project))


def test_accepts_absolute_path_through_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (external / "note.txt").write_text("data", encoding="utf-8")

    (project / "link").symlink_to(external)

    target = str(project / "link" / "note.txt")
    resolved = resolve_safe_path(target, root=str(project))

    assert resolved.read_text(encoding="utf-8") == "data"


def test_rejects_dot_dot_escape_even_through_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")

    with pytest.raises(ValueError, match="outside all allowed project roots"):
        resolve_safe_path("../outside/secret.txt", root=str(project))


def test_rejects_direct_path_outside_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (external / "config.yml").write_text("x", encoding="utf-8")
    (project / "homelab").symlink_to(external)

    # Even though `homelab` -> `external` is a symlink inside the root,
    # accessing `external` directly is outside the root and must fail.
    with pytest.raises(ValueError, match="outside all allowed project roots"):
        resolve_safe_path(str(external / "config.yml"), root=str(project))


def test_accepts_when_root_itself_traverses_symlink(tmp_path: Path) -> None:
    """If the configured root is reached via a symlink, paths under it must work.

    Example: user configures ``projects.foo.path = /var/foo`` where
    ``/var -> /private/var``. ``Path.resolve(root)`` returns
    ``/private/var/foo`` while the user passes ``/var/foo/file``. The
    logical-view fallback accepts this.
    """
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    (real_root / "file.txt").write_text("ok", encoding="utf-8")

    link_root = tmp_path / "link_root"
    link_root.symlink_to(real_root)

    target = str(link_root / "file.txt")
    resolved = resolve_safe_path(target, root=str(link_root))

    assert resolved.read_text(encoding="utf-8") == "ok"


def test_rejects_path_outside_all_roots_when_no_symlink_involved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="outside all allowed project roots"):
        resolve_safe_path("/etc/hostname", root=str(project))


def test_read_tool_reads_through_symlink(tmp_path: Path) -> None:
    """Integration: ReadFileTool must read a file via a symlink inside root."""
    from meeseeks_core.classes import ActionStep
    from meeseeks_tools.integration.aider_file_tools import ReadFileTool

    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (external / "litellm-config.yml").write_text("model_list: []\n", encoding="utf-8")
    (project / "homelab").symlink_to(external)

    # Set up a nested symlink structure matching the real-world report.
    nested = external / "litellm"
    nested.mkdir()
    (nested / "litellm-config.yml").write_text("model_list: []\n", encoding="utf-8")

    tool = ReadFileTool()
    step = ActionStep(
        tool_id="read_file",
        operation="get",
        tool_input={
            "path": "homelab/litellm/litellm-config.yml",
            "root": str(project),
        },
    )
    result = tool.get_state(step)

    payload = result.content
    assert isinstance(payload, dict), f"expected dict, got error: {payload!r}"
    assert payload.get("kind") == "file"
    assert "model_list" in payload.get("text", "")
