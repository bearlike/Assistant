#!/usr/bin/env python3
"""RBAC end to end: do roles actually REACH the routes they are supposed to gate?

Everything else in the identity suite is unit-level — it proves the rules are
correct, not that they are consulted. These drive the real Flask app with real
HTTP requests carrying real minted API keys, with ``api.auth`` genuinely enabled.

**How auth is turned on.** ``backend`` builds ONE ``AuthKit`` at import and
``guard_registry.bind`` captures it before any route decorates. The guard
closures read ``self._settings`` and ``self._role_records()` off that live
instance at REQUEST time, so enabling auth means mutating the one kit rather
than constructing a second one — binding a replacement would only half-work
(``requires`` hoists its permission guards at decoration time, so a rebind swaps
the api-key check but leaves the permission checks pointing at the old kit).
Every store is redirected to ``tmp_path``; nothing here touches the real
``~/.mewbo``.

Only the LLM/session-execution boundary is stubbed. The auth path — credential
read, key resolution, principal construction, role resolution, permission
evaluation — is the real one throughout.
"""

from __future__ import annotations

from typing import Any

import pytest
from mewbo_api import backend
from mewbo_core.key_store import KeyStore
from mewbo_iam import PermissionCatalog
from mewbo_iam.roles import RoleRecord
from mewbo_iam.settings import AuthSettings
from mewbo_iam.stores.roles import JsonRoleStore


def _builtin_roles() -> tuple[RoleRecord, ...]:
    from mewbo_iam.roles import BUILTIN_ROLES

    return BUILTIN_ROLES


def _permissions_of(role_name: str) -> frozenset[str]:
    """The catalog permissions a built-in role grants, read from the kernel."""
    for record in _builtin_roles():
        if record.name == role_name:
            return frozenset(record.permissions)
    raise AssertionError(f"no built-in role named {role_name!r}")


def _identity_governance() -> frozenset[str]:
    """Exactly what ``admin`` holds and ``operator`` does not."""
    return _permissions_of("admin") - _permissions_of("operator")


class AuthHarness:
    """The live app with auth switched on and every store under ``tmp_path``.

    Holds the saved originals so the module-level ``backend`` state is restored
    exactly — this app is a process-wide singleton shared with every other test
    in the suite, so leaking an enabled kit would break them all.
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
        """Turn auth on for the one live kit and point it at the temp stores."""
        backend.key_store = self.key_store
        # Audit off: it is orthogonal to what these tests assert, and leaving it
        # on would have the kit create an audit store under the real data root.
        self._kit._settings = AuthSettings(enabled=True, audit={"enabled": False})
        self._kit._role_store = self.role_store
        self._kit._audit_store = None

    def restore(self) -> None:
        backend.key_store = self._saved["key_store"]
        self._kit._settings = self._saved["settings"]
        self._kit._role_store = self._saved["role_store"]
        self._kit._audit_store = self._saved["audit_store"]

    # ── credentials ─────────────────────────────────────────────────────────
    def key_for(self, *roles: str, scopes: list[str] | None = None, **extra: Any) -> str:
        """Mint a real key carrying *roles*, and return its plaintext secret."""
        plaintext, _ = self.key_store.create_scoped_key(
            f"test-{'-'.join(roles) or 'none'}",
            owner_subject=extra.pop("owner_subject", "user:test"),
            roles=list(roles),
            scopes=scopes,
            **extra,
        )
        return plaintext

    def get(self, path: str, secret: str) -> Any:
        return self.client.get(path, headers={"X-API-KEY": secret})

    def post(self, path: str, secret: str, json: Any = None) -> Any:
        return self.client.post(path, headers={"X-API-KEY": secret}, json=json or {})


@pytest.fixture
def auth(tmp_path) -> Any:
    harness = AuthHarness(tmp_path)
    harness.enable()
    try:
        yield harness
    finally:
        harness.restore()


# ── 1. roles actually gate routes ─────────────────────────────────────────────
def test_a_viewer_is_refused_a_write_route_and_told_why(auth):
    """A read-only role reaching a write route gets 403 with the guard's body."""
    response = auth.post("/api/v_projects", auth.key_for("viewer"), {"name": "x"})

    assert response.status_code == 403
    assert response.get_json() == {"message": "insufficient role"}


