#!/usr/bin/env python3
"""Core tool implementations and shared utilities."""

from __future__ import annotations

import os
from pathlib import Path

from truss_core.config import get_config_value
from truss_core.exit_plan_mode import PLAN_DIR_ROOT


def _get_allowed_roots() -> list[Path]:
    """Return resolved paths for all configured project directories + CWD.

    Also includes :data:`truss_core.exit_plan_mode.PLAN_DIR_ROOT` so the
    edit and file tools can write/read the per-session plan scratch file
    at ``/tmp/truss/plans/<session_id>/plan.md`` during plan mode.
    Per-session containment is enforced by ``is_inside_plan_dir()`` at the
    upper (permission) guard in ``tool_use_loop._plan_mode_permission``,
    so widening the root list here does not relax session isolation.
    """
    roots: list[Path] = [Path(os.getcwd()).resolve()]
    projects: dict = get_config_value("projects", default={})
    for cfg in projects.values():
        raw = cfg.get("path", "") if isinstance(cfg, dict) else getattr(cfg, "path", "")
        if raw:
            p = Path(raw).expanduser().resolve()
            if p not in roots:
                roots.append(p)
    plan_root = Path(PLAN_DIR_ROOT).resolve()
    if plan_root not in roots:
        roots.append(plan_root)
    # Truss-owned scratch root: covers /tmp/truss/widgets,
    # /tmp/truss/plans, and any future ephemeral subdirs we
    # spawn there. Scoping to /tmp/truss (not all of /tmp) keeps
    # tools from reaching into unrelated tempfiles like /tmp/ssh-* or
    # other users' pytest dirs.
    scratch_root = Path("/tmp/truss").resolve()
    if scratch_root not in roots:
        roots.append(scratch_root)
    return roots


def resolve_safe_path(path: str, root: str | None = None) -> Path:
    """Resolve *path* and verify it falls under an allowed project root.

    Checks (in order): *root* if given, then every ``projects[*].path``
    from the app config, then the process CWD.

    Two views of the path are checked so legitimate symlinks inside a
    project root are honored:

    * ``resolved`` — ``Path.resolve()``; normalizes ``..`` *and* follows
      symlinks. Authoritative physical location on disk.
    * ``logical``  — ``os.path.abspath``; normalizes ``..`` but preserves
      symlinks. Reflects the user's intent when a symlink lives inside
      an allowed root (e.g. ``<project>/homelab`` → ``/mnt/external``).

    The path is accepted if *either* view lands under an allowed root.
    ``../`` escape attempts still fail both checks and are rejected.

    Raises ``ValueError`` when the path is outside all roots.
    """
    candidate = Path(path)
    root_path = Path(root).resolve() if root else None

    if not candidate.is_absolute():
        base = root_path or Path(os.getcwd()).resolve()
        candidate = base / candidate
    resolved = candidate.resolve()
    logical = Path(os.path.abspath(candidate))

    # Build a single check list: explicit root first, then config roots.
    roots = _get_allowed_roots()
    if root_path is not None and root_path not in roots:
        roots.insert(0, root_path)
    # Also accept roots under their logical (symlink-preserving) form so a
    # project whose configured path traverses a symlink still matches the
    # user's logical view of the path.
    logical_roots: list[Path] = []
    for r in roots:
        lr = Path(os.path.abspath(r))
        if lr != r and lr not in roots and lr not in logical_roots:
            logical_roots.append(lr)

    for r in roots:
        try:
            resolved.relative_to(r)
            return resolved
        except ValueError:
            pass
    for r in [*roots, *logical_roots]:
        try:
            logical.relative_to(r)
            return logical
        except ValueError:
            continue

    raise ValueError(f"Path '{path}' resolves outside all allowed project roots.")


__all__ = ["resolve_safe_path"]
