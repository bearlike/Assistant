"""IAM administration surface — ``/api/iam/*`` plus authenticator discovery.

The HTTP face of the identity kernel: the stores in ``mewbo_iam`` ship full
CRUD, and this is where an operator (and the console's admin panel) reaches it.
Wire shape decisions that must not drift:

* **The whole surface 404s while ``api.auth.enabled`` is false.** There is no
  identity to administer on a deployment that never turned auth on, and the
  companion ``init_iam_routes`` constructs NO store in that case — so a default
  deployment never gains an ``iam_*.json`` it did not ask for. Mounting is
  therefore safe unconditionally, mirroring ``init_scim``.
* **4xx bodies are ``{"message": ...}``**, matching the auth guards
  (``AuthKit.require_api_key`` / ``require_permission``) that reject on these
  same routes. A route that authenticates with one shape and fails validation
  with another forces every client to parse two error shapes.
* **Every list is the same envelope: ``{items, total, limit, offset}``.**
  ``total`` counts the filtered set, not the page, so a client can render a
  pager without a second request.
* **``GET /api/auth/authenticators`` is the one UNAUTHENTICATED route here**,
  because the login screen calls it before any session exists. It is therefore
  deliberately thin: a name, a kind, and an enabled flag for the browser-login
  kinds, plus whether a password form should render. Client secrets, issuer
  URLs, discovery endpoints, trusted-proxy CIDRs and LDAP bind DNs are
  configuration an anonymous caller has no business reading, and none of them
  reach this body.
* **Roles the store owns as built-ins are read-only here.** ``RoleStoreBase``
  already refuses to update or delete them; this surface reports that refusal
  as a 409 rather than papering over it with a silent no-op.

Hard delete is deliberately absent for users: ``DELETE /api/iam/users/<id>``
disables the account so its audit history and any resource it stamped stay
resolvable. Erasure is SCIM's job (``/api/scim/v2/Users/<id>``), where an IdP
drives the deprovisioning lifecycle.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from flask import Blueprint, Flask, request
from mewbo_core.common import get_logger
from mewbo_iam import (
    BUILTIN_ROLE_NAMES,
    AuthAuditStoreBase,
    AuthSettings,
    AvatarPolicy,
    PermissionCatalog,
    Principal,
    RoleChangedEvent,
    RoleRecord,
    RoleStoreBase,
    TeamChangedEvent,
    TeamMembershipRecord,
    TeamRecord,
    TeamRole,
    TeamStoreBase,
    UserRecord,
    UserStatusChangedEvent,
    UserStoreBase,
)
from mewbo_iam.audit import AuthAuditEventUnion
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mewbo_api.auth.guard_registry import guard
from mewbo_api.auth.kit import BROWSER_LOGIN_KINDS, current_principal, utcnow
from mewbo_api.contracts import ApiResponse, Page
from mewbo_api.errors import (
    ApiError,
    RequestInvalid,
    ResourceNotFound,
    StateConflict,
)

logging = get_logger(name="api.iam")

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200

# A custom role name: lowercase, url-safe, and distinguishable from a slug in a
# path segment. Validated here rather than on ``RoleRecord`` because it is a
# wire-address constraint (the name IS the path segment on PATCH/DELETE), not an
# intrinsic property of a role.
_ROLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ---------------------------------------------------------------------------
# Wire models — every request body and response body, ``extra="forbid"``
# ---------------------------------------------------------------------------


class AuthenticatorView(ApiResponse):
    """One browser-login authenticator, as an anonymous caller may see it.

    ``name`` is the value to pass back as ``/api/auth/login?authenticator=``;
    it is the only field that is functionally required. ``kind`` lets a client
    pick an icon or label.
    """

    name: str = Field(description="Authenticator name, as sent to /api/auth/login.")
    kind: str = Field(description="Authenticator kind (oidc or saml).")
    enabled: bool = Field(description="Whether this authenticator accepts logins.")


class AuthenticatorsResponse(ApiResponse):
    """``GET /api/auth/authenticators`` — what the login screen may render.

    Carries no ``auth_enabled`` flag. The whole blueprint 404s while auth is off,
    so a body from this route could only ever have said ``true`` — the REACHABILITY
    of the route is the answer, and a field that cannot express the other value is
    a second channel for a fact the status code already carries.
    """

    password_login: bool = Field(description="Whether a username/password form should be offered.")
    authenticators: tuple[AuthenticatorView, ...] = Field(
        default=(), description="Browser-login authenticators, in configured order."
    )


class AvatarChain(ApiResponse):
    """The avatar precedence chain: identity-provider picture, then Gravatar.

    Either may be null — ``picture_url`` only when the provider supplied one,
    ``gravatar_url`` only when the deployment's avatar policy enables Gravatar
    and the user has an email. A client renders initials when both are null.
    """

    picture_url: str | None = Field(default=None, description="Picture asserted by the provider.")
    gravatar_url: str | None = Field(default=None, description="Gravatar URL, or null per policy.")


class ExternalIdentityView(ApiResponse):
    """One linked external identity — the join key a federated login matches on."""

    issuer: str = Field(description="Identity provider that asserted this subject.")
    subject: str = Field(description="Stable subject id at that provider.")


class UserView(ApiResponse):
    """A durable user as the admin surface reports it. Carries no secret."""

    id: str = Field(description="Stable user id (user:<uuid>).")
    email: str | None = Field(default=None, description="Primary email, when known.")
    email_verified: bool | None = Field(
        default=None, description="Whether the provider asserted the email as verified."
    )
    display_name: str | None = Field(default=None, description="Human-facing display name.")
    status: str = Field(description="Account status: active or disabled.")
    roles: tuple[str, ...] = Field(default=(), description="Assigned role names.")
    external_identities: tuple[ExternalIdentityView, ...] = Field(
        default=(), description="Linked provider identities."
    )
    avatar: AvatarChain = Field(description="Avatar precedence chain.")
    created_at: datetime = Field(description="When the record was first provisioned.")
    updated_at: datetime = Field(description="When the record was last written.")


class UserPatch(BaseModel):
    """``PATCH /api/iam/users/<id>`` body — roles and/or status.

    Both fields are optional but at least one must be present: an empty patch
    is a client bug worth surfacing, not a successful no-op. Every other user
    field is provider-owned (it is overwritten on the next federated login or
    SCIM push), so accepting a write here would be a change that silently
    reverts.
    """

    model_config = ConfigDict(extra="forbid")

    roles: tuple[str, ...] | None = Field(
        default=None, description="Replacement role set; each must be a known role."
    )
    status: Literal["active", "disabled"] | None = Field(
        default=None, description="Replacement account status."
    )

    @model_validator(mode="after")
    def _require_one_field(self) -> UserPatch:
        if self.roles is None and self.status is None:
            raise ValueError("provide at least one of 'roles' or 'status'")
        return self


class TeamView(ApiResponse):
    """A durable team. The roster lives on its own paginated sub-resource."""

    id: str = Field(description="Stable team id (team:<uuid>).")
    slug: str = Field(description="Lowercase kebab-case slug; the mapping target.")
    name: str = Field(description="Human-facing team name.")
    description: str = Field(default="", description="Free-form description.")


class TeamMemberView(ApiResponse):
    """One membership edge, with the member's profile resolved where possible.

    The profile fields are ``None`` when ``user_id`` no longer resolves to a
    stored user. That is a deliberate divergence from the SCIM group
    projection, which SKIPS such a member: an IdP is reconciling a roster and
    must not be told about a user the deployment cannot describe, whereas this
    surface exists for an operator to find and repair inconsistent state.
    Hiding the row here would leave an edge that ``DELETE`` can still remove
    but nothing can discover.
    """

    user_id: str = Field(description="The member's user id (user:<uuid>).")
    team_role: str = Field(description="Role within this team: team_admin or team_member.")
    display_name: str | None = Field(default=None, description="Member display name, when known.")
    email: str | None = Field(default=None, description="Member email, when known.")
    status: str | None = Field(
        default=None, description="Account status, or null when the id no longer resolves."
    )


class TeamMemberUpsert(BaseModel):
    """``PUT /api/iam/teams/<id>/members/<user_id>`` body — the role to confer."""

    model_config = ConfigDict(extra="forbid")

    team_role: TeamRole = Field(
        default="team_member", description="Role within the team: team_admin or team_member."
    )


class TeamCreate(BaseModel):
    """``POST /api/iam/teams`` body. The id is server-owned and minted here."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(description="Lowercase kebab-case slug; must be unique.")
    name: str = Field(description="Human-facing team name.")
    description: str = Field(default="", description="Free-form description.")

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class TeamPatch(BaseModel):
    """``PATCH /api/iam/teams/<id>`` body — the human-facing fields only.

    ``slug`` is deliberately immutable: ``api.auth.team_mappings`` rules target
    a team BY SLUG, so renaming one here would silently detach every group→team
    rule pointing at it. Recreate the team (and update the mapping) instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description="Replacement team name.")
    description: str | None = Field(default=None, description="Replacement description.")

    @model_validator(mode="after")
    def _require_one_field(self) -> TeamPatch:
        if self.name is None and self.description is None:
            raise ValueError("provide at least one of 'name' or 'description'")
        return self


class RoleView(ApiResponse):
    """A role with its full permission set, and whether it is built-in."""

    name: str = Field(description="Role name; the path segment on PATCH/DELETE.")
    description: str = Field(default="", description="What the role is for.")
    permissions: tuple[str, ...] = Field(
        default=(), description="Catalog permission ids this role grants, sorted."
    )
    builtin: bool = Field(description="Built-in roles are read-only.")


class RoleCreate(BaseModel):
    """``POST /api/iam/roles`` body — a custom role over catalog permission ids."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Role name: lowercase, digits, dash or underscore.")
    description: str = Field(default="", description="What the role is for.")
    permissions: tuple[str, ...] = Field(default=(), description="Catalog permission ids to grant.")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _ROLE_NAME_RE.match(value):
            raise ValueError(
                "role name must be lowercase alphanumeric with dashes or underscores "
                f"(e.g. 'release-manager'), got {value!r}"
            )
        return value


