#!/usr/bin/env python3
"""Provider drivers — the network + crypto legs behind the optional extras.

The identity kernel's authenticator models are pure: verified claims or an
already-fetched directory entry arrive as a ``Mapping`` and map to a
``RawIdentity`` with no I/O. THIS subpackage is the other half — the drivers that
actually fetch and verify.

The ``[oidc]`` extra (authlib + joserfc + requests):

* :class:`~mewbo_iam.drivers.discovery.DiscoveryCache` — the OIDC discovery
  document (RFC 8414 metadata), fetched once and cached with a TTL.
* :class:`~mewbo_iam.drivers.jwks.JwksVerifier` — JWKS fetch/cache and the pure
  signature + claims verification (joserfc).
* :class:`~mewbo_iam.drivers.oidc_client.OidcClient` — the authorization-code +
  PKCE relying-party flow (authlib): authorize-URL build, code exchange, and the
  optional userinfo fetch.
* :class:`~mewbo_iam.drivers.introspection.IntrospectionClient` — RFC 7662 token
  introspection for opaque access tokens, short-TTL cached.

The ``[ldap]`` extra (ldap3):

* :class:`~mewbo_iam.drivers.ldap.LdapBinder` — the service bind, user search and
  the rebind-as-the-user that IS the password check, plus group resolution.

The ``[saml]`` extra (python3-saml):

* :class:`~mewbo_iam.drivers.saml.SamlSp` — SP metadata, the AuthnRequest
  redirect, and ACS validation (signature, audience, destination, expiry,
  replay).

**Import guards are PER-EXTRA and LAZY, and that is load-bearing.** Each name
below is resolved on first attribute access (PEP 562 ``__getattr__``), which runs
that name's own dependency probe and raises a clean, actionable ``ImportError``
naming the missing extra. Importing this package eagerly instead — the shape this
module started with — meant a deployment that configured ONLY LDAP still had to
install the OIDC extra to reach ``LdapBinder``, because the package body imported
every driver up front. A bare ``import mewbo_iam`` still never reaches here at
all (nothing in the kernel imports up into a driver), so the models and stores
stay dependency-light either way.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only; the runtime path is lazy
    from mewbo_iam.drivers.discovery import DiscoveryCache, OidcDiscovery
    from mewbo_iam.drivers.introspection import IntrospectionClient, IntrospectionResult
    from mewbo_iam.drivers.jwks import (
        GroupsOverageError,
        JwksVerificationError,
        JwksVerifier,
    )
    from mewbo_iam.drivers.ldap import ConnectionFactory, LdapBinder, LdapBindError
    from mewbo_iam.drivers.oidc_client import OidcClient, OidcExchangeError, TokenBundle
    from mewbo_iam.drivers.saml import (
        SamlLoginStart,
        SamlReplayError,
        SamlSp,
        SamlValidationError,
    )

# extra → the third-party modules an install of that extra must provide.
_EXTRA_REQUIRES: dict[str, tuple[str, ...]] = {
    "oidc": ("authlib", "joserfc", "requests"),
    "saml": ("onelogin.saml2",),
    "ldap": ("ldap3",),
}

# Exported name → (submodule it lives in, the extra that supplies its deps).
# This table is what the lazy guard below resolves against, so a name absent
# here is unreachable no matter what ``__all__`` advertises — keep the two in
# step when adding a driver export.
_EXPORTS: dict[str, tuple[str, str]] = {
    "DiscoveryCache": ("discovery", "oidc"),
    "OidcDiscovery": ("discovery", "oidc"),
    "JwksVerifier": ("jwks", "oidc"),
    "JwksVerificationError": ("jwks", "oidc"),
    "GroupsOverageError": ("jwks", "oidc"),
    "OidcClient": ("oidc_client", "oidc"),
    "OidcExchangeError": ("oidc_client", "oidc"),
    "TokenBundle": ("oidc_client", "oidc"),
    "IntrospectionClient": ("introspection", "oidc"),
    "IntrospectionResult": ("introspection", "oidc"),
    "SamlSp": ("saml", "saml"),
    "SamlLoginStart": ("saml", "saml"),
    "SamlValidationError": ("saml", "saml"),
    "SamlReplayError": ("saml", "saml"),
    "LdapBinder": ("ldap", "ldap"),
    "LdapBindError": ("ldap", "ldap"),
    "ConnectionFactory": ("ldap", "ldap"),
}


def __getattr__(name: str) -> Any:
    """Resolve a driver export, probing only ITS extra's dependencies.

    Raises ``ImportError`` naming the missing extra and modules — the actionable
    message an operator needs — rather than letting a bare third-party
    ``ModuleNotFoundError`` surface from deep inside a driver.
    """
    entry = _EXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    submodule, extra = entry
    missing = []
    for module in _EXTRA_REQUIRES[extra]:
        try:
            __import__(module)
        except ImportError:  # pragma: no cover - exercised only without the extra
            missing.append(module)
    if missing:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            f"{name} requires the '{extra}' extra "
            f"(missing: {', '.join(missing)}). Install with "
            f"`pip install mewbo-iam[{extra}]`."
        )
    return getattr(importlib.import_module(f"{__name__}.{submodule}"), name)


def __dir__() -> list[str]:
    """Expose the lazy names to ``dir()`` and interactive completion."""
    return sorted(_EXPORTS)


# Mirrors the export table above; kept literal so a static checker can resolve
# each name to its TYPE_CHECKING import.
__all__ = [
    "ConnectionFactory",
    "DiscoveryCache",
    "GroupsOverageError",
    "IntrospectionClient",
    "IntrospectionResult",
    "JwksVerificationError",
    "JwksVerifier",
    "LdapBindError",
    "LdapBinder",
    "OidcClient",
    "OidcDiscovery",
    "OidcExchangeError",
    "SamlLoginStart",
    "SamlReplayError",
    "SamlSp",
    "SamlValidationError",
    "TokenBundle",
]
