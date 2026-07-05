#!/usr/bin/env python3
"""Permission service for the Mewbo TUI (issue #154, epic #149).

Replaces the broken blocking ``console.input`` approval with a layered
decision chain that integrates with the Textual App's modal approval
dialog.  The decision chain is evaluated per :class:`~mewbo_core.classes.ActionStep`
and returns a ``bool`` (``True`` = approve).

Chain order (first match wins):
  1. **Skip / bypass predicate** — injected callable that returns ``True`` to
     auto-approve (used for the auto-approve flag, etc.).
  2. **Mode-based auto-approve** — ``AUTO_ACCEPT_EDITS`` mode approves ``READ``
     and ``WRITE`` tier tools without asking.
  3. **Deny glob rules** — persisted deny rules in ``PermissionRuleStore``.
  4. **Allowlist glob rules** — persisted allow rules matched on ``tool`` and/or
     ``tool:action`` using :func:`fnmatch`.
  5. **Session-grant dict** — in-memory grants keyed by
     ``(session_id, tool_id, operation, path)``.
  6. **Textual modal** — dispatched via an injected ``modal_resolver`` callable
     (real App: ``call_from_thread(push_screen_wait, modal)``; tests: fake that
     returns the outcome string directly).
  7. **Exception fallback** — any unhandled error inside steps 1-6 defaults to
     **deny** (safe).

On any internal error the chain defaults to **deny** (safe).

Threading note
--------------
``decide()`` is called synchronously from inside the tool-use loop, which runs
on the App's worker thread.  The real ``modal_resolver`` must use
``app.call_from_thread(app.push_screen_wait, modal)`` to schedule the modal
on the main thread and block the worker until dismissed.  The service itself
is thread-agnostic — the threading boundary is the resolver's responsibility.
"""

from __future__ import annotations

import enum
import json
import logging
import os
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from mewbo_core.classes import ActionStep

if TYPE_CHECKING:
    from mewbo_cli.tui.seams import PermissionGateway

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PermissionMode enum
# ---------------------------------------------------------------------------


class PermissionMode(enum.Enum):
    """Permission operating mode.

    - ``NORMAL``            — ask for everything the allowlist/session-grant
      doesn't cover.
    - ``AUTO_ACCEPT_EDITS`` — auto-approve ``set``/write-class tools; still ask
      for ``execute``/destructive tools.
    - ``PLAN``              — extra-restrictive (no writes without explicit
      approval; future use — currently handled identically to NORMAL by the
      chain).
    """

    NORMAL = "normal"
    AUTO_ACCEPT_EDITS = "auto_accept_edits"
    PLAN = "plan"

    def next(self) -> PermissionMode:
        """Return the next mode in the cycle (NORMAL → AUTO_ACCEPT_EDITS → PLAN → NORMAL)."""
        members = list(PermissionMode)
        idx = members.index(self)
        return members[(idx + 1) % len(members)]


# ---------------------------------------------------------------------------
# RiskTier classifier
# ---------------------------------------------------------------------------

# Tool IDs that are always classified as EXEC regardless of operation
_EXEC_TOOL_IDS: frozenset[str] = frozenset({"bash", "shell", "terminal", "subprocess"})

# Tool ID patterns (fnmatch) that mark a shell/exec tool — matched in addition to
# the exact set so vendored ids like ``aider_shell_tool`` / ``run_shell_command``
# / ``execute_bash`` are correctly tiered EXEC (not WRITE) by their name.
_EXEC_TOOL_PATTERNS: tuple[str, ...] = (
    "*bash*",
    "*shell*",
    "*terminal*",
    "*subprocess*",
    "*execute*",
)

# Tool ID patterns (fnmatch) for WRITE-class tools
_WRITE_TOOL_PATTERNS: tuple[str, ...] = (
    "edit*",
    "write*",
    "patch*",
    "create_file*",
    "delete*",
    "move*",
    "rename*",
)