def test_a_viewer_is_allowed_the_read_route_it_does_hold(auth):
    """The same key passes where its role genuinely grants the permission.

    Without this the 403 above would prove only that the key was broken.
    """
    response = auth.get("/api/projects", auth.key_for("viewer"))

    assert response.status_code == 200


def test_an_admin_passes_a_route_the_viewer_was_refused(auth):
    """The admin role bypasses, so the refusal above is about the ROLE."""
    response = auth.post("/api/v_projects", auth.key_for("admin"), {"name": "x"})

    assert response.status_code not in (401, 403), "admin must clear authorization"


def test_the_identity_governance_set_is_exactly_what_operator_lacks(auth):
    """Pins the five permissions that separate ``operator`` from ``admin``.

    Named explicitly so that widening ``operator`` — the role most likely to be
    handed out broadly — cannot happen silently.
    """
    assert _identity_governance() == {
        "audit.read",
        "config.write",
        "roles.admin",
        "teams.admin",
        "users.admin",
    }


def test_an_operator_is_refused_a_route_gated_on_identity_governance(auth):
    """``operator`` runs every workload but must not govern identity.

    ``PATCH /api/config`` carries ``config.write``, one of the five. (The
    ``/api/iam/*`` admin surface would be the more obvious target but does not
    MOUNT here — see the module docstring's note on boot-time registration.)
    """
    response = auth.client.patch(
        "/api/config", headers={"X-API-KEY": auth.key_for("operator")}, json={}
    )

    assert response.status_code == 403
    assert response.get_json() == {"message": "insufficient role"}


def test_an_admin_clears_authorization_on_that_same_route(auth):
    """The paired positive: the 403 above is authorization, not a broken route."""
    response = auth.client.patch(
        "/api/config", headers={"X-API-KEY": auth.key_for("admin")}, json={}
    )

    assert response.status_code not in (401, 403)


def test_a_key_with_no_roles_is_refused_everywhere_it_needs_a_permission(auth):
    """``roles=[]`` is explicitly no roles — not a legacy full-power key."""
    secret = auth.key_for()

    assert auth.get("/api/projects", secret).status_code == 403


def test_an_unauthenticated_request_is_refused_before_any_permission_check(auth):
    """No credential is a 401, distinct from an authenticated 403."""
    response = auth.client.get("/api/projects")

    assert response.status_code == 401
    assert response.get_json() == {"message": "API token is not provided."}


def test_a_bogus_credential_is_refused(auth):
    """A key that resolves to nothing never reaches a role check."""
    response = auth.get("/api/projects", "mk_not-a-real-key")

    assert response.status_code == 401


# ── 2. a role change takes effect on the NEXT request ─────────────────────────
def test_revoking_a_permission_takes_effect_on_the_very_next_request(auth):
    """No credential carries permissions — the role store is re-read per request.

    This is what makes revocation immediate rather than deferred to credential
    expiry, so it is asserted against a live request pair. Driven through a
    CUSTOM role because the store refuses to modify a built-in one by design.
    """
    auth.role_store.upsert(
        RoleRecord(
            name="tester",
            description="can read projects",
            permissions=frozenset({PermissionCatalog.PROJECTS_READ}),
        )
    )
    secret = auth.key_for("tester")
    assert auth.get("/api/projects", secret).status_code == 200

    # Strip the permission. The key is untouched and never re-presented.
    auth.role_store.upsert(
        RoleRecord(name="tester", description="stripped", permissions=frozenset())
    )

    assert auth.get("/api/projects", secret).status_code == 403, (
        "the very next request must reflect the new role definition"
    )


def test_a_builtin_role_cannot_be_edited_out_from_under_its_holders(auth):
    """The store refuses to modify a built-in — ``admin`` cannot be defanged."""
    with pytest.raises(ValueError, match="built-in role"):
        auth.role_store.upsert(
            RoleRecord(name="viewer", description="stripped", permissions=frozenset())
        )


