#!/usr/bin/env python3
"""``PermissionGuard`` — declare a route's access requirement AT its definition.

The inline idiom this replaces threads authentication and authorization through
the first two statements of every handler::

    auth_error = _require_api_key() or _require_permission("sessions.read")()
    if auth_error:
        return auth_error

That form has three properties this class exists to remove:

1. **It is forgettable.** Nothing forces the guard to be *invoked*. A stored but
   uncalled guard callable reads as protection while enforcing nothing, and a
   reader cannot tell the difference at a glance.
2. **It is invisible.** "What does this route require?" and "which routes are
   unguarded?" are answerable only by reading ~90 handler bodies.
3. **It is duplicated.** One composition repeated at every call site.

A decorator fixes all three at once: the requirement moves ABOVE the handler
where it is part of the signature, it is recorded in a queryable registry, and
:meth:`PermissionGuard.audit` can partition the live URL map into guarded,
deliberately-public, and UNBOUND routes.

**The load-bearing property is decoration-time validation.** Permission ids are
checked against :class:`~mewbo_iam.PermissionCatalog` when the decorator runs —
which, for a module-scope route, is import time, which in this app is boot. A
typo'd ``"session.read"`` raises a ``ValueError`` naming the id and the handler
before the server accepts traffic, instead of surfacing as a runtime 403 on a
route nobody exercised.

Design: one atomic class holding the registry as state, with the ``AuthKit``
injected as its single collaborator. The guard COMPOSES the kit's existing
``require_api_key`` / ``require_master_token`` / ``require_permission`` — it
never re-derives a check or a wire body, so 401/403 responses stay byte-for-byte
what the inline call sites returned. The mode combination (``all`` vs ``any``) is
the only decision this module owns, and it lives on
:class:`PermissionRequirement` as a pure method that takes the guard callables as
arguments and imports no Flask.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, TypeVar, cast, final

from mewbo_core.common import get_logger
from mewbo_iam import PermissionCatalog
from pydantic import BaseModel, ConfigDict, model_validator

from mewbo_api.auth.kit import AuthKit, Guard

logging = get_logger(name="mewbo-api.auth")

F = TypeVar("F", bound=Callable[..., Any])

# How multiple permissions on one route combine. ``all`` is least privilege and
# the default; ``any`` exists for a route that legitimately serves two tiers
# (e.g. an owner reading their own session OR an operator reading every session).
RequirementMode = Literal["all", "any"]

# Which credential tier a route sits behind. ``public`` is not the absence of a
# binding — it is an explicit, reasoned declaration, which is what makes an
# unbound route distinguishable from a deliberately open one. ``dual`` is the
# same kind of declaration for a route that accepts EITHER of two unrelated
# credentials (see :meth:`PermissionGuard.dual_channel`).
RouteTier = Literal["permission", "master", "public", "dual"]

# The credential a ``dual`` route's FIRST tier needs, orthogonal to the fact that
# a second tier exists. Kept a separate field rather than multiplying the tier
# Literal into ``dual_permission``/``dual_master``: a third primary would then
# double the enum instead of adding one member here.
PrimaryCredential = Literal["permission", "master"]

# The second credential a ``dual`` route accepts. Each member names a REAL,
# separately-implemented channel, so the set is closed by what the app actually
# ships rather than open-ended prose:
#
# * ``app_token`` — a short-lived HMAC token scoped to one ``app_id``, presented
#   by a SERVED app's frontend, which carries no principal and therefore no
#   permission at all (``apps/tokens.py``).
# * ``self_service`` — an authenticated principal acting on its OWN keys, where
#   authority comes from ownership of the target rather than from an admin role.
AlternateChannel = Literal["app_token", "self_service"]

# Endpoints that belong to the framework rather than the product surface. They
# carry no principal and are excluded from the coverage report so a boot-time
# warning lists only routes an operator can act on. Matched on the endpoint's
# LAST dotted segment, because a blueprint namespaces its own — flask-restx
# serves the Swagger UI assets as ``restx_doc.static``, which an exact-name
# comparison would report as an unguarded route forever.
_INFRASTRUCTURE_ENDPOINTS: frozenset[str] = frozenset({"static", "doc", "root", "specs"})

# Werkzeug adds these to every rule; they are never separately authored handlers.
_IMPLICIT_METHODS: frozenset[str] = frozenset({"HEAD", "OPTIONS"})

# Attribute stamped on a decorated handler so ``audit`` can recover its binding
# from the live view function without depending on registration order.
_BINDING_ATTR = "__mewbo_route_binding__"


class PermissionRequirement(BaseModel):
    """A route's permission requirement — the pure, Flask-free decision core.

    Validated at construction: every id must be a known catalog id and the set
    must be non-empty. Constructing one inside the decorator is what turns a
    typo into a boot failure.

    :meth:`evaluate` takes the guard callables as ARGUMENTS rather than reaching
    for a kit — the house rule that a model never imports its own I/O. It is
    therefore testable with plain zero-arg lambdas, with no request context, no
    ``AuthKit``, and no Flask import anywhere in its path.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    permissions: tuple[str, ...]
    mode: RequirementMode = "all"

    @model_validator(mode="after")
    def _validate_catalog_ids(self) -> PermissionRequirement:
        """Reject an empty requirement or any id outside the closed catalog."""
        if not self.permissions:
            raise ValueError("at least one permission id is required")
        unknown = [perm for perm in self.permissions if not PermissionCatalog.is_valid(perm)]
        if unknown:
            known = ", ".join(sorted(PermissionCatalog.ALL))
            raise ValueError(f"unknown permission id(s) {unknown!r}. Valid ids are: {known}")
        return self

    def evaluate(self, guards: Sequence[Guard]) -> tuple[dict, int] | None:
        """Combine *guards* under this requirement's mode. ``None`` means allow.

        Guards are the kit's own per-permission guards, so the returned body and
        status are whatever the kit produced — this method chooses BETWEEN
        outcomes and never authors one.

        The first failure is the one returned, in both modes. That ordering is
        deliberate: an unauthenticated caller trips the kit's 401 on the first
        guard, so they never receive a 403 about a role they were never asked to
        present. ``all`` short-circuits on that first failure; ``any`` must run
        every guard before it can conclude denial, which also means a denied
        ``any`` route records one ``access_denied`` audit event per unmet
        permission — each of which is a true statement about the request.
        """
        first_failure: tuple[dict, int] | None = None
        for guard in guards:
            result = guard()
            if result is None:
                if self.mode == "any":
                    return None
                continue
            if self.mode == "all":
                return result
            if first_failure is None:
                first_failure = result
        return first_failure


