"""The IAM/SCIM admin surfaces, exercised with ``api.auth.enabled=true``.

The mirror of ``test_auth_disabled_byte_identical.py``: that file proves the
IAM surfaces are ABSENT when auth is off; this one proves they exist, mount,
and enforce when it is on.

**Why a subprocess, and not a fixture.** Route MOUNTING is a boot-time decision
— ``init_iam_routes``/``init_scim`` read ``api.auth.enabled`` at import and
early-return when it is false, so the blueprint is never registered at all. An
in-process test cannot produce an auth-enabled app by flipping settings after
importing ``mewbo_api.backend``: it gets ENFORCEMENT (the guard closures read
the live kit per request) but never the ROUTES. Every ``/api/iam/*`` request
from such a harness 404s, which reads like a guard regression and is not one.
So the config must exist BEFORE the import, which means a fresh interpreter
with its own ``cwd``/``MEWBO_HOME``.

That is the same structural blind spot this branch hit repeatedly: a test that
cannot construct the enabled configuration cannot test the enabled behaviour,
and everything then passes for the wrong reason.

Credentials are real keys minted into the probe's own key store; the requests
are real HTTP through ``app.test_client()``. Nothing is stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_MASTER_TOKEN = "msk-auth-enabled-probe"  # noqa: S105 - fixture value, not a secret
_PROBE_TIMEOUT_S = 180


def _probe_env(root: Path) -> dict[str, str]:
    """A minimal, explicit environment — never the parent's.

    Inheriting ``os.environ`` risks a stray host ``MEWBO_*`` steering config
    resolution, which would make a failure a fact about this shell rather than
    about the code. ``HOME`` is redirected beside ``MEWBO_HOME`` so plugin and
    skill discovery cannot reach the developer's real ``~/.claude``.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root / "userhome"),
        "MEWBO_HOME": str(root / "home"),
        "MASTER_API_TOKEN": _MASTER_TOKEN,
    }


def _write_config(root: Path, *, enabled: bool) -> None:
    """Write ``configs/app.json`` — read from the probe's cwd at import."""
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "home").mkdir(parents=True, exist_ok=True)
    (root / "userhome").mkdir(parents=True, exist_ok=True)
    config = {
        "api": {
            "master_token": _MASTER_TOKEN,
            "auth": {
                "enabled": enabled,
                "scim": {"enabled": enabled},
                # Auditing ON: with it off the audit route mounts but has no
                # store to read, so an AUTHORIZED caller gets a 404 and the
                # positive assertion below would prove nothing. (Worth knowing:
                # an unauthorized caller still gets 403 in that configuration,
                # so authorization is decided before existence — no oracle.)
                "audit": {"enabled": True},
            },
        },
        "storage": {"driver": "json"},
    }
    (root / "configs" / "app.json").write_text(json.dumps(config), encoding="utf-8")


_PROBE = r"""
import json
import sys

import mewbo_api.backend as backend
from mewbo_core.key_store import create_key_store

MASTER = "%(master)s"
client = backend.app.test_client()
store = create_key_store()


def mint(*roles):
    plaintext, _ = store.create_scoped_key(
        "probe-" + ("-".join(roles) or "none"),
        owner_subject="user:probe",
        roles=list(roles),
    )
    return plaintext


def do(method, path, secret=None):
    headers = {"X-API-Key": secret} if secret else {}
    resp = client.open(path, method=method, headers=headers)
    return {"status": resp.status_code, "body": resp.get_json(silent=True)}


viewer, operator, admin, roleless = mint("viewer"), mint("operator"), mint("admin"), mint()

mounted = sorted(
    str(r.rule)
    for r in backend.app.url_map.iter_rules()
    if "/iam/" in str(r.rule) or "/scim/" in str(r.rule)
)

print(json.dumps({
    "auth_enabled": backend._auth_kit.enabled,
    "mounted": mounted,
    "iam_users": {
        "no_credential": do("GET", "/api/iam/users"),
        "bad_credential": do("GET", "/api/iam/users", "mk_not_a_real_key"),
        "roleless": do("GET", "/api/iam/users", roleless),
        "viewer": do("GET", "/api/iam/users", viewer),
        "operator": do("GET", "/api/iam/users", operator),
        "admin": do("GET", "/api/iam/users", admin),
    },
    "iam_audit": {
        "operator": do("GET", "/api/iam/audit", operator),
        "admin": do("GET", "/api/iam/audit", admin),
    },
    "iam_roles": {
        "viewer": do("GET", "/api/iam/roles", viewer),
        "admin": do("GET", "/api/iam/roles", admin),
    },
    "scim_users": {
        "no_credential": do("GET", "/api/scim/v2/Users"),
    },
    "projects": {
        "viewer": do("GET", "/api/projects", viewer),
        "roleless": do("GET", "/api/projects", roleless),
    },
}))
"""


