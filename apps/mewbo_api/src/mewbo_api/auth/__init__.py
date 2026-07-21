"""API-side identity & access management — the AuthKit and its public seams.

The ``AuthKit`` resolves every REST request to a ``mewbo_iam.Principal`` (api-key,
bearer JWT, session cookie, or a trusted reverse proxy's identity headers) and
backs the key-auth guards; ``current_principal()`` reads the resolved identity off
``flask.g`` for a route/guard. The OIDC relying-party login legs live in the auth
routes, driven by the shared ``OidcRuntime``; LDAP username/password login runs
through ``LdapLoginService``. Both provision through the ONE ``FederatedRuntime``,
which owns the identity flow, the user store and the session cookie. Import the
kernel models straight from ``mewbo_iam``; this package is only the app-side
wiring.
"""

from mewbo_api.auth.federated import FederatedRuntime, build_federated_runtime
from mewbo_api.auth.kit import AuthKit, Guard, current_principal
from mewbo_api.auth.ldap_login import LdapLoginService, build_ldap_login
from mewbo_api.auth.permission_guard import (
    CoverageReport,
    PermissionGuard,
    PermissionRequirement,
    RouteBinding,
    RouteCoverage,
)
from mewbo_api.auth.routes import AuthRoutesController, init_auth_routes

__all__ = [
    "AuthKit",
    "Guard",
    "current_principal",
    "CoverageReport",
    "PermissionGuard",
    "PermissionRequirement",
    "RouteBinding",
    "RouteCoverage",
    "AuthRoutesController",
    "init_auth_routes",
    "FederatedRuntime",
    "build_federated_runtime",
    "LdapLoginService",
    "build_ldap_login",
]
