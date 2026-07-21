"""SCIM 2.0 provisioning — ``/api/scim/v2/*``, gated on ``api.auth.scim.enabled``.

``init_scim(app, settings=..., deprovision=...)`` builds the IAM stores and
mounts the blueprint. Safe to call unconditionally at boot, mirroring
``init_wiki``: it returns ``None`` and touches NO store when SCIM is off, so a
deployment that never turns it on never writes an
``iam_users.json``/``iam_teams.json`` it never asked for.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from mewbo_core.common import get_logger
from mewbo_iam import AuthSettings

from .routes import ScimRoutesController, register

logging = get_logger(name="api.scim")


def init_scim(
    app: Any,
    *,
    settings: AuthSettings,
    deprovision: Callable[[str], None],
    clock: Callable[[], datetime] | None = None,
) -> ScimRoutesController | None:
    """Build the SCIM stores + controller and mount ``/api/scim/v2/*`` on *app*.

    Returns ``None`` and constructs no store when SCIM is off
    (``settings.enabled`` or ``settings.scim.enabled`` is False) — the
    byte-zero-impact law. *deprovision* is forwarded verbatim to the
    controller; see ``ScimRoutesController``'s docstring for its contract
    (revoke owned keys + terminate live sessions for the deprovisioned
    subject).
    """
    if not (settings.enabled and settings.scim.enabled):
        logging.info(
            "scim: disabled (api.auth.enabled or api.auth.scim.enabled is False); skipping"
        )
        return None
    from mewbo_iam import create_auth_audit_store, create_team_store, create_user_store

    controller = ScimRoutesController(
        user_store=create_user_store(),
        team_store=create_team_store(),
        audit_store=create_auth_audit_store() if settings.audit.enabled else None,
        settings=settings,
        deprovision=deprovision,
        clock=clock,
    )
    register(app, controller)
    logging.info("scim routes mounted at /api/scim/v2/*")
    return controller


__all__ = ["init_scim", "ScimRoutesController"]
