#!/usr/bin/env python3
"""``SessionScopeResolver`` — the one place a principal's role becomes a run scope.

RBAC does not stop at the route: the resolved :class:`~mewbo_iam.Principal` also
governs which tools the session binds and what its agents may do. This module is
the pure-decision seam between the identity kernel and the orchestration run —
given a principal plus the caller's requested tool/capability grants, it produces
a :class:`SessionScope` the backend threads into ``SessionRuntime.start_async`` /
``run_sync`` at every call site.

Design:

* **One atomic class, collaborators by DI.** The role store view and the parsed
  ``api.auth`` settings arrive as fields; the store is read LAZILY (mirroring
  ``AuthKit``) so a disabled deployment does zero IAM store I/O.
* **``SessionScope`` is a frozen dataclass, NOT Pydantic.** It carries a live
  ``PermissionPolicy`` object and a callable ``approval_callback`` and never
  crosses a trust boundary — the documented carve-out from the house Pydantic
  rule ("the Pydantic rule stops at the process boundary"), the same one
  ``RenderedInstructions`` takes.
* **Byte-identical when auth is off (or the caller is admin/legacy).** The
  disabled/admin branch reproduces today's run exactly: the client's grants pass
  through untouched, permissive scope, ``capability_mode="all"``,
  ``permission_policy=None`` (so the loop keeps its own default policy), and
  ``auto_approve``. This is the #1 rejection guard for the whole IAM effort.

Enforcement for a role-bounded (non-admin) caller pins ALL THREE tool channels
so they agree — binding, capability tier, and execution policy:

* ``strict_tool_scope=True`` is FORCED. Under permissive scope the
  ``allowed_tools`` channel is a no-op (the FE's ``mcp_tools`` ceiling never
  restricts built-ins, spawn, or unconditional session tools). Strict makes the
  allowlist authoritative, so a role ceiling reaches BINDING — the model never
  even sees a tool it may not use — not just execution.
* ``allowed_tools`` is composed COMPLETE for the role: the caller's requested
  MCP ceiling (never widened) ∪ the live built-in registry tool ids (from the
  DI'd ``baseline_tool_ids`` provider, so a new built-in flows in automatically —
  never a hardcoded list) ∪ the spawn family for a NON-viewer ∪ the
  session-tool families the role's permissions unlock. ``always_load`` specs
  (``tool_search``) are deliberately NOT listed — ``filter_specs`` exempts them
  from the allowlist gate, so listing them would be double-handling.
* ``capability_mode`` — the coarse read/write/execute tier. A viewer-class
  principal (no ``sessions.interact``) resolves to ``read_only`` so even the
  composed allowlist is filtered to read-tier; operators/members get ``all``.
* ``permission_policy`` — DENY rules for the session-executable tool FAMILIES a
  role lacks (belt to the composed-allowlist suspenders): execution-time refusal
  for anything that slips the binding gate. Route-only permissions
  (``users.admin``/``config.write``/…) are the per-route guards' job, never a
  session gate.

Disabled auth or an admin caller skips all of the above — a pure passthrough,
byte-identical to the pre-IAM run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from mewbo_core.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRule,
    auto_approve,
)
from mewbo_core.triggers import TriggerAuthority
from mewbo_iam import (
    AuthMethod,
    AuthSettings,
    PermissionCatalog,
    Principal,
    RoleRecord,
    RoleStoreBase,
    create_role_store,
)

# The auth method a trigger-authority-derived principal carries: no live
# authenticator re-proves identity at fire time, so the internal ``system``
# method stands in (the kernel reserves it for exactly this internal-caller case).
_SYSTEM_AUTH_METHOD = AuthMethod(kind="system")

# Session-executable tool ids paired with the catalog permission that gates them.
# A role LACKING the permission gets a DENY rule for the tool id (belt to the
# capability_mode suspenders). Deliberately SMALL and verified: only tools an
# agent can actually invoke inside a run map here — route-only verbs do not.
# ``schedule_trigger`` (mewbo_core.triggers.session_tool) and ``submit_app``
# (mewbo_api.apps.plugin.submit_app) are the two such tools today.
_SESSION_TOOL_PERMISSIONS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "schedule_trigger",
        frozenset({PermissionCatalog.TRIGGERS_ARM, PermissionCatalog.TRIGGERS_MANAGE}),
    ),
    ("submit_app", frozenset({PermissionCatalog.APPS_SUBMIT})),
)


@dataclass(frozen=True)
class SessionScope:
    """The server-authoritative run scope resolved for one session start.

    A frozen dataclass (not Pydantic): it holds a live ``PermissionPolicy`` and a
    callable, is computed server-side from an already-validated principal, and is
    handed straight to the runtime — it crosses no trust boundary.

    Fields map 1:1 onto the ``start_async``/``run_sync`` kwargs the backend
    passes: ``allowed_tools`` (the composed role-complete allowlist for a
    role-bounded caller; the untouched request for admin/disabled),
    ``strict_tool_scope`` (forced ``True`` under a role ceiling, else the caller's
    value), ``capability_mode`` (the coarse tier), ``permission_policy`` (``None``
    ⇒ the loop keeps its own default — the byte-identical admin path), and
    ``approval_callback``. ``client_capabilities`` is the (currently pass-through)
    intersected capability set the caller should persist into the session context.
    """

    allowed_tools: list[str] | None
    strict_tool_scope: bool
    capability_mode: str
    client_capabilities: list[str] | None
    permission_policy: PermissionPolicy | None
    approval_callback: Callable[..., bool]


class SessionScopeResolver:
    """Resolves a ``(principal, requested grants)`` pair into a :class:`SessionScope`.

    Construct once at app boot beside the ``AuthKit``. ``settings`` is the parsed
    ``api.auth`` block (the master ``enabled`` switch lives here); the role store
    is built lazily on first use, so a disabled deployment never touches IAM
    storage. A test injects a ``role_records`` provider to drive the decision core
    with fixed roles and no I/O.
    """

    def __init__(
        self,
        *,
        settings: AuthSettings,
        role_records: Callable[[], Mapping[str, RoleRecord]] | None = None,
        baseline_tool_ids: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        """Capture settings + lazy providers for role records and baseline tools.

        ``baseline_tool_ids`` returns the built-in (non-MCP) tool ids a
        role-bounded session should keep once strict scope is forced. It is a
        callable read from the LIVE registry (never a hardcoded list) so a newly
        registered built-in flows into every composed allowlist automatically.
        ``None`` (a unit test with no registry) yields an empty baseline.
        """
        self._settings = settings
        self._role_records_provider = role_records
        self._baseline_tool_ids_provider = baseline_tool_ids
        self._role_store: RoleStoreBase | None = None

    # -- role store (lazy, mirrors AuthKit) --------------------------------
    def _roles(self) -> RoleStoreBase:
        if self._role_store is None:
            self._role_store = create_role_store()
        return self._role_store

    def _role_records(self) -> Mapping[str, RoleRecord]:
        """All roles as a ``name -> record`` map for ``effective_permissions``."""
        if self._role_records_provider is not None:
            return self._role_records_provider()
        return {record.name: record for record in self._roles().list()}

    # -- resolution --------------------------------------------------------
    def resolve(
        self,
        principal: Principal | None,
        *,
        requested_allowed_tools: list[str] | None = None,
        requested_capabilities: Sequence[str] | None = None,
        requested_strict_tool_scope: bool = False,
    ) -> SessionScope:
        """Resolve the run scope for *principal* under the caller's requested grants.

        ``requested_strict_tool_scope`` is beyond the minimal signature but
        load-bearing for the byte-identical guarantee: the admin/disabled
        passthrough must reproduce the *exact* strict flag each call site would
        have used (permissive for the FE default, the persisted value for a
        re-engage), so it is carried through rather than reset.

        Disabled auth, an unauthenticated request (``None``), or an admin
        principal all take the passthrough branch: grants untouched, permissive
        scope preserved, ``capability_mode="all"``, no permission policy (the loop
        keeps its default), ``auto_approve`` — byte-identical to the pre-IAM run.
        """
        requested_caps = (
            list(requested_capabilities) if requested_capabilities is not None else None
        )
        if not self._settings.enabled or principal is None or principal.is_admin:
            return SessionScope(
                allowed_tools=requested_allowed_tools,
                strict_tool_scope=requested_strict_tool_scope,
                capability_mode="all",
                client_capabilities=requested_caps,
                permission_policy=None,
                approval_callback=auto_approve,
            )

        perms = principal.effective_permissions(self._role_records())
        # A viewer-class principal (cannot interact with a session) runs read-only
        # so the whole tool surface — registry + session tools — attenuates to
        # read tier. Operators/members keep "all"; a scoped service key whose own
        # scopes are narrower is attenuated separately, by the key's scope check.
        capability_mode = (
            "read_only" if PermissionCatalog.SESSIONS_INTERACT not in perms else "all"
        )
        return SessionScope(
            allowed_tools=self._compose_allowed_tools(
                perms, capability_mode, requested_allowed_tools
            ),
            # FORCED under a role ceiling: strict is the only scope where the
            # allowlist restricts built-ins/spawn/unconditional tools, so the
            # composed allowlist above becomes the authoritative BINDING gate.
            strict_tool_scope=True,
            capability_mode=capability_mode,
            client_capabilities=requested_caps,
            permission_policy=self._build_permission_policy(perms, capability_mode),
            approval_callback=auto_approve,
        )

    def _compose_allowed_tools(
        self,
        perms: frozenset[str],
        capability_mode: str,
        requested_allowed_tools: list[str] | None,
    ) -> list[str]:
        """Compose the role-complete allowlist that strict scope makes authoritative.

        Union of: the caller's requested MCP ceiling (server-authoritative — the
        client can RESTRICT but never widen it), the live built-in registry tool
        ids, the spawn family, and the session-tool families the role's
        permissions unlock. ``always_load`` specs are omitted on purpose —
        ``filter_specs`` exempts them from the allowlist gate, so naming them
        would be redundant double-handling.

        **Spawn membership is role-conditional.** Under strict scope, omitting
        ``spawn_agent``/``spawn_agents`` disables delegation at the
        ``spawn_in_scope`` seam (``tool_use_loop``). That is the deliberate lever
        for a viewer-class role: a read-only principal gets no delegation (its
        children would only inherit its own read-only ceiling anyway), while an
        operator/member keeps it. Keyed on ``capability_mode`` because
        ``read_only`` IS the viewer-class signal.
        """
        allowed: set[str] = set(requested_allowed_tools or ())
        allowed |= self._baseline_tool_ids()
        if capability_mode != "read_only":
            allowed |= {"spawn_agent", "spawn_agents"}
        for tool_id, required in _SESSION_TOOL_PERMISSIONS:
            if perms & required:
                allowed.add(tool_id)
        return sorted(allowed)

    def _baseline_tool_ids(self) -> frozenset[str]:
        """The built-in tool ids a role-bounded session keeps (empty if no provider)."""
        if self._baseline_tool_ids_provider is None:
            return frozenset()
        return self._baseline_tool_ids_provider()

    def _build_permission_policy(
        self, perms: frozenset[str], capability_mode: str
    ) -> PermissionPolicy:
        """Build the run's permission policy from the role's effective permissions.

        DENY rules for the session-executable tool families the role lacks
        (:data:`_SESSION_TOOL_PERMISSIONS`), layered over the same defaults the
        loop uses when handed no policy (``get`` allowed, ``set`` asks — which
        ``auto_approve`` resolves to allow). A read-only principal additionally
        denies ``set`` outright as defense-in-depth behind ``capability_mode``,
        so even a mis-classified write can never execute in a viewer's session.
        A role holding every mapped permission (member/operator) yields no DENY
        rules, so its policy matches today's default — the sensible baseline.
        """
        rules: list[PermissionRule] = [
            PermissionRule(tool_id=tool_id, operation="*", decision=PermissionDecision.DENY)
            for tool_id, required in _SESSION_TOOL_PERMISSIONS
            if not (perms & required)
        ]
        if capability_mode == "read_only":
            return PermissionPolicy(
                rules=rules,
                default_by_operation={
                    "get": PermissionDecision.ALLOW,
                    "set": PermissionDecision.DENY,
                },
                default_decision=PermissionDecision.DENY,
            )
        return PermissionPolicy(
            rules=rules,
            default_by_operation={
                "get": PermissionDecision.ALLOW,
                "set": PermissionDecision.ASK,
            },
            default_decision=PermissionDecision.ASK,
        )


def authority_from_principal(principal: Principal | None) -> TriggerAuthority | None:
    """Snapshot an arming caller's authority for a trigger record, or ``None``.

    Returns ``None`` for an unauthenticated (``None``) or admin/legacy caller —
    exactly the cases whose fired trigger should re-engage with today's ambient
    full power (a captured admin snapshot would resolve back to full power
    anyway). So a ``None`` result is the auth-disabled / admin path, and the
    record's optional ``authority`` field stays unset — byte-identical. Only a
    ROLE-BOUNDED caller yields a snapshot, freezing the exact roles + three-state
    scopes the fire path must rebuild.
    """
    if principal is None or principal.is_admin:
        return None
    return TriggerAuthority(
        principal_subject=principal.subject,
        roles=principal.roles,
        scopes=principal.scopes,
    )


def principal_from_authority(authority: TriggerAuthority) -> Principal:
    """Rebuild a :class:`~mewbo_iam.Principal` from a stored trigger authority.

    The inverse of :func:`authority_from_principal`, used at fire time to feed
    :meth:`SessionScopeResolver.resolve` the arming caller's identity. ``kind``
    is inferred from the subject prefix (``user:`` → user, else service), and the
    three-state ``scopes`` law is preserved verbatim (``None`` unrestricted vs
    ``()`` scopeless are never collapsed).
    """
    kind = "user" if authority.principal_subject.startswith("user:") else "service"
    return Principal(
        subject=authority.principal_subject,
        kind=kind,
        roles=authority.roles,
        scopes=authority.scopes,
        auth_method=_SYSTEM_AUTH_METHOD,
    )


__all__ = [
    "SessionScope",
    "SessionScopeResolver",
    "authority_from_principal",
    "principal_from_authority",
]