class AlternateCredential(BaseModel):
    """The SECOND credential a ``dual`` route accepts, and where it is checked.

    A dual route's fall-through is real domain logic — an app token resolves an
    ``app_id`` from the URL and compares it to the signed blob; a self-service
    key mint forces the owner to the caller's own subject and subset-checks the
    requested roles. None of that belongs in a decorator, so this model records
    WHAT the channel is and WHERE it lives, and the decorator delegates to it.

    ``enforced_by`` is the load-bearing field: without it "there is a second
    channel" is unfalsifiable prose. With it, an auditor reading the registry
    has the symbol to go read. It is required and validated non-empty, for the
    same reason :meth:`PermissionGuard.public` requires a reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: AlternateChannel
    permissions: tuple[str, ...] = ()
    enforced_by: str

    @model_validator(mode="after")
    def _validate_channel_shape(self) -> AlternateCredential:
        """Require an enforcement site and reject ids outside the catalog."""
        if not self.enforced_by.strip():
            raise ValueError("an alternate credential requires a non-empty enforced_by symbol")
        unknown = [perm for perm in self.permissions if not PermissionCatalog.is_valid(perm)]
        if unknown:
            known = ", ".join(sorted(PermissionCatalog.ALL))
            raise ValueError(f"unknown permission id(s) {unknown!r}. Valid ids are: {known}")
        return self


class RouteBinding(BaseModel):
    """What one handler declared about its own access requirement.

    Served over HTTP by the IAM admin surface, so it is a wire contract with
    ``extra="forbid"`` rather than internal bookkeeping. The tier invariants are
    enforced here, at definition, so a malformed binding cannot be recorded:
    a permission binding carries ids and no reason, a master binding may carry
    ids (the key routes compose ``master + keys.admin``), a public binding
    carries no ids and a REQUIRED reason, and a dual binding carries the primary
    ids, the primary credential, and the alternate channel.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    handler: str  # ``module.QualName`` of the decorated function
    permissions: tuple[str, ...] = ()
    mode: RequirementMode = "all"
    tier: RouteTier
    reason: str | None = None
    primary: PrimaryCredential | None = None
    alternate: AlternateCredential | None = None

    @model_validator(mode="after")
    def _validate_tier_shape(self) -> RouteBinding:
        """Enforce the per-tier field invariants."""
        if self.tier == "permission" and not self.permissions:
            raise ValueError("a permission binding requires at least one permission id")
        if self.tier == "public":
            if self.permissions:
                raise ValueError("a public binding cannot carry permission ids")
            if not (self.reason or "").strip():
                raise ValueError("a public binding requires a non-empty reason")
        elif self.reason is not None:
            raise ValueError("only a public binding carries a reason")
        if self.tier == "dual":
            if not self.permissions:
                raise ValueError("a dual binding requires at least one primary permission id")
            if self.alternate is None:
                raise ValueError("a dual binding requires an alternate credential")
            if self.primary is None:
                raise ValueError("a dual binding requires a primary credential")
        elif self.alternate is not None or self.primary is not None:
            raise ValueError("only a dual binding carries a primary/alternate credential")
        return self