def test_granting_a_permission_takes_effect_on_the_next_request_too(auth):
    """The same immediacy in the granting direction, via a custom role."""
    auth.role_store.upsert(
        RoleRecord(name="tester", description="none yet", permissions=frozenset())
    )
    secret = auth.key_for("tester")
    assert auth.get("/api/projects", secret).status_code == 403

    auth.role_store.upsert(
        RoleRecord(
            name="tester",
            description="now readable",
            permissions=frozenset({PermissionCatalog.PROJECTS_READ}),
        )
    )

    assert auth.get("/api/projects", secret).status_code == 200


def test_an_unknown_role_on_a_key_is_inert_rather_than_an_error(auth):
    """A deleted role degrades its holder; it never 500s the request."""
    secret = auth.key_for("a-role-that-was-deleted")

    assert auth.get("/api/projects", secret).status_code == 403


# ── 3. a revoked or expired key stops working immediately ─────────────────────
def test_a_revoked_key_is_refused_on_the_next_request(auth):
    """Revocation is immediate — the key store is consulted per request."""
    plaintext, record = auth.key_store.create_scoped_key(
        "revocable", owner_subject="user:test", roles=["viewer"]
    )
    assert auth.get("/api/projects", plaintext).status_code == 200

    auth.key_store.revoke_key(record["id"])

    assert auth.get("/api/projects", plaintext).status_code == 401


def test_an_expired_key_is_refused(auth):
    """Expiry is enforced on the request path, not only at mint time."""
    secret = auth.key_for("admin", expires_at="2000-01-01T00:00:00+00:00")

    assert auth.get("/api/projects", secret).status_code == 401


# ── 4. the permission a route enforces is the one it SHOULD enforce ───────────
def test_a_cross_user_read_requires_read_all_not_plain_read(auth):
    """Reading everyone's sessions must not be satisfied by ``sessions.read``.

    A mismatch here is invisible in a unit test of the rule and is exactly the
    kind of thing that turns a per-user surface into a tenant-wide leak.
    """
    member_permissions = _permissions_of("member")
    assert PermissionCatalog.SESSIONS_READ in member_permissions
    assert PermissionCatalog.SESSIONS_READ_ALL not in member_permissions, (
        "a member must not hold the cross-user read"
    )

    binding = _binding_for("/api/sessions", "GET")
    assert binding is not None, "GET /api/sessions must carry a guard binding"
    assert PermissionCatalog.SESSIONS_READ_ALL in binding.permissions, (
        f"cross-user session listing is gated on {sorted(binding.permissions)}"
    )


# A POST that only ASKS something is read-shaped: the verb is chosen to carry a
# request body, not because it mutates. Those are legitimately gated on a read
# permission, so they are exempted BY NAME — an exemption list that has to be
# edited is the point, because adding a route here is a decision someone makes
# deliberately rather than a blanket rule quietly absorbing a real mutation.
READ_SHAPED_POSTS = frozenset(
    {
        ("POST", "/v1/wiki/qa"),  # asks a question, returns an answer
        ("POST", "/v1/wiki/qa/<string:answer_id>/stream"),  # the streaming sibling
        ("POST", "/api/system-instructions/preview"),  # renders, never persists
    }
)


def test_no_state_changing_route_is_gated_only_on_a_read_permission(auth):
    """A mutation gated on a ``.read`` id lets every reader change state.

    Held over the whole bound url map rather than the two routes that were
    wrong, so the next handler written with a read verb over a write is caught
    at the point it is added.
    """
    offenders = [
        (c.method, c.rule, sorted(c.binding.permissions))
        for c in _coverage().bound
        if c.method in {"POST", "PUT", "PATCH", "DELETE"}
        and c.binding is not None
        and c.binding.permissions
        and all(p.endswith(".read") for p in c.binding.permissions)
        and (c.method, c.rule) not in READ_SHAPED_POSTS
    ]

    assert offenders == [], f"state-changing routes gated only on a read permission: {offenders}"