class RolePatch(BaseModel):
    """``PATCH /api/iam/roles/<name>`` body. The name itself is the address."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, description="Replacement description.")
    permissions: tuple[str, ...] | None = Field(
        default=None, description="Replacement permission set."
    )

    @model_validator(mode="after")
    def _require_one_field(self) -> RolePatch:
        if self.description is None and self.permissions is None:
            raise ValueError("provide at least one of 'description' or 'permissions'")
        return self


class PermissionDomainView(ApiResponse):
    """One product domain and every permission id inside it."""

    domain: str = Field(description="Domain prefix, e.g. 'sessions'.")
    permissions: tuple[str, ...] = Field(description="Permission ids in this domain, sorted.")


class PermissionCatalogResponse(ApiResponse):
    """``GET /api/iam/permissions`` — the closed catalog, grouped for an editor."""

    domains: tuple[PermissionDomainView, ...] = Field(description="Domains, sorted by name.")
    total: int = Field(description="Total number of permission ids in the catalog.")


class MappingRuleView(ApiResponse):
    """One configured group→target rule, in evaluation order."""

    match: str = Field(description="Group name or pattern to match.")
    match_kind: str = Field(description="How 'match' is compared: exact or regex.")
    target: str = Field(description="Role name or team slug conferred on a match.")


class BootstrapRuleView(ApiResponse):
    """The cold-start admin rule, if one is configured."""

    admin_group: str | None = Field(default=None, description="Group that confers admin.")
    admin_subjects: tuple[str, ...] = Field(
        default=(), description="Subjects that bootstrap as admin."
    )


class MappingsResponse(ApiResponse):
    """``GET /api/iam/mappings`` — why a federated user got the roles they got."""

    default_role: str = Field(description="Role conferred when no role rule matches.")
    role_rules: tuple[MappingRuleView, ...] = Field(
        default=(), description="Group→role rules, in evaluation order."
    )
    team_rules: tuple[MappingRuleView, ...] = Field(
        default=(), description="Group→team-slug rules, in evaluation order."
    )
    bootstrap: BootstrapRuleView | None = Field(
        default=None, description="Cold-start admin rule, or null when unset."
    )


# The four list envelopes are ONE contract parameterized four ways, not four
# contracts that happen to agree. ``Page`` (``contracts.py``) owns the
# ``{items, total, limit, offset}`` shape and the "total counts the filtered set"
# invariant; these aliases only choose the item type. ``AuditPage`` is
# parameterized with the kernel's own discriminated union, so every audit variant
# serializes with exactly the fields it owns and this surface gains a new event
# kind the moment the kernel declares one.
UserPage = Page[UserView]
TeamPage = Page[TeamView]
TeamMemberPage = Page[TeamMemberView]
RolePage = Page[RoleView]
AuditPage = Page[AuthAuditEventUnion]


# ---------------------------------------------------------------------------
# Controller — one atomic class over its injected collaborators
# ---------------------------------------------------------------------------


class IamRoutesController:
    """Owns the IAM admin REST behavior over its injected collaborators.

    Atomic feature class (the ``ScimRoutesController``/``TriggerRoutesController``
    idiom): every collaborator is a FIELD, every domain rule is a METHOD, and the
    Blueprint handlers built by :func:`register` are thin adapters closing over
    this ONE instance — no module-level mutable wiring, and a test drives the
    class directly without a Flask app.

    ``deprovision`` is the one collaborator this controller does not implement
    itself: a ``Callable[[str], None]`` invoked with a user's id when an admin
    disables the account, with the same contract SCIM's callback carries (revoke
    every credential that subject owns). Sharing ONE implementation is the point
    — an account disabled through the admin panel must lose its keys exactly as
    an account disabled by an identity provider does. Called best-effort: a
    failure is logged, never raised into the request, so a key-store outage
    cannot leave the account looking still-active because the disable rolled
    back.

    ``principal_reader`` supplies the ACTING admin for the audit trail. It is a
    field rather than a direct ``current_principal()`` call so the audited actor
    is injectable — an audit record that cannot be exercised without a live Flask
    request context is an audit record nobody writes a test for.
    """

    def __init__(
        self,
        *,
        user_store: UserStoreBase,
        team_store: TeamStoreBase,
        role_store: RoleStoreBase,
        audit_store: AuthAuditStoreBase | None,
        settings: AuthSettings,
        deprovision: Callable[[str], None] | None = None,
        principal_reader: Callable[[], Principal | None] = current_principal,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Capture the injected collaborators as instance state."""
        self.user_store = user_store
        self.team_store = team_store
        self.role_store = role_store
        self.audit_store = audit_store
        self.settings = settings
        self.deprovision = deprovision
        self.principal_reader = principal_reader
        self.clock = clock or utcnow

    # ── gating + auth composition ───────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """Whether there is any identity to administer on this deployment."""
        return self.settings.enabled

    # ── authenticator discovery (UNAUTHENTICATED) ───────────────────────────

    @guard.public("the login screen calls this before any session exists")
    def authenticators(self) -> tuple[dict, int]:
        """``GET /api/auth/authenticators`` — what the login screen may offer.

        Anonymous by necessity: it is called before a session exists. Only
        ENABLED browser authenticators are listed, because a login screen must
        not render a button that cannot work; the ``enabled`` field is kept on
        the wire so a client reads one stable shape either way. Nothing
        provider-confidential is included — see the module docstring.
        """
        browser = tuple(
            AuthenticatorView(name=spec.name, kind=spec.kind, enabled=True)
            for spec in self.settings.authenticators
            if spec.kind in BROWSER_LOGIN_KINDS and spec.enabled
        )
        password_login = any(
            spec.kind == "ldap" and spec.enabled for spec in self.settings.authenticators
        )
        body = AuthenticatorsResponse(
            password_login=password_login,
            authenticators=browser,
        )
        return body.response()

    # ── users ───────────────────────────────────────────────────────────────

    @guard.requires(PermissionCatalog.USERS_ADMIN)
    def list_users(self) -> tuple[dict, int]:
        """``GET /api/iam/users?limit=&offset=&q=`` — paginated, optionally filtered.

        ``q`` is a case-insensitive substring over email and display name — the
        two fields an operator actually searches by when they have a person, not
        an id, in hand.
        """
        query = (request.args.get("q") or "").strip().casefold()
        records = [record for record in self.user_store.list() if self._user_matches(record, query)]
        records.sort(key=lambda record: (record.email or "", record.id))
        limit, offset = self._page_args()
        page = records[offset : offset + limit]
        body = UserPage(
            items=tuple(self._map_views(page, self._user_view, kind="user")),
            total=len(records),
            limit=limit,
            offset=offset,
        )
        return body.response()

    @guard.requires(PermissionCatalog.USERS_ADMIN)
    def get_user(self, user_id: str) -> tuple[dict, int]:
        """``GET /api/iam/users/<id>``."""
        record = self._require_user(user_id)
        return self._user_view(record).response()

    @guard.requires(PermissionCatalog.USERS_ADMIN)
    def patch_user(self, user_id: str) -> tuple[dict, int]:
        """``PATCH /api/iam/users/<id>`` — reassign roles and/or set status.

        Roles are validated against the role store BEFORE the write: assigning a
        role that does not exist is inert at permission-resolution time (an
        unknown role contributes nothing), so accepting it would silently strip
        the user's access instead of reporting the typo.
        """
        record = self._require_user(user_id)
        patch = self._parse_body(UserPatch)
        updates: dict[str, Any] = {"updated_at": self.clock()}
        if patch.roles is not None:
            self._validate_roles(patch.roles)
            updates["roles"] = patch.roles
        if patch.status is not None:
            updates["status"] = patch.status
        saved = self.user_store.update(record.model_copy(update=updates))
        if patch.roles is not None:
            self._audit_role_change(record, saved)
        self._audit_status_change(record, saved)
        if patch.status == "disabled" and record.status != "disabled":
            self._safe_deprovision(saved.id)
        return self._user_view(saved).response()

    @guard.requires(PermissionCatalog.USERS_ADMIN)
    def delete_user(self, user_id: str) -> tuple[dict, int]:
        """``DELETE /api/iam/users/<id>`` — a SOFT delete: the account is disabled.

        The record is never erased here, so its audit history and any resource it
        stamped stay resolvable. Hard delete belongs to SCIM
        (``DELETE /api/scim/v2/Users/<id>``), where an identity provider drives
        the deprovisioning lifecycle. Idempotent: disabling an already-disabled
        account succeeds and fires no second deprovision.
        """
        record = self._require_user(user_id)
        if record.status == "disabled":
            return self._user_view(record).response()
        saved = self.user_store.update(
            record.model_copy(update={"status": "disabled", "updated_at": self.clock()})
        )
        self._audit_status_change(record, saved)
        self._safe_deprovision(saved.id)
        return self._user_view(saved).response()

    # ── teams ───────────────────────────────────────────────────────────────

    @guard.requires(PermissionCatalog.TEAMS_ADMIN)
    def list_teams(self) -> tuple[dict, int]:
        """``GET /api/iam/teams?limit=&offset=&q=`` — paginated, optionally filtered."""
        query = (request.args.get("q") or "").strip().casefold()
        records = [
            record
            for record in self.team_store.list()
            if not query or query in record.name.casefold() or query in record.slug.casefold()
        ]
        records.sort(key=lambda record: record.slug)
        limit, offset = self._page_args()
        page = records[offset : offset + limit]
        body = TeamPage(
            items=tuple(self._map_views(page, self._team_view, kind="team")),
            total=len(records),
            limit=limit,
            offset=offset,
        )
        return body.response()

    @guard.requires(PermissionCatalog.TEAMS_ADMIN)
    def get_team(self, team_id: str) -> tuple[dict, int]:
        """``GET /api/iam/teams/<id>``."""
        return self._team_view(self._require_team(team_id)).response()

    @guard.requires(PermissionCatalog.TEAMS_ADMIN)
    def create_team(self) -> tuple[dict, int]:
        """``POST /api/iam/teams`` — mints the id; the slug must be unique."""
        body = self._parse_body(TeamCreate)
        try:
            record = TeamRecord(
                id=f"team:{uuid4().hex}",
                slug=body.slug,
                name=body.name,
                description=body.description,
            )
        except ValidationError as exc:
            raise RequestInvalid.from_validation_error(exc) from exc
        # The store owns uniqueness across id, slug AND external_id, and re-checks
        # it inside the write. A pre-check here would cover only the slug, would
        # still lose the read-then-write race, and would be a second statement of
        # a rule that already has one home.
        return self._team_view(self._write_team(self.team_store.create, record)).created()

    @guard.requires(PermissionCatalog.TEAMS_ADMIN)
    def patch_team(self, team_id: str) -> tuple[dict, int]:
        """``PATCH /api/iam/teams/<id>`` — rename or re-describe. Slug is immutable."""
        record = self._require_team(team_id)
        patch = self._parse_body(TeamPatch)
        updates = patch.model_dump(exclude_none=True)
        saved = self._write_team(self.team_store.update, record.model_copy(update=updates))
        return self._team_view(saved).response()

    @guard.requires(PermissionCatalog.TEAMS_ADMIN)
    def delete_team(self, team_id: str) -> tuple[dict, int]:
        """``DELETE /api/iam/teams/<id>`` — removes the team record.

        A configured group→team mapping targeting this slug is left alone: it
        lives in ``api.auth.team_mappings`` (edited through ``/api/config``), and
        a rule whose target no longer exists is inert, not an error.
        """
        self._require_team(team_id)
        self.team_store.delete(team_id)
        return {}, 204

    # ── team membership ─────────────────────────────────────────────────────

    @guard.requires(PermissionCatalog.TEAMS_ADMIN)
    def list_team_members(self, team_id: str) -> tuple[dict, int]:
        """``GET /api/iam/teams/<id>/members?limit=&offset=`` — the roster, paginated."""
        self._require_team(team_id)
        records = self.team_store.list_members(team_id)
        limit, offset = self._page_args()
        page = records[offset : offset + limit]
        body = TeamMemberPage(
            items=tuple(self._map_views(page, self._team_member_view, kind="team member")),
            total=len(records),
            limit=limit,
            offset=offset,
        )
        return body.response()

    @guard.requires(PermissionCatalog.TEAMS_ADMIN)
    def put_team_member(self, team_id: str, user_id: str) -> tuple[dict, int]:
        """``PUT /api/iam/teams/<id>/members/<user_id>`` — add a member, or re-role one.

        Idempotent by construction: the store keys an edge on
        ``(team_id, user_id)``, so repeating this call with a different role
        replaces the role rather than storing a second edge.

        **The user must exist here, unlike on the SCIM surface**, and the
        asymmetry is intentional. SCIM skips an unknown member id because an
        IdP pushes rosters in bulk and retries aggressively — one id it has not
        provisioned yet must not fail the batch. An operator addressing a
        single user by id is instead almost certainly reporting a typo, and
        silently storing an edge to nobody would leave them staring at a roster
        that never gained the member they just added.
        """
        self._require_team(team_id)
        self._require_user(user_id)
        body = self._parse_body(TeamMemberUpsert)
        previous = self._membership_role(team_id, user_id)
        record = self.team_store.add_member(team_id, user_id, body.team_role)
        if previous is None:
            self._audit_membership(record.team_id, record.user_id, "added")
        elif previous != record.team_role:
            self._audit_membership(record.team_id, record.user_id, "role_changed")
        return self._team_member_view(record).response()

    @guard.requires(PermissionCatalog.TEAMS_ADMIN)
    def delete_team_member(self, team_id: str, user_id: str) -> tuple[dict, int]:
        """``DELETE /api/iam/teams/<id>/members/<user_id>`` — remove one member.

        204 whether or not the edge existed: the desired state (this user is
        not in this team) holds either way, and a retried removal is not an
        error. The user id is deliberately NOT resolved first — that is what
        lets an operator clear an edge whose user is gone, which is the one
        case this endpoint is uniquely able to repair.
        """
        self._require_team(team_id)
        if self.team_store.remove_member(team_id, user_id):
            self._audit_membership(team_id, user_id, "removed")
        return {}, 204

    # ── roles + the permission catalog ──────────────────────────────────────

    @guard.requires(PermissionCatalog.ROLES_ADMIN)
    def list_roles(self) -> tuple[dict, int]:
        """``GET /api/iam/roles`` — built-ins first, then custom roles by name."""
        records = sorted(self.role_store.list(), key=lambda role: (not role.builtin, role.name))
        limit, offset = self._page_args()
        page = records[offset : offset + limit]
        body = RolePage(
            items=tuple(self._map_views(page, self._role_view, kind="role")),
            total=len(records),
            limit=limit,
            offset=offset,
        )
        return body.response()

    @guard.requires(PermissionCatalog.ROLES_ADMIN)
    def create_role(self) -> tuple[dict, int]:
        """``POST /api/iam/roles`` — a custom role. Built-in names are refused."""
        body = self._parse_body(RoleCreate)
        if self.role_store.get(body.name) is not None:
            raise StateConflict.for_reason(f"a role named {body.name!r} already exists")
        record = self._build_role(
            name=body.name, description=body.description, permissions=body.permissions
        )
        return self._role_view(self._upsert_role(record)).created()

    @guard.requires(PermissionCatalog.ROLES_ADMIN)
    def patch_role(self, name: str) -> tuple[dict, int]:
        """``PATCH /api/iam/roles/<name>`` — custom roles only."""
        record = self._require_role(name)
        patch = self._parse_body(RolePatch)
        updated = self._build_role(
            name=record.name,
            description=record.description if patch.description is None else patch.description,
            permissions=(
                tuple(sorted(record.permissions))
                if patch.permissions is None
                else patch.permissions
            ),
        )
        return self._role_view(self._upsert_role(updated)).response()

    @guard.requires(PermissionCatalog.ROLES_ADMIN)
    def delete_role(self, name: str) -> tuple[dict, int]:
        """``DELETE /api/iam/roles/<name>`` — custom roles only.

        A user still carrying the deleted role is not rewritten: an unknown role
        contributes no permissions, so the principal degrades to their remaining
        roles rather than the request failing on a fan-out the store cannot make
        atomic.
        """
        self._require_role(name)
        try:
            self.role_store.delete(name)
        except ValueError as exc:
            raise StateConflict.for_reason(str(exc)) from exc
        return {}, 204

    @guard.requires(PermissionCatalog.ROLES_ADMIN)
    def list_permissions(self) -> tuple[dict, int]:
        """``GET /api/iam/permissions`` — the closed catalog, grouped by domain.

        Grouping is DERIVED from each id's ``<domain>.<verb>`` shape rather than
        a hand-kept second list, so a permission the kernel adds appears here
        with no change on this side — the same single-source-of-truth rule that
        assembles ``PermissionCatalog.ALL`` from its own constants.
        """
        grouped: dict[str, list[str]] = {}
        for permission in PermissionCatalog.ALL:
            grouped.setdefault(permission.split(".", 1)[0], []).append(permission)
        body = PermissionCatalogResponse(
            domains=tuple(
                PermissionDomainView(domain=domain, permissions=tuple(sorted(permissions)))
                for domain, permissions in sorted(grouped.items())
            ),
            total=len(PermissionCatalog.ALL),
        )
        return body.response()

    # ── audit ───────────────────────────────────────────────────────────────

    @guard.requires(PermissionCatalog.AUDIT_READ)
    def list_audit(self) -> tuple[dict, int]:
        """``GET /api/iam/audit?limit=&offset=&type=&subject=&since=`` — newest first.

        ``subject`` matches the event's ACTOR (who did it), not the target of the
        action — that is the store's only indexed identity filter, and it is the
        one an investigation starts from. ``since`` is an ISO-8601 timestamp.

        The offset window is applied here rather than pushed down because the
        store's query API is limit-only; ``total`` therefore counts the whole
        filtered set, which is what a pager needs.
        """
        if self.audit_store is None:
            raise ResourceNotFound.for_reason(
                "the auth audit trail is not enabled", shape="message"
            )
        limit, offset = self._page_args()
        events = self.audit_store.list(
            actor_subject=request.args.get("subject") or None,
            type=request.args.get("type") or None,
            since=self._since_arg(),
        )
        body = AuditPage(
            items=tuple(events[offset : offset + limit]),
            total=len(events),
            limit=limit,
            offset=offset,
        )
        return body.response()

    # ── mappings (read-only) ────────────────────────────────────────────────

    @guard.requires(PermissionCatalog.ROLES_ADMIN)
    def mappings(self) -> tuple[dict, int]:
        """``GET /api/iam/mappings`` — the configured group→role/team rules.

        READ-ONLY by design, and not an oversight: these rules live in the
        ``api.auth`` config block, which is edited through ``/api/config``
        (``config.write``) and re-validated at boot. Exposing a second, HTTP-only
        write path would let the running server's rules diverge from the config
        file that will be reloaded on the next restart. This surface exists so an
        operator can answer "why does this person have this role" without reading
        the config by hand.
        """
        body = MappingsResponse(
            default_role=self.settings.role_mappings.default_role,
            role_rules=self._rule_views(self.settings.role_mappings.rules),
            team_rules=self._rule_views(self.settings.team_mappings.rules),
            bootstrap=(
                None
                if self.settings.bootstrap is None
                else BootstrapRuleView(
                    admin_group=self.settings.bootstrap.admin_group,
                    admin_subjects=self.settings.bootstrap.admin_subjects,
                )
            ),
        )
        return body.response()

    # ── record lookups ──────────────────────────────────────────────────────

    def _require_user(self, user_id: str) -> UserRecord:
        record = self.user_store.get(user_id)
        if record is None:
            raise ResourceNotFound.for_reason(f"no such user: {user_id}", shape="message")
        return record

    def _require_team(self, team_id: str) -> TeamRecord:
        record = self.team_store.get(team_id)
        if record is None:
            raise ResourceNotFound.for_reason(f"no such team: {team_id}", shape="message")
        return record

    def _require_role(self, name: str) -> RoleRecord:
        record = self.role_store.get(name)
        if record is None:
            raise ResourceNotFound.for_reason(f"no such role: {name}", shape="message")
        if record.builtin or name in BUILTIN_ROLE_NAMES:
            raise StateConflict.for_reason(f"role {name!r} is built-in and cannot be modified")
        return record

    # ── user helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _user_matches(record: UserRecord, query: str) -> bool:
        """Whether a user matches the ``q`` substring over email + display name."""
        if not query:
            return True
        haystack = f"{record.email or ''}\n{record.display_name or ''}".casefold()
        return query in haystack

    def _user_view(self, record: UserRecord) -> UserView:
        return UserView(
            id=record.id,
            email=record.email,
            email_verified=record.email_verified,
            display_name=record.display_name,
            status=record.status,
            roles=record.roles,
            external_identities=tuple(
                ExternalIdentityView(issuer=identity.issuer, subject=identity.subject)
                for identity in record.external_identities
            ),
            avatar=self._avatar(record),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _avatar(self, record: UserRecord) -> AvatarChain:
        """The avatar chain, both legs read from the model.

        ``gravatar_url`` is asked for directly. This used to blank ``picture_url``
        on a copy of the record and read ``avatar_url``, to force the precedence
        resolver down its Gravatar branch — a workaround for the model exposing
        only the collapsed value, which is now a member of its own.
        """
        policy: AvatarPolicy = self.settings.avatars
        return AvatarChain(
            picture_url=record.picture_url,
            gravatar_url=record.gravatar_url(policy),
        )

    def _validate_roles(self, roles: Sequence[str]) -> None:
        """Refuse any role name the role store does not know."""
        known = {record.name for record in self.role_store.list()}
        unknown = sorted(set(roles) - known)
        if unknown:
            raise RequestInvalid.field_error("roles", f"unknown roles: {', '.join(unknown)}")

    def _audit_role_change(self, previous: UserRecord, current: UserRecord) -> None:
        """Append a ``role_changed`` event when the assignment actually changed.

        ``subject`` is the user whose roles moved; ``actor_subject`` is the admin
        who moved them. Both matter — a role-escalation entry naming only the
        beneficiary cannot answer who granted it.
        """
        added = tuple(sorted(set(current.roles) - set(previous.roles)))
        removed = tuple(sorted(set(previous.roles) - set(current.roles)))
        if not added and not removed:
            return
        self._append_audit(
            RoleChangedEvent(
                ts=self.clock(),
                source="api",
                actor_subject=self._actor(),
                subject=current.id,
                added=added,
                removed=removed,
            )
        )

    def _audit_status_change(self, previous: UserRecord, current: UserRecord) -> None:
        """Append a ``user_status_changed`` event when the status actually moved.

        Fires in BOTH directions. Re-enabling an account is as security-relevant
        as disabling one — a trail that recorded only the disable would show a
        revocation that appears never to have been undone.

        Guarded on an actual change rather than on the route that was called, so
        the two callers need no agreement between them: a PATCH that rewrites
        ``status`` to the value it already held records nothing, and the soft
        DELETE of an already-disabled account returns before reaching here.
        ``subject`` is the account that moved, ``actor_subject`` the admin who
        moved it — the same pairing :meth:`_audit_role_change` records, and for
        the same reason.
        """
        if previous.status == current.status:
            return
        self._append_audit(
            UserStatusChangedEvent(
                ts=self.clock(),
                source="api",
                actor_subject=self._actor(),
                subject=current.id,
                status=current.status,
            )
        )

    def _membership_role(self, team_id: str, user_id: str) -> TeamRole | None:
        """This user's role in this team, or ``None`` when they are not in it.

        Read BEFORE the write, because ``add_member`` is an upsert: after it
        returns, an addition and a re-role are indistinguishable, and the trail
        has to tell them apart.
        """
        for membership in self.team_store.list_for_user(user_id):
            if membership.team_id == team_id:
                return membership.team_role
        return None

    def _audit_membership(
        self, team_id: str, user_id: str, change: Literal["added", "removed", "role_changed"]
    ) -> None:
        """Append a ``team_changed`` event for one membership edge.

        The caller passes the change it observed rather than the before/after
        pair, because only the caller can distinguish them: an upsert that
        re-roles looks identical to one that adds, once it has returned. Callers
        guard on an ACTUAL change, so a PUT that rewrites the role it already
        held records nothing, and a DELETE of an edge that was not there records
        nothing — the same rule :meth:`_audit_status_change` follows.

        ``subject`` is the member whose access moved; ``actor_subject`` the admin
        who moved it. A membership entry naming only the beneficiary cannot
        answer who granted it, which is the question an audit is read to answer.
        """
        self._append_audit(
            TeamChangedEvent(
                ts=self.clock(),
                source="api",
                actor_subject=self._actor(),
                subject=user_id,
                team_id=team_id,
                change=change,
            )
        )

    def _actor(self) -> str | None:
        """The acting admin's subject, or ``None`` when it cannot be resolved.

        Never raises: an audit write must not be the thing that fails a request
        that already succeeded, so an unresolvable actor degrades to an
        unattributed (but still recorded) event.
        """
        try:
            principal = self.principal_reader()
        except Exception:  # noqa: BLE001 - attribution is best-effort
            return None
        return None if principal is None else principal.subject

    def _safe_deprovision(self, subject: str) -> None:
        """Fire the deprovision callback — best-effort, never raises."""
        if self.deprovision is None:
            return
        try:
            self.deprovision(subject)
        except Exception:  # noqa: BLE001 - best-effort, never raises into the request
            logging.warning("iam: deprovision callback failed for {}", subject, exc_info=True)

    def _append_audit(self, event: Any) -> None:
        """Append one audit event — best-effort, never raises."""
        if self.audit_store is None:
            return
        try:
            self.audit_store.append(event)
        except Exception:  # noqa: BLE001 - audit is best-effort, never breaks the request
            logging.warning("iam: audit write failed", exc_info=True)

    # ── team + role helpers ─────────────────────────────────────────────────

    @staticmethod
    def _team_view(record: TeamRecord) -> TeamView:
        return TeamView(
            id=record.id, slug=record.slug, name=record.name, description=record.description
        )

    def _team_member_view(self, record: TeamMembershipRecord) -> TeamMemberView:
        """Project one edge, resolving the member's profile against the user store.

        An id that no longer resolves still yields a row, with the profile
        fields left ``None`` — see :class:`TeamMemberView` for why this surface
        reports a stranded edge where the SCIM projection drops it.
        """
        user = self.user_store.get(record.user_id)
        return TeamMemberView(
            user_id=record.user_id,
            team_role=record.team_role,
            display_name=user.display_name if user is not None else None,
            email=user.email if user is not None else None,
            status=user.status if user is not None else None,
        )

    @staticmethod
    def _role_view(record: RoleRecord) -> RoleView:
        return RoleView(
            name=record.name,
            description=record.description,
            permissions=tuple(sorted(record.permissions)),
            builtin=record.builtin,
        )

    def _build_role(self, *, name: str, description: str, permissions: Sequence[str]) -> RoleRecord:
        """Build a custom role, mapping the model's own validation to a 400."""
        try:
            return RoleRecord(
                name=name,
                description=description,
                permissions=frozenset(permissions),
                builtin=False,
            )
        except ValidationError as exc:
            raise RequestInvalid.from_validation_error(exc) from exc

    @staticmethod
    def _write_team(
        write: Callable[[TeamRecord], TeamRecord], record: TeamRecord
    ) -> TeamRecord:
        """Persist a team, surfacing the store's uniqueness refusal as a 409.

        Mirrors :meth:`_upsert_role`: the store raises ``ValueError`` naming the
        colliding key (id, slug or external_id) and this maps that ONE message to
        the wire, so create and patch report a collision identically.
        """
        try:
            return write(record)
        except ValueError as exc:
            raise StateConflict.for_reason(str(exc)) from exc

    def _upsert_role(self, record: RoleRecord) -> RoleRecord:
        """Persist a custom role, surfacing the store's built-in refusal as a 409."""
        try:
            return self.role_store.upsert(record)
        except ValueError as exc:
            raise StateConflict.for_reason(str(exc)) from exc

    @staticmethod
    def _rule_views(rules: Sequence[Any]) -> tuple[MappingRuleView, ...]:
        return tuple(
            MappingRuleView(match=rule.match, match_kind=rule.match_kind, target=rule.target)
            for rule in rules
        )

    # ── shared: body parsing, pagination, per-record isolation ──────────────

    @staticmethod
    def _parse_body(model: type[Any]) -> Any:
        """Validate the JSON body against *model*, mapping a failure to a 400."""
        try:
            return model.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            raise RequestInvalid.from_validation_error(exc) from exc

    def _page_args(self) -> tuple[int, int]:
        """The clamped ``(limit, offset)`` for this request."""
        limit = self._int_arg(
            "limit", default=_DEFAULT_PAGE_SIZE, minimum=0, maximum=_MAX_PAGE_SIZE
        )
        offset = self._int_arg("offset", default=0, minimum=0)
        return limit, offset

    @staticmethod
    def _int_arg(name: str, *, default: int, minimum: int, maximum: int | None = None) -> int:
        """Read a clamped integer query arg; an unparseable value takes the default."""
        raw = request.args.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        value = max(value, minimum)
        if maximum is not None:
            value = min(value, maximum)
        return value

    @staticmethod
    def _since_arg() -> datetime | None:
        """Parse the ``since`` ISO-8601 query arg, or raise a 400."""
        raw = request.args.get("since")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError as exc:
            raise RequestInvalid.field_error(
                "since", f"since must be an ISO-8601 timestamp, got {raw!r}"
            ) from exc

    @staticmethod
    def _map_views(records: Iterable[Any], build: Callable[[Any], Any], *, kind: str) -> list[Any]:
        """Build one view per record, SKIPPING any record that will not map.

        A single malformed record must not empty the page an operator opened to
        find it. The failure is logged with the record's id and dropped from the
        response; every other record still renders.
        """
        views: list[Any] = []
        for record in records:
            try:
                views.append(build(record))
            except Exception:  # noqa: BLE001 - one bad record must not fail the list
                logging.warning(
                    "iam: skipping malformed {} record {}",
                    kind,
                    getattr(record, "id", "<unknown>"),
                    exc_info=True,
                )
        return views


# ---------------------------------------------------------------------------
# Blueprint — thin HTTP adapters over the one controller instance
# ---------------------------------------------------------------------------


def register(app: Flask, controller: IamRoutesController) -> None:
    """Mount the IAM admin routes on *app*, bound to *controller*.

    Every route consults ``controller.enabled`` first (via the blueprint's
    ``before_request`` gate) and 404s cleanly while auth is off — no store is
    touched and no guard runs on that path, so mounting is safe even for a
    deployment that never enables auth.
    """
    app.register_blueprint(_build_blueprint(controller))


def _build_blueprint(controller: IamRoutesController) -> Blueprint:
    bp = Blueprint("iam", __name__)

    @bp.before_request
    def _gate() -> tuple[dict, int] | None:
        """404 the whole surface while auth is off, before any guard or store runs."""
        if not controller.enabled:
            return ResourceNotFound.for_reason(
                "identity and access management is not enabled", shape="message"
            ).response()
        return None

    @bp.errorhandler(ApiError)
    def _handle_refusal(error: ApiError) -> tuple[dict, int]:
        """Render a typed refusal raised by a controller method.

        Registered on the BLUEPRINT as well as the app (``backend.py``) so this
        surface renders its own refusals even when mounted on a bare app — which
        is exactly how its route tests build it.
        """
        return error.to_response()

    # Authenticator discovery — the one unauthenticated route on this surface.
    bp.add_url_rule(
        "/api/auth/authenticators",
        view_func=controller.authenticators,
        methods=["GET"],
        endpoint="authenticators",
    )

    # Each handler declares its own requirement with ``@guard.requires`` at its
    # definition, so this table carries only the addressing. The endpoint is
    # named explicitly because three verbs share one rule.
    for rule, methods, handler, endpoint in (
        ("/api/iam/users", ["GET"], controller.list_users, "list_users"),
        ("/api/iam/users/<user_id>", ["GET"], controller.get_user, "get_user"),
        ("/api/iam/users/<user_id>", ["PATCH"], controller.patch_user, "patch_user"),
        ("/api/iam/users/<user_id>", ["DELETE"], controller.delete_user, "delete_user"),
        ("/api/iam/teams", ["GET"], controller.list_teams, "list_teams"),
        ("/api/iam/teams", ["POST"], controller.create_team, "create_team"),
        ("/api/iam/teams/<team_id>", ["GET"], controller.get_team, "get_team"),
        ("/api/iam/teams/<team_id>", ["PATCH"], controller.patch_team, "patch_team"),
        ("/api/iam/teams/<team_id>", ["DELETE"], controller.delete_team, "delete_team"),
        (
            "/api/iam/teams/<team_id>/members",
            ["GET"],
            controller.list_team_members,
            "list_team_members",
        ),
        (
            "/api/iam/teams/<team_id>/members/<user_id>",
            ["PUT"],
            controller.put_team_member,
            "put_team_member",
        ),
        (
            "/api/iam/teams/<team_id>/members/<user_id>",
            ["DELETE"],
            controller.delete_team_member,
            "delete_team_member",
        ),
        ("/api/iam/roles", ["GET"], controller.list_roles, "list_roles"),
        ("/api/iam/roles", ["POST"], controller.create_role, "create_role"),
        ("/api/iam/roles/<name>", ["PATCH"], controller.patch_role, "patch_role"),
        ("/api/iam/roles/<name>", ["DELETE"], controller.delete_role, "delete_role"),
        ("/api/iam/permissions", ["GET"], controller.list_permissions, "list_permissions"),
        ("/api/iam/mappings", ["GET"], controller.mappings, "mappings"),
        ("/api/iam/audit", ["GET"], controller.list_audit, "list_audit"),
    ):
        bp.add_url_rule(rule, view_func=handler, methods=methods, endpoint=endpoint)

    return bp


__all__ = ["IamRoutesController", "register"]