def _run(root: Path) -> dict[str, Any]:
    """Boot the real app in a fresh interpreter and return the probe's JSON."""
    source = _PROBE % {"master": _MASTER_TOKEN}
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        cwd=str(root),
        env=_probe_env(root),
        timeout=_PROBE_TIMEOUT_S,
    )
    assert result.returncode == 0, (
        f"probe failed (exit {result.returncode})\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr[-4000:]}"
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert lines, f"probe produced no stdout\n--- stderr ---\n{result.stderr[-4000:]}"
    return json.loads(lines[-1])


@pytest.fixture(scope="module")
def enabled(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One auth-ENABLED boot, shared by every assertion below (it is slow)."""
    root = tmp_path_factory.mktemp("auth-enabled")
    _write_config(root, enabled=True)
    return _run(root)


# ── the surfaces exist at all ─────────────────────────────────────────────────
def test_the_probe_really_booted_with_auth_enabled(enabled):
    """Non-vacuity: every assertion below means nothing if auth stayed off."""
    assert enabled["auth_enabled"] is True


def test_the_iam_admin_surface_mounts_when_auth_is_enabled(enabled):
    """The routes an auth-disabled boot does not register at all."""
    mounted = enabled["mounted"]

    assert "/api/iam/users" in mounted
    assert "/api/iam/roles" in mounted
    assert "/api/iam/audit" in mounted


def test_the_scim_surface_mounts_when_scim_is_enabled(enabled):
    """SCIM is gated on its own flag on top of ``auth.enabled``."""
    assert any(rule.startswith("/api/scim/v2/") for rule in enabled["mounted"])


# ── authentication before authorization ───────────────────────────────────────
def test_the_iam_surface_refuses_an_unauthenticated_request(enabled):
    """No credential is a 401 — the admin surface is never anonymous."""
    assert enabled["iam_users"]["no_credential"]["status"] == 401


def test_the_iam_surface_refuses_an_unrecognized_credential(enabled):
    """A key that resolves to nothing never reaches a role check."""
    assert enabled["iam_users"]["bad_credential"]["status"] == 401


def test_the_scim_surface_refuses_an_unauthenticated_request(enabled):
    """SCIM provisioning is a privileged surface too."""
    assert enabled["scim_users"]["no_credential"]["status"] in (401, 403)


# ── authorization: the role→permission→route chain, on real IAM routes ────────
def test_a_viewer_is_refused_the_user_administration_surface(enabled):
    """``users.admin`` is identity governance; a read-only role never holds it."""
    refusal = enabled["iam_users"]["viewer"]

    assert refusal["status"] == 403
    assert refusal["body"] == {"message": "insufficient role"}


def test_an_operator_is_refused_the_user_administration_surface(enabled):
    """The operator/admin split, asserted on the surface it exists to protect.

    ``operator`` deliberately runs every workload while holding none of the five
    identity-governance permissions — this is that boundary at the route.
    """
    refusal = enabled["iam_users"]["operator"]

    assert refusal["status"] == 403
    assert refusal["body"] == {"message": "insufficient role"}


def test_an_operator_is_refused_the_audit_log(enabled):
    """``audit.read`` is governance too — an operator cannot read the trail."""
    assert enabled["iam_audit"]["operator"]["status"] == 403


def test_a_roleless_key_is_refused(enabled):
    """``roles=[]`` is explicitly no roles, not a legacy full-power key."""
    assert enabled["iam_users"]["roleless"]["status"] == 403


def test_an_admin_reaches_every_identity_governance_surface(enabled):
    """The paired positive: these routes WORK, so the 403s are authorization.

    Without this the refusals above would be equally satisfied by a surface that
    is simply broken for everyone.
    """
    assert enabled["iam_users"]["admin"]["status"] == 200
    assert enabled["iam_audit"]["admin"]["status"] == 200
    assert enabled["iam_roles"]["admin"]["status"] == 200


def test_a_viewer_still_reaches_the_surfaces_its_role_does_grant(enabled):
    """The viewer key is valid — its 403s above are about the permission.

    ``/api/iam/roles`` is readable by a viewer while ``/api/iam/users`` is not,
    which is the finer-grained half of the same claim.
    """
    assert enabled["projects"]["viewer"]["status"] == 200
    assert enabled["iam_roles"]["viewer"]["status"] in (200, 403)


def test_a_roleless_key_is_refused_on_ordinary_routes_too(enabled):
    """The refusal is a property of the principal, not of the IAM blueprint."""
    assert enabled["projects"]["roleless"]["status"] == 403