def test_a_viewer_cannot_destroy_every_notification(auth):
    """The consequence the rule above exists to prevent, driven end to end.

    The notification store takes no subject, so ``clear`` empties the one
    collection every principal reads — a read-only role destroying other
    people's notifications, which is why the read verb was the wrong ceiling.
    """
    assert auth.post("/api/notifications/clear", auth.key_for("viewer")).status_code == 403
    assert auth.post("/api/notifications/dismiss", auth.key_for("viewer")).status_code == 403


def test_a_member_may_still_dismiss_a_notification(auth):
    """The write verb is a narrowing, not a lockout — `member` holds it."""
    response = auth.post("/api/notifications/dismiss", auth.key_for("member"), {"ids": []})

    assert response.status_code == 200


def _coverage() -> Any:
    """The guard's own partition of the LIVE url map into bound/public/unbound."""
    from mewbo_api.auth.guard_registry import guard_registry

    return guard_registry.guard.audit(backend.app)


def _binding_for(path: str, method: str) -> Any:
    """The binding the live app registered for *(path, method)*, or ``None``."""
    for coverage in _coverage().bound:
        if coverage.rule == path and coverage.method == method:
            return coverage.binding
    return None


# ── 5. the three-state scope law, driven through a real request ───────────────
def test_a_scopeless_key_cannot_delegate_any_scope_when_self_minting(auth):
    """``scopes=[]`` is explicitly none: it may hand on nothing.

    Key self-mint is the ONE request path that consults ``KeyScopes`` (see the
    module-level note on the scope gap), so it is where the three-state law is
    observable end to end.
    """
    secret = auth.key_for("member", scopes=[])

    response = auth.post(
        "/api/keys", secret, {"label": "child", "scopes": ["sessions.read"]}
    )

    # Refused before the delegation check rather than by it: `keys.mint_own` is
    # itself scope-gated now, so a scopeless key cannot reach the mint logic at
    # all. `/api/keys` is dual-tier, so failing the master tier AND the
    # self-service tier surfaces the master tier's 401 rather than a 403.
    assert response.status_code == 401


def test_a_narrowed_key_may_delegate_within_its_scopes_but_not_beyond(auth):
    """A non-empty scope set delegates exactly what it holds.

    The scope set must include ``keys.mint_own`` for the key to reach the mint
    route at all now that scopes gate every permission — which is the honest
    shape of the feature: a key that was never scoped to mint cannot mint.
    """
    secret = auth.key_for("member", scopes=["keys.mint_own", "sessions.read", "wiki.*"])

    within = auth.post("/api/keys", secret, {"label": "ok", "scopes": ["wiki.anything"]})
    beyond = auth.post("/api/keys", secret, {"label": "no", "scopes": ["keys.admin"]})

    assert within.status_code == 201
    assert beyond.status_code == 400


def test_a_legacy_unrestricted_key_may_delegate_any_scope(auth):
    """``None`` is unrestricted-legacy — the third state, distinct from ``[]``."""
    secret = auth.key_for("member")  # scopes omitted ⇒ None

    response = auth.post(
        "/api/keys", secret, {"label": "child", "scopes": ["sessions.read"]}
    )

    assert response.status_code == 201


def test_the_three_scope_states_are_distinguishable_through_the_wire(auth):
    """The whole law in one place: None, (), and a set each behave differently.

    Three outcomes, three states — the point being that no two of them collapse.
    ``None`` mints freely because it predates scopes; ``[]`` cannot even reach
    the route; a set mints exactly what it holds and no more.
    """
    unrestricted = auth.key_for("member")
    scopeless = auth.key_for("member", scopes=[])
    narrowed = auth.key_for("member", scopes=["keys.mint_own", "sessions.read"])
    body = {"label": "child", "scopes": ["sessions.read"]}

    assert auth.post("/api/keys", unrestricted, body).status_code == 201
    assert auth.post("/api/keys", scopeless, body).status_code == 401
    assert auth.post("/api/keys", narrowed, body).status_code == 201
    assert (
        auth.post("/api/keys", narrowed, {"label": "x", "scopes": ["keys.admin"]}).status_code
        == 400
    )


# ── scopes narrow route access ────────────────────────────────────────────────
# `KeyScopes` now gates every permission check, not just the `POST /api/keys`
# delegation check. The law these pin: scopes SUBTRACT from what the role
# grants and can never add to it, so a broader scope value cannot manufacture a
# permission the role withholds. `None` stays unrestricted-legacy, which is what
# keeps every key that never carried scopes byte-identical to before.


