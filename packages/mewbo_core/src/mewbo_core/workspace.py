#!/usr/bin/env python3
"""Workspace containment — the per-agent filesystem firebreak.

``workspace_mode`` is the SECOND privilege axis a spawn can attenuate, orthogonal
to ``capability_mode``: where ``capability_mode`` gates *which tools* an
agent holds, ``workspace_mode`` gates *which paths* those tools may touch. The
three tiers (widest → narrowest):

* ``full_access``  — no path restriction; the historical behaviour. Root default.
* ``workspace_write`` — reads and writes confined to the agent's workspace root
  (plus the Mewbo-owned scratch roots).
* ``read_only`` — reads confined to the workspace root; NO writes anywhere.

``WorkspaceContainment`` is an atomic frozen dataclass carrying the resolved
policy (``mode`` + ``root``). It lives in ``mewbo_core`` — NOT ``mewbo_tools`` —
because BOTH sides of the DAG need it: the loop (``mewbo_core.tool_use_loop``)
builds it and forces the authoritative root, and the path guard
(``mewbo_tools.core.resolve_safe_path``) enforces it. Dependencies flow
``core → tools`` (tools imports core, never the reverse), so the shared type sits
at the bottom in core and both layers import DOWN to it.

**Staging (v1).** Enforcement is gated on ``agent.workspace_enforcement`` (default
``False``). While that flag is off, no ``WorkspaceContainment`` is ever activated,
so every path resolves byte-identically to the historical tenant-union behaviour.
Flipping the flip is what turns the tiers live — see ``resolve_safe_path``.

**Loop → tool bridge.** The path guard's callers live in ``mewbo_tools`` and
receive their arguments as JSON (which cannot carry a live object), and the loop
(in ``mewbo_core``) must not import an app or reach sideways into ``mewbo_tools``.
The active containment therefore travels through a context variable set by the
loop around each tool's execution and read by ``resolve_safe_path`` as a fallback
when no explicit ``containment=`` is passed. ``contextvars`` propagate into
``asyncio.to_thread`` (the sync-tool execution path), so a threaded ``tool.run``
sees the same active containment as the awaiting frame that set it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from mewbo_core.exit_plan_mode import PLAN_DIR_ROOT

# The three workspace-containment tiers. A plain module type alias (not owned by
# ``AgentContext``) so both the spawn schema and the config validator can pin the
# same vocabulary via ``typing.get_args`` — the same idiom ``CapabilityMode`` uses
# for ``capability_mode``.
WorkspaceMode = Literal["read_only", "workspace_write", "full_access"]

# Monotonic privilege ranking for ``workspace_mode`` narrowing: lower rank
# = more restrictive. The narrowing itself is the shared ``AgentContext._narrow``
# min-wins helper; this table only encodes the ordering, mirroring
# ``AgentContext._CAPABILITY_MODE_RANK`` for the other axis.
WORKSPACE_MODE_RANK: dict[str, int] = {
    "read_only": 0,
    "workspace_write": 1,
    "full_access": 2,
}

# Mewbo-owned scratch root — mirrors the constant in
# ``mewbo_tools.core._get_allowed_roots``. Scoping to ``/tmp/mewbo`` (not all of
# ``/tmp``) is the tenant boundary the tools guard already draws; a contained
# agent keeps the same carve-out so plan-mode + widget scratch writes still land.
_SCRATCH_ROOT = "/tmp/mewbo"


@dataclass(frozen=True, slots=True)
class WorkspaceContainment:
    """Resolved filesystem-containment policy for one agent.

    ``mode`` is one of :data:`WorkspaceMode`; ``root`` is the agent's workspace
    directory (its loop ``cwd``). Behaviour intrinsic to the data lives ON the
    model — ``permits_read``/``permits_write`` own the tier semantics so the path
    guard delegates rather than re-deriving them with a service-side ``if mode ==``
    switch. Paths arrive as method ARGS (the model never reaches for the
    filesystem itself), keeping it trivially testable.
    """

    mode: str
    root: str

    @property
    def active(self) -> bool:
        """True when this containment restricts anything (``mode != full_access``).

        ``full_access`` is the historical no-op tier — an ``active`` containment is
        the only kind the loop ever builds or the guard ever enforces.
        """
        return self.mode != "full_access"

    def allowed_roots(self) -> tuple[str, ...]:
        """The directories this agent may touch: workspace root + scratch roots.

        The workspace ``root`` plus the two Mewbo-owned scratch roots
        (``/tmp/mewbo`` and the plan-scratch ``PLAN_DIR_ROOT``, which is itself
        under ``/tmp/mewbo`` but named explicitly for clarity + future-proofing).
        An empty ``root`` contributes nothing, so a containment with no root
        confines to the scratch roots alone.
        """
        roots: list[str] = []
        if self.root:
            roots.append(self.root)
        roots.append(_SCRATCH_ROOT)
        if PLAN_DIR_ROOT not in roots:
            roots.append(PLAN_DIR_ROOT)
        return tuple(roots)

    def _under_allowed(self, path: str) -> bool:
        """True when *path* physically resolves under an allowed root.

        Realpath-prefix semantics matching the ``FileCatalog`` jail precedent:
        both the candidate and each allowed root are ``realpath``-normalised
        (following symlinks) and compared with ``startswith(root + os.sep)``. This
        is DELIBERATELY stricter than the tenant-union's dual logical/physical
        view — a symlink whose *logical* path sits inside the workspace but whose
        *target* escapes it is rejected, closing the ``/tmp`` symlink → ``/etc/
        passwd`` hole the broad union tolerates. Containment is a firebreak, so
        the physical target is authoritative.
        """
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            return False
        for root in self.allowed_roots():
            try:
                real_root = os.path.realpath(root)
            except (OSError, ValueError):
                continue
            if real == real_root or real.startswith(real_root + os.sep):
                return True
        return False

    def permits_read(self, path: str) -> bool:
        """True when this agent may READ *path*.

        ``full_access`` reads anything; every narrower tier confines reads to the
        allowed roots.
        """
        if self.mode == "full_access":
            return True
        return self._under_allowed(path)

    def permits_write(self, path: str) -> bool:
        """True when this agent may WRITE *path*.

        ``read_only`` writes NOTHING (anywhere); ``full_access`` writes anything;
        ``workspace_write`` confines writes to the allowed roots.
        """
        if self.mode == "read_only":
            return False
        if self.mode == "full_access":
            return True
        return self._under_allowed(path)


# The active containment for the current execution context. The loop sets it
# around a tool's run when its own containment is active; ``resolve_safe_path``
# reads it as the fallback when no explicit ``containment=`` is passed. Defaults
# to ``None`` (no active containment ⇒ byte-identical tenant-union behaviour).
_ACTIVE_CONTAINMENT: ContextVar[WorkspaceContainment | None] = ContextVar(
    "mewbo_active_containment", default=None
)


def get_active_containment() -> WorkspaceContainment | None:
    """Return the containment active for the current execution context, or ``None``.

    Read by ``mewbo_tools.core.resolve_safe_path`` (fallback) and the LSP tool so
    they enforce the same firebreak the loop established, without the loop having
    to thread a live object through JSON tool arguments.
    """
    return _ACTIVE_CONTAINMENT.get()


@contextmanager
def active_containment(containment: WorkspaceContainment | None) -> Iterator[None]:
    """Set the active containment for the duration of the ``with`` block.

    A ``None`` (or inactive) *containment* is a no-op that leaves the current
    active containment untouched — so a full_access / enforcement-off tool run
    pays nothing and stays byte-identical. Reset via the ``ContextVar`` token in
    ``finally`` so nesting and early exits never leak.
    """
    if containment is None or not containment.active:
        yield
        return
    token = _ACTIVE_CONTAINMENT.set(containment)
    try:
        yield
    finally:
        _ACTIVE_CONTAINMENT.reset(token)


__all__ = [
    "WORKSPACE_MODE_RANK",
    "WorkspaceContainment",
    "WorkspaceMode",
    "active_containment",
    "get_active_containment",
]
