#!/usr/bin/env python3
"""The ONE process-wide :class:`PermissionGuard`, importable before it is wired.

:class:`~mewbo_api.auth.permission_guard.PermissionGuard` decorates at IMPORT
time, which in this app is boot. That creates an ordering problem the guard
itself cannot solve: a decorator needs a guard instance the moment a route
module is imported, but the only :class:`~mewbo_api.auth.kit.AuthKit` is built
part-way down ``backend.py``. A route module that imported ``backend`` at module
scope to reach it would re-open the ``backend`` ↔ routes import cycle that
``wiki/routes.py`` dodges with a lazy function-level import — and a lazy import
cannot serve a decorator, because the decorator has already run by the time any
function body executes.

This module breaks the ordering without breaking the layering. It is a LEAF: it
imports the kit and the guard and nothing else from the app, so any route module
can import it at module scope with zero cycle risk. The guard it exposes is
usable as a decorator immediately, before any kit exists.

**The mechanism is late binding, and the split is the whole design:**

* **Declaration is eager.** ``@guard.requires("users.admin")`` runs at import:
  the permission ids are validated against the closed catalog and the binding is
  recorded, so a typo is still a BOOT failure naming the handler — the
  load-bearing property of the guard, preserved exactly.
* **Enforcement is lazy.** The guard callables close over :class:`LateBoundKit`,
  which resolves the live kit at REQUEST time. Nothing reads a kit at decoration,
  so decoration before binding is well-defined rather than merely tolerated.

``backend.py`` binds the live kit once at boot (``guard_registry.bind(_auth_kit)``),
immediately after constructing it. That call is the composition root, mirroring
the ``_controller`` handle in ``triggers/routes.py``: production never reads the
module-level instance as mutable state, and a test can point the one guard at a
fresh kit by re-binding.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast, final

from mewbo_api.auth.kit import AuthKit, Guard
from mewbo_api.auth.permission_guard import PermissionGuard


@final
class LateBoundKit:
    """An ``AuthKit`` stand-in that resolves the real kit at REQUEST time.

    Implements exactly the three methods :class:`PermissionGuard` calls —
    ``require_api_key`` and ``require_master_token`` (invoked inside the request
    wrapper) and ``require_permission`` (invoked at DECORATION, but returning a
    guard that defers). Every one of them returns the live kit's own body and
    status unchanged; this class chooses nothing and authors no wire shape.

    ``require_permission`` is the subtle one: the guard hoists its per-permission
    guards at decoration time, so this must hand back a callable WITHOUT touching
    a kit. It returns a closure that resolves the kit on each call instead —
    which is also why re-binding takes effect immediately for already-decorated
    routes, with no re-import.
    """

    def __init__(self) -> None:
        """Start unbound. Every method raises until :meth:`bind` is called."""
        self._kit: AuthKit | None = None

    @property
    def bound(self) -> bool:
        """Whether a live kit has been bound yet."""
        return self._kit is not None

    def bind(self, kit: AuthKit) -> None:
        """Bind the live kit. Idempotent-by-overwrite: the last bind wins.

        Re-binding is deliberately allowed rather than refused. In production this
        fires exactly once (``backend.py`` builds at import, and a module is
        imported once per process), so the only caller that ever re-binds is a
        test pointing the one guard at a fresh kit — the same reason
        ``triggers/routes.py`` keeps its ``_controller`` handle reassignable.
        """
        self._kit = kit

    def resolve(self) -> AuthKit:
        """The bound kit, or raise — an unbound guard must never fail OPEN.

        Reaching this unbound means a route decorated with a requirement is
        serving traffic in an app whose composition root never ran. Both
        alternatives are worse than raising: passing would silently unguard the
        route, and synthesizing a 401 would disguise a boot-wiring defect as a
        routine auth failure, in the one place that makes it unfindable. So this
        raises loudly, and it cannot happen in the composed app — ``backend.py``
        binds immediately after building the kit, before any request is served.

        A test that mounts a decorated blueprint on a bare Flask app must bind a
        kit first (a disabled-auth kit passes everything, matching production's
        default deployment).
        """
        if self._kit is None:
            raise RuntimeError(
                "the permission guard is enforcing a route but no AuthKit is bound. "
                "Call guard_registry.bind(kit) at app composition (backend.py does "
                "this at boot); a test mounting a guarded route must bind one too."
            )
        return self._kit

    # ── the AuthKit guard surface PermissionGuard consumes ──────────────────
    def require_api_key(self) -> tuple[dict, int] | None:
        """Delegate to the live kit — same bodies and statuses, unchanged."""
        return self.resolve().require_api_key()

    def require_master_token(self) -> tuple[dict, int] | None:
        """Delegate to the live kit — same bodies and statuses, unchanged."""
        return self.resolve().require_master_token()

    def require_permission(self, permission: str) -> Guard:
        """A guard for *permission* that resolves the kit when CALLED, not now.

        Called at decoration time, so it must not touch a kit. The returned
        closure re-derives the kit's own guard per request, which costs one
        attribute read and keeps a re-bind visible to routes decorated long
        before it.
        """

        def guard() -> tuple[dict, int] | None:
            return self.resolve().require_permission(permission)()

        return guard


@final
class GuardRegistry:
    """The composition root for the one process-wide :class:`PermissionGuard`.

    State is the late-bound kit and the single guard built over it. The guard is
    constructed in ``__init__`` — before any kit exists — which is precisely what
    lets a route module import it and decorate at module scope.
    """

    def __init__(self) -> None:
        """Build the late-bound kit and the one guard standing on it."""
        self._late_kit = LateBoundKit()
        # ``PermissionGuard`` annotates ``kit`` as ``AuthKit`` because that is the
        # only implementation the app has. ``LateBoundKit`` satisfies the three
        # methods the guard actually calls and delegates each to the real kit, so
        # the cast states a structural truth a nominal annotation cannot. Widening
        # the guard's own annotation to a Protocol would be the alternative, and is
        # the right change to make if a second kit implementation ever appears.
        self._guard = PermissionGuard(kit=cast("AuthKit", self._late_kit))

    @property
    def guard(self) -> PermissionGuard:
        """The one guard every route module decorates with."""
        return self._guard

    @property
    def bound(self) -> bool:
        """Whether the live kit has been bound (false until boot wires it)."""
        return self._late_kit.bound

    def bind(self, kit: AuthKit) -> None:
        """Bind the live ``AuthKit``, activating enforcement for every binding.

        Call once at app composition. Bindings recorded before this — i.e. all of
        them, since decoration runs at import — start enforcing against *kit*
        from the next request, with no re-import and no re-decoration.
        """
        self._late_kit.bind(kit)

    @contextmanager
    def rebound(self, kit: AuthKit) -> Iterator[None]:
        """Bind *kit* for the duration of the block, then restore what was there.

        The registry is process-wide, so a caller that binds without restoring
        leaks its kit into everything that runs afterwards. Save-and-restore
        belongs here rather than at each call site: the previous kit lives on
        the inner ``LateBoundKit``, so every caller doing this by hand had to
        reach through two layers of private state to find it, and a caller that
        reached one layer short still *passed* whenever the registry happened to
        be unbound — the failure only surfaces once something else binds first.
        """
        previous = self._late_kit._kit
        self._late_kit.bind(kit)
        try:
            yield
        finally:
            self._late_kit._kit = previous


# The ONE registry. Route modules import ``guard`` and decorate with it; the
# composition root (``backend.py``) imports ``guard_registry`` and binds the kit.
# ``guard`` is a thin alias to a never-reassigned instance attribute — the
# documented factory-alias carve-out, not mutable module state.
guard_registry = GuardRegistry()
guard = guard_registry.guard


__all__ = ["GuardRegistry", "LateBoundKit", "guard", "guard_registry"]
