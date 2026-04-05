#!/usr/bin/env python3
"""Core tool implementations and shared utilities."""

from __future__ import annotations

import os
from pathlib import Path

from meeseeks_core.config import get_config_value
from meeseeks_core.exit_plan_mode import PLAN_DIR_ROOT


def _get_allowed_roots() -> list[Path]:
    """Return resolved paths for all configured project directories + CWD.

    Also includes :data:`meeseeks_core.exit_plan_mode.PLAN_DIR_ROOT` so the
    edit and file tools can write/read the per-session plan scratch file
    at ``/tmp/meeseeks/plans/<session_id>/plan.md`` during plan mode.
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
    return roots


def resolve_safe_path(path: str, root: str | None = None) -> Path:
    """Resolve *path* and verify it falls under an allowed project root.

    Checks (in order): *root* if given, then every ``projects[*].path``
    from the app config, then the process CWD.

    Raises ``ValueError`` when the resolved path is outside all roots.
    """
    candidate = Path(path)
    root_path = Path(root).resolve() if root else None

    if not candidate.is_absolute():
        base = root_path or Path(os.getcwd()).resolve()
        candidate = base / candidate
    resolved = candidate.resolve()

    # Build a single check list: explicit root first, then config roots.
    roots = _get_allowed_roots()
    if root_path is not None and root_path not in roots:
        roots.insert(0, root_path)

    for r in roots:
        try:
            resolved.relative_to(r)
            return resolved
        except ValueError:
            continue

    raise ValueError(
        f"Path '{path}' resolves outside all allowed project roots."
    )


__all__ = ["resolve_safe_path"]