class RiskTier(enum.Enum):
    """Risk tier for a tool call, used to drive modal badge colour and default focus.

    - ``READ``   — read-only tool with no write-pattern name; muted/success colour.
    - ``WRITE``  — ``operation == "set"`` OR tool name matches ``_WRITE_TOOL_PATTERNS``
      (e.g. ``delete_file``, ``edit_*``); warning colour.
    - ``EXEC``   — ``_EXEC_TOOL_IDS`` (bash/shell) OR ``operation == "execute"``;
      error/destructive colour.  Takes precedence over WRITE.
    """

    READ = "read"
    WRITE = "write"
    EXEC = "exec"

    @staticmethod
    def classify(step: ActionStep) -> RiskTier:
        """Return the risk tier for ``step``.

        EXEC takes precedence: ``_EXEC_TOOL_IDS`` (bash/shell) or
        ``operation == "execute"`` → EXEC regardless of tool name.

        WRITE: ``operation == "set"`` OR tool name matches any
        ``_WRITE_TOOL_PATTERNS`` glob (e.g. ``delete_file`` with ``get``
        operation is still WRITE, not READ, because destructive-named tools
        must never be mis-tiered as safe).

        READ: everything else.

        Args:
            step: The tool call to classify.

        Returns:
            The :class:`RiskTier` for ``step``.
        """
        tool = step.tool_id.lower()
        op = step.operation.lower()

        # EXEC: bash/shell always EXEC regardless of operation — match the exact
        # set OR a shell/exec name pattern (so e.g. ``aider_shell_tool`` tiers EXEC).
        if tool in _EXEC_TOOL_IDS or any(fnmatch(tool, p) for p in _EXEC_TOOL_PATTERNS):
            return RiskTier.EXEC

        if op == "execute":
            return RiskTier.EXEC

        # WRITE: explicit set operation OR destructive-named tool pattern
        if op == "set":
            return RiskTier.WRITE

        if any(fnmatch(tool, pat) for pat in _WRITE_TOOL_PATTERNS):
            return RiskTier.WRITE

        return RiskTier.READ


# ---------------------------------------------------------------------------
# PermissionRuleStore — persisted glob rules
# ---------------------------------------------------------------------------

_DEFAULT_HOME = Path.home() / ".mewbo"
_MEWBO_HOME_ENV = "MEWBO_HOME"


def _rules_path(path: Path | None = None) -> Path:
    """Resolve the permissions file path, honouring ``$MEWBO_HOME``."""
    if path is not None:
        return path
    home = os.environ.get(_MEWBO_HOME_ENV)
    base = Path(home) if home else _DEFAULT_HOME
    return base / "permissions.json"


class PermissionRuleStore:
    """Persisted glob rules for ``allow`` and ``deny`` decisions.

    Atomic class: one JSON file under ``~/.mewbo/`` (or ``$MEWBO_HOME``).
    A missing/corrupt file degrades to empty rules — never raises.

    The JSON schema is::

        {
            "rules": [
                {"tool": "bash", "action": "*", "decision": "allow"},
                {"tool": "dangerous_tool", "action": "*", "decision": "deny"},
            ]
        }

    ``tool`` and ``action`` are :func:`fnmatch` glob patterns.  ``decision``
    is ``"allow"`` or ``"deny"``.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Bind the rules file path and load existing rules.

        Args:
            path: Explicit path override (useful in tests with a tmp dir).
                  Defaults to ``~/.mewbo/permissions.json`` (or ``$MEWBO_HOME``).
        """
        self._path: Path = path or _rules_path()
        self._rules: list[dict[str, str]] = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> list[dict[str, str]]:
        """Load rules from disk; return empty list on any error."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("rules", [])
            return [r for r in raw if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            _logger.warning("Failed to load permission rules from %s", self._path)
            return []

    def _save(self) -> None:
        """Persist rules to disk; logs a warning on failure (never raises)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"rules": self._rules}, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            _logger.warning("Failed to save permission rules to %s", self._path)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add_allow_glob(self, tool: str, action: str) -> None:
        """Append a persistent allow rule for ``tool``/``action`` glob."""
        self._rules.append({"tool": tool, "action": action, "decision": "allow"})
        self._save()

    def add_deny_glob(self, tool: str, action: str) -> None:
        """Append a persistent deny rule for ``tool``/``action`` glob."""
        self._rules.append({"tool": tool, "action": action, "decision": "deny"})
        self._save()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list_rules(self) -> list[dict[str, str]]:
        """Return all loaded rules (a shallow copy)."""
        return list(self._rules)

    def _matches(self, step: ActionStep, decision: str) -> bool:
        tool = step.tool_id
        op = step.operation
        for rule in self._rules:
            if rule.get("decision") != decision:
                continue
            rtool = rule.get("tool", "*")
            raction = rule.get("action", "*")
            if fnmatch(tool, rtool) and fnmatch(op, raction):
                return True
        return False

    def matches_allow(self, step: ActionStep) -> bool:
        """Return True if a persisted *allow* rule matches ``step``."""
        return self._matches(step, "allow")

    def matches_deny(self, step: ActionStep) -> bool:
        """Return True if a persisted *deny* rule matches ``step``."""
        return self._matches(step, "deny")


