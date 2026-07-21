#!/usr/bin/env python3
"""Self-service key minting may attenuate authority — never widen it.

``POST /api/keys`` has two tiers. The admin tier mints anything. The
self-service tier (``keys.mint_own``, carried by ``member`` and ``operator``)
mints a key for the CALLER, and every field it can influence is TRI-STATE on
the wire: omitted, explicitly empty, or explicitly set. Coalescing the first
two — ``payload.get("roles") or None`` — is what turned this route into a
privilege escalation: a ``member`` posting ``{"label": "x"}`` wrote a record
with NO ``roles`` field, which the key store faithfully persisted as absent and
``AuthKit._principal_from_record`` faithfully read back as the legacy
unrestricted default, ``("admin",)``.

**These assert on the RESOLVED PRINCIPAL, not on the 201 body.** The body was
always innocent — it echoes the record, and the record's tell was a MISSING
field. The escalation only becomes visible one layer later, when the minted
key is resolved back into a principal, which is exactly where a test that
checked the response body would have seen nothing wrong.

The negative controls matter as much as the positive ones: a fix that simply
refused every self-mint would satisfy the escalation assertions and break the
feature, so the explicit-subset cases that were always correct are asserted
too.
"""

# mypy: ignore-errors
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from flask import request as flask_request
from mewbo_api import backend
from mewbo_core.key_store import KeyStore
from mewbo_iam.settings import AuthSettings
from mewbo_iam.stores.roles import JsonRoleStore


class SelfMintHarness:
    """The live app with auth on, every store under ``tmp_path``.

    Mutates the ONE ``AuthKit`` the app built at import rather than binding a
    replacement: ``guard.requires`` hoists its permission guards at decoration
    time, so a rebind would swap the api-key check and leave the permission
    checks pointed at the old kit. Saves and restores the originals because
    this app is a process-wide singleton shared with the rest of the suite.
    """

    def __init__(self, tmp_path: Any) -> None:
        self.key_store = KeyStore(path=str(tmp_path / "api_keys.json"))
        self.role_store = JsonRoleStore(path=str(tmp_path / "iam_roles.json"))
        self.role_store.seed_builtins()
        self.client = backend.app.test_client()
        self._kit = backend._auth_kit
        self._saved = {
            "key_store": backend.key_store,
            "settings": self._kit._settings,
            "role_store": self._kit._role_store,
            "audit_store": self._kit._audit_store,
        }

    def enable(self) -> None:
        backend.key_store = self.key_store
        # Audit off: orthogonal here, and leaving it on has the kit create an
        # audit store under the real data root.
        self._kit._settings = AuthSettings(enabled=True, audit={"enabled": False})
        self._kit._role_store = self.role_store
        self._kit._audit_store = None

    def restore(self) -> None:
        backend.key_store = self._saved["key_store"]
        self._kit._settings = self._saved["settings"]
        self._kit._role_store = self._saved["role_store"]
        self._kit._audit_store = self._saved["audit_store"]

    def caller(self, *roles: str, **fields: Any) -> str:
        """Mint a real caller key directly in the store; return its secret."""
        plaintext, _ = self.key_store.create_scoped_key(
            f"caller-{'-'.join(roles) or 'none'}",
            owner_subject=fields.pop("owner_subject", "user:caller"),
            roles=list(roles),
            **fields,
        )
        return plaintext

    @property
    def admin(self) -> str:
        """The credential the ADMIN mint tier actually accepts.

        Not an ``admin``-role key: ``admin_error`` composes
        ``_require_master_token() or _require_permission("keys.admin")()``,
        and that ``or`` is an AND (the first FAILURE wins — the house
        authenticate-then-authorize idiom). A role-only admin fails the master
        check and short-circuits into the self-service tier, exactly as the
        route's own docs say ("Requires the **master** token"). Using a
        role-admin key here would silently test the self-mint path twice.
        """
        return backend.MASTER_API_TOKEN

    def mint(self, secret: str, body: dict) -> Any:
        """Self-mint through the real HTTP route as the holder of *secret*."""
        return self.client.post("/api/keys", headers={"X-API-KEY": secret}, json=body)

    def principal_for(self, secret: str) -> Any:
        """Resolve *secret* the way a real request would — the assertion seam.

        The escalation is invisible in the mint response and visible only
        here, so every authority assertion in this file goes through the same
        ``AuthKit.resolve`` a live request uses.
        """
        with backend.app.test_request_context("/api/sessions", headers={"X-API-KEY": secret}):
            return self._kit.resolve(flask_request)

    def record_for(self, key_id: str) -> Any:
        return next(r for r in self.key_store.list_keys() if r["id"] == key_id)


@pytest.fixture
def mints(tmp_path) -> Any:
    harness = SelfMintHarness(tmp_path)
    harness.enable()
    try:
        yield harness
    finally:
        harness.restore()


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


