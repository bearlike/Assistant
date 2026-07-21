#!/usr/bin/env python3
"""A viewer's role ceiling must actually reach the engine and deny delegation.

``SessionScopeResolver._compose_allowed_tools`` disables delegation for a
viewer-class principal by OMITTING ``spawn_agent``/``spawn_agents`` from the
composed allowlist — the omission IS the lever, and strict scope is what makes
the allowlist authoritative over built-ins.

That lever is only as good as the consumer reading it. The composed allowlist
can compose down to EMPTY (the baseline-tool provider is optional and yields an
empty frozenset when unwired), and the loop's spawn gate tested it with
truthiness — so ``not []`` was True, the gate concluded "no allowlist declared",
and re-injected ``spawn_agent`` for exactly the principal the read-only ceiling
exists to deny.

These tests drive the REAL resolver into the REAL ``ToolUseLoop`` rather than
asserting on the composed list alone, because the list being right is not the
property that matters — the engine honouring it is.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mewbo_api.auth.session_scope import SessionScopeResolver
from mewbo_core.tool_use_loop import ToolUseLoop
from mewbo_iam import (
    AuthMethod,
    AuthSettings,
    PermissionCatalog,
    Principal,
    RoleRecord,
)

from test_tool_use_loop import (  # isort: skip
    _allow_all_policy,
    _make_agent_context,
    _make_hook_manager,
    _make_registry,
    _make_spec,
)

VIEWER = RoleRecord(
    name="viewer",
    permissions=frozenset({PermissionCatalog.SESSIONS_READ}),
)
MEMBER = RoleRecord(
    name="member",
    permissions=frozenset(
        {PermissionCatalog.SESSIONS_READ, PermissionCatalog.SESSIONS_INTERACT}
    ),
)
ROLES = {role.name: role for role in (VIEWER, MEMBER)}


def _principal(role: str) -> Principal:
    return Principal(
        subject=f"user:{role}",
        kind="user",
        roles=(role,),
        auth_method=AuthMethod(kind="password"),
    )


def _resolver(*, baseline: frozenset[str] | None = None) -> SessionScopeResolver:
    """The real resolver, driven by fixed roles and no I/O.

    *baseline* ``None`` reproduces an UNWIRED baseline-tool provider — the
    documented "a unit test with no registry" case, and equally any embedder
    that constructs the resolver without one. That is the configuration in which
    the composed allowlist reaches empty.
    """
    return SessionScopeResolver(
        settings=AuthSettings(enabled=True),
        role_records=lambda: ROLES,
        baseline_tool_ids=None if baseline is None else (lambda: baseline),
    )


def _loop_for(scope) -> ToolUseLoop:
    """Build a real ``ToolUseLoop`` under *scope*, stubbing only the LLM client."""
    with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = MagicMock()
        return ToolUseLoop(
            agent_context=_make_agent_context(),  # depth 0 → can_spawn True
            tool_registry=_make_registry(_make_spec("read_file", "Read a file")),
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            allowed_tools=scope.allowed_tools,
            strict_tool_scope=scope.strict_tool_scope,
        )


class TestViewerCeilingReachesTheEngine:
    def test_an_unwired_baseline_composes_a_viewer_down_to_an_empty_allowlist(self):
        """The precondition the fail-open depended on — pinned so it stays real."""
        scope = _resolver().resolve(_principal("viewer"))

        assert scope.allowed_tools == []
        assert scope.strict_tool_scope is True
        assert scope.capability_mode == "read_only"

    def test_a_viewer_with_an_empty_allowlist_is_denied_delegation(self):
        """THE property: an empty ceiling denies spawn, it does not grant it."""
        scope = _resolver().resolve(_principal("viewer"))

        loop = _loop_for(scope)

        assert loop._spawn_agent_tool is None, (
            "an empty composed allowlist must deny delegation — reading it as "
            "unrestricted hands spawn_agent back to a read-only principal"
        )

    @pytest.mark.parametrize(
        "baseline",
        [
            pytest.param(None, id="unwired-baseline"),
            pytest.param(frozenset({"read_file", "shell"}), id="live-registry-baseline"),
        ],
    )
    def test_a_viewer_is_denied_delegation_however_the_baseline_is_wired(self, baseline):
        """Latent-in-production is not fixed-in-production.

        With a live registry the baseline is non-empty, so the composed
        allowlist is too and the truthiness bug never fired — which is why this
        shipped undetected. The ceiling must hold in BOTH configurations.
        """
        scope = _resolver(baseline=baseline).resolve(_principal("viewer"))

        assert "spawn_agent" not in (scope.allowed_tools or [])
        assert _loop_for(scope)._spawn_agent_tool is None

    def test_a_member_keeps_delegation(self):
        """The ceiling must not over-deny: an interacting role still spawns."""
        scope = _resolver(baseline=frozenset({"read_file"})).resolve(_principal("member"))

        assert "spawn_agent" in (scope.allowed_tools or [])
        assert _loop_for(scope)._spawn_agent_tool is not None