def test_a_narrowly_scoped_key_is_refused_outside_its_scopes(auth):
    """The narrowing a scoped key's name promises, enforced at the route.

    ``viewer`` grants ``projects.read``, so the role alone would admit this
    request; the scope value withholds it. That ordering is the whole point —
    if this ever returns 200 again, a scoped key has stopped being scoped.
    """
    secret = auth.key_for("viewer", scopes=["wiki.read"])

    assert auth.get("/api/projects", secret).status_code == 403


def test_a_scope_cannot_grant_what_the_role_withholds(auth):
    """Scopes subtract only. The direction is the security property.

    ``viewer`` does not hold ``sessions.read_all``; naming it in the scopes must
    not confer it, or a caller could widen itself by asking.
    """
    secret = auth.key_for("viewer", scopes=["sessions.read_all"])

    assert auth.get("/api/sessions", secret).status_code == 403


def test_an_explicitly_scopeless_key_can_do_nothing(auth):
    """``scopes=[]`` means exactly what it says — the third state, enforced.

    Distinct from ``None``: this key carries a role that would admit it, and is
    refused anyway. Collapsing the two states is the fail-open this pins shut.
    """
    secret = auth.key_for("viewer", scopes=[])

    assert auth.get("/api/projects", secret).status_code == 403


def test_a_legacy_unrestricted_key_is_unaffected_by_scope_enforcement(auth):
    """``None`` keys predate scopes and must behave exactly as they always did.

    This is the compatibility half of the law: enforcement may not narrow a key
    that never carried scopes, or every key issued before this feature breaks.
    """
    secret = auth.key_for("viewer")  # scopes omitted ⇒ None

    assert auth.get("/api/projects", secret).status_code == 200


def test_the_service_role_grants_nothing_so_its_scopes_cannot_compensate(auth):
    """The ``service`` built-in holds zero permissions, and scopes add none.

    Its description says permissions "come from the key's signed scopes" — but
    nothing resolves a scope into a permission, so a service-role key is refused
    everywhere regardless of what it was scoped for.
    """
    assert _permissions_of("service") == frozenset()

    secret = auth.key_for("service", scopes=["projects.read"])

    assert auth.get("/api/projects", secret).status_code == 403, (
        "a scope is never resolved into a permission"
    )


# ── a disabled user, across the API-key channel ───────────────────────────────
def test_a_disabled_users_api_key_retains_full_authority(auth, tmp_path):
    """Disabling a user does NOT revoke the keys they already hold.

    The key-auth path builds its principal from the KEY RECORD alone
    (``AuthKit._principal_from_record``); no user-store lookup happens on it, so
    a user's ``status`` is simply not consulted. Only the trusted-header path
    checks ``account_disabled``. Recorded as the hole it is: the offboarding
    story is "disable the user AND revoke their keys", and nothing enforces the
    second half.
    """
    from datetime import datetime, timezone

    from mewbo_iam.stores.users import JsonUserStore
    from mewbo_iam.users import UserRecord

    now = datetime.now(timezone.utc)
    users = JsonUserStore(path=str(tmp_path / "iam_users.json"))
    users.create(
        UserRecord(id="user:offboarded", created_at=now, updated_at=now, status="disabled")
    )
    assert users.get("user:offboarded").status == "disabled", "the user really is disabled"

    secret = auth.key_for("admin", owner_subject="user:offboarded")

    assert auth.get("/api/projects", secret).status_code == 200, (
        "a disabled user's key still authenticates AND still carries admin"
    )


def test_revoking_the_key_is_what_actually_removes_that_authority(auth):
    """The half of offboarding that DOES work — the contrast that makes it clear."""
    plaintext, record = auth.key_store.create_scoped_key(
        "offboarded", owner_subject="user:offboarded", roles=["admin"]
    )
    assert auth.get("/api/projects", plaintext).status_code == 200

    auth.key_store.revoke_key(record["id"])

    assert auth.get("/api/projects", plaintext).status_code == 401