# ── the escalation itself ─────────────────────────────────────────────────────


def test_omitted_roles_inherit_the_caller_rather_than_widening_to_admin(mints):
    """The reported escalation: `member` posts a bare label and gets admin."""
    member = mints.caller("member")
    resp = mints.mint(member, {"label": "pwn"})
    assert resp.status_code == 201

    minted = resp.get_json()["key"]
    principal = mints.principal_for(minted)
    assert principal is not None
    assert principal.roles == ("member",)
    assert not principal.is_admin


def test_the_minted_key_is_refused_where_the_caller_is_refused(mints):
    """End to end: the escalation's payoff route stays 403 for the new key.

    ``GET /api/sessions`` requires ``sessions.read_all``, which ``member``
    does not hold — it is the route that flipped 403→200 once an omitted
    ``roles`` resolved to admin.
    """
    member = mints.caller("member")
    assert mints.client.get("/api/sessions", headers={"X-API-KEY": member}).status_code == 403

    minted = mints.mint(member, {"label": "pwn"}).get_json()["key"]
    assert mints.client.get("/api/sessions", headers={"X-API-KEY": minted}).status_code == 403


def test_operator_omitting_roles_does_not_gain_identity_governance(mints):
    """`operator` also carries `keys.mint_own`, so it took the same path."""
    operator = mints.caller("operator")
    minted = mints.mint(operator, {"label": "x"}).get_json()["key"]
    assert mints.principal_for(minted).roles == ("operator",)


def test_explicitly_empty_roles_persist_as_empty_not_as_absent(mints):
    """`[]` is a value — "grant no roles" — never the legacy absent default."""
    member = mints.caller("member")
    body = mints.mint(member, {"label": "none", "roles": []}).get_json()
    assert mints.record_for(body["id"])["roles"] == []
    assert mints.principal_for(body["key"]).roles == ()


# ── scopes: the same tri-state law, now that scopes gate routes ───────────────


# A scope-narrowed caller needs `keys.mint_own` IN ITS SCOPES to reach this
# route at all — `require_permission` now intersects role grants with
# `KeyScopes`. That is precisely why the omitted-scopes case stopped being
# latent: the minted key's scopes decide what it can call.
_MINT = "keys.mint_own"


def test_omitted_scopes_inherit_the_callers_own(mints):
    """Omission must not widen a scope-narrowed caller to unrestricted."""
    member = mints.caller("member", scopes=[_MINT, "sessions.read"])
    minted = mints.mint(member, {"label": "x"}).get_json()["key"]
    assert mints.principal_for(minted).scopes == (_MINT, "sessions.read")


def test_explicitly_empty_scopes_persist_as_empty_not_unrestricted(mints):
    """`[]` → `()` (explicitly none). Collapsing it to `None` is fail-open."""
    member = mints.caller("member", scopes=[_MINT])
    body = mints.mint(member, {"label": "x", "scopes": []}).get_json()
    assert mints.record_for(body["id"])["scopes"] == []
    assert mints.principal_for(body["key"]).scopes == ()


def test_a_scope_the_caller_lacks_is_refused(mints):
    """The subset check ran only `if requested_scopes:` — so it never ran here."""
    member = mints.caller("member", scopes=[_MINT, "sessions.read"])
    resp = mints.mint(member, {"label": "x", "scopes": ["config.write"]})
    assert resp.status_code == 400


def test_a_narrower_scope_than_the_callers_own_is_allowed(mints):
    """Negative control: attenuation is the point, so it must still work."""
    member = mints.caller("member", scopes=[_MINT, "wiki.read"])
    minted = mints.mint(member, {"label": "x", "scopes": ["wiki.read"]}).get_json()["key"]
    assert mints.principal_for(minted).scopes == ("wiki.read",)


def test_an_unrestricted_caller_may_still_narrow(mints):
    """A caller with no scope ceiling can hand out a narrowed key."""
    member = mints.caller("member")
    minted = mints.mint(member, {"label": "x", "scopes": ["wiki.read"]}).get_json()["key"]
    assert mints.principal_for(minted).scopes == ("wiki.read",)


# ── expiry: a self-minted key must not outlive its parent ─────────────────────


def test_omitted_expiry_inherits_the_callers_own(mints):
    """A key expiring in five minutes minted one that never expired."""
    expiry = _iso(timedelta(minutes=5))
    member = mints.caller("member", expires_at=expiry)
    body = mints.mint(member, {"label": "x"}).get_json()
    assert mints.record_for(body["id"])["expires_at"] == expiry


def test_expiry_beyond_the_callers_own_is_refused(mints):
    member = mints.caller("member", expires_at=_iso(timedelta(minutes=5)))
    resp = mints.mint(member, {"label": "x", "expires_at": _iso(timedelta(days=365))})
    assert resp.status_code == 400