# ---------------------------------------------------------------------------
# PermissionService — the decision chain
# ---------------------------------------------------------------------------

# Modal outcome strings (returned by the real modal and the fake test resolver)
_OUTCOME_ALLOW_ONCE = "allow_once"
_OUTCOME_ALLOW_SESSION = "allow_session"
_OUTCOME_DENY = "deny"

ModalResolver = Callable[[ActionStep], str]
"""Callable that presents the approval modal and returns the outcome string.

Real implementation (worker-thread path)::

    def _resolve(step: ActionStep) -> str:
        modal = PermissionModal(step, palette=palette)
        return app.call_from_thread(app.push_screen_wait, modal)

Test/unit implementation::

    modal_resolver = lambda step: "allow_once"
"""


class PermissionService:
    """Layered tool-approval decision chain for the Mewbo TUI.

    Atomic class: all state via attrs + injected DI.  Thread-agnostic —
    the ``modal_resolver`` owns the threading boundary.

    Decision chain (first match wins):
      1. ``skip_predicate(step) → True`` → approve (auto-approve / mode bypass).
      2. Mode-based auto-approve (AUTO_ACCEPT_EDITS → approve READ/WRITE tiers).
      3. ``rule_store.matches_deny(step)`` → deny.
      4. ``rule_store.matches_allow(step)`` → approve.
      5. Session-grant cache hit → approve.
      6. ``modal_resolver(step)`` → maps outcome to approve/deny/session-grant.
      7. Any unhandled exception → deny (safe default).
    """

    def __init__(
        self,
        *,
        rule_store: PermissionRuleStore,
        session_id: Callable[[], str],
        mode_getter: Callable[[], PermissionMode],
        modal_resolver: ModalResolver | None = None,
        skip_predicate: Callable[[ActionStep], bool] | None = None,
    ) -> None:
        """Construct the service with injected dependencies.

        Args:
            rule_store:      Persisted glob rules (allow/deny).
            session_id:      Callable that returns the current session ID.
            mode_getter:     Callable that returns the current :class:`PermissionMode`.
            modal_resolver:  Callable to invoke the approval modal; defaults to
                             always-deny (safe) if ``None``.
            skip_predicate:  Optional extra bypass: if it returns ``True`` the
                             step is auto-approved without hitting the chain.
        """
        self._rule_store = rule_store
        self._session_id = session_id
        self._mode_getter = mode_getter
        self._modal_resolver: ModalResolver = modal_resolver or (lambda _: _OUTCOME_DENY)
        self._skip_predicate = skip_predicate
        # Session grants: frozenset of (session_id, tool_id, operation, path)
        self._session_grants: set[tuple[str, str, str, str]] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide(self, step: ActionStep) -> bool:
        """Evaluate the decision chain for ``step``.

        Returns:
            ``True`` to approve; ``False`` to deny.  Never raises.
        """
        try:
            return self._decide_inner(step)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "PermissionService.decide raised unexpectedly; defaulting to deny",
                exc_info=True,
            )
            return False

    def grant_session(self, step: ActionStep) -> None:
        """Grant approval for ``step`` for the rest of the current session."""
        self._session_grants.add(self._grant_key(step))

    def allow_always(self, step: ActionStep) -> None:
        """Persist a glob allow rule for ``step``'s tool/operation."""
        self._rule_store.add_allow_glob(step.tool_id, step.operation)

    # ------------------------------------------------------------------
    # Internal chain
    # ------------------------------------------------------------------

    def _decide_inner(self, step: ActionStep) -> bool:
        # 1. Skip / bypass predicate
        if self._skip_predicate is not None and self._skip_predicate(step):
            return True

        # 2. Mode-based auto-approve
        mode = self._mode_getter()
        if mode == PermissionMode.AUTO_ACCEPT_EDITS:
            tier = RiskTier.classify(step)
            if tier in (RiskTier.WRITE, RiskTier.READ):
                return True

        # 3. Deny rules (before allow so explicit deny wins)
        if self._rule_store.matches_deny(step):
            return False

        # 4. Allow rules
        if self._rule_store.matches_allow(step):
            return True

        # 5. Session-grant cache
        if self._grant_key(step) in self._session_grants:
            return True

        # 6. Modal
        outcome = self._modal_resolver(step)
        return self._map_outcome(step, outcome)

    def _grant_key(self, step: ActionStep) -> tuple[str, str, str, str]:
        """Return the session-grant cache key for ``step``."""
        path = ""
        ti = step.tool_input
        if isinstance(ti, dict):
            path = str(ti.get("file_path") or ti.get("path") or "")
        return (self._session_id(), step.tool_id, step.operation, path)

    def _map_outcome(self, step: ActionStep, outcome: str | None) -> bool:
        """Map a modal outcome string to a boolean approval decision."""
        if outcome == _OUTCOME_ALLOW_ONCE:
            return True
        if outcome == _OUTCOME_ALLOW_SESSION:
            self.grant_session(step)
            return True
        # "deny", "esc", "", None → deny
        return False