class RouteCoverage(BaseModel):
    """One ``(rule, method)`` pair of the live URL map and what guards it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: str
    method: str
    handler: str
    binding: RouteBinding | None = None


class CoverageReport(BaseModel):
    """The partition of the live URL map into guarded, public, and unbound.

    ``unbound`` is the finding this report exists for: a route that declares
    nothing. It may still be guarded inline (most are, mid-migration), so the
    report is a worklist, not a vulnerability list.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bound: tuple[RouteCoverage, ...] = ()
    public: tuple[RouteCoverage, ...] = ()
    unbound: tuple[RouteCoverage, ...] = ()

    @property
    def total(self) -> int:
        """How many ``(rule, method)`` pairs were examined."""
        return len(self.bound) + len(self.public) + len(self.unbound)

    def summary(self) -> str:
        """One-line census, for the boot log."""
        return (
            f"{self.total} routes: {len(self.bound)} bound, "
            f"{len(self.public)} explicitly public, {len(self.unbound)} unbound"
        )


@final
class PermissionGuard:
    """Declares, records, and enforces per-route access requirements.

    Construct once at app boot with the one :class:`AuthKit`; every decorator it
    hands out closes over that kit, so a route never reaches for a global. State
    is the binding registry keyed by qualified handler name.
    """

    def __init__(self, *, kit: AuthKit) -> None:
        """Capture the kit and start with an empty registry."""
        self._kit = kit
        self._bindings: dict[str, RouteBinding] = {}

    # ── decorators ──────────────────────────────────────────────────────────
    def requires(self, *permissions: str, mode: RequirementMode = "all") -> Callable[[F], F]:
        """Require a valid API key plus *permissions* on the resolved principal.

        Validates the ids against the catalog AT DECORATION — a typo raises here,
        at import, not at a caller's request. At request time it runs the kit's
        key guard first and its permission guards second, returning the kit's own
        bodies unchanged, so the response is identical to the inline composition
        this replaces.
        """
        requirement = self._build_requirement(permissions, mode)
        # Built once per route, not per request; each kit guard reads the
        # in-flight principal when called, so hoisting them is safe.
        guards = tuple(self._kit.require_permission(perm) for perm in requirement.permissions)

        def decorator(func: F) -> F:
            binding = self._record(
                func,
                permissions=requirement.permissions,
                mode=requirement.mode,
                tier="permission",
            )

            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                auth_error = self._kit.require_api_key()
                if auth_error is not None:
                    return auth_error
                denial = requirement.evaluate(guards)
                if denial is not None:
                    return denial
                return func(*args, **kwargs)

            return cast("F", self._stamp(wrapper, binding))

        return decorator

    def requires_master(self, *permissions: str) -> Callable[[F], F]:
        """Require the master token — the break-glass tier (key minting).

        An issued key is rejected by the kit even when it carries every
        permission, which is the point: a leaked key must not mint more keys.
        Optional *permissions* compose on top, matching the existing key routes
        (master token AND ``keys.admin``).
        """
        requirement = self._build_requirement(permissions, "all") if permissions else None
        guards = (
            tuple(self._kit.require_permission(perm) for perm in requirement.permissions)
            if requirement is not None
            else ()
        )

        def decorator(func: F) -> F:
            binding = self._record(
                func,
                permissions=requirement.permissions if requirement else (),
                mode="all",
                tier="master",
            )

            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                auth_error = self._kit.require_master_token()
                if auth_error is not None:
                    return auth_error
                if requirement is not None:
                    denial = requirement.evaluate(guards)
                    if denial is not None:
                        return denial
                return func(*args, **kwargs)

            return cast("F", self._stamp(wrapper, binding))

        return decorator

    def dual_channel(
        self,
        *permissions: str,
        channel: AlternateChannel,
        enforced_by: str,
        primary: PrimaryCredential = "permission",
        alternate_permissions: Sequence[str] = (),
    ) -> Callable[[F], F]:
        """Declare a route that accepts *permissions* OR a second credential.

        This is the ONE shape :meth:`requires` cannot express, and the reason is
        not ergonomics — it is that the two tiers disagree about what a
        permission FAILURE means. Under :meth:`requires`, a key that
        authenticates but lacks the permission is refused. On these routes it
        must instead FALL THROUGH to the alternate channel, exactly as an
        anonymous caller does, because that channel is a separate credential
        with its own authority. A decorator that returned 403 there would break
        every served app and delete the self-service key tier.

        So this decorator adds NO request-time behavior — like :meth:`public`,
        it only records the claim, and the handler's own first statement remains
        the enforcement (``authorize_read``/``authorize_write`` for an app
        token; the admin-then-self-mint ladder for a key). That is the honest
        division: the guard owns DECLARATION, and the second channel's logic —
        which reads the URL's ``app_id``, or forces a mint's owner to the
        caller's subject — is domain logic that would drift the moment it was
        copied into a decorator. Byte-identical wire behavior is a consequence
        of adding no behavior at all, not something re-verified per route.

        What it DOES buy is the audit's whole purpose: these routes are
        authenticated, so marking them ``public`` would be a false statement,
        and leaving them unbound makes "deliberate" and "forgotten"
        indistinguishable. A ``dual`` binding says which permission the first
        tier wants, which credential the second tier is, and — via
        *enforced_by* — the symbol where the fall-through actually lives.

        Ids on BOTH tiers are validated against the catalog at decoration, so a
        typo in either fails at boot exactly as it does under :meth:`requires`.
        """
        requirement = self._build_requirement(permissions, "all")
        alternate = AlternateCredential(
            channel=channel,
            permissions=tuple(alternate_permissions),
            enforced_by=enforced_by,
        )

        def decorator(func: F) -> F:
            binding = self._record(
                func,
                permissions=requirement.permissions,
                mode=requirement.mode,
                tier="dual",
                primary=primary,
                alternate=alternate,
            )
            return cast("F", self._stamp(func, binding))

        return decorator

    def public(self, reason: str) -> Callable[[F], F]:
        """Declare a route deliberately unauthenticated, with a *reason*.

        This matters as much as :meth:`requires`. Three surfaces are open by
        design — share links, the capability-URL trigger webhook, and HMAC
        channel webhooks — and each carries its own non-principal proof (an
        unguessable token, a signature). Without an explicit marker, "open by
        design" and "someone forgot" are indistinguishable to an audit, so this
        decorator adds no request-time behavior at all: it only records the
        claim. The reason is required and validated non-empty at decoration.
        """
        text = reason.strip()
        if not text:
            raise ValueError("public() requires a non-empty reason")

        def decorator(func: F) -> F:
            binding = self._record(func, permissions=(), mode="all", tier="public", reason=text)
            return cast("F", self._stamp(func, binding))

        return decorator

    # ── introspection ───────────────────────────────────────────────────────
    @property
    def bindings(self) -> Mapping[str, RouteBinding]:
        """Every recorded binding, keyed by ``module.QualName``."""
        return dict(self._bindings)

    def audit(
        self,
        app: Any,
        *,
        strict: bool = False,
        allow_unbound: Mapping[str, str] | None = None,
    ) -> CoverageReport:
        """Partition *app*'s live URL map into bound, public, and unbound routes.

        Takes the Flask app rather than a bare ``url_map`` because a
        ``Rule`` carries only an endpoint string: resolving a rule to the
        function that serves it needs ``app.view_functions``, and resolving a
        flask-restx ``Resource`` to its per-verb methods needs the class behind
        that view. The walk itself reads nothing but those two maps.

        Default is a loud structured WARNING listing every unbound rule. It
        deliberately does not raise: mid-migration most routes are still guarded
        inline, so raising by default would refuse to boot an app that is in fact
        protected. Pass ``strict=True`` once the sweep completes to make an
        unbound route a boot failure.

        ``allow_unbound`` maps a handler's ``module.QualName`` to the REASON it
        may stay unbound under ``strict``, for the case where decorating a route
        would itself be the defect. It exists because the alternative is worse:
        a caller who cannot express one deliberate exception either abandons
        strict mode entirely, or edits a route purely to satisfy the audit. An
        exempted route still appears in ``unbound`` — the report keeps telling
        the truth about what is bound — it simply does not fail the check, and
        its reason is logged beside it so the exemption is as reviewable as a
        :meth:`public` declaration.
        """
        report = self._walk(app)
        exemptions = dict(allow_unbound or {})
        if report.unbound:
            # One rule per line: the list runs to the hundreds mid-migration, and
            # a single joined line at that size is not readable in a boot log.
            listing = "\n".join(
                f"  {item.method} {item.rule}"
                + (f"  [exempt: {exemptions[item.handler]}]" if item.handler in exemptions else "")
                for item in report.unbound
            )
            if strict:
                unexcused = [item for item in report.unbound if item.handler not in exemptions]
                if unexcused:
                    detail = "\n".join(f"  {item.method} {item.rule}" for item in unexcused)
                    raise RuntimeError(
                        f"route permission coverage is incomplete — "
                        f"{len(unexcused)} unbound routes:\n{detail}"
                    )
            logging.warning(
                "route permission coverage: {}\nunbound routes:\n{}", report.summary(), listing
            )
        else:
            logging.info("route permission coverage: {}", report.summary())
        return report

    # ── internals ───────────────────────────────────────────────────────────
    def _build_requirement(
        self, permissions: tuple[str, ...], mode: RequirementMode
    ) -> PermissionRequirement:
        """Validate ids + mode at decoration, re-raising with catalog context."""
        return PermissionRequirement(permissions=permissions, mode=mode)

    def _record(
        self,
        func: Callable[..., Any],
        *,
        permissions: tuple[str, ...],
        mode: RequirementMode,
        tier: RouteTier,
        reason: str | None = None,
        primary: PrimaryCredential | None = None,
        alternate: AlternateCredential | None = None,
    ) -> RouteBinding:
        """Register a binding for *func*, refusing a conflicting redeclaration.

        A repeated identical registration is accepted so that re-importing a
        route module (which the test suite does) is idempotent; a DIFFERENT
        binding for the same handler is a definition error and raises, since two
        stacked requirements would silently enforce only one of them.
        """
        key = f"{func.__module__}.{func.__qualname__}"
        binding = RouteBinding(
            handler=key,
            permissions=permissions,
            mode=mode,
            tier=tier,
            reason=reason,
            primary=primary,
            alternate=alternate,
        )
        existing = self._bindings.get(key)
        if existing is not None and existing != binding:
            raise ValueError(
                f"{key} already declares {existing.tier} binding "
                f"{existing.permissions!r}; a handler carries exactly one requirement"
            )
        self._bindings[key] = binding
        return binding

    @staticmethod
    def _stamp(func: Callable[..., Any], binding: RouteBinding) -> Callable[..., Any]:
        """Attach *binding* to the object the route will actually register."""
        setattr(func, _BINDING_ATTR, binding)
        return func

    def _walk(self, app: Any) -> CoverageReport:
        """Resolve every rule/method pair to its handler and its binding, if any."""
        bound: list[RouteCoverage] = []
        public: list[RouteCoverage] = []
        unbound: list[RouteCoverage] = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint.rsplit(".", 1)[-1] in _INFRASTRUCTURE_ENDPOINTS:
                continue
            view = app.view_functions.get(rule.endpoint)
            if view is None:
                continue
            for method in sorted((rule.methods or set()) - _IMPLICIT_METHODS):
                handler = self._handler_for(view, method)
                if handler is None:
                    continue
                binding = getattr(handler, _BINDING_ATTR, None)
                coverage = RouteCoverage(
                    rule=str(rule.rule),
                    method=method,
                    handler=f"{handler.__module__}.{handler.__qualname__}",
                    binding=binding,
                )
                if binding is None:
                    unbound.append(coverage)
                elif binding.tier == "public":
                    public.append(coverage)
                else:
                    bound.append(coverage)
        return CoverageReport(bound=tuple(bound), public=tuple(public), unbound=tuple(unbound))

    @staticmethod
    def _handler_for(view: Any, method: str) -> Callable[..., Any] | None:
        """The function serving *method* — a Resource verb, or the view itself.

        flask-restx (like any ``MethodView``) registers ONE dispatching view per
        Resource and hangs the real handlers off ``view_class`` as verb methods,
        so a per-verb binding is only reachable through that class.
        """
        view_class = getattr(view, "view_class", None)
        if view_class is None:
            return view if callable(view) else None
        return getattr(view_class, method.lower(), None)


__all__ = [
    "AlternateChannel",
    "AlternateCredential",
    "CoverageReport",
    "PermissionGuard",
    "PermissionRequirement",
    "PrimaryCredential",
    "RequirementMode",
    "RouteBinding",
    "RouteCoverage",
    "RouteTier",
]
