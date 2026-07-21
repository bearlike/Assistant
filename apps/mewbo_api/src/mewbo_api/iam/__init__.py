"""IAM administration routes — ``/api/iam/*``, gated on ``api.auth.enabled``.

``init_iam_routes(app, settings=...)`` builds the IAM stores and mounts the admin
surface. Safe to call unconditionally at boot, mirroring ``init_scim``: it returns
``None`` and touches NO store when auth is off, so a deployment that never enables
auth never writes an ``iam_users.json``/``iam_teams.json``/``iam_roles.json`` it
never asked for.

No auth guard is passed in: each handler declares its own requirement with
``@guard.requires`` at its definition, enforced through the one process-wide
``PermissionGuard``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from mewbo_core.common import get_logger
from mewbo_iam import AuthSettings

from .routes import IamRoutesController, register

logging = get_logger(name="api.iam")


def init_iam_routes(
    app: Any,
    *,
    settings: AuthSettings,
    deprovision: Callable[[str], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> IamRoutesController | None:
    """Build the IAM stores + controller and mount the admin routes on *app*.

    Returns ``None`` and constructs no store when auth is off
    (``settings.enabled`` is False) — the byte-zero-impact law. *deprovision* is
    forwarded verbatim to the controller; pass the SAME callback SCIM receives so
    an account disabled through the admin panel loses its credentials exactly as
    one disabled by an identity provider does.

    The role store is created through ``create_role_store``, which seeds the
    built-in roles — that seeding is what makes ``GET /api/iam/roles`` answer
    with the five built-ins on a deployment that has never defined a custom one.

    The acting admin recorded on audit events comes from the controller's default
    ``principal_reader`` (``current_principal``), which reads the principal the
    AuthKit already resolved onto ``g`` for this request — this surface never
    re-derives identity.
    """
    if not settings.enabled:
        logging.info("iam routes: disabled (api.auth.enabled is False); skipping")
        return None
    from mewbo_iam import (
        create_auth_audit_store,
        create_role_store,
        create_team_store,
        create_user_store,
    )

    controller = IamRoutesController(
        user_store=create_user_store(),
        team_store=create_team_store(),
        role_store=create_role_store(),
        audit_store=create_auth_audit_store() if settings.audit.enabled else None,
        settings=settings,
        deprovision=deprovision,
        clock=clock,
    )
    register(app, controller)
    logging.info("iam admin routes mounted at /api/iam/* (+ /api/auth/authenticators)")
    return controller


__all__ = ["init_iam_routes", "IamRoutesController"]