# ---------------------------------------------------------------------------
# Builder (called by cli_master controller)
# ---------------------------------------------------------------------------


def install_permission_service(
    gateway: PermissionGateway,
    *,
    rule_store: PermissionRuleStore,
    session_id: Callable[[], str],
    mode_getter: Callable[[], PermissionMode],
    modal_resolver: ModalResolver | None = None,
    auto_skip: Callable[[ActionStep], bool] | None = None,
) -> PermissionService:
    """Build a :class:`PermissionService` and wire it into ``gateway``.

    This is the entry point for the controller (``cli_master._run_app``) — it
    constructs the service, calls ``gateway.set_decision(service.decide)``, and
    returns the service for further configuration.

    Args:
        gateway:        The :class:`~mewbo_cli.tui.seams.PermissionGateway`
                        already bound to the App.
        rule_store:     Persisted glob rules store.
        session_id:     Callable returning the current session ID.
        mode_getter:    Callable returning the current :class:`PermissionMode`.
        modal_resolver: Callable that presents the approval modal (real or fake).
        auto_skip:      Extra bypass predicate (e.g. auto-approve flag).

    Returns:
        The constructed :class:`PermissionService` (already wired into ``gateway``).
    """
    service = PermissionService(
        rule_store=rule_store,
        session_id=session_id,
        mode_getter=mode_getter,
        modal_resolver=modal_resolver,
        skip_predicate=auto_skip,
    )
    gateway.set_decision(service.decide)
    return service


__all__ = [
    "PermissionMode",
    "PermissionRuleStore",
    "PermissionService",
    "RiskTier",
    "install_permission_service",
]