def test_expiry_within_the_callers_own_is_kept_verbatim(mints):
    """Negative control: attenuating the expiry downward still works."""
    member = mints.caller("member", expires_at=_iso(timedelta(hours=2)))
    sooner = _iso(timedelta(minutes=1))
    body = mints.mint(member, {"label": "x", "expires_at": sooner}).get_json()
    assert mints.record_for(body["id"])["expires_at"] == sooner


def test_an_unparseable_expiry_is_refused(mints):
    member = mints.caller("member")
    assert mints.mint(member, {"label": "x", "expires_at": "whenever"}).status_code == 400


def test_an_unexpiring_caller_may_still_set_an_expiry(mints):
    member = mints.caller("member")
    soon = _iso(timedelta(minutes=5))
    body = mints.mint(member, {"label": "x", "expires_at": soon}).get_json()
    assert mints.record_for(body["id"])["expires_at"] == soon


# ── negative controls that already passed — the fix must not disable them ─────


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param(["admin"], id="bare-admin"),
        pytest.param(["member", "admin"], id="own-role-plus-admin"),
        pytest.param("admin", id="bare-string-not-a-list"),
        pytest.param(["ADMIN"], id="case-variant"),
        pytest.param(["operator"], id="sibling-role"),
    ],
)
def test_roles_the_caller_does_not_hold_are_refused(mints, roles):
    member = mints.caller("member")
    resp = mints.mint(member, {"label": "x", "roles": roles})
    assert resp.status_code == 400
    assert mints.key_store.list_keys_for_owner("user:caller") == [
        r for r in mints.key_store.list_keys() if r["label"] == "caller-member"
    ]


def test_an_explicit_subset_of_the_callers_roles_is_allowed(mints):
    """Negative control: the feature itself — explicit attenuation works."""
    caller = mints.caller("member", "operator")
    minted = mints.mint(caller, {"label": "x", "roles": ["member"]}).get_json()["key"]
    assert mints.principal_for(minted).roles == ("member",)


def test_self_mint_forces_the_owner_to_the_caller(mints):
    """A self-mint naming another owner is silently re-pointed at the caller."""
    member = mints.caller("member")
    body = mints.mint(member, {"label": "x", "owner_subject": "user:victim"}).get_json()
    assert mints.record_for(body["id"])["owner_subject"] == "user:caller"
    assert mints.principal_for(body["key"]).subject == "user:caller"


def test_a_caller_without_mint_own_is_refused(mints):
    """`viewer` holds no `keys.mint_own`, so it never reaches the self tier."""
    viewer = mints.caller("viewer")
    assert mints.mint(viewer, {"label": "x"}).status_code in (401, 403)


# ── admin tier: owner_subject is a WRITE of a subject, so it obeys the law ────


def test_admin_mint_refuses_a_malformed_owner_subject(mints):
    """`{"owner_subject": "alice"}` used to mint a key that raised at auth time."""
    resp = mints.mint(mints.admin, {"label": "x", "owner_subject": "alice"})
    assert resp.status_code == 400
    assert "user:<id>" in resp.get_json()["message"]
    assert not [r for r in mints.key_store.list_keys() if r["label"] == "x"]


@pytest.mark.parametrize("subject", ["", "user:", "svc:  ", "team:eng"])
def test_admin_mint_refuses_every_malformed_subject_shape(mints, subject):
    assert mints.mint(mints.admin, {"label": "x", "owner_subject": subject}).status_code == 400


def test_admin_mint_accepts_a_well_formed_owner_subject(mints):
    """Negative control: naming any valid owner stays an admin prerogative."""
    body = mints.mint(mints.admin, {"label": "x", "owner_subject": "user:someone"}).get_json()
    assert mints.record_for(body["id"])["owner_subject"] == "user:someone"
    assert mints.principal_for(body["key"]).subject == "user:someone"


def test_admin_mint_may_still_omit_the_owner_entirely(mints):
    """An ownerless key is legal — `is None`, not truthiness, decides."""
    body = mints.mint(mints.admin, {"label": "x"}).get_json()
    assert "owner_subject" not in mints.record_for(body["id"])


def test_admin_mint_may_still_grant_roles_it_does_not_hold(mints):
    """The admin branch is untouched: an admin mints unrestricted authority."""
    body = mints.mint(mints.admin, {"label": "x", "roles": ["operator"]}).get_json()
    assert mints.principal_for(body["key"]).roles == ("operator",)


def test_admin_mint_may_still_leave_roles_absent(mints):
    """The legacy unrestricted key is an ADMIN prerogative and must survive.

    This is the one case the self-service tier can no longer produce, and the
    line between the fix and a regression: an admin omitting `roles` still
    mints a record with no `roles` field, which resolves to the legacy
    full-power default.
    """
    body = mints.mint(mints.admin, {"label": "legacy"}).get_json()
    assert "roles" not in mints.record_for(body["id"])
    assert mints.principal_for(body["key"]).roles == ("admin",)
